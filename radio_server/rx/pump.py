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
import concurrent.futures
import logging
import time
from typing import TYPE_CHECKING, Callable, Protocol

from ..arbiter import RadioArbiter
from ..backends import AudioFrame, Radio
from ..shutdown import join_bounded
from .hub import AudioHub

if TYPE_CHECKING:
    from ..controller import Controller

logger = logging.getLogger(__name__)

#: A wall clock (Unix seconds) — the timestamp handed to ``controller.step``. Injectable for tests.
Clock = Callable[[], float]

#: RX chunk cadence (seconds) — how often the pump polls ``receive()``. Kept **> 0** so a silent
#: radio (empties skipped) does not hot-spin the event loop. The real cadence is bounded by how
#: long ``receive()`` blocks and the audio chunk size — hardware facts (VERIFY AGAINST HARDWARE,
#: guardrail 1). On the mock ``receive()`` returns instantly, so this only paces the idle loop.
DEFAULT_RX_POLL = 0.02

#: Seconds :meth:`RxPump.stop` waits for an in-flight capture read to return before abandoning it
#: (ADR 0183). **DERIVED**: a capture block is ``DEFAULT_BLOCKSIZE / CANONICAL_RATE`` = 960/48000 =
#: **20 ms**, so a healthy read always returns inside a small multiple of it; this is ~12 of them.
#:
#: It has to be long enough that the *normal* case never abandons, because an abandoned reader is
#: still inside PortAudio when the backend's ``close()`` frees the stream — the use-after-free the
#: kernel logged six times as ``rx-read_0`` faulting in ``libasound``/``libportaudio``/``libc``.
#: And short enough to keep ADR 0104's promise that shutdown latency never depends on a blocking
#: call cooperating: 0.25 s is invisible against uvicorn's 5 s graceful window and the unit's
#: ``TimeoutStopSec=20``, so a genuinely wedged read is still abandoned rather than waited on.
READER_JOIN_TIMEOUT_S = 0.25


class RxActivityGate(Protocol):
    """Predicate deciding whether an RX frame is live enough to relay.

    Injected into :class:`RxPump`; the default :data:`pass_through_gate` returns ``True`` for every
    frame. Real squelch/VAD implements this same shape in a later cycle without touching the pump.
    """

    def __call__(self, frame: AudioFrame) -> bool: ...


class _PassThroughGate:
    """The default gate: relay every frame (no squelch).

    ``detects_signal = False`` marks that this gate carries **no signal knowledge** — it opens on
    every frame, so "gate open" means nothing about the channel being busy. The pump therefore
    never asserts :attr:`RxPump.active` under this gate; consumers that defer to a live RF signal
    (the Mumble bridge, ADR 0041/0045) must not treat a squelch-less channel as busy.
    """

    detects_signal = False

    def __call__(self, frame: AudioFrame) -> bool:
        return True


#: The shared default gate instance — stays a plain callable, so existing imports and injections
#: (`gate=pass_through_gate`) keep working unchanged.
pass_through_gate = _PassThroughGate()


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
        on_activity: Callable[[bool], None] | None = None,
    ) -> None:
        self._radio = radio
        self._hub = hub
        self._gate = gate
        # Whether the gate's "open" decision means "a real signal is present". Pass-through gates
        # (squelch off) open on every frame, so their decisions carry no signal knowledge and the
        # pump must never report the channel as active off them (ADR 0045). Absent attribute (an
        # injected lambda, e.g. in tests) is treated as signal-aware — the pre-0045 behavior.
        self._signal_aware = bool(getattr(gate, "detects_signal", True))
        self._poll = poll
        # RX activity edges (squelch open/close) for the operating log: fired only when the gate
        # decision flips, mirroring the recorder's segment edges. `True` = the gate opened on a live
        # frame; `False` = it closed (a rejected frame, a TX pause, or the pump stopping). None on
        # apps that don't surface it. Guarded like the recorder calls so a subscriber fault can never
        # kill the shared capture task.
        self._on_activity = on_activity
        self._active = False
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
        # The pump's own capture-reader thread, created in `start()` and released in `stop()`.
        self._reader: concurrent.futures.ThreadPoolExecutor | None = None
        # The capture read currently on that thread, so `stop()` can wait for it to come back
        # before the backend closes the stream underneath it (ADR 0183). Deliberately NOT cleared
        # when a read completes: each read overwrites it, and waiting on an already-finished future
        # is free — whereas clearing it in a `finally` would lose the handle at the one moment it
        # matters, because cancelling the task unwinds that `finally` while the thread runs on.
        self._inflight: concurrent.futures.Future[AudioFrame] | None = None

    @property
    def running(self) -> bool:
        return self._running

    @property
    def active(self) -> bool:
        """Whether the activity gate is currently open — a live RF signal is being relayed.

        The queryable form of the ``on_activity`` edge callback, so a consumer (the Mumble bridge,
        ADR 0041) can defer keying Mumble→RF while a real signal is coming in over the air.

        Always ``False`` under a pass-through gate (``squelch = "off"``): no squelch means no
        signal knowledge, so consumers must not treat the channel as busy (ADR 0045).
        """
        return self._active

    def _set_active(self, active: bool) -> None:
        """Fire the activity callback only on an edge (open↔close), then remember the new state."""
        if active == self._active:
            return
        self._active = active
        if self._on_activity is not None:
            try:
                self._on_activity(active)
            except Exception:
                pass

    async def run(self) -> None:
        """Pump received audio to the hub until :meth:`stop` cancels the task.

        Guardrail 1 said "on real hardware ``receive()`` blocks for the chunk duration; whether to
        run it in a thread executor rather than directly in the event loop is a bring-up decision."
        **The bench decided it: it runs in a thread** (ADR 0130). A ``py-spy`` dump of the deployed
        server caught the event loop parked in ``sounddevice._raw_read`` via this line, and the
        card overran 33 times in 40 minutes — the exact "the RX reader fell behind the card"
        warning the uvk5 backend logs. Calling a blocking capture read directly on the loop means
        every unrelated thing the loop does (a WebSocket frame, a TTS synth, an HTTP request)
        delays the next read, and the card does not wait.
        """
        self._running = True
        self._arbiter.begin_receive()
        # Some gates need a background lifecycle: the CAT squelch's busy read is a ~100 ms serial
        # round-trip that must NOT run on this per-frame reader (ADR 0125), so `PolledGate` moves it
        # to a background thread and exposes start()/stop(). Bracket it to the pump's demand-driven
        # lifetime — the poller runs exactly while the pump runs. Gates without the hooks
        # (pass_through, AudioLevelGate) are untouched. Guarded: a gate lifecycle fault must never
        # keep the shared capture task from running.
        self._start_gate()
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
                    # A keyed gap is a channel-clear edge for the log too (we can't hear RX now).
                    self._set_active(False)
                    await asyncio.sleep(self._poll)
                    continue
                # Off the event loop, on the pump's own reader thread (ADR 0130): a backend
                # `receive()` blocks for the chunk duration on real hardware. The awaits below are
                # sequential, so exactly one read is ever in flight — the backend still sees a
                # single reader, which is the invariant the ALSA/serial capture paths are built on.
                frame = await self._read_frame()
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
                        # Gate-open edge → an "rx active" event for the log (fired only on the flip).
                        # Skipped for signal-blind (pass-through) gates: every frame "opens" them,
                        # so asserting active would latch permanently and wrongly report a busy
                        # channel (ADR 0045).
                        if self._signal_aware:
                            self._set_active(True)
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
                        # recording segment and fire the channel-clear event. Both idempotent, so
                        # repeated closed frames are cheap.
                        self._set_active(False)
                        try:
                            self._recorder.end_segment()
                        except Exception:
                            pass
                # Pace the loop ONLY when the read returned nothing (ADR 0125). On hardware a busy
                # reader must consume the ALSA ring at the card's rate; the read already blocked for
                # the frame, so an unconditional `sleep(self._poll)` after a *successful* read spent
                # 50×20 ms = 1 s of every second sleeping — zero headroom, so the ring backfilled and
                # overran. A real frame yields to the event loop with NO delay (`sleep(0)`), keeping
                # the WS writers / `/events` fair without pacing the reader; the poll delay stays as
                # the idle guard the docstring always described (a silent mock's instant receive()
                # must not hot-spin).
                if frame.samples:
                    await asyncio.sleep(0)
                else:
                    await asyncio.sleep(self._poll)
        finally:
            self._running = False
            # Finalize any open recording segment when the demand-driven pump stops, and clear a
            # lingering "rx active" state so the log doesn't show the channel open after the reader dies.
            try:
                self._recorder.end_segment()
            except Exception:
                pass
            self._set_active(False)
            # Stop the gate's background poller (if any) — symmetric with `_start_gate` above, so a
            # torn-down pump leaves no CAT-poll thread reading a radio that may be about to close.
            self._stop_gate()
            self._arbiter.end_receive()

    async def _read_frame(self) -> AudioFrame:
        """One capture read on the pump's own reader thread, keeping the in-flight future.

        Submitted rather than handed to ``run_in_executor`` for one reason: ``run_in_executor``
        returns only the *asyncio* wrapper, and cancelling that does nothing to the worker — the
        underlying ``concurrent.futures.Future`` is already RUNNING, so its ``cancel()`` fails and
        the thread reads on. Holding the concurrent future is what lets :meth:`stop` know whether a
        read is still in the driver (ADR 0183).
        """
        executor = self._reader
        if executor is None:
            # `run()` driven directly, without `start()` (several tests do this). No dedicated
            # reader thread exists, so there is nothing for teardown to abandon either.
            return await asyncio.get_running_loop().run_in_executor(None, self._radio.receive)
        future = executor.submit(self._radio.receive)
        self._inflight = future
        return await asyncio.wrap_future(future)

    def _start_gate(self) -> None:
        """Start the gate's background lifecycle if it has one (``PolledGate``); a no-op otherwise."""
        start = getattr(self._gate, "start", None)
        if callable(start):
            try:
                start()
            except Exception:
                # Still swallowed — a gate that will not start must not stop the pump receiving.
                # But it is no longer silent (ADR 0178): a `PolledGate` whose thread never spawned
                # answers "not busy" for every frame, so the pump drops everything and the station
                # looks exactly like a quiet band. This was the only place that could ever know.
                logger.exception("rx pump: the gate's poller failed to start; RX will read as quiet")

    def _stop_gate(self) -> None:
        """Stop the gate's background lifecycle if it has one; idempotent and guarded."""
        stop = getattr(self._gate, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception:
                pass

    def start(self) -> None:
        """Start the pump task if it is not already running (idempotent).

        Sets ``running`` synchronously (before the task first executes) so a caller — e.g. the
        demand-driven WS handler checking whether to start — sees the state immediately.
        """
        if self._task is not None:
            return
        self._running = True
        # A dedicated single-worker executor, not the loop's default pool (ADR 0130). The default
        # pool is shared with everything else that calls `to_thread` — the D-STAR/DVAP status
        # refreshes the web UI polls, for instance — and a capture read that queues behind one of
        # those is a dropped block, because the card does not wait. One worker also means the same
        # thread every time: no per-frame thread churn, and the backend still sees a single reader.
        self._reader = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="rx-read"
        )
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
        # Bounded join (ADR 0104): the pump task can be parked in a blocking backend receive() (the
        # ADR 0029 known limitation), where an unbounded `await task` would hang shutdown until
        # SIGKILL. Bound it and abandon a still-parked task instead — the stop budget must hold.
        # `asyncio.wait`, not `wait_for` (ADR 0184): `wait_for` cancels the task on expiry and then
        # waits for that cancel to be delivered, which a task parked in a blocking call cannot do —
        # so the guard blocked for as long as the thing it was guarding. Measured, and the reason
        # `test_shutdown_budget.py` never caught it is in that ADR.
        await join_bounded([task], 2.0)
        # Wait — briefly — for a capture read that is still in the driver, BEFORE the caller goes on
        # to close the radio (ADR 0183). `holder.stop()` calls `radio.close()` three lines after
        # this returns, and that closes the PortAudio stream: `Pa_CloseStream` -> `snd_pcm_close`
        # frees the very object a parked read is dereferencing. The kernel logged the result six
        # times as `rx-read_0` segfaulting in `libasound`/`libportaudio`/`libc` — three libraries,
        # one thread, which is a use-after-free rather than one bad pointer.
        #
        # Cancelling the task above does NOT stop the worker (see `_read_frame`), so without this
        # the reader was abandoned mid-read on EVERY stop, not just a wedged one: measured at
        # 0.06 ms to return with the thread still inside `receive()`. The TX side already got this
        # right — `SoundCardTxPacer.stop()` joins its writer before the stream is closed, and says
        # why in as many words. This is the same rule, applied to the reader.
        #
        # Still BOUNDED, so ADR 0104's promise holds: a read that never returns is abandoned as
        # before rather than holding shutdown open.
        # Awaited, NOT `concurrent.futures.wait` — that would block the event loop for the whole
        # bound, and a 0.25 s synchronous stall on the loop is exactly what ADR 0181 and ADR 0182
        # spent two cycles removing. `stop()` is also reachable from `holder.rebuild` on a live
        # server, so this must yield. On timeout the wrapper is cancelled and the worker is
        # abandoned, which is the pre-0183 behaviour and ADR 0104's escape.
        inflight, self._inflight = self._inflight, None
        if inflight is not None and not inflight.done():
            try:
                await asyncio.wait_for(
                    asyncio.wrap_future(inflight), timeout=READER_JOIN_TIMEOUT_S
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception:
                # The read itself raised (a dying card). Not our problem here — the point is only
                # that the thread is no longer inside the driver when `close()` frees the stream.
                pass
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.shutdown(wait=False)
