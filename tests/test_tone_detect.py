"""Real-time DTMF tone detector (ADR 0049).

The detector answers "is a DTMF dual-tone present in this frame?" without decoding a digit — it
is what lets the Mumble bridge mute control tones and yield keying in real time, independent of
multimon-ng's decode latency. These tests prove it fires on every one of the 16 keys (whole tone
and a single 20 ms frame slice) and rejects silence, noise, and non-DTMF tones/chords.
"""

import numpy as np

from radio_server.audio.dtmf import DTMF_FREQS, synth_dtmf
from radio_server.audio.format import CANONICAL_FORMAT
from radio_server.link.tone_detect import DtmfToneDetector

RATE = CANONICAL_FORMAT.rate
FRAME_SAMPLES = RATE // 50  # 20 ms canonical frame == 960 samples
FRAME_BYTES = FRAME_SAMPLES * CANONICAL_FORMAT.frame_bytes


def _pcm(signal: np.ndarray) -> bytes:
    """Float [-1, 1] samples → canonical s16le bytes."""
    clipped = np.clip(signal, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def _tone(freq: float, n: int = FRAME_SAMPLES, amp: float = 0.4) -> np.ndarray:
    t = np.arange(n) / RATE
    return amp * np.sin(2 * np.pi * freq * t)


def test_detects_every_dtmf_key_full_tone():
    det = DtmfToneDetector()
    for digit in DTMF_FREQS:
        assert det.detect(synth_dtmf(digit).samples), f"missed full tone for {digit!r}"


def test_detects_every_dtmf_key_single_frame_slice():
    # A real bridge frame is one ~20 ms slice, not the whole 120 ms fixture.
    det = DtmfToneDetector()
    for digit in DTMF_FREQS:
        frame = synth_dtmf(digit).samples[:FRAME_BYTES]
        assert det.detect(frame), f"missed 20 ms slice for {digit!r}"


def test_rejects_silence():
    det = DtmfToneDetector()
    assert not det.detect(bytes(FRAME_BYTES))


def test_rejects_empty_frame():
    det = DtmfToneDetector()
    assert not det.detect(b"")


def test_rejects_white_noise():
    det = DtmfToneDetector()
    rng = np.random.default_rng(1234)
    noise = 0.3 * rng.standard_normal(FRAME_SAMPLES)
    assert not det.detect(_pcm(noise))


def test_rejects_single_non_dtmf_tone():
    det = DtmfToneDetector()
    assert not det.detect(_pcm(_tone(1000.0)))  # 1 kHz, not a DTMF frequency


def test_rejects_single_dtmf_frequency_alone():
    # One valid low-group tone with no high-group partner is not a keypress.
    det = DtmfToneDetector()
    assert not det.detect(_pcm(_tone(697.0)))


def test_rejects_voice_like_chord():
    # A spread of non-DTMF partials (a crude vowel), no two exact DTMF bins dominating.
    det = DtmfToneDetector()
    chord = _tone(220.0, amp=0.3) + _tone(500.0, amp=0.25) + _tone(1900.0, amp=0.2)
    assert not det.detect(_pcm(chord))


def test_detects_dtmf_buried_in_light_noise():
    # Real HT audio is a tone plus channel hiss; the pair should still win.
    det = DtmfToneDetector()
    rng = np.random.default_rng(7)
    low, high = DTMF_FREQS["5"]
    sig = _tone(low, amp=0.35) + _tone(high, amp=0.35) + 0.03 * rng.standard_normal(FRAME_SAMPLES)
    assert det.detect(_pcm(sig))


def test_accepts_ndarray_samples():
    det = DtmfToneDetector()
    low, high = DTMF_FREQS["9"]
    samples = ((_tone(low) + _tone(high)) * 32767.0).astype("<i2")
    assert det.detect(samples)
