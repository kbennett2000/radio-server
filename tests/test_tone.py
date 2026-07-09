"""synth_tone: real canonical PCM, correct frequency, click-free envelope (ADR 0006).

synth_tone is the first producer of *real* samples (not a symbolic stub), so it is where we
prove the AudioFrame type carries genuine audio. It is also the CW-ID substrate for cycle 6,
so the anti-click envelope is asserted here.
"""

import numpy as np

from radio_server.audio import CANONICAL_FORMAT, AudioFormat, synth_tone


def _samples(frame):
    return np.frombuffer(frame.samples, dtype="<i2").astype(np.float64)


def test_sample_count_and_byte_length():
    frame = synth_tone(1000, 50)  # 50 ms @ 48000 Hz = 2400 samples
    assert frame.format == CANONICAL_FORMAT
    assert _samples(frame).size == 2400
    assert len(frame.samples) == 2400 * CANONICAL_FORMAT.frame_bytes


def test_frequency_is_correct_via_fft():
    frame = synth_tone(1200, 500)  # long enough for tight bin resolution
    pcm = _samples(frame)
    mag = np.abs(np.fft.rfft(pcm))
    freqs = np.fft.rfftfreq(pcm.size, 1.0 / frame.format.rate)
    assert abs(freqs[mag.argmax()] - 1200) <= freqs[1]  # within one bin


def test_envelope_ramps_off_the_edges():
    pcm = _samples(synth_tone(1000, 50, amplitude=0.8))
    peak = np.abs(pcm).max()
    # No hard edge: the tone starts and ends at (near) zero rather than full amplitude.
    assert abs(pcm[0]) < 0.05 * peak
    assert abs(pcm[-1]) < 0.05 * peak
    # And it does reach full amplitude in the sustained middle (a window, so we don't land
    # on a single-sample zero crossing).
    mid = pcm[len(pcm) // 2 - 50 : len(pcm) // 2 + 50]
    assert np.abs(mid).max() > 0.95 * peak


def test_envelope_rise_is_monotonic_at_the_attack():
    # The first few milliseconds should be a smooth rise, not an instantaneous jump.
    pcm = _samples(synth_tone(200, 100, amplitude=0.8, ramp_ms=5.0))
    # Envelope of a low tone over the 5 ms (240-sample) attack: track the running peak.
    attack = np.abs(pcm[:240])
    running = np.maximum.accumulate(attack)
    assert running[0] < running[-1]  # amplitude grows across the attack


def test_amplitude_scales_full_scale():
    loud = np.abs(_samples(synth_tone(1000, 100, amplitude=0.9))).max()
    quiet = np.abs(_samples(synth_tone(1000, 100, amplitude=0.3))).max()
    assert loud > quiet
    assert loud <= 32767  # never clips past full scale


def test_deterministic():
    assert synth_tone(1633, 40) == synth_tone(1633, 40)


def test_zero_duration_is_empty():
    frame = synth_tone(1000, 0)
    assert frame.samples == b""
    assert frame.format == CANONICAL_FORMAT
