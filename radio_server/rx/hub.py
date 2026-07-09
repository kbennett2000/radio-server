"""The RX audio fan-out: a bounded, drop-oldest binary hub (ADR 0014).

This is the audio-stream sibling of :class:`radio_server.api.events.EventHub`. Both fan one
producer out to every connected WebSocket; the two differences are what the audio path forces:

- **Binary, not JSON.** A subscriber's queue carries raw canonical-PCM ``bytes`` (the wire form
  sent via ``websocket.send_bytes``), not :class:`~radio_server.api.events.Event` objects.
- **Bounded + drop-oldest, not unbounded.** ``EventHub`` uses unbounded queues — correct for
  low-rate control events. A continuous ~96 KB/s audio stream to a slow consumer would grow such
  a queue without limit, so each subscriber here gets a **bounded** queue and :meth:`publish`
  **drops the oldest frame** when it is full. Dropping the oldest (rather than the newest) keeps
  the live stream near-real-time: a slow listener hears a glitch, not ever-growing latency. A
  slow or stuck listener therefore drops frames without blocking the pump or any other listener.
"""

from __future__ import annotations

import asyncio

#: Per-subscriber queue depth (frames). Bounds a slow/stuck listener's backlog; on overflow the
#: oldest frame is dropped so the stream stays near-live. Sized for a small live buffer — a few
#: hundred ms at the real chunk cadence — but that cadence is a hardware fact (VERIFY AGAINST
#: HARDWARE, guardrail 1); on the mock, frames are scripted so depth only bounds the drop test.
DEFAULT_AUDIO_QUEUE_MAXSIZE = 64


class AudioHub:
    """A minimal in-process fan-out of binary audio frames to WebSocket subscribers.

    Each subscriber gets its own bounded queue via :meth:`subscribe`; :meth:`publish` puts one
    frame onto every live subscriber's queue, dropping that subscriber's oldest frame if its
    queue is full. One hub per app, shared by all ``/audio/rx`` connections and fed by a single
    :class:`~radio_server.rx.pump.RxPump`. ``publish`` is synchronous and non-blocking, so the
    pump never awaits a socket.
    """

    def __init__(self, maxsize: int = DEFAULT_AUDIO_QUEUE_MAXSIZE) -> None:
        self._maxsize = maxsize
        self._subscribers: set[asyncio.Queue[bytes]] = set()

    def subscribe(self) -> asyncio.Queue[bytes]:
        """Register a new subscriber and return its bounded frame queue."""
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[bytes]) -> None:
        """Drop a subscriber (idempotent) — call on disconnect."""
        self._subscribers.discard(queue)

    def publish(self, frame: bytes) -> None:
        """Fan one PCM ``frame`` out to every live subscriber, drop-oldest on a full queue.

        Non-blocking and synchronous. On a full queue the get_nowait/put_nowait pair is atomic
        in single-threaded asyncio (no await between them), so no consumer can interleave and
        ``get_nowait`` cannot see an empty queue — the queue is full by construction.
        """
        for queue in self._subscribers:
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                queue.get_nowait()  # evict the oldest, keep the stream near-live
                queue.put_nowait(frame)

    @property
    def subscriber_count(self) -> int:
        """Number of live subscribers (drives the pump's demand-driven lifecycle and tests)."""
        return len(self._subscribers)
