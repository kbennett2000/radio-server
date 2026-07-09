"""Canonical audio format and the format-carrying, fail-loud :class:`AudioFrame`.

Cycles 1–4 modeled audio as an opaque ``AudioFrame = bytes`` alias. Cycle 1 flagged the
risk that plain bytes "silently paper over a format mismatch until you hear garbage." This
module pins the format (ADR 0006) and makes it load-bearing: an :class:`AudioFrame` carries
its :class:`AudioFormat` and **fails loud** — a mismatched concatenation or transmit raises
:class:`AudioFormatMismatch` rather than coercing bytes into nonsense.

The canonical internal format is 48000 Hz, signed 16-bit little-endian, mono. It matches the
real-time sound-card edge (USB audio codecs are 48k-native); resampling happens only at the
tolerant software edges (see :mod:`radio_server.audio.resample`), so a device that only does
44.1k is an edge-resample config change, not an architecture change.

The payload is opaque ``bytes``: real PCM for synthesized/decoded audio, or a symbolic
placeholder for the deterministic stubs (``b"<id:AE9S>"``). The fail-loud contract is
**format identity only** — deliberately not PCM-length divisibility — so the symbolic stubs
remain valid frames and stay exactly assertable in ``tx_log``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Canonical internal sample rate (Hz). Pinned architecture — the sound-card edge is
#: 48k-native; other device rates are handled by edge resampling, not by changing this.
CANONICAL_RATE = 48000

#: Canonical sample width in bytes: signed 16-bit little-endian (numpy dtype ``'<i2'``).
CANONICAL_WIDTH = 2

#: Canonical channel count: mono.
CANONICAL_CHANNELS = 1


class AudioFormatMismatch(ValueError):
    """Raised when audio of one format is combined with, or transmitted as, another.

    This is the fail-loud guard that replaces the old opaque-``bytes`` alias: concatenating
    two frames of differing format, or handing a radio a frame whose format it does not
    expect, raises rather than silently producing garbage.
    """


@dataclass(frozen=True)
class AudioFormat:
    """The rate/width/channels a block of PCM is in.

    Frozen (hashable, value equality) so it can be compared for the fail-loud check and
    passed around as a plain value (e.g. to :func:`radio_server.audio.tone.synth_tone`).
    """

    rate: int
    width: int
    channels: int

    @property
    def frame_bytes(self) -> int:
        """Bytes per sample-frame (all channels): ``width * channels``."""
        return self.width * self.channels

    def __str__(self) -> str:
        return f"{self.rate}Hz/{self.width * 8}bit/{self.channels}ch"


#: The one canonical internal format every backend-agnostic layer works in.
CANONICAL_FORMAT = AudioFormat(CANONICAL_RATE, CANONICAL_WIDTH, CANONICAL_CHANNELS)


@dataclass(frozen=True)
class AudioFrame:
    """A block of PCM samples that carries its own :class:`AudioFormat`.

    ``samples`` is an opaque payload: real little-endian PCM for synthesized/decoded audio,
    or a symbolic placeholder for the deterministic stubs. ``format`` defaults to
    :data:`CANONICAL_FORMAT` so producers and tests stay terse (``AudioFrame(b"...")``).

    Frozen, so it has value equality and is hashable — existing ``tx_log == [...]``
    assertions keep working unchanged.
    """

    samples: bytes
    format: AudioFormat = field(default=CANONICAL_FORMAT)

    def __add__(self, other: AudioFrame) -> AudioFrame:
        """Concatenate two frames of the **same** format into one over.

        Raises :class:`AudioFormatMismatch` if the formats differ (or ``other`` is not an
        :class:`AudioFrame`). This is what makes ``StationId``'s ``id + audio`` prepend safe
        by construction: two frames can only join if they are genuinely the same format.
        """
        if not isinstance(other, AudioFrame):
            raise AudioFormatMismatch(
                f"cannot concatenate AudioFrame with {type(other).__name__}"
            )
        if other.format != self.format:
            raise AudioFormatMismatch(
                f"cannot concatenate frames of different format: "
                f"{self.format} + {other.format}"
            )
        return AudioFrame(self.samples + other.samples, self.format)
