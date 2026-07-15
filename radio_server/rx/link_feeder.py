"""The outbound-link feeder: fan received audio out to the network link (ADR 0044).

The second audio direction — ``radio.receive()`` → ``link.transmit()``, "the world hears your radio."
Where :class:`~radio_server.rx.link_pump.LinkPump` reads the network and fans it to browsers, this
feeder reads the **RX hub** and fans it to the **network link**. It is neither a pump nor a hub — it
reuses both (guardrail: *no new pump, no new hub*):

- **A subscriber of the existing** :class:`~radio_server.rx.hub.AudioHub`. The pump publishes to that
  hub **only while the RX activity gate is open** (``RxPump`` gates ``hub.publish``), so a plain
  subscriber already receives gate-open frames only — "feed only while the gate is OPEN" for the
  payload is inherited, not re-derived.
- **A demand source on the** :class:`~radio_server.rx.pump.RxPump`. It reference-counts RX demand via
  the injected ``acquire``/``release`` (``create_app``'s ``_acquire_rx``/``_release_rx``), so enabling
  the link starts the shared reader even when nobody is browsing.

**Stream boundaries (the load-bearing subtlety).** A gate open/close is an M17 *stream* boundary — an
LSF at the start, an EOT at the end — expressed via :meth:`~radio_server.link.base.Link.stream`, the
network mirror of ``Radio.ptt``. Those edges come from the pump's ``on_activity(active)`` callback,
fanned in here as :meth:`note_activity`. But ``on_activity`` is **synchronous** in the pump loop while
frame delivery is **async** via the hub queue: emitting ``stream(False)`` straight from the callback
could beat frames still sitting in the queue (an EOT before the last audio). So a boundary is pushed as
a **sentinel into the feeder's own subscriber queue** — the same queue ``hub.publish`` feeds. Because
``on_activity`` and ``hub.publish`` are both synchronous within one pump iteration (the open edge fires
*before* the frame is published, the close edge fires with no frame after it), the sentinel lands in
strict order relative to the frames, and the single consumer drains them in that order. No frame-gap
inference anywhere.

Deliberately thin: no arbiter (the pump already stands down under TX — ADR 0017 — so a local key-up
looks like a gate-close here and the feed pauses/resumes for free), no recorder, no ledger, no gate.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from ..audio import AudioFrame
from ..link import Link
from .hub import AudioHub

# Per-feeder queue sentinels for the stream boundaries, distinct from the ``bytes`` frames the hub
# publishes. Identity-compared (``is``) in the consumer, so plain unique objects suffice.
_START = object()
_END = object()


class LinkFeeder:
    """Forward gate-open RX frames to ``link.transmit()``, bracketed by ``link.stream(on)`` edges.

    Owns a single consumer task over its hub-subscriber queue. :meth:`start`/:meth:`stop` are
    idempotent and mirror the pump lifecycle; :meth:`note_activity` is the ``on_activity`` fan-in.
    """

    def __init__(
        self,
        link: Link,
        hub: AudioHub,
        *,
        acquire: Callable[[], Awaitable[None]],
        release: Callable[[], Awaitable[None]],
    ) -> None:
        self._link = link
        self._hub = hub
        self._acquire = acquire
        self._release = release
        self._queue: asyncio.Queue[object] | None = None
        self._task: asyncio.Task[None] | None = None
        self._streaming = False

    @property
    def running(self) -> bool:
        """Whether the feeder's consumer task is active."""
        return self._task is not None

    def note_activity(self, active: bool) -> None:
        """Fan in a gate edge from the pump's ``on_activity`` — the stream boundary.

        Pushes a boundary sentinel into the SAME subscriber queue the hub publishes frames to, so it
        is ordered exactly against the frames (open edge before the first frame, close edge after the
        last). A no-op when the feeder is not started (no queue). Synchronous — safe to call from the
        pump loop.
        """
        if self._queue is None:
            return
        self._put(_START if active else _END)

    def _put(self, item: object) -> None:
        # Mirror the hub's drop-oldest policy so a boundary never blocks and never raises on a full
        # queue. Boundaries are rare (once per span), so eviction here is a backpressure edge case.
        queue = self._queue
        if queue is None:
            return
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            queue.get_nowait()  # evict the oldest, matching AudioHub.publish
            queue.put_nowait(item)

    async def _run(self) -> None:
        """Drain the queue, transmitting frames bracketed by stream open/close, until cancelled."""
        assert self._queue is not None
        while True:
            item = await self._queue.get()
            if item is _END:
                self._end_stream()
            elif item is _START:
                self._begin_stream()
            else:  # a bytes PCM frame from the hub
                # Lazy-open guard: covers a mid-span subscribe where the START edge was missed, and is
                # idempotent when START already opened the stream.
                self._begin_stream()
                self._link.transmit(AudioFrame(item))  # type: ignore[arg-type]

    def _begin_stream(self) -> None:
        if not self._streaming:
            self._link.stream(True)  # LSF
            self._streaming = True

    def _end_stream(self) -> None:
        if self._streaming:
            self._link.stream(False)  # EOT
            self._streaming = False

    async def start(self) -> None:
        """Subscribe to the RX hub, take RX demand, and start the consumer task (idempotent)."""
        if self._task is not None:
            return
        # Subscribe BEFORE acquiring demand so the reader can never publish a frame we miss.
        self._queue = self._hub.subscribe()
        await self._acquire()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop the consumer, send a final EOT if a stream was open, then drop demand (idempotent)."""
        task = self._task
        if task is None:
            return
        self._task = None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # A clean shutdown ends any open stream (disable mid-transmission → EOT). The pump's own
        # teardown edge can't reach us — we unsubscribe below — so close it here.
        self._end_stream()
        if self._queue is not None:
            self._hub.unsubscribe(self._queue)
            self._queue = None
        await self._release()
