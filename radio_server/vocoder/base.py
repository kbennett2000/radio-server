"""The vocoder seam: PCM <-> compressed-voice frames, backend-independent (ADR 0086).

A *vocoder* turns 8 kHz speech PCM into a compressed digital-voice frame and back. This module
pins the seam — the surface every implementation shares — separate from any one device. The first
implementation, :class:`~radio_server.vocoder.dvdongle.DVDongleVocoder`, drives the DV Dongle's
on-board DVSI **AMBE2000** chip over an FTDI serial link; a later **AMBE3000** (ThumbDV) device or a
**software codec (Griffin)** implements this same :class:`Vocoder` protocol and drops in behind it.

**The seam operates at the vocoder's native 8 kHz / 160-sample / 20 ms frame**, deliberately *not*
the app's 48 kHz :data:`~radio_server.audio.format.CANONICAL_FORMAT`. Every real vocoder (AMBE2000,
AMBE3000, Codec2/Griffin) is natively 8 kHz, so 8 kHz is the common denominator that keeps drop-in
implementations rate-identical. The 48k<->8k resample belongs at the *edge of the future backend*
that wires a vocoder into the live audio path (reusing :mod:`radio_server.audio.resample`, the same
"resample only at the tolerant edge" rule as the DTMF and piper edges) — never inside the vocoder.
This differs on purpose from the reverted Codec2 seam (the dead M17 arc, commit 176ce99), which took
the 48 kHz canonical frame and resampled internally; pushing the resample out keeps the seam a pure
codec.

The frame is still carried as a fail-loud :class:`AudioFrame` (ADR 0006) so a wrong-rate or
wrong-length buffer raises :class:`AudioFormatMismatch` at the boundary rather than being silently
mis-encoded.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..audio import AudioFormat, AudioFrame

#: The vocoder's native PCM sample rate (Hz). D-STAR / AMBE and Codec2 all operate at 8 kHz.
PCM_RATE = 8000

#: PCM samples in one 20 ms voice frame at :data:`PCM_RATE` (160 = 8000 * 0.020). A D-STAR AMBE
#: full-rate frame encodes exactly this many samples.
SAMPLES_PER_FRAME = 160

#: Bytes in one PCM voice frame: :data:`SAMPLES_PER_FRAME` signed-16-bit mono samples (320).
PCM_BYTES_PER_FRAME = SAMPLES_PER_FRAME * 2

#: Coded bits in one D-STAR AMBE full-rate voice frame (72 bits of the 96-bit on-air frame; the
#: other 24 are FEC the vocoder chip does not carry). Verify against the device/DVTool (guardrail 1).
AMBE_BITS = 72

#: Packed bytes in one AMBE voice frame: ``AMBE_BITS / 8`` (9).
AMBE_BYTES_PER_FRAME = AMBE_BITS // 8

#: The one audio format the seam speaks: 8 kHz signed-16-bit little-endian mono. ``encode`` requires
#: a frame in this format; ``decode`` returns one. Distinct from ``CANONICAL_FORMAT`` (48 kHz).
PCM_FORMAT = AudioFormat(PCM_RATE, 2, 1)


class VocoderUnavailable(RuntimeError):
    """Raised when a vocoder cannot be constructed or brought up.

    A config/hardware error surfaced loudly at construction — missing ``pyserial`` (the ``hardware``
    extra), the device absent on the given port, or the start-up handshake failing — named so the
    operator gets an actionable message, not a stack trace. Same shape as the AIOC backend's
    ``_EXTRA_MSG`` (ADR 0029) and the reverted Codec2 missing-library path (commit 176ce99).
    """


class VocoderTimeout(RuntimeError):
    """Raised when a per-frame ``encode``/``decode`` exchange gets no device reply in time.

    Distinct from :class:`VocoderUnavailable` (a bring-up failure): the device opened and handshook,
    but a single frame's reply did not arrive within the bounded deadline — a stall, not a
    misconfiguration.
    """


@runtime_checkable
class Vocoder(Protocol):
    """PCM <-> compressed-voice frame, one 20 ms frame at a time.

    Implementations are constructed against a specific device or codec but expose only this
    surface, so a future digital-voice backend depends on the seam, not the DV Dongle. All work in
    :data:`PCM_FORMAT` (8 kHz); rate conversion to/from the app's 48 kHz canonical audio is the
    caller's concern.
    """

    def encode(self, frame: AudioFrame) -> bytes:
        """Encode one 8 kHz / 160-sample PCM frame to one AMBE voice frame.

        ``frame`` must be in :data:`PCM_FORMAT` and exactly :data:`PCM_BYTES_PER_FRAME` bytes;
        returns exactly :data:`AMBE_BYTES_PER_FRAME` bytes. Raises :class:`AudioFormatMismatch` on a
        wrong-format or wrong-length frame (fail loud, before any device I/O).
        """
        ...

    def decode(self, ambe: bytes) -> AudioFrame:
        """Decode one AMBE voice frame to one 8 kHz / 160-sample PCM frame.

        ``ambe`` must be exactly :data:`AMBE_BYTES_PER_FRAME` bytes; returns a frame in
        :data:`PCM_FORMAT` of :data:`PCM_BYTES_PER_FRAME` bytes. Raises ``ValueError`` on a
        wrong-length input rather than mis-decoding a partial frame.
        """
        ...

    def close(self) -> None:
        """Release the device/codec. Idempotent."""
        ...
