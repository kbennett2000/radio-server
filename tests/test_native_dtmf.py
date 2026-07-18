"""GoertzelStream — the in-process native DTMF decoder (ADR 0054).

These tests carry **no `skipif` gate**: unlike every `multimon-ng` decode test, the native decoder is
pure Python, so it runs unconditionally in CI. That is the point of the cycle. The acceptance table
reproduces ADR 0038's empirical multimon behaviour (held tone → one key, genuine repeat → two, even at
a 30 ms gap) driven by real `synth_dtmf` audio, so `native` is a like-for-like drop-in for `streaming`
on the properties the framing layer above it depends on.

Real-RF robustness (talk-off on voice, weak-signal / HT-flutter) is a separate hardware bring-up item
(ADR 0054, guardrail 1) — these synthetic-audio tests deliberately do not claim it.
"""

from __future__ import annotations

import numpy as np
import pytest
import soxr

from radio_server.audio import (
    CANONICAL_FORMAT,
    DECODE_MODE_NATIVE,
    DTMF_FREQS,
    AudioFrame,
    DtmfFramer,
    DtmfStream,
    GoertzelStream,
    StreamingDtmfInput,
    synth_dtmf,
)
from radio_server.audio.resample import MULTIMON_RATE, to_multimon
from radio_server.backends import MockRadio
from radio_server.backends.kv4p.audio import OPUS_RATE, RxAudioDecoder
from radio_server.config import resolve_settings
from radio_server.controller import build_controller
from radio_server.services import StubTts

_PCM = np.dtype("<i2")

#: A per-tone amplitude representative of real received audio, ~half the 0.4 fixture level the rest of
#: the suite uses. Real DTMF off the air lands ~10x quieter than the 0.4 fixtures (measured Goertzel
#: power ~0.012 on the bench capture, ADR 0072), so tests that must reflect on-air behaviour — the
#: sample-rate offset (ADR 0070) and the energy-floor (ADR 0072) regressions — synth at this level, not
#: the loud fixture level where a partial defect can still squeak a tone through.
_RECEIVED_AMPLITUDE = 0.15


def _tone_pcm(digit: str, ms: float) -> bytes:
    """One DTMF tone as the `MULTIMON_RATE` PCM `GoertzelStream.write` expects (via the real edge)."""
    return to_multimon(synth_dtmf(digit, ms)).samples


def _silence_pcm(ms: float) -> bytes:
    """`ms` of true silence at `MULTIMON_RATE`, built through the same canonical → decode edge."""
    n = int(CANONICAL_FORMAT.rate * ms / 1000)
    return to_multimon(AudioFrame(np.zeros(n, dtype=_PCM).tobytes(), CANONICAL_FORMAT)).samples


def _decode(sequence: list[tuple[str | None, float]]) -> str:
    """Drive a fresh `GoertzelStream` with a `(digit|None, ms)` script; return all recognized keys.

    A digit is a tone; ``None`` is silence. One `write`/`read` pair per segment mirrors how the RX
    pump feeds the stream frame by frame.
    """
    stream = GoertzelStream()
    keys: list[str] = []
    for digit, ms in sequence:
        stream.write(_tone_pcm(digit, ms) if digit is not None else _silence_pcm(ms))
        keys.append(stream.read())
    return "".join(keys)


# --- the acceptance table (reproduces ADR 0038's multimon behaviour) ------------------------------

@pytest.mark.parametrize(
    "sequence, expected",
    [
        # A held tone emits exactly once, regardless of length.
        ([("9", 500)], "9"),
        ([("9", 1500)], "9"),
        # Two genuine presses emit twice — even at a 30 ms gap (the ADR 0038 crux).
        ([("9", 120), (None, 30), ("9", 120)], "99"),
        ([("9", 120), (None, 80), ("9", 120)], "99"),
        # Repeated-digit codes frame correctly end to end.
        ([("9", 120), (None, 80), ("9", 120), (None, 80), ("#", 120)], "99#"),
        (
            [("1", 120), (None, 80), ("5", 120), (None, 80), ("5", 120), (None, 80), ("#", 120)],
            "155#",
        ),
    ],
    ids=["held-500ms", "held-1500ms", "repeat-30ms-gap", "repeat-80ms-gap", "99#", "155#"],
)
def test_acceptance_table(sequence, expected):
    assert _decode(sequence) == expected


def test_all_sixteen_keys_round_trip():
    for key in DTMF_FREQS:
        assert _decode([(key, 150)]) == key, f"key {key!r} did not round-trip"


def test_digit_straddling_a_block_boundary_is_not_split():
    # 11 ms of leading silence is not a whole number of 205-sample blocks, so the tone starts
    # mid-block; contiguous block processing must still yield exactly one key, not zero or two.
    assert _decode([(None, 11), ("7", 150)]) == "7"


def test_full_scale_white_noise_emits_nothing():
    # Basic talk-off floor: broadband energy has no dominant tone pair, so nothing decodes.
    rng = np.random.default_rng(0)
    noise = (rng.uniform(-1.0, 1.0, MULTIMON_RATE) * 32767).astype(_PCM).tobytes()
    stream = GoertzelStream()
    stream.write(noise)
    assert stream.read() == ""


# --- the kv4p firmware sample-rate offset (ADR 0070) ----------------------------------------------
# The regression the whole DTMF suite was missing: every test above uses *exact* frequencies, so none
# could catch that the shipped firmware clocks its RX ADC ~2% fast (rxAudio.h: AUDIO_SAMPLE_RATE *
# 1.02) while labelling the audio 48 kHz. That ~2% shift knocks every tone off its Goertzel bin
# (spacing 8000/205 ≈ 39 Hz; 1633 Hz moves ~33 Hz), so DTMF cannot decode as received — until the
# backend resamples the true device rate back to a real 48 kHz.
#
# These synth at _RECEIVED_AMPLITUDE, not the loud 0.4 fixture level: at full fixture loudness the
# scalloped off-bin tone still clears the (post-ADR-0072) energy floor for some digits, so the offset
# alone isn't always fatal there. On real received audio — quiet *and* offset — the correction is
# genuinely required, which is what this level reproduces.

_FIRMWARE_CORRECTION = 1.02  # rxAudio.h @ 3f0e809 (ADR 0064/0070)


def _as_captured_fast(canonical: AudioFrame, correction: float = _FIRMWARE_CORRECTION) -> bytes:
    """Simulate the firmware's ~2%-fast ADC: resample true-48 kHz audio up to the real device rate
    and re-label it 48 kHz — exactly what the mislabelled Opus stream delivers to the host (every
    tone then reads ~2% low when processed at the nominal 48 kHz)."""
    device_rate = round(OPUS_RATE * correction)
    samples = np.frombuffer(canonical.samples, dtype=_PCM).astype(np.float32) / 32767.0
    fast = soxr.resample(samples, OPUS_RATE, device_rate, quality="HQ")
    return np.rint(np.clip(fast, -1.0, 1.0) * 32767).astype(_PCM).tobytes()


def _decode_canonical(pcm: bytes) -> str:
    stream = GoertzelStream()
    stream.write(to_multimon(AudioFrame(pcm, CANONICAL_FORMAT)).samples)
    return stream.read()


@pytest.mark.parametrize("digit", list("1234#"))
def test_firmware_offset_breaks_dtmf_and_the_correction_fixes_it(digit):
    captured = _as_captured_fast(synth_dtmf(digit, 200, amplitude=_RECEIVED_AMPLITUDE))

    # As received (2% fast, mislabelled 48 kHz): the tone lands off its bin → nothing decodes.
    assert _decode_canonical(captured) != digit

    # Through the backend's correction (true device rate → real 48 kHz): the digit decodes cleanly.
    corrected = RxAudioDecoder(sample_rate_correction=_FIRMWARE_CORRECTION)._correct(captured)
    assert _decode_canonical(corrected) == digit


def test_a_correction_free_decoder_leaves_the_offset_uncorrected():
    # Guards the pass-through default: correction=1.0 must NOT silently fix the firmware offset —
    # the offset is a kv4p hardware fact threaded from config, not baked into the generic decoder.
    assert RxAudioDecoder()._resampler is None  # default builds no resampler
    captured = _as_captured_fast(synth_dtmf("1", 200, amplitude=_RECEIVED_AMPLITUDE))
    assert _decode_canonical(captured) != "1"  # so a default node's DTMF stays broken until configured


# --- the received-level blind spot (ADR 0072) -----------------------------------------------------
# The second regression the suite was missing. Fixing the sample-rate offset above still left kv4p
# DTMF dead: the captured tones were on-frequency but *quieter than the decoder's energy floor*. Every
# test above uses the 0.4-amplitude synth fixtures (per-tone Goertzel power ~0.039), an order of
# magnitude above real received audio (measured ~0.012 on the bench capture), so none could catch that
# NATIVE_ENERGY_FLOOR = 0.02 rejected every real block as silence. The floor is now 0.002; these guard
# that a clean-but-quiet tone (at _RECEIVED_AMPLITUDE) decodes while the ratio gates still reject noise
# at the lower floor.


def _low_level_code(code: str, amplitude: float) -> bytes:
    """A `code` string as one continuous canonical PCM buffer at `amplitude` per tone (150 ms tones,
    80 ms gaps) — the received-audio analogue of the 0.4-amplitude fixtures the rest of the suite uses."""
    parts: list[np.ndarray] = []
    for digit in code:
        parts.append(np.frombuffer(synth_dtmf(digit, 150, amplitude=amplitude).samples, dtype=_PCM))
        parts.append(np.zeros(int(CANONICAL_FORMAT.rate * 0.08), dtype=_PCM))
    return np.concatenate(parts).tobytes()


def test_quiet_received_level_dtmf_decodes():
    # A clean 1234# at a received-audio level (well below the 0.4-amp fixtures) must decode. This is
    # the test that would have caught the bug: on-frequency tones, just quiet, that the old floor ate.
    pcm = _low_level_code("1234#", _RECEIVED_AMPLITUDE)
    assert _decode_canonical(pcm) == "1234#"


def test_the_old_energy_floor_would_have_dropped_quiet_dtmf(monkeypatch):
    # Pin the defect to the constant: at the pre-ADR-0072 floor the same quiet tones decode nothing;
    # at the shipped floor they decode. So this is a floor-calibration fix, not a codec/wiring change.
    import radio_server.audio.dtmf as dtmf_mod

    pcm = _low_level_code("1234#", _RECEIVED_AMPLITUDE)
    monkeypatch.setattr(dtmf_mod, "NATIVE_ENERGY_FLOOR", 0.02)
    assert _decode_canonical(pcm) == ""  # the bench symptom, reproduced
    monkeypatch.setattr(dtmf_mod, "NATIVE_ENERGY_FLOOR", 0.002)
    assert _decode_canonical(pcm) == "1234#"


def test_talk_off_holds_at_the_lower_floor():
    # Lowering the floor must not reopen talk-off: full-scale white noise still decodes nothing across
    # seeds, because group dominance (not the floor) is what rejects broadband energy.
    for seed in range(12):
        rng = np.random.default_rng(seed)
        noise = (rng.uniform(-1.0, 1.0, MULTIMON_RATE) * 32767).astype(_PCM).tobytes()
        stream = GoertzelStream()
        stream.write(noise)
        assert stream.read() == "", f"white noise leaked a key at seed {seed}"


# --- decode is independent of the backend's RX frame size -----------------------------------------
# kv4p delivers ~1882/1920-sample frames; AIOC delivers 960. GoertzelStream buffers to its 205-sample
# block grid across writes, so decode must not depend on the pump frame size — otherwise a new backend
# could silently break DTMF the way the level/offset blind spots did.


@pytest.mark.parametrize("frame_samples", [960, 1920, 1882, 441, 705])
def test_decode_is_invariant_to_pump_frame_size(frame_samples):
    pcm = np.frombuffer(_low_level_code("1234#", 0.4), dtype=_PCM)
    dtmf = StreamingDtmfInput(GoertzelStream(), DtmfFramer())
    decoded: list[str] = []
    dtmf.on_digit = decoded.append
    now = 0.0
    for i in range(0, len(pcm), frame_samples):
        frame = AudioFrame(pcm[i : i + frame_samples].tobytes(), CANONICAL_FORMAT)
        dtmf.pump(frame, now)
        now += frame_samples / CANONICAL_FORMAT.rate
    dtmf.flush(now)
    assert "".join(decoded) == "1234#"


# --- protocol + lifecycle -------------------------------------------------------------------------

def test_is_a_dtmf_stream():
    assert isinstance(GoertzelStream(), DtmfStream)


def test_close_is_idempotent_and_stops_decoding():
    stream = GoertzelStream()
    stream.close()
    stream.close()  # no raise
    stream.write(_tone_pcm("5", 200))  # writes after close are ignored
    assert stream.read() == ""


# --- framing through the shared StreamingDtmfInput path -------------------------------------------

def test_frames_a_repeated_digit_entry_through_streaming_input():
    # The native stream reuses StreamingDtmfInput + DtmfFramer unchanged: `99#` must frame as one
    # entry "99" (the repeated-digit case ADR 0038 exists to get right), proving no de-dup is needed.
    dtmf = StreamingDtmfInput(GoertzelStream(), DtmfFramer())
    entries: list[str] = []
    for digit, ms in [("9", 120), (None, 80), ("9", 120), (None, 80), ("#", 120)]:
        frame = synth_dtmf(digit, ms) if digit is not None else _silence_frame(ms)
        entries.extend(dtmf.pump(frame, 0.0))
    assert entries == ["99"]


def _silence_frame(ms: float) -> AudioFrame:
    n = int(CANONICAL_FORMAT.rate * ms / 1000)
    return AudioFrame(np.zeros(n, dtype=_PCM).tobytes(), CANONICAL_FORMAT)


# --- wiring: `dtmf.decode_mode = native` selects the Goertzel stream ------------------------------

def test_native_mode_wires_the_goertzel_stream():
    # No injected `decoder`, so the decode-mode dispatch runs; StubTts avoids building PiperTts.
    ctrl = build_controller(
        resolve_settings({"dtmf.decode_mode": DECODE_MODE_NATIVE, "station.callsign": "W1AW"}),
        radio=MockRadio(),
        totp_secret=None,
        tts=StubTts(),
    )
    dtmf = ctrl._dtmf  # noqa: SLF001 — asserting the wired decode path
    assert isinstance(dtmf, StreamingDtmfInput)
    assert isinstance(dtmf._stream, GoertzelStream)  # noqa: SLF001


def test_native_mode_is_accepted_by_config():
    assert resolve_settings({"dtmf.decode_mode": "native"}).get("dtmf.decode_mode") == "native"
