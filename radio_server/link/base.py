"""Link protocol surface and shared types (ADR 0041).

A :class:`Link` is radio-server's **second port**: a peer on the audio bus that is *not* the
antenna — an M17 reflector, an AllStar node, and (later, via AllStar) EchoLink. It mirrors the
:class:`~radio_server.backends.Radio` abstraction (ADR 0001/0002): one protocol, a hardware-free
mock (:class:`~radio_server.link.mock.MockLink`) everything is built against, and real transports
brought up last.

**Direction convention — the single easiest thing to get backwards.** ``Radio.transmit`` means
"out the antenna"; ``Link.transmit`` means "out to the network." So a bridge (a later cycle) wires
radio RX into ``link.transmit`` (local RF → internet) and ``link.receive`` into ``radio.transmit``
(internet → out the antenna). Everything arriving on a Link is third-party traffic the server puts
on the air under the licensee's callsign.

**Capability split (guardrail 3).** The shared surface (connect/disconnect/status/transmit/receive)
is universal; ``DIRECTORY`` and ``LISTEN_ONLY`` are real per-backend differences. ``capabilities()``
reports what a backend implements, and the two optional operations raise :class:`UnsupportedLinkCapability`
(carrying the attempted capability) instead of silently no-op'ing, so the API layer can later 501 by
name. Unlike :class:`~radio_server.backends.CatRadio`, the optional operations are **orthogonal** — a
backend may have either, both, or neither — so ``Link`` is one flat protocol whose optional methods
self-gate, not a two-tier superset. See ``docs/adr/0041`` for the reasoning.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

# Audio payloads carry their format and fail loud on a mismatch (ADR 0006). `..audio` is the ONLY
# radio_server layer this package imports — the Link surface is otherwise a self-contained leaf.
from ..audio import AudioFrame


class LinkCapability(StrEnum):
    """A single operation a Link backend may or may not support.

    ``capabilities()`` returns the subset a backend actually implements; the optional operations
    raise :class:`UnsupportedLinkCapability` when unsupported rather than silently no-op'ing, so the
    API can fail loudly (a future 501 by name) — guardrail 3.
    """

    # Shared surface — every Link backend supports these.
    CONNECT = "connect"
    DISCONNECT = "disconnect"
    STATUS = "status"
    TRANSMIT = "transmit"
    RECEIVE = "receive"
    # Non-universal — real per-backend differences, orthogonal to each other.
    #: A central user/peer database. EchoLink and AllStar have one; M17 has NO central directory —
    #: on M17 your callsign is your ID.
    DIRECTORY = "directory"
    #: A protocol-level listen-only mode (mrefd ``LSTN``). This is what makes a listen-before-you-talk
    #: tier possible with zero credentials; EchoLink has no such mode. A first-class capability, not
    #: a UI flag.
    LISTEN_ONLY = "listen_only"


#: The universal surface — every Link implements these.
SHARED_CAPS: frozenset[LinkCapability] = frozenset(
    {
        LinkCapability.CONNECT,
        LinkCapability.DISCONNECT,
        LinkCapability.STATUS,
        LinkCapability.TRANSMIT,
        LinkCapability.RECEIVE,
    }
)
#: The per-backend options. Orthogonal: a backend may advertise either, both, or neither.
OPTIONAL_CAPS: frozenset[LinkCapability] = frozenset(
    {LinkCapability.DIRECTORY, LinkCapability.LISTEN_ONLY}
)
#: Every capability — the maximal backend (what MockLink advertises by default).
FULL_CAPS: frozenset[LinkCapability] = SHARED_CAPS | OPTIONAL_CAPS


class UnsupportedLinkCapability(Exception):
    """Raised when an optional Link operation is used on a backend that lacks the capability.

    Carries the attempted :class:`LinkCapability` so a caller (or the API layer) can report *which*
    operation is unavailable — never a silent no-op (guardrail 3).
    """

    def __init__(self, capability: LinkCapability):
        self.capability = capability
        super().__init__(f"link capability not supported by this backend: {capability}")


@dataclass(frozen=True)
class Station:
    """A peer on the link.

    Minimal on purpose — for M17 the callsign *is* the identity (there is no directory to resolve it
    against). Extend by ADR when a backend needs more (module, node number, gateway).
    """

    callsign: str


@dataclass(frozen=True)
class LinkStatus:
    """A point-in-time snapshot of a Link.

    ``enabled`` is the deliberate operator gate (see :meth:`Link.enable`) and is **never** sticky —
    a Link comes up disabled. ``connected``/``target`` describe the current session; ``stations`` is
    who is on and ``talker`` is who is transmitting right now (``None`` when the channel is idle).
    """

    backend: str
    enabled: bool = False
    connected: bool = False
    target: str | None = None
    stations: tuple[Station, ...] = ()
    talker: Station | None = None


@runtime_checkable
class Link(Protocol):
    """The network-peer surface, parallel to :class:`~radio_server.backends.Radio`.

    One flat protocol: the shared methods are always meaningful, and the two optional operations
    (:meth:`directory`, :meth:`set_listen_only`) raise :class:`UnsupportedLinkCapability` on backends
    that do not advertise the matching capability. As with ``Radio``, ``runtime_checkable`` only
    checks *method presence*; the real "unsupported here" contract is the raising, not ``isinstance``.
    """

    def enable(self, on: bool) -> None:
        """Set the master enable gate. A Link comes up disabled; enabling it is a deliberate act."""
        ...

    def connect(self, target: str) -> None:
        """Connect to ``target`` (a reflector/module name or a peer callsign)."""
        ...

    def disconnect(self) -> None:
        """Drop the current connection."""
        ...

    def transmit(self, audio: AudioFrame) -> None:
        """Send a frame OUT to the network (the antenna side is :meth:`Radio.transmit`)."""
        ...

    def receive(self) -> AudioFrame | None:
        """Pull a frame IN from the network, or ``None`` when the network is idle."""
        ...

    def status(self) -> LinkStatus:
        """A point-in-time :class:`LinkStatus` snapshot."""
        ...

    def capabilities(self) -> frozenset[LinkCapability]:
        """The subset of :class:`LinkCapability` this backend implements."""
        ...

    def directory(self) -> tuple[Station, ...]:
        """The known peers/users from the central directory. Requires ``DIRECTORY``."""
        ...

    def set_listen_only(self, on: bool) -> None:
        """Enter/leave protocol-level listen-only mode. Requires ``LISTEN_ONLY``."""
        ...
