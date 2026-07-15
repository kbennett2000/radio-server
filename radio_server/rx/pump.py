"""The RX reader: read ``radio.receive()`` once and fan each frame out to every consumer (ADR 0031).

This is the **single capture reader** the earlier cycles deferred. On real hardware the sound-card
capture is single-open and single-reader: each received block is consumed exactly once by whoever
calls ``receive()`` first. Two independent readers (this pump for browser audio, plus the old
``ControllerRunner`` for DTMF) therefore stole blocks from each other AND the controller's 0.5 s poll
sampled only ~4 % of the audio — so a keyed DTMF tone never reached the decoder as one contiguous
span. This loop fixes both: it reads ``receive()`` **back-to-back** (paced by the blocking read on
hardware; the small ``poll`` only keeps the mock's instant ``receive()`` from hot-spinning) and hands
each frame to, in order:

1. **the DTMF controller** (``controller.step``) — the **raw** frame, so decode sees contiguous audio
   exactly like ``doctor --dtmf`` (independent of the browser squelch gate);
2. **the browser audio hub + recorder** — behind the activity gate, unchanged.

Lifecycle is **demand-driven** and owned by the API: the reader runs while a ``/audio/rx`` listener is
connected OR the controller is active (``POST /controller {on:true}``). :meth:`start` is idempotent and
:meth:`stop` joins the task; ``create_app`` reference-counts those two demands.

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
import time
from typing import TYPE_CHECKING, Callable, Protocol

from ..arbiter import RadioArbiter
from ..backends import AudioFrame, Radio
from .hub import AudioHub

if TYPE_CHECKING:
    from ..controller import Controller

#: A wall clock (Unix seconds) — the timestamp handed to ``controller.step``. Injectable for tests.
Clock = Callable[[], float]

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


class RxRecorder(Protocol):
    """A passive sink for received audio — the recorder seam (ADR 0020).

    The pump calls :meth:`write` for each live (gate-open) frame and :meth:`end_segment` at the
    gate-close edge, so a gate-open → gate-close span is one recording segment. The default
    :data:`null_recorder` does nothing; the concrete :class:`radio_server.recording.Recorder`
    implements this shape without the pump importing it (the arrow stays ``rx -> {audio, backends}``).
    """

    def write(self, pcm: bytes) -> None: ...

    def end_segment(self) -> None: ...


class _NullRecorder:
    """The no-op default recorder: records nothing (recording is opt-in)."""

    def write(self, pcm: bytes) -> None: ...

    def end_segment(self) -> None: ...


#: The default recorder — a shared no-op, so an un-injected pump behaves exactly as before.
null_recorder = _NullRecorder()


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
        recorder: RxRecorder = null_recorder,
        controller: Controller | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._radio = radio
        self._hub = hub
        self._gate = gate
        self._poll = poll
        # The live DTMF controller (ADR 0031): when set, every raw received frame is fed to
        # `controller.step(now, frame)` here, so DTMF decode sees one contiguous capture instead of a
        # separate, under-sampled `receive()` loop. None on the mock/no-secret app (no stepping).
        self._controller = controller
        self._clock = clock if clock is not None else time.time
        # The audio recorder (ADR 0020): a passive sink for the same gate-open frames the hub
        # streams. Off by default (`null_recorder`); `build_app` injects a real one when
        # `RADIO_RECORD` is on. Its writes are guarded here (see `run`) so a disk fault can never
        # break the pump — the single shared capture task whose death would blind every listener.
        self._recorder = recorder
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
                    # Finalize any open RX segment at the keyed gap (ADR 0021): a recording must
                    # reflect one continuous receive, not concatenate across a TX pause. Idempotent,
                    # so calling it every transmitting iteration is a no-op after the first; the next
                    # live frame on resume lazy-opens a fresh file. Guarded — a disk fault here must
                    # never kill the shared capture task.
                    try:
                        self._recorder.end_segment()
                    except Exception:
                        pass
                    await asyncio.sleep(self._poll)
                    continue
                frame = self._radio.receive()
                # Drive the live DTMF controller FIRST, on the RAW frame (ADR 0031): decode must see
                # the full contiguous capture, independent of the browser squelch gate below — the
                # same raw audio `doctor --dtmf` decodes. `step` is pure/synchronous and swallows its
                # own faults via the event callback, but guard it anyway so a controller hiccup can
                # never kill the shared capture task that also feeds every listener.
                if self._controller is not None:
                    try:
                        self._controller.step(self._clock(), frame)
                    except Exception:
                        pass
                # Empty frames carry no audio (transport skip); the gate decides the rest.
                if frame.samples:
                    if self._gate(frame):
                        # Publish to the hub FIRST so recording can never add latency to the live
                        # stream, then record. Both recorder calls are guarded: a disk fault must
                        # never kill the pump (the shared capture task).
                        self._hub.publish(frame.samples)
                        try:
                            self._recorder.write(frame.samples)
                        except Exception:
                            pass
                    else:
                        # A non-empty frame the gate rejects is the gate-close edge: end the
                        # recording segment. Idempotent, so repeated closed frames are cheap.
                        try:
                            self._recorder.end_segment()
                        except Exception:
                            pass
                await asyncio.sleep(self._poll)
        finally:
            self._running = False
            # Finalize any open recording segment when the demand-driven pump stops.
            try:
                self._recorder.end_segment()
            except Exception:
                pass
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
