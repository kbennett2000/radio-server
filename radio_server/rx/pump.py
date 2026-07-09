"""The RX pump: read ``radio.receive()`` and fan each live frame out to the audio hub (ADR 0014).

Mirrors the shape of :class:`radio_server.controller.engine.ControllerRunner` — a thin async loop
over a synchronous ``receive()`` on a poll cadence — but owns its own task lifecycle because it is
**demand-driven**: the ``/audio/rx`` WebSocket handler calls :meth:`RxPump.start` when the first
listener connects and :meth:`RxPump.stop` when the last disconnects. It is independent of the
controller loop; the eventual single-capture-reader consolidation (one ``receive()`` feeding both
``controller.step`` and this pump) is a hardware-bring-up decision, not made here.

Two orthogonal filters sit in front of the hub:

- **The activity gate** (:data:`RxActivityGate`) — an injectable ``(AudioFrame) -> bool`` predicate
  deciding whether a frame is "live" and worth relaying. The default passes everything through;
  real software squelch / VAD is a later cycle. This is the seam, not the detector.
- **The empty-frame skip** — a transport sanity rule (distinct from the gate): a 0-byte frame
  carries no audio, so it is never put on the wire. This is what lets an unscripted ``MockRadio``
  (whose ``canned_rx`` defaults to an empty frame) produce no traffic.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from ..arbiter import RadioArbiter
from ..backends import AudioFrame, Radio
from .hub import AudioHub

#: RX chunk cadence (seconds) — how often the pump polls ``receive()``. Kept **> 0** so a silent
#: radio (empties skipped) does not hot-spin the event loop. The real cadence is bounded by how
#: long ``receive()`` blocks and the audio chunk size — hardware facts (VERIFY AGAINST HARDWARE,
#: guardrail 1). On the mock ``receive()`` returns instantly, so this only paces the idle loop.
DEFAULT_RX_POLL = 0.02


class RxActivityGate(Protocol):
    """Predicate deciding whether an RX frame is live enough to relay.

    Injected into :class:`RxPump`; the default :data:`pass_through_gate` returns ``True`` for every
    frame. Real squelch/VAD implements this same shape in a later cycle without touching the pump.
    """

    def __call__(self, frame: AudioFrame) -> bool: ...


def pass_through_gate(frame: AudioFrame) -> bool:
    """The default gate: relay every frame (no squelch)."""
    return True


class RxPump:
    """Reads ``radio.receive()`` in a loop and publishes each live frame's PCM to the hub.

    Owns a single asyncio task. :meth:`start` is idempotent (creates the task only when none is
    running); :meth:`stop` clears its task reference **before** awaiting the cancel, so a listener
    that reconnects during a teardown starts a fresh pump rather than observing a dying one.
    """

    def __init__(
        self,
        radio: Radio,
        hub: AudioHub,
        *,
        gate: RxActivityGate = pass_through_gate,
        poll: float = DEFAULT_RX_POLL,
        arbiter: RadioArbiter | None = None,
    ) -> None:
        self._radio = radio
        self._hub = hub
        self._gate = gate
        self._poll = poll
        # The shared half-duplex arbiter (ADR 0017): while TX holds the radio the pump must not
        # pull `receive()` (keying blinds the receiver). A private idle arbiter is the safe default
        # — `transmitting` is always False, so an un-injected pump behaves exactly as before.
        self._arbiter = arbiter if arbiter is not None else RadioArbiter()
        self._running = False
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._running

    async def run(self) -> None:
        """Pump received audio to the hub until :meth:`stop` cancels the task.

        Guardrail 1: on real hardware ``receive()`` blocks for the chunk duration; whether to run
        it in a thread executor rather than directly in the event loop is a bring-up decision. The
        mock returns instantly, so this loop is a faithful software stand-in.
        """
        self._running = True
        self._arbiter.begin_receive()
        try:
            while self._running:
                if self._arbiter.transmitting:
                    # Half-duplex (ADR 0017): TX owns the radio. Do NOT pull `receive()` while keyed
                    # — keying blinds the receiver. Listeners stay subscribed (their sockets are
                    # untouched); frame delivery just pauses here and resumes when TX drops.
                    await asyncio.sleep(self._poll)
                    continue
                frame = self._radio.receive()
                # Empty frames carry no audio (transport skip); the gate decides the rest.
                if frame.samples and self._gate(frame):
                    self._hub.publish(frame.samples)
                await asyncio.sleep(self._poll)
        finally:
            self._running = False
            self._arbiter.end_receive()

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
