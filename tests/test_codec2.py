"""Codec2 mode-3200 seam (ADR 0049): geometry, round-trip, framing, fail-loud.

The default suite is hardware- and dependency-free: every test that needs the real
``libcodec2`` is ``skipif``-gated on ``find_library("codec2")``, exactly the posture of the
AIOC hardware tests. The one test that runs unconditionally is the missing-library fail-loud
path (driven by monkeypatching the loader), so CI proves the config-error behavior with no
library present.

Codec2 is LOSSY, so nothing here asserts sample equality — only queried geometry, frame count,
round-trip length, and that decoded audio is not silence. Perceptual quality is a bench fact.
"""

import numpy as np
import pytest

from radio_server.audio import AudioFrame, synth_tone
from radio_server.audio.format import CANONICAL_FORMAT, AudioFormat, AudioFormatMismatch
from radio_server.audio.codec2 import Codec2, _MISSING_MSG, find_library

_CODEC2_SKIP = pytest.mark.skipif(
    find_library("codec2") is None,
    reason="libcodec2 not installed; real Codec2 encode/decode is a build check",
)


def _pcm(frame: AudioFrame) -> np.ndarray:
    return np.frombuffer(frame.samples, dtype="<i2")


# --- fail-loud config error (runs with NO library installed) -----------------


def test_missing_library_fails_loud_by_name(monkeypatch):
    monkeypatch.setattr("radio_server.audio.codec2.find_library", lambda _name: None)
    with pytest.raises(RuntimeError) as excinfo:
        Codec2()
    msg = str(excinfo.value)
    assert "libcodec2" in msg
    assert "codec2" in msg  # names the extra / install path
    assert msg == _MISSING_MSG


# --- library-gated behavior --------------------------------------------------


@_CODEC2_SKIP
def test_queried_geometry_matches_assumptions():
    codec = Codec2()
    try:
        # Guardrail 1: these come from the library, not from memory. Mode 3200 = 20 ms @ 8 kHz.
        assert codec.samples_per_frame == 160
        assert codec.bits_per_frame == 64
        assert codec.bytes_per_frame == 8
    finally:
        codec.close()


@_CODEC2_SKIP
def test_round_trip_returns_canonical_audio_of_expected_length():
    codec = Codec2()
    try:
        # 100 ms @ 48 kHz = 4800 samples -> 800 @ 8 kHz -> exactly 5 frames, no padding.
        original = synth_tone(1000, 100)
        packets = codec.encode(original)
        frames = len(packets) // codec.bytes_per_frame
        decoded = codec.decode(packets)

        assert decoded.format == CANONICAL_FORMAT
        # Each frame is samples_per_frame @ 8 kHz, upsampled 6x back to 48 kHz.
        expected = frames * codec.samples_per_frame * (CANONICAL_FORMAT.rate // 8000)
        assert abs(_pcm(decoded).size - expected) <= 4  # allow resampler edge rounding

        # LOSSY: no sample equality — but a decoded tone must not be silence.
        assert np.abs(_pcm(decoded).astype(np.int32)).max() > 0
    finally:
        codec.close()


@_CODEC2_SKIP
def test_known_length_buffer_produces_expected_frame_count():
    codec = Codec2()
    try:
        spf = codec.samples_per_frame  # 8 kHz samples per frame
        ratio = CANONICAL_FORMAT.rate // 8000  # 48k -> 8k downsample ratio

        # Exact multiple: 5 whole frames' worth of 8 kHz samples, expressed at 48 kHz.
        exact_ms = (5 * spf) * 1000 // 8000  # 100 ms
        exact = synth_tone(1000, exact_ms)
        assert len(exact.samples) // 2 == 5 * spf * ratio  # sanity: 4800 @ 48 kHz
        assert len(codec.encode(exact)) == 5 * codec.bytes_per_frame

        # Non-multiple: a trailing partial frame is silence-padded up to a whole frame.
        # 90 ms @ 48 kHz -> 720 @ 8 kHz -> ceil(720/160) = 5 frames.
        partial = synth_tone(1000, 90)
        assert len(codec.encode(partial)) == 5 * codec.bytes_per_frame
    finally:
        codec.close()


@_CODEC2_SKIP
def test_encode_rejects_non_canonical_format():
    codec = Codec2()
    try:
        wrong = AudioFrame(b"\x00\x00" * 160, AudioFormat(8000, 2, 1))
        with pytest.raises(AudioFormatMismatch):
            codec.encode(wrong)
    finally:
        codec.close()


@_CODEC2_SKIP
def test_decode_rejects_partial_packet():
    codec = Codec2()
    try:
        # One byte short of a whole frame.
        junk = b"\x00" * (codec.bytes_per_frame - 1)
        with pytest.raises(ValueError):
            codec.decode(junk)
    finally:
        codec.close()
