"""The WebSocket event surface: a typed event and a minimal in-process fan-out (ADR 0011).

There is no event bus below the API — ``Radio.status()``/``ptt()`` are poll-only — so this
module introduces the smallest thing that turns state changes into a live push stream: a
``type``-discriminated :class:`Event` and an :class:`EventHub` that fans one published event
out to every connected WebSocket.

The ``type`` field is deliberately open. The app publishes ``"status"`` and ``"ptt"``
events, ``"scan"`` progress from the scan engine (cycle 11), ``"session"`` lifecycle from the
controller loop (session open/close, forced ID) since cycle 12, and ``"rx"`` squelch-open/close
edges from the RX pump; ``"busy"`` remains a reserved name for a future cycle.

Each subscriber's queue is **bounded** since ADR 0180 — see :data:`DEFAULT_EVENT_QUEUE_MAXSIZE`
and :meth:`EventHub.subscribe` for what happens when one fills, and why the answer differs between
a WebSocket (which can reconnect and resync) and the station ledger (which cannot).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from ..backends import Radio

logger = logging.getLogger(__name__)

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
#: ``data.tot`` the fired cap in seconds; ADR 0117). ``"overflow"`` is the odd one out and is
#: **never published**: it is placed directly in ONE subscriber's queue as the last thing that
#: subscriber will ever receive, because it fell too far behind to keep (ADR 0180). ``"busy"`` is
#: reserved.
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
    "overflow",
    "busy",
)

#: Per-subscriber queue depth, in events. **DERIVED, not chosen** (ADR 0180): ``4 × 40 = 160``.
#:
#: ``4`` is MEASURED — the busiest one-second bucket seen on ``/events`` through a full
#: ``scripts/bench/acceptance.py`` run against the deployed station (25 frames in 116 s of a run
#: that tunes, keys, authenticates over RF, dispatches a service and restarts the unit; an idle
#: station published *nothing at all* in 60 s beyond the connect snapshot).
#:
#: ``40`` is ``WS_PING_INTERVAL_SECONDS + WS_PING_TIMEOUT_SECONDS`` (``radio_server.__main__``) —
#: the 40.0 s ADR 0171 *measured* as the worst case for uvicorn to notice a peer that has gone
#: silent with its socket still open.
#:
#: The product is what makes the bound mean something rather than being a round number: **at the
#: busiest rate this station has been measured at, a subscriber can only fill this queue by being
#: slower than real time for longer than it takes the server to give up on a peer that is not there
#: at all.** Overflow is therefore worse than dead, which is exactly the condition under which
#: dropping the subscriber is the right answer. Read the other way, it is the stall this hub
#: tolerates: ``160 / rate`` seconds, so a client that drives the REST surface harder buys itself
#: proportionally less patience — which is correct, because it is also the one making the backlog.
#:
#: Worst case memory is ~765 B (the largest frame, a ``status``) × 160 ≈ 122 KB per subscriber, and
#: less in practice: one ``Event`` is referenced from every queue, not copied into each.
#:
#: VERIFY AGAINST HARDWARE if the taxonomy grows a high-rate publisher. ``deepest_queue`` in
#: ``GET /status`` is the standing instrument that says whether this number is still generous.
DEFAULT_EVENT_QUEUE_MAXSIZE = 160

#: What :meth:`EventHub.publish` does when a subscriber's queue is full. There are two, because the
#: hub has two kinds of subscriber and only one of them has a way back.
#:
#: ``DROP_OLDEST`` — evict the oldest queued event and keep the subscriber. The conservative choice
#: and the default: correct for a consumer with **no reconnect**, like the station ledger's drain
#: task, where dropping the subscriber would stop the Part 97 operating log permanently and
#: silently. It loses events, which is why the loss is counted.
#:
#: ``DROP_SUBSCRIBER`` — unregister the subscriber, discard its backlog, and leave a single
#: :func:`overflow_event` in its queue as a readable notice. Correct for ``/events``, whose handler
#: sends a full ``status`` snapshot on connect: a reconnect **is** a resync, so a dropped browser is
#: back with correct current state in about a second. Silently dropping an event instead would leave
#: a *connected* client rendering stale state with no way to know — this repo's recurring failure
#: shape — and would drop the ``alarm`` while keeping the ``status``.
DROP_OLDEST = "drop_oldest"
DROP_SUBSCRIBER = "drop_subscriber"


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


def overflow_event(missed: int, queue_maxsize: int) -> Event:
    """The last frame a dropped subscriber receives — a notice it can actually read.

    ``talker_slot``'s idiom (ADR 0161/0170): a browser cannot act on a close code alone, so the
    reason travels in a message and the close follows it. ``missed`` is what THIS subscriber lost,
    not a station-wide total: its discarded backlog plus the one event that did not fit.
    """
    return Event(type="overflow", data={"missed": missed, "queue_maxsize": queue_maxsize})


class EventHub:
    """A minimal in-process async fan-out for WebSocket subscribers.

    Each subscriber gets its own **bounded** queue via :meth:`subscribe`; :meth:`publish` puts the
    event onto every live subscriber's queue. One hub per app, shared by all connections — no
    external broker. ``publish`` is synchronous and non-blocking, so any request handler or
    background task can emit an event without being async-aware.

    **``publish`` is total: it never raises, whatever a subscriber does.** It is called from REST
    handlers, the RX pump, the duplex arbiter and the transport watcher, so a ``QueueFull`` escaping
    into one of those would let a slow browser fault a transmission path. ADR 0018's rule for the
    ledger holds for the hub verbatim — a passive consumer is a place data goes to rest, never a
    place a fault comes from. That is also why "cap it and raise" was rejected in ADR 0180.

    **Loop-thread only.** ``asyncio.Queue`` is not thread-safe, so every caller must already be on
    the app's event loop; the one cross-thread publisher (the transmitter time-out alarm, fired on a
    ``threading.Timer``) hops via ``call_soon_threadsafe`` before it gets here. This class does not
    enforce that — it inherits it, as it always has.
    """

    def __init__(self, maxsize: int = DEFAULT_EVENT_QUEUE_MAXSIZE) -> None:
        self._maxsize = maxsize
        #: queue -> its overflow policy. A dict rather than ADR 0011's set because the policy is a
        #: property of the SUBSCRIBER, not of the hub: see :data:`DROP_OLDEST`.
        self._subscribers: dict[asyncio.Queue[Event], str] = {}
        self._published = 0
        self._deepest = 0
        self._dropped_subscribers = 0
        self._dropped_deliveries = 0

    def subscribe(self, on_overflow: str = DROP_OLDEST) -> asyncio.Queue[Event]:
        """Register a new subscriber and return its bounded event queue.

        ``on_overflow`` defaults to the conservative :data:`DROP_OLDEST`, so a subscriber that
        cannot recover — a task, a file writer, a bridge — cannot silently inherit the policy that
        would end it. A WebSocket handler that sends a snapshot on connect should pass
        :data:`DROP_SUBSCRIBER` and close on the notice. ``tests/test_event_hub_bound.py`` pins
        every call site's choice by scanning this source, so the next one has to state it.
        """
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers[queue] = on_overflow
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        """Drop a subscriber (idempotent) — call on disconnect.

        Idempotent matters more since ADR 0180: the hub may already have dropped this queue for
        overflow, and the handler's ``finally`` still runs.
        """
        self._subscribers.pop(queue, None)

    def publish(self, event: Event) -> None:
        """Fan ``event`` out to every live subscriber. Never raises."""
        self._published += 1
        # A snapshot of the items: `_overflow` can unregister a subscriber mid-fan-out.
        for queue, policy in list(self._subscribers.items()):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self._overflow(queue, policy, event)
            self._deepest = max(self._deepest, queue.qsize())

    def _overflow(self, queue: asyncio.Queue[Event], policy: str, event: Event) -> None:
        """One subscriber's queue is full. Lose the event, or lose the subscriber."""
        if policy != DROP_SUBSCRIBER:
            # Keep the subscriber, keep the stream near-live: `AudioHub.publish`'s eviction, and
            # atomic for the same reason (no await between the get and the put, so no consumer can
            # interleave and the queue cannot be empty by the time we look).
            queue.get_nowait()
            queue.put_nowait(event)
            self._dropped_deliveries += 1
            return

        # Everything this subscriber has not read is about to be superseded by the snapshot it gets
        # on reconnect, and delivering a full backlog to a consumer too slow to keep up takes longer
        # than reconnecting does. So the backlog goes and the notice takes its place.
        self._subscribers.pop(queue, None)
        missed = queue.qsize() + 1  # the backlog it will never read, plus the one that did not fit
        while not queue.empty():
            queue.get_nowait()
        queue.put_nowait(overflow_event(missed, self._maxsize))
        self._dropped_subscribers += 1
        self._dropped_deliveries += missed
        # The third trace of one fact (ADR 0167): this journal line, the counters in `GET /status`,
        # and the notice the subscriber itself receives. The journal line is the durable one — the
        # counters reset with the process and the notice goes to a client that is about to leave.
        logger.warning(
            "event subscriber dropped: it fell %d events behind a %d-deep queue and was "
            "unsubscribed; a /events client reconnects and resyncs from the connect snapshot",
            missed,
            self._maxsize,
        )

    def stats(self) -> dict[str, int]:
        """The hub's own instrument, served into ``GET /status`` as the ``events`` block.

        Every name says what it COUNTS and not what it implies (the ADR 0170 ``requested`` rule);
        `docs/api.md` carries the documented meaning of a nonzero value for each. ``published`` is
        the denominator that makes the zeroes readable, the way ``WireStats.key_ups`` does.
        """
        return {
            "published": self._published,
            "subscribers": len(self._subscribers),
            "queue_maxsize": self._maxsize,
            "deepest_queue": self._deepest,
            "dropped_subscribers": self._dropped_subscribers,
            "dropped_deliveries": self._dropped_deliveries,
        }

    @property
    def subscriber_count(self) -> int:
        """Number of live subscribers (inspectable by tests)."""
        return len(self._subscribers)
