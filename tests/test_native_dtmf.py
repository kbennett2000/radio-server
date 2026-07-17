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
from radio_server.config import resolve_settings
from radio_server.controller import build_controller
from radio_server.services import StubTts

_PCM = np.dtype("<i2")


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
