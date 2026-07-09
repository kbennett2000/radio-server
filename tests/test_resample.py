"""Resample-at-edges: in-band energy survives, and no aliasing into the DTMF band.

The load-bearing claim is that downsampling canonical 48k audio to multimon-ng's rate does
NOT corrupt DTMF detection — an in-band tone comes through at the right frequency, and a
tone above the target Nyquist is *attenuated* (anti-aliased away), never folded down into
the 697–1633 Hz DTMF range. We prove both with an rFFT.
"""

import numpy as np

from radio_server.audio import (
    MULTIMON_RATE,
    AudioFrame,
    resample,
    synth_tone,
    to_multimon,
)


def _rfft_mag(frame: AudioFrame):
    pcm = np.frombuffer(frame.samples, dtype="<i2").astype(np.float64)
    mag = np.abs(np.fft.rfft(pcm))
    freqs = np.fft.rfftfreq(pcm.size, 1.0 / frame.format.rate)
    return freqs, mag


def _peak_freq(frame: AudioFrame) -> float:
    freqs, mag = _rfft_mag(frame)
    return freqs[mag.argmax()]


def test_output_frame_carries_the_target_rate():
    out = to_multimon(synth_tone(1000, 100))
    assert out.format.rate == MULTIMON_RATE
    assert out.format.width == 2
    assert out.format.channels == 1


def test_inband_tone_survives_downsample_at_the_right_frequency():
    # A high DTMF column tone (1633 Hz) is the worst in-band case; it must come through.
    src = synth_tone(1633, 250, amplitude=0.8, ramp_ms=2.0)
    out = to_multimon(src)
    freqs, mag = _rfft_mag(out)
    bin_hz = freqs[1]  # rFFT bin spacing
    assert abs(_peak_freq(out) - 1633) <= bin_hz  # dominant bin within one bin

    # In-band energy is preserved (not gutted by the filter): compare the resampled peak to
    # the source peak, both normalized per-sample so the different lengths are comparable.
    _, src_mag = _rfft_mag(src)
    src_peak = src_mag.max() / src.format.rate
    out_peak = mag.max() / out.format.rate
    assert out_peak == 0 or 0.5 < out_peak / src_peak < 2.0


def test_out_of_band_tone_is_attenuated_not_aliased():
    # 20 kHz exists at 48k but is far above the 11025 Hz Nyquist of the 22050 target. A
    # quality resampler removes it; naive decimation would alias it down into audio band.
    hi = synth_tone(20000, 250, amplitude=0.9, ramp_ms=2.0)
    ref = synth_tone(1000, 250, amplitude=0.9, ramp_ms=2.0)  # a normal in-band tone
    ref_peak = _rfft_mag(ref)[1].max()

    freqs, mag = _rfft_mag(to_multimon(hi))
    # Nothing meaningful anywhere, and in particular nothing in the DTMF band.
    assert mag.max() < 0.01 * ref_peak
    dtmf_band = mag[(freqs >= 600) & (freqs <= 1700)]
    assert dtmf_band.max() < 0.01 * ref_peak


def test_resample_to_same_rate_is_identity():
    src = synth_tone(1000, 50)
    assert resample(src, src.format.rate) == src


def test_empty_frame_resamples_to_empty():
    src = AudioFrame(b"")
    out = to_multimon(src)
    assert out.samples == b""
    assert out.format.rate == MULTIMON_RATE
