"""CwId: PARIS timing, the Morse keying envelope, and real keyed-CW station-ID audio (ADR 0007).

Two layers are proven separately, matching how cw.py is built:

- The *timing* layer (`unit_ms`, `cw_timeline`) is pure and exactly assertable against PARIS
  and against a hand-decoded callsign — no PCM involved.
- The *render* layer (`CwId.encode`) keys `synth_tone` on/off along that timeline; it is
  checked by slicing the rendered PCM at the timeline's own sample boundaries (tone segments
  carry energy, gap segments are exact canonical zeros) rather than by fragile run-detection,
  because a 600 Hz sine crosses zero many times *within* each element.

The end-to-end tests reuse the conftest FakeClock/verifier fixtures and assert that swapping
`StubId` -> `CwId` leaves the cycle-4 scheduler behavior identical: the first over carries the
(now real) ID, within-interval overs do not repeat it.
"""

from zoneinfo import ZoneInfo

import numpy as np
import pytest

from radio_server.audio import CANONICAL_FORMAT, AudioFormat, AudioFrame
from radio_server.auth import AuthGate, OutcomeKind, Session, SessionState
from radio_server.backends import MockRadio
from radio_server.services import (
    CwId,
    DEFAULT_CW_TONE_HZ,
    DEFAULT_CW_WPM,
    Dispatcher,
    IdEncoder,
    ServiceContext,
    ServiceRegistry,
    StationId,
    StubTts,
    cw_timeline,
    format_spoken_time,
    load_cw_tone_hz,
    load_cw_wpm,
    TIME_DIGIT,
    TIME_NAME,
    time_service,
    unit_ms,
)

from .conftest import make_settings

CALLSIGN = "AE9S"
TZ = ZoneInfo("UTC")


def _samples(frame):
    return np.frombuffer(frame.samples, dtype="<i2").astype(np.float64)


# --- PARIS timing math (pure) ------------------------------------------------


def test_unit_ms_matches_paris():
    # 1200 / wpm is the definition; 20 WPM -> 60 ms per dot-unit, exactly.
    assert unit_ms(20) == 60.0
    assert unit_ms(25) == 48.0
    assert unit_ms(DEFAULT_CW_WPM) == 60.0


def test_timeline_element_units_are_paris():
    # "K" = dah dit dah: on/off multiples 3,1,1,1,3 in dot-units.
    u = unit_ms(20)
    assert cw_timeline("K", 20) == [(True, 3 * u), (False, 1 * u), (True, 1 * u),
                                    (False, 1 * u), (True, 3 * u)]


def test_timeline_gaps_between_chars_and_words():
    u = unit_ms(20)
    # "E E" = dit, inter-char? no — space -> inter-word gap (7u) between the two E's.
    assert cw_timeline("E E", 20) == [(True, 1 * u), (False, 7 * u), (True, 1 * u)]
    # "EE" (one word) -> inter-char gap (3u) between them.
    assert cw_timeline("EE", 20) == [(True, 1 * u), (False, 3 * u), (True, 1 * u)]


def test_timeline_is_lowercase_insensitive():
    assert cw_timeline("ae9s", 20) == cw_timeline("AE9S", 20)


def test_known_callsign_decodes_to_expected_sequence():
    # AE9S, hand-expanded to (on, dot-units): A=.-  E=.  9=----.  S=...
    u = unit_ms(20)
    expected_units = [
        (True, 1), (False, 1), (True, 3),          # A  .-
        (False, 3),                                 # inter-char
        (True, 1),                                  # E  .
        (False, 3),                                 # inter-char
        (True, 3), (False, 1), (True, 3), (False, 1),
        (True, 3), (False, 1), (True, 3), (False, 1), (True, 1),   # 9  ----.
        (False, 3),                                 # inter-char
        (True, 1), (False, 1), (True, 1), (False, 1), (True, 1),   # S  ...
    ]
    expected = [(on, units * u) for on, units in expected_units]
    assert cw_timeline(CALLSIGN, 20) == expected
    # 11 keyed elements (2 + 1 + 5 + 3), no leading/trailing gap.
    assert sum(1 for on, _ in cw_timeline(CALLSIGN, 20) if on) == 11
    assert cw_timeline(CALLSIGN, 20)[0][0] is True
    assert cw_timeline(CALLSIGN, 20)[-1][0] is True


def test_unknown_char_fails_loud():
    with pytest.raises(ValueError):
        cw_timeline("AE9$", 20)
    with pytest.raises(ValueError):
        CwId().encode("W1@X")


# --- render layer: keyed PCM -------------------------------------------------


def test_output_is_canonical_and_concatenates_with_a_service_frame():
    frame = CwId().encode(CALLSIGN)
    assert frame.format == CANONICAL_FORMAT
    # The whole point of canonical output: prepending an ID to service audio never mismatches.
    combined = frame + AudioFrame(b"CONTENT")
    assert combined.samples == frame.samples + b"CONTENT"


def test_total_duration_matches_timing_math():
    wpm = 20
    frame = CwId(wpm=wpm).encode(CALLSIGN)
    expected_samples = sum(
        round(CANONICAL_FORMAT.rate * ms / 1000.0) for _, ms in cw_timeline(CALLSIGN, wpm)
    )
    assert _samples(frame).size == expected_samples
    assert len(frame.samples) == expected_samples * CANONICAL_FORMAT.frame_bytes


def test_render_places_tone_and_silence_per_timeline():
    # Slice the rendered PCM at the timeline's own sample boundaries: every keyed segment
    # must carry energy, every gap must be exact canonical zeros. This proves the renderer
    # honored the on/off structure without depending on where the sine crosses zero.
    wpm = 20
    frame = CwId(wpm=wpm, tone_hz=600).encode(CALLSIGN)
    pcm = _samples(frame)
    pos = 0
    for on, ms in cw_timeline(CALLSIGN, wpm):
        n = round(CANONICAL_FORMAT.rate * ms / 1000.0)
        segment = pcm[pos : pos + n]
        if on:
            assert np.abs(segment).max() > 0.1 * 32767  # keyed tone carries energy
        else:
            assert np.all(segment == 0)  # gap is exact silence, no click
        pos += n
    assert pos == pcm.size  # segments tile the whole frame exactly


def test_sidetone_frequency_is_honored():
    # FFT the first keyed element (A's dit, 60 ms @ 20 WPM = 2880 samples) and confirm the
    # dominant frequency is the configured sidetone, not RF — audio content only.
    frame = CwId(wpm=20, tone_hz=600).encode(CALLSIGN)
    dit = _samples(frame)[: round(CANONICAL_FORMAT.rate * unit_ms(20) / 1000.0)]
    mag = np.abs(np.fft.rfft(dit))
    freqs = np.fft.rfftfreq(dit.size, 1.0 / CANONICAL_FORMAT.rate)
    assert abs(freqs[mag.argmax()] - 600) <= freqs[1]  # within one FFT bin


def test_faster_wpm_is_shorter():
    slow = CwId(wpm=15).encode(CALLSIGN)
    fast = CwId(wpm=30).encode(CALLSIGN)
    assert _samples(fast).size < _samples(slow).size


def test_encode_is_deterministic():
    assert CwId().encode(CALLSIGN) == CwId().encode(CALLSIGN)


def test_encode_honors_explicit_format_argument():
    # The optional format arg (cycle-6 encode(callsign, format) shape) defaults to canonical.
    assert CwId().encode(CALLSIGN, CANONICAL_FORMAT) == CwId().encode(CALLSIGN)


def test_cwid_satisfies_the_encoder_protocol():
    assert isinstance(CwId(), IdEncoder)


# --- config loaders (marked defaults, fail loud on invalid) ------------------


def test_load_cw_wpm_default_and_override():
    assert load_cw_wpm(make_settings({})) == DEFAULT_CW_WPM == 20.0
    assert load_cw_wpm(make_settings({"station.cw_wpm": 25})) == 25.0
    assert load_cw_wpm(make_settings({"station.cw_wpm": ""})) == DEFAULT_CW_WPM


def test_load_cw_tone_hz_default_and_override():
    assert load_cw_tone_hz(make_settings({})) == DEFAULT_CW_TONE_HZ == 600.0
    assert load_cw_tone_hz(make_settings({"station.cw_tone_hz": 700})) == 700.0


@pytest.mark.parametrize("bad", ["abc", 0, -5])
def test_cw_config_rejects_invalid_values(bad):
    with pytest.raises(RuntimeError):
        make_settings({"station.cw_wpm": bad})
    with pytest.raises(RuntimeError):
        make_settings({"station.cw_tone_hz": bad})


# --- end-to-end through the scheduler (behavior unchanged from cycle 4) -------


def frame(payload: bytes) -> AudioFrame:
    return AudioFrame(payload)


def test_station_id_prepends_real_cw_and_does_not_repeat_within_interval(clock):
    # Swap StubId -> CwId: the scheduler is unchanged, only the ID audio is now real CW.
    radio = MockRadio()
    cw_id = CwId().encode(CALLSIGN)
    station = StationId(radio, CwId(), CALLSIGN, interval=600.0, clock=clock)

    station.transmit(frame(b"one"))
    assert radio.tx_log == [cw_id + frame(b"one")]

    clock.advance(60.0)  # within the interval: no repeated ID
    station.transmit(frame(b"two"))
    assert radio.tx_log == [cw_id + frame(b"one"), frame(b"two")]


def test_authed_one_prepends_real_cw_id(verifier, clock, code_for):
    radio = MockRadio()
    registry = ServiceRegistry()
    registry.register(TIME_DIGIT, TIME_NAME, time_service(TZ))
    ctx = ServiceContext(clock=clock, tts=StubTts())
    station = StationId(radio, CwId(), CALLSIGN, clock=clock)
    dispatcher = Dispatcher(station, ctx, registry)
    gate = AuthGate(verifier, timeout=120.0, clock=clock, dispatch=dispatcher)

    session = Session()
    gate.on_dtmf(code_for(clock.now), session)  # authenticate
    outcome = gate.on_dtmf("1", session)

    assert outcome.kind is OutcomeKind.COMMAND
    expected_time = StubTts().render(format_spoken_time(clock.now, TZ))
    assert radio.tx_log == [CwId().encode(CALLSIGN) + expected_time]
