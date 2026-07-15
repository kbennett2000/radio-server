"""The inbound-link reader: read ``link.receive()`` and fan each frame to browser listeners (ADR 0043).

The network-audio mirror of :class:`~radio_server.rx.pump.RxPump`. Where that pump reads the *radio*
and fans received audio to ``/audio/rx``, this one reads the *network link* and fans it to
``/audio/link`` through a **second, independent** :class:`~radio_server.rx.hub.AudioHub` — two
producers into one hub would interleave frames, not mix them (ADR 0043), so the link gets its own hub.

It is deliberately thinner than ``RxPump``: **no arbiter, no activity gate, no recorder, no
controller, no ledger**. This path never touches the radio — it cannot key the transmitter and is not
blinded by a key-up — which is exactly why the listening direction is built first. Just two behaviours
sit in front of the hub:

- **The enable gate** — the load-bearing safety rule. While ``link.status().enabled`` is false the
  pump does not call ``receive()`` and publishes nothing; enabling (a runtime ``POST /link/enable``,
  ADR 0042) lets frames flow, disabling stops them on the next poll. A disabled pump never drains the
  backend, so queued inbound frames survive until enable — a clean gate on the stream, not a lossy
  shutter.
- **The idle / edge skip** — ``Link.receive()`` returns ``AudioFrame | StreamEdge | None`` (ADR 0047:
  ``None`` when the network is idle, a ``StreamEdge`` at a peer's stream boundary), unlike
  ``Radio.receive()`` which always returns a frame. So this loop publishes only ``AudioFrame`` frames —
  it drops ``None`` (idle) and ``StreamEdge`` boundaries (the listening tier needs no boundaries; only
  the transmit path, a later cycle, keys/unkeys on them). An idle or boundary poll publishes nothing and
  never raises. Format is the backend's concern (M17's Codec2→canonical resample happens before the
  frame reaches here); the pump publishes ``frame.samples`` verbatim.

Lifecycle is **demand-driven** and owned by the API, exactly like ``RxPump``: the reader runs while a
``/audio/link`` listener is connected. :meth:`start` is idempotent and :meth:`stop` joins the task;
``create_app`` reference-counts the listener demand.
"""

from __future__ import annotations

import asyncio

from ..audio import AudioFrame
from ..link import Link
from .hub import AudioHub

#: Inbound-link poll cadence (seconds) — how often the pump polls ``receive()``. Kept **> 0** so an
#: idle link (``receive()`` returning ``None``) does not hot-spin the event loop. On the mock
#: ``receive()`` returns instantly, so this only paces the idle loop; a real backend's cadence is
#: bounded by its network read (VERIFY AGAINST HARDWARE, guardrail 1).
DEFAULT_LINK_POLL = 0.02


class LinkPump:
    """Read ``link.receive()`` on a loop and publish each live frame's PCM to an :class:`AudioHub`.

    Gated by the link's runtime enable state: no frames flow while ``status().enabled`` is false.
    """

    def __init__(self, link: Link, hub: AudioHub, *, poll: float = DEFAULT_LINK_POLL) -> None:
        self._link = link
        self._hub = hub
        self._poll = poll
        self._running = False
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        """Whether the pump loop is active."""
        return self._running

    async def run(self) -> None:
        """Poll ``link.receive()`` and publish live frames until stopped, gated on ``enabled``."""
        self._running = True
        try:
            while self._running:
                # The enable gate (ADR 0041/0042): while disabled, read nothing and publish nothing,
                # so queued inbound frames survive until the link is deliberately enabled.
                if not self._link.status().enabled:
                    await asyncio.sleep(self._poll)
                    continue
                frame = self._link.receive()  # AudioFrame | StreamEdge | None (ADR 0047)
                # Publish only audio: skip None (idle) and StreamEdge boundaries — the listening tier
                # needs no stream edges; only the transmit path (a later cycle) acts on them.
                if isinstance(frame, AudioFrame) and frame.samples:
                    self._hub.publish(frame.samples)
                await asyncio.sleep(self._poll)
        finally:
            self._running = False

    def start(self) -> None:
        """Start the pump task if it is not already running (idempotent).

        Sets ``running`` synchronously (before the task first executes) so a caller — e.g. the
        demand-driven WS handler checking whether to start — sees the state immediately.
        """
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        """Stop the pump and join its task; safe to call when already stopped (idempotent)."""
        task = self._task
        if task is None:
            return
        # Clear state before awaiting the cancel so a concurrent reconnect starts a fresh task.
        self._task = None
        self._running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
