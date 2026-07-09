"""DTMF decode + framing → on_dtmf: the audio-in seam and first full end-to-end (ADR 0008).

Three layers are proven here without touching a radio or (in CI) the multimon-ng binary:

- **Fixture generation** — `synth_dtmf` produces a real dual-tone frame, verified by FFT.
  Deterministic, no external assets, no binary.
- **Framing grammar** — `DtmfFramer` turns a single-digit stream into complete entries
  (`#` submit, `*` clear, inter-digit timeout discards a partial), driven by `FakeClock`.
- **The end-to-end** — a fake decoder drives fixture audio → framed digits → TOTP auth →
  `"1"` command → a genuinely CW-ID'd time announcement in `mock.tx_log`.

The one test that needs the real `multimon-ng` binary is `skipif`-gated on its presence,
so it runs where multimon-ng is installed and skips cleanly where it is not (guardrail 1:
the real decode + its exact flags are a hardware/installed-build verification).
"""

import shutil
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from radio_server.audio import (
    DEFAULT_MULTIMON_BIN,
    CANONICAL_FORMAT,
    AudioFrame,
    DtmfFramer,
    DtmfInput,
    MultimonDtmfDecoder,
    synth_dtmf,
)
from radio_server.audio.dtmf import DEFAULT_DTMF_TIMEOUT
from radio_server.auth import AuthGate, OutcomeKind, Session, SessionState
from radio_server.backends import MockRadio
from radio_server.services import (
    CwId,
    Dispatcher,
    ServiceContext,
    ServiceRegistry,
    StationId,
    StubTts,
    format_spoken_time,
    register,
)

from .conftest import FakeClock

TZ = ZoneInfo("UTC")
CALLSIGN = "AE9S"


def _samples(frame: AudioFrame) -> np.ndarray:
    return np.frombuffer(frame.samples, dtype="<i2").astype(np.float64)


def _peak_freq_in_band(frame: AudioFrame, low: float, high: float) -> float:
    """Return the frequency with the most energy inside [low, high] Hz."""
    pcm = _samples(frame)
    mag = np.abs(np.fft.rfft(pcm))
    freqs = np.fft.rfftfreq(pcm.size, 1.0 / frame.format.rate)
    band = (freqs >= low) & (freqs <= high)
    band_idx = np.flatnonzero(band)
    return float(freqs[band_idx[mag[band].argmax()]])


# --- Fixture generation (always green, no binary) ---------------------------------------

def test_synth_dtmf_has_both_standard_tones():
    # '5' is the (770 Hz, 1336 Hz) pair. Both must dominate their half of the spectrum.
    frame = synth_dtmf("5", 500)  # long tone → tight FFT bins
    assert abs(_peak_freq_in_band(frame, 300, 1000) - 770) <= 5
    assert abs(_peak_freq_in_band(frame, 1000, 2000) - 1336) <= 5


def test_synth_dtmf_is_canonical_and_right_length():
    frame = synth_dtmf("1", 120)  # 120 ms @ 48000 Hz = 5760 samples
    assert frame.format == CANONICAL_FORMAT
    assert _samples(frame).size == 5760


def test_synth_dtmf_does_not_clip():
    # Two tones sum; the default amplitude keeps the peak inside full scale.
    assert np.abs(_samples(synth_dtmf("9"))).max() <= 32767


def test_synth_dtmf_is_deterministic():
    assert synth_dtmf("7", 80) == synth_dtmf("7", 80)


def test_synth_dtmf_unknown_key_fails_loud():
    with pytest.raises(ValueError):
        synth_dtmf("X-not-a-key")


# --- Real decode (needs the multimon-ng binary) -----------------------------------------

@pytest.mark.skipif(
    shutil.which(DEFAULT_MULTIMON_BIN) is None,
    reason="multimon-ng not installed; real-decode is a hardware/installed-build check",
)
def test_real_multimon_decodes_synth_dtmf():
    decoded = MultimonDtmfDecoder().decode(synth_dtmf("5", 200))
    assert "5" in decoded


# --- Framing grammar (pure, FakeClock) --------------------------------------------------

def test_full_run_frames_into_one_entry():
    clock = FakeClock()
    framer = DtmfFramer(clock=clock)
    for digit in "123456":
        assert framer.feed(digit) is None
    assert framer.feed("#") == "123456"


def test_star_clears_a_partial_entry():
    clock = FakeClock()
    framer = DtmfFramer(clock=clock)
    framer.feed("1")
    framer.feed("2")
    framer.feed("*")  # cancel
    framer.feed("3")
    assert framer.feed("#") == "3"


def test_inter_digit_timeout_closes_a_partial_entry():
    clock = FakeClock()
    framer = DtmfFramer(timeout=DEFAULT_DTMF_TIMEOUT, clock=clock)
    framer.feed("1")
    framer.feed("2")
    clock.advance(DEFAULT_DTMF_TIMEOUT)  # stall past the inter-digit timeout
    framer.feed("3")
    # '1','2' were abandoned by the timeout; only the post-timeout digit survives.
    assert framer.feed("#") == "3"


def test_lone_submit_emits_nothing():
    framer = DtmfFramer(clock=FakeClock())
    assert framer.feed("#") is None


def test_tick_expires_a_stale_partial_without_a_new_digit():
    clock = FakeClock()
    framer = DtmfFramer(timeout=DEFAULT_DTMF_TIMEOUT, clock=clock)
    framer.feed("9")
    clock.advance(DEFAULT_DTMF_TIMEOUT)
    framer.tick()  # a real polling loop would call this
    assert framer.feed("#") is None  # nothing buffered to submit


# --- The end-to-end: fixture audio → framed digits → auth → CW-ID'd answer ----------------

class FakeDtmfDecoder:
    """A decoder whose output is scripted per `decode` call — drives the stack without
    the multimon-ng binary. Each received frame yields the next scripted digit string."""

    def __init__(self, scripts: list[str]) -> None:
        self._scripts = iter(scripts)

    def decode(self, frame: AudioFrame) -> str:
        return next(self._scripts)


def _build_gate(radio, verifier, clock):
    registry = ServiceRegistry()
    register(registry, TZ)
    ctx = ServiceContext(clock=clock, tts=StubTts())
    station = StationId(radio, CwId(), CALLSIGN, clock=clock)  # real CW ID out
    dispatcher = Dispatcher(station, ctx, registry)
    return AuthGate(verifier, timeout=120.0, clock=clock, dispatch=dispatcher)


def test_end_to_end_dtmf_audio_to_cw_id_time_announcement(verifier, clock, code_for):
    radio = MockRadio()
    gate = _build_gate(radio, verifier, clock)
    session = Session()

    code = code_for(clock.now)
    # Two received "overs": the TOTP code then the command, each terminated by '#'.
    decoder = FakeDtmfDecoder([code + "#", "1#"])
    framer = DtmfFramer(clock=clock)
    dtmf_in = DtmfInput(decoder, framer)

    # The audio content is irrelevant to the fake decoder; a canonical frame stands in for
    # a real received over. (A real decoder would read these samples.)
    rx = synth_dtmf("1")

    # Over 1: the framed TOTP code authenticates the session.
    for entry in dtmf_in.pump(rx):
        outcome = gate.on_dtmf(entry, session)
    assert outcome.kind is OutcomeKind.ACCEPTED
    assert radio.tx_log == []  # authenticating never transmits

    # Over 2: authed '1' dispatches the time service.
    for entry in dtmf_in.pump(rx):
        outcome = gate.on_dtmf(entry, session)
    assert outcome.kind is OutcomeKind.COMMAND
    assert outcome.detail.service == "time"
    assert outcome.detail.transmitted is True

    # The single over carries a genuine CW station ID prepended to the time announcement.
    expected = CwId().encode(CALLSIGN) + StubTts().render(format_spoken_time(clock.now, TZ))
    assert radio.tx_log == [expected]


def test_end_to_end_partial_then_clear_never_reaches_auth(verifier, clock):
    radio = MockRadio()
    gate = _build_gate(radio, verifier, clock)
    session = Session()

    # A fumbled partial then '*': no '#', so nothing is ever framed or sent to the gate.
    decoder = FakeDtmfDecoder(["123*"])
    dtmf_in = DtmfInput(decoder, DtmfFramer(clock=clock))

    entries = dtmf_in.pump(synth_dtmf("1"))
    assert entries == []
    assert session.state is SessionState.UNAUTHENTICATED
    assert radio.tx_log == []
