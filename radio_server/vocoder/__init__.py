"""The vocoder seam: PCM <-> compressed digital-voice frames (ADR 0086).

:class:`Vocoder` is the backend-independent surface (encode 8 kHz PCM -> AMBE, decode back);
:class:`DVDongleVocoder` implements it over the DV Dongle's DVSI AMBE2000 chip. A future AMBE3000
device or a software codec (Griffin) implements the same :class:`Vocoder` and drops in behind it.

Not wired into the live app: only the ``doctor --vocoder-loopback`` self-test constructs a vocoder.
See :mod:`radio_server.vocoder.base` for why the seam operates at the native 8 kHz frame.
"""

from __future__ import annotations

from .base import (
    AMBE_BITS,
    AMBE_BYTES_PER_FRAME,
    PCM_BYTES_PER_FRAME,
    PCM_FORMAT,
    PCM_RATE,
    SAMPLES_PER_FRAME,
    Vocoder,
    VocoderTimeout,
    VocoderUnavailable,
)
from .dvdongle import DEFAULT_BAUD, DEFAULT_DVDONGLE_PORT, DVDongleVocoder

__all__ = [
    "AMBE_BITS",
    "AMBE_BYTES_PER_FRAME",
    "PCM_BYTES_PER_FRAME",
    "PCM_FORMAT",
    "PCM_RATE",
    "SAMPLES_PER_FRAME",
    "Vocoder",
    "VocoderTimeout",
    "VocoderUnavailable",
    "DEFAULT_BAUD",
    "DEFAULT_DVDONGLE_PORT",
    "DVDongleVocoder",
]
