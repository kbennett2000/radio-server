"""Resample only at the tolerant software edges (ADR 0006).

Audio lives in the canonical 48k format everywhere internally; we convert only where a
neighbour demands a different rate — the DTMF decode edge (``multimon-ng``) and the
TTS-native edge. Downsampling is done with a quality resampler (``soxr``, VHQ) whose
anti-alias filter protects the DTMF band: naive decimation would fold high-frequency energy
down into the 697–1633 Hz tones and corrupt detection.
"""

from __future__ import annotations

import numpy as np
import soxr

from .format import AudioFrame, AudioFormat

#: ``multimon-ng``'s expected input rate for raw ``s16le`` mono. Documented default —
#: VERIFY AGAINST THE INSTALLED multimon-ng BUILD (guardrail 1) before trusting it on
#: hardware; it is a config target, not an asserted hardware fact.
MULTIMON_RATE = 22050

#: numpy dtype for canonical signed-16-bit little-endian samples, host-endian independent.
_PCM_DTYPE = np.dtype("<i2")

_INT16_MAX = 32767


def resample(frame: AudioFrame, target_rate: int) -> AudioFrame:
    """Return ``frame`` resampled to ``target_rate`` (same width/channels).

    Mono ``s16le`` only for now — the canonical format. A non-mono or non-16-bit frame
    raises, rather than silently mis-decoding; wider format support is a later concern.
    Uses ``soxr`` VHQ, whose steep anti-alias filter keeps a downsample from aliasing
    out-of-band energy into the DTMF band.
    """
    fmt = frame.format
    if fmt.channels != 1:
        raise ValueError(f"resample supports mono only, got {fmt.channels} channels")
    if fmt.width != 2:
        raise ValueError(f"resample supports 16-bit only, got width {fmt.width}")
    if target_rate == fmt.rate:
        return frame

    pcm = np.frombuffer(frame.samples, dtype=_PCM_DTYPE)
    if pcm.size == 0:
        out = pcm
    else:
        as_float = pcm.astype(np.float32) / _INT16_MAX
        resampled = soxr.resample(as_float, fmt.rate, target_rate, quality="VHQ")
        clipped = np.clip(resampled, -1.0, 1.0)
        out = np.rint(clipped * _INT16_MAX).astype(_PCM_DTYPE)

    return AudioFrame(out.tobytes(), AudioFormat(target_rate, fmt.width, fmt.channels))


def to_multimon(frame: AudioFrame) -> AudioFrame:
    """Canonical 48k → :data:`MULTIMON_RATE` for the DTMF decode edge."""
    return resample(frame, MULTIMON_RATE)


def to_canonical(frame: AudioFrame) -> AudioFrame:
    """Any rate (e.g. TTS-native ~22050) → canonical 48k for playback/transmit."""
    from .format import CANONICAL_RATE

    return resample(frame, CANONICAL_RATE)
