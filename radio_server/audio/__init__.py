"""Canonical audio format, fail-loud frames, edge resampling, and tone synthesis.

The lowest layer of the stack: everything backend-agnostic works in the canonical format
defined here. See ADR 0006.
"""

from .format import (
    CANONICAL_CHANNELS,
    CANONICAL_FORMAT,
    CANONICAL_RATE,
    CANONICAL_WIDTH,
    AudioFormat,
    AudioFormatMismatch,
    AudioFrame,
)
from .resample import MULTIMON_RATE, resample, to_canonical, to_multimon
from .tone import DEFAULT_RAMP_MS, synth_tone

__all__ = [
    "AudioFormat",
    "AudioFormatMismatch",
    "AudioFrame",
    "CANONICAL_CHANNELS",
    "CANONICAL_FORMAT",
    "CANONICAL_RATE",
    "CANONICAL_WIDTH",
    "DEFAULT_RAMP_MS",
    "MULTIMON_RATE",
    "resample",
    "synth_tone",
    "to_canonical",
    "to_multimon",
]
