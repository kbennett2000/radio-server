"""The WebSocket event surface: a typed event and a minimal in-process fan-out (ADR 0011).

There is no event bus below the API — ``Radio.status()``/``ptt()`` are poll-only — so this
module introduces the smallest thing that turns state changes into a live push stream: a
``type``-discriminated :class:`Event` and an :class:`EventHub` that fans one published event
out to every connected WebSocket.

The ``type`` field is deliberately open. The app publishes ``"status"`` and ``"ptt"``
events, ``"scan"`` progress from the scan engine (cycle 11), ``"session"`` lifecycle from the
controller loop (session open/close, forced ID) since cycle 12, and ``"rx"`` squelch-open/close
edges from the RX pump; ``"busy"`` remains a reserved name for a future cycle.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from typing import Any

from ..backends import Radio

#: The event ``type`` values the app emits or reserves. ``"status"`` is a full RadioStatus
#: snapshot; ``"ptt"`` carries key up/down (``data.on``); ``"scan"`` carries scan-engine progress
#: (phases in ``radio_server.scan.SCAN_PHASES``); ``"rx"`` carries squelch open/close
#: (``data.active``); ``"arbiter"`` carries duplex-mode transitions (``data.mode``); ``"session"``
#: carries controller lifecycle (phases in ``radio_server.controller.CONTROLLER_PHASES``);
#: ``"auth"`` carries over-RF login results (``data.result``); ``"command"`` carries a dispatched
#: DTMF service (``data.service``); ``"link"`` carries Mumble link state changes;
#: ``"capabilities"`` carries the active radio's capability set (``data.capabilities``), re-emitted on
#: a live backend switch so a connected client re-greys its controls without reconnecting (ADR 0076).
#: ``"dstar"``/``"activity"`` carry D-STAR link + heard-station changes (ADR 0088/0089); ``"dvap"``
#: carries a DVAP module link change with the confirmed snapshot (ADR 0095). ``"alarm"`` carries a
#: safety-cutoff notice — today the transmitter time-out force-unkey (``data.kind == "tx_timeout"``,
#: ``data.tot`` the fired cap in seconds; ADR 0117). ``"busy"`` is reserved.
EVENT_TYPES = (
    "status",
    "ptt",
    "scan",
    "rx",
    "arbiter",
    "session",
    "auth",
    "command",
    "link",
    "capabilities",
    "dstar",
    "activity",
    "dvap",
    "alarm",
    "busy",
)


@dataclass(frozen=True)
class Event:
    """A single event pushed to WebSocket subscribers.

    ``type`` is the discriminator a client switches on; ``data`` is its JSON-ready payload
    (for a ``"status"`` event, the fields of :class:`~radio_server.backends.RadioStatus`).
    """

    type: str
    data: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        """Return the wire form (``{"type": ..., "data": {...}}``) sent over the socket."""
        return asdict(self)


def status_event(radio: Radio) -> Event:
    """Snapshot ``radio``'s current status as a ``"status"`` event."""
    return Event(type="status", data=asdict(radio.status()))


def capabilities_event(radio: Radio) -> Event:
    """Snapshot ``radio``'s capability set as a ``"capabilities"`` event (ADR 0076).

    The payload mirrors ``GET /capabilities`` (a sorted string array) wrapped in ``data.capabilities``,
    so a live backend switch can push the new set to connected clients and they re-grey their controls
    without reconnecting.
    """
    return Event(
        type="capabilities",
        data={"capabilities": sorted(str(c) for c in radio.capabilities())},
    )


class EventHub:
    """A minimal in-process async fan-out for WebSocket subscribers.

    Each subscriber gets its own queue via :meth:`subscribe`; :meth:`publish` puts the event
    onto every live subscriber's queue. One hub per app, shared by all connections — no
    external broker. ``publish`` is synchronous and non-blocking (unbounded queues), so any
    request handler or background task can emit an event without being async-aware.
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()

    def subscribe(self) -> asyncio.Queue[Event]:
        """Register a new subscriber and return its event queue."""
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        """Drop a subscriber (idempotent) — call on disconnect."""
        self._subscribers.discard(queue)

    def publish(self, event: Event) -> None:
        """Fan ``event`` out to every live subscriber."""
        for queue in self._subscribers:
            queue.put_nowait(event)

    @property
    def subscriber_count(self) -> int:
        """Number of live subscribers (inspectable by tests)."""
        return len(self._subscribers)
