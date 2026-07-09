"""Sine-tone synthesis with a click-free envelope — the real audio primitive (ADR 0006).

``synth_tone`` produces genuine canonical PCM, so it is the minimal real consumer that
proves :class:`~radio_server.audio.format.AudioFrame` is not theoretical. It is also the
substrate for CW station ID (cycle 6): Morse keys this exact primitive on and off, and the
raised-cosine on/off envelope here is what keeps those keyings from clicking.
"""

from __future__ import annotations

import numpy as np

from .format import CANONICAL_FORMAT, AudioFrame, AudioFormat

_PCM_DTYPE = np.dtype("<i2")
_INT16_MAX = 32767

#: Default rise/fall time (ms) of the on/off envelope. ~5 ms is the usual anti-click ramp.
DEFAULT_RAMP_MS = 5.0


def synth_tone(
    freq_hz: float,
    duration_ms: float,
    format: AudioFormat = CANONICAL_FORMAT,
    *,
    amplitude: float = 0.5,
    ramp_ms: float = DEFAULT_RAMP_MS,
) -> AudioFrame:
    """Synthesize a sine at ``freq_hz`` for ``duration_ms`` as a canonical PCM frame.

    ``amplitude`` is a 0..1 fraction of full scale. A raised-cosine on/off envelope of
    ``ramp_ms`` (auto-shrunk so the two ramps never overlap) fades the tone in and out to
    avoid key clicks. Deterministic — no RNG — so the output is exactly assertable.

    Mono 16-bit only (the canonical format); other widths/channels are out of scope here.
    """
    if format.channels != 1 or format.width != 2:
        raise ValueError(f"synth_tone supports canonical mono 16-bit only, got {format}")

    n = round(format.rate * duration_ms / 1000.0)
    if n <= 0:
        return AudioFrame(b"", format)

    t = np.arange(n, dtype=np.float64) / format.rate
    wave = amplitude * np.sin(2.0 * np.pi * freq_hz * t)
    wave *= _envelope(n, format.rate, ramp_ms)

    pcm = np.rint(np.clip(wave, -1.0, 1.0) * _INT16_MAX).astype(_PCM_DTYPE)
    return AudioFrame(pcm.tobytes(), format)


def _envelope(n: int, rate: int, ramp_ms: float) -> np.ndarray:
    """A raised-cosine rise/fall window of length ``n`` (flat 1.0 in the middle)."""
    env = np.ones(n, dtype=np.float64)
    ramp_n = int(round(rate * ramp_ms / 1000.0))
    # Never let the two ramps overlap: cap each at half the tone.
    ramp_n = min(ramp_n, n // 2)
    if ramp_n <= 0:
        return env

    k = np.arange(ramp_n, dtype=np.float64)
    rise = 0.5 * (1.0 - np.cos(np.pi * k / ramp_n))
    env[:ramp_n] = rise
    env[n - ramp_n :] = rise[::-1]
    return env
