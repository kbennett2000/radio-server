"""Radio protocol surface and shared types.

The whole stack above the radio layer (DTMF decode, auth, sessions, services, TTS)
operates on sound-card audio and depends only on the shared :class:`Radio` surface.
CAT tuning (frequency/channel/tone/mode/scan) is a strict superset provided by
:class:`CatRadio`, implemented only by radios with serial control (the TM-V71A).

Guardrail (ADR 0001): PTT is keyed via the DATA port (SignaLink) or the AIOC serial
line — NEVER via a CAT ``TX`` command. ``ptt()`` and ``transmit()`` are the only
keying paths; no CAT method keys the radio. See docs/adr/0002 for the protocol shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

# Audio payloads carry their format and fail loud on a mismatch (ADR 0006). The type lives
# in the lowest layer, `radio_server.audio`; it is re-exported here so the protocol and its
# consumers keep importing it from `..backends`.
from ..audio import AudioFormat, AudioFormatMismatch, AudioFrame


class Capability(StrEnum):
    """A single operation a backend may or may not support.

    ``capabilities()`` returns the subset a backend actually implements; the API
    layer checks membership before dispatching so unsupported operations fail loudly
    (``UnsupportedCapability``) instead of silently no-op'ing (guardrail 3).
    """

    # Shared surface — every backend supports these.
    TRANSMIT = "transmit"
    RECEIVE = "receive"
    PTT = "ptt"
    STATUS = "status"

    # CAT-only surface — TM-V71A (serial control) only.
    SET_FREQUENCY = "set_frequency"
    SET_CHANNEL = "set_channel"
    SET_SPLIT = "set_split"
    SET_TONE = "set_tone"
    SET_MODE = "set_mode"
    SCAN = "scan"


#: The always-present shared operations.
SHARED_CAPS: frozenset[Capability] = frozenset(
    {Capability.TRANSMIT, Capability.RECEIVE, Capability.PTT, Capability.STATUS}
)

#: The CAT tuning operations, provided only by :class:`CatRadio` backends.
CAT_CAPS: frozenset[Capability] = frozenset(
    {
        Capability.SET_FREQUENCY,
        Capability.SET_CHANNEL,
        Capability.SET_SPLIT,
        Capability.SET_TONE,
        Capability.SET_MODE,
        Capability.SCAN,
    }
)

#: Every capability — what a full-control (V71) backend advertises.
FULL_CAPS: frozenset[Capability] = SHARED_CAPS | CAT_CAPS


class UnsupportedCapability(Exception):
    """Raised when a CAT operation is attempted on a backend that lacks it.

    Carries the attempted :class:`Capability` so callers/the API can report exactly
    which operation is unavailable in the current mode.
    """

    def __init__(self, capability: Capability):
        self.capability = capability
        super().__init__(f"capability not supported in this mode: {capability}")


@dataclass(frozen=True)
class RadioStatus:
    """A point-in-time snapshot of radio state.

    ``transmitting`` and ``busy`` are shared. The CAT fields are populated only by
    backends that support tuning; they stay ``None`` on audio-only backends.
    """

    backend: str
    transmitting: bool = False
    busy: bool = False
    # CAT-only — None unless the backend supports tuning.
    frequency: int | None = None
    #: The transmit frequency when a repeater split is armed, else ``None`` (simplex — TX = RX,
    #: i.e. ``frequency``). Never a mirror of ``frequency``: ``None`` means "not split", which is
    #: what every tuning call resets it to (ADR 0133).
    tx_frequency: int | None = None
    channel: int | None = None
    tone: float | None = None
    mode: str | None = None
    #: Raw received-signal-strength reading behind ``busy``, on whatever scale the backend's
    #: hardware uses, or ``None`` where there is no such number (a hardware carrier-detect pin,
    #: or software VAD). Diagnostic only — nothing decides anything on it. It exists because
    #: ``busy`` is a threshold applied to a number nobody could see: sizing that threshold meant
    #: stopping the service and opening the serial port by hand, on a bench where stopping the
    #: service is exactly what broke it (ADR 0127/0132). Now it can be read off a running station.
    rssi: int | None = None


@runtime_checkable
class Radio(Protocol):
    """The shared surface every backend implements.

    Everything above the radio layer is written against this protocol so that a
    service behaves identically regardless of which radio is attached.
    """

    def transmit(self, audio: AudioFrame) -> None:
        """Key the radio and play ``audio`` out (PTT via DATA port / AIOC RTS)."""
        ...

    def receive(self) -> AudioFrame:
        """Return the most recent received audio from the sound card."""
        ...

    def ptt(self, on: bool) -> None:
        """Assert (``True``) or release (``False``) push-to-talk.

        Keyed via the DATA port (SignaLink self-key) or the AIOC serial line —
        never via CAT.
        """
        ...

    def status(self) -> RadioStatus:
        """Return a :class:`RadioStatus` snapshot."""
        ...

    def capabilities(self) -> frozenset[Capability]:
        """Return the set of operations this backend supports."""
        ...


@runtime_checkable
class CatRadio(Radio, Protocol):
    """A :class:`Radio` that additionally supports CAT tuning (TM-V71A only).

    NOTE: these signatures are intentionally minimal for cycle 1 and may be refined
    (tone type, scan parameters/return) via a future ADR once the CAT layer lands.
    """

    def set_frequency(self, hz: int) -> None:
        """Tune to ``hz`` (CAT). Does NOT key the radio.

        **Clears any armed split** — after this call the radio is simplex until :meth:`set_split`
        re-arms it. That direction is the fail-safe one: a TX leg that survived a retune would let
        an unattended station ID key a repeater's uplink (ADR 0133).
        """
        ...

    def set_channel(self, n: int) -> None:
        """Select memory channel ``n`` (CAT)."""
        ...

    def set_split(self, tx_hz: int | None) -> None:
        """Transmit on ``tx_hz`` while receiving on the tuned frequency, or ``None`` for simplex.

        Repeater access: the offset TX leg is applied inside the radio's own key path — tuned
        before the transmitter is enabled, and returned to the RX leg after the PA drops (ADR
        0133). Does NOT key. Backends validate ``tx_hz`` more strictly than ``set_frequency``
        validates its argument, because this one is a frequency that actually radiates.
        """
        ...

    def set_tone(self, tone: float | None) -> None:
        """Set the CTCSS/subaudible tone (Hz), or ``None`` to disable (CAT)."""
        ...

    def set_mode(self, mode: str) -> None:
        """Set the operating mode, e.g. ``"FM"`` (CAT)."""
        ...

    def scan(self, on: bool) -> None:
        """Start (``True``) or stop (``False``) scanning (CAT)."""
        ...
