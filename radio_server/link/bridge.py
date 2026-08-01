"""The RF <-> Mumble bridge: the peer state machine (ADR 0041).

This wires a :class:`~radio_server.link.client.MumbleClient` to the RF radio using the seams that
already exist — it invents no new RX or TX mechanism:

- **RF -> Mumble.** Subscribe to the shared :class:`~radio_server.rx.hub.AudioHub` exactly like a
  browser ``/audio/rx`` listener; a loop task forwards each frame to ``mumble.send_audio``. The hub
  only ever carries gate-open, non-empty PCM, so the bridge relays squelched audio, and it goes
  quiet while the radio transmits (the pump skips ``receive()`` under the arbiter). The bridge holds
  an rx *demand* (``acquire_rx``/``release_rx``) so the shared pump runs even with no browser
  connected. When a :class:`~radio_server.link.tone_detect.DtmfToneDetector` +
  :class:`~radio_server.link.mute.DtmfMuteGate` are injected (ADR 0049), each frame is checked for
  DTMF tone energy in real time and dropped from the feed — control tones never reach Mumble
  listeners (superseding ADR 0045's decode-latency-bound delay line).
- **Mumble -> RF** (only when ``tx_to_rf``). ``mumble.on_audio`` is fired by the client's own network
  thread, so the sink hands PCM across the thread boundary onto the event loop
  (``loop.call_soon_threadsafe``) into a bounded, drop-oldest queue (the ``MultimonStream`` posture,
  ADR 0040) — a slow link drops audio, never blocks the loop. A drain task keys the radio through a
  :class:`~radio_server.tx.session.TxSession` (sharing the single :class:`~radio_server.tx.session.TxSlot`
  with the browser talker, so two TX sources can never race the arbiter) and, crucially, the same
  :class:`~radio_server.services.station_id.StreamingId`, so bridged TX is auto-identified (Part 97,
  guardrail 5). A Mumble talker sends voice only while talking; when it stops, a hang timeout drops
  PTT and frees the slot — the ``/audio/tx`` idle-timeout lifecycle.

Doubling is inherent to bridging a full-duplex conference onto a half-duplex radio (ADR 0041): while
the browser holds the slot inbound Mumble audio is dropped, and while a live RF signal is being
received the bridge defers keying (``rx_active``).

The web UI also uses the bridge as a **Mumble client** (ADR 0050): received frames are published to a
fanned-out ``mumble_rx_hub`` (in addition to the RF drain) for the browser to monitor, and the
operator's mic goes out via :meth:`send_operator_audio` on the one shared connection — which arms an
operator-talk yield so the RF→Mumble relay steps aside. That path keys no RF.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from ..activity.broadcast_fm_poll import cadence_stats, start_cadence, stop_cadence
from ..audio import AudioFormatMismatch
from ..backends import Radio, RadioUnavailable
from ..backends.base import BroadcastFm, relay_mute_reason
from ..tx import TxIdentifier, TxSession, TxSlot
from .client import DEFAULT_MUMBLE_TX_HANG, MumbleClient, MumbleStatus
from .mute import DtmfMuteGate
from .tone_detect import DtmfToneDetector

log = logging.getLogger(__name__)

#: A clock returns seconds as a float (``time.monotonic`` by default) — injectable so the hang timer
#: and station-ID due-checks are exactly testable with a fake clock.
Clock = Callable[[], float]

#: Bounded hand-off queue depth for received Mumble voice (~1.3 s at 20 ms framing) before
#: drop-oldest, mirroring the audio hub / multimon write queue.
DEFAULT_TX_QUEUE_MAXSIZE = 64

#: How long one operator-mic frame keeps the RF→Mumble relay yielded (ADR 0050), refreshed per frame.
#: Long enough to bridge the gaps between the browser's 20 ms frames without chopping the relay back
#: in mid-phrase; a marked, tunable default (guardrail 1).
DEFAULT_OPERATOR_TALK_HOLD = 0.5

class MumbleBridge:
    """Bridge RF audio to/from a Mumble channel (ADR 0041). A pure DI object (Settings-free).

    Start/stop are idempotent (mirroring :class:`~radio_server.rx.pump.RxPump`). ``start`` connects
    the client, subscribes to the audio hub, raises an rx demand, and launches the loop task(s);
    ``stop`` cancels them, releases the demand + slot, and disconnects.
    """

    def __init__(
        self,
        mumble: MumbleClient,
        radio: Radio,
        *,
        arbiter,
        tx_slot: TxSlot,
        audio_hub,
        acquire_rx: Callable[[], Awaitable[None]] | None = None,
        release_rx: Callable[[], Awaitable[None]] | None = None,
        station_id: TxIdentifier | None = None,
        tx_to_rf: bool = True,
        tx_hang: float = DEFAULT_MUMBLE_TX_HANG,
        clock: Clock | None = None,
        rx_active: Callable[[], bool] | None = None,
        tx_queue_maxsize: int = DEFAULT_TX_QUEUE_MAXSIZE,
        dtmf_mute: DtmfMuteGate | None = None,
        tone_detector: DtmfToneDetector | None = None,
        mumble_rx_hub=None,
        operator_talk_hold: float = DEFAULT_OPERATOR_TALK_HOLD,
        rx_guard: DtmfMuteGate | None = None,
        broadcast_fm: Callable[[], BroadcastFm | None] | None = None,
    ) -> None:
        if clock is None:
            import time

            clock = time.monotonic
        self._mumble = mumble
        self._radio = radio
        self._arbiter = arbiter
        self._tx_slot = tx_slot
        self._audio_hub = audio_hub
        self._acquire_rx = acquire_rx
        self._release_rx = release_rx
        self._station_id = station_id
        self._tx_to_rf = tx_to_rf
        self._tx_hang = tx_hang
        self._clock = clock
        self._rx_active = rx_active
        self._tx_queue_maxsize = tx_queue_maxsize
        # Mumble → browser fan-out (ADR 0050): received Mumble voice is published here so the web UI
        # can monitor the channel as a client, in addition to the RF drain. App-scoped and shared
        # across reconnects (the bridge only publishes; it never owns the hub's lifetime). None keeps
        # the pre-0050 behavior (Mumble audio goes only to RF).
        self._mumble_rx_hub = mumble_rx_hub
        # Operator-talk yield (ADR 0050): the web operator's mic (via `send_operator_audio`) arms this
        # gate, and the RF→Mumble relay steps aside while it is armed — one voice on the single shared
        # Mumble user at a time. Reuses the ADR 0049 timed latch; shares the injectable clock.
        self._operator_talk = DtmfMuteGate(hold=operator_talk_hold, clock=clock)
        self._operator_hold = operator_talk_hold
        # DTMF activity (ADR 0049): the real-time tone detector marks the shared gate the instant
        # tone energy appears on RF; the gate then (a) drops those frames from the Mumble feed and
        # (b) yields Mumble→RF keying so an inbound over does not transmit over the command. Both
        # None (the default) keeps the original zero-latency raw relay and no yield.
        self._dtmf_mute = dtmf_mute
        self._tone_detector = tone_detector
        # RX guard (ADR 0085): a short window after any local transmit ends during which the RF→Mumble
        # relay is suppressed, swallowing the AIOC's TX→RX turnaround transient before it reaches
        # Mumble. Armed from outside — the arbiter's TX→RX edge, source-agnostic so a browser talker
        # arms it too — so it is app-scoped (a reused ADR 0049 timed latch, sharing the clock) and
        # outlives per-connect bridge rebuilds. None keeps the raw relay (no guard). Scoped to the
        # Mumble feed only; browser Listen (a separate hub subscriber) and recording are untouched.
        self._rx_guard = rx_guard

        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._rx_sub = None
        self._tx_queue: asyncio.Queue[bytes] | None = None
        self._tasks: list[asyncio.Task] = []
        # Mumble→RF observability (ADR 0045): every inbound frame lands in exactly one bucket —
        # transmitted as part of a keyed over, or dropped for a stated reason. Surfaced by
        # `tx_stats()` in `/link/status` so a silent field failure (e.g. a permanently-latched
        # rx_active) is diagnosable on the box.
        self._frames_in = 0
        self._dropped_rx_active = 0
        self._dropped_slot_busy = 0
        self._dropped_dtmf_yield = 0
        self._overs_keyed = 0
        #: Mumble→RF frames dropped because the radio REFUSED the key-up (ADR 0153) — it is
        #: demodulating AM and will not key its own PTT path (ADR 0150). A *standing* condition:
        #: it recurs on every frame until the operator changes the demodulator, which is why it is
        #: counted separately from `_relay_errors` and why its log line is throttled.
        self._dropped_key_refused = 0
        #: Mumble→RF frames dropped by an UNEXPECTED fault on the key path (ADR 0153) — a dead
        #: audio device, a yanked serial cable. A *fault*, not a condition: rare, and each one
        #: matters, so it is never folded into the refusal count above (a standing condition would
        #: bury it) and it is logged with a full traceback every time.
        self._relay_errors = 0
        #: RF→Mumble frames dropped because DTMF tone energy was detected in them (ADR 0049).
        self._dtmf_muted = 0
        #: RF→Mumble frames withheld because the web operator was talking on Mumble (ADR 0050).
        self._op_yielded = 0
        #: RF→Mumble frames dropped during the post-transmit RX guard window (ADR 0085).
        self._rx_guarded = 0
        #: RF→Mumble frames withheld because the station's second receiver is playing broadcast FM,
        #: so what it hears is a commercial station and not this channel (ADR 0162). `None` keeps
        #: the raw relay, the `rx_guard` shape — and the predicate is a callable rather than a latch
        #: because the state lives on the radio, not in a timer. It closes over the composition
        #: root's live `radio`, the `rx_active` precedent, so a `/radio/select` swap is picked up by
        #: a bridge that was built before it.
        self._broadcast_fm = broadcast_fm
        self._rx_deafened = 0
        #: The last reason given, and the last time one was logged. A standing condition recurring
        #: at frame rate must not print fifty lines a second — the `dropped_key_refused` throttle.
        self._deafened_reason: str | None = None
        self._deafened_logged = 0.0

    @property
    def running(self) -> bool:
        """Whether the bridge is connected and its loop task(s) are live."""
        return self._running

    @property
    def tx_to_rf(self) -> bool:
        """Whether Mumble voice is bridged onto RF (False = receive-only monitor)."""
        return self._tx_to_rf

    def status(self) -> MumbleStatus:
        """The Mumble connection snapshot, for ``GET /link/status``."""
        return self._mumble.status()

    def tx_stats(self) -> dict:
        """Bridge frame counters for ``GET /link/status`` (ADR 0045, 0049).

        Mumble→RF: ``frames_in`` counts every frame received from Mumble; ``dropped_rx_active``
        those dropped deferring to a live RF signal; ``dropped_slot_busy`` those dropped because the
        browser talker held the slot; ``dropped_dtmf_yield`` those withheld because the operator was
        keying DTMF (ADR 0049); ``overs_keyed`` how many transmissions the bridge keyed. RF→Mumble:
        ``dtmf_muted`` counts frames dropped from the Mumble feed as detected DTMF tones (ADR 0049);
        ``op_yielded`` counts frames withheld while the web operator was talking on Mumble (ADR 0050);
        ``rx_guarded`` counts frames dropped during the post-transmit RX guard window (ADR 0085).

        ``dropped_key_refused`` and ``relay_errors`` (ADR 0153) both count Mumble→RF frames the
        relay could not put on the air, and are deliberately **two** counters: the first is a
        standing condition the operator can clear from the radio's front panel (it is demodulating
        AM and refuses its own PTT path, ADR 0150), the second an unexpected fault that usually
        means hardware — a dead audio device, an unplugged cable. Sharing one counter would let a
        refusal recurring at frame rate bury a single I/O error.

        ``rx_deafened`` counts RF frames withheld because the station's second receiver is playing
        broadcast FM (ADR 0162), and ``deafened`` is the **tri-state** beside it, because the counter
        alone cannot be read safely. ``rx_deafened: 0`` means "verified hearing" when ``deafened`` is
        ``false`` and "nobody has ever asked this radio" when it is ``null`` — and those rendering
        identically is precisely how a deaf station gets trusted (`BroadcastFm`'s own docstring says
        so about the layer below). ``deafened_reason`` carries the sentence an operator can act on,
        and is ``null`` whenever nothing is being withheld.

        ``deafened_age_s`` and ``deafened_unknown`` come from the cadence (ADR 0163) and are
        ``null``/``0`` without one. Age matters because ``deafened: true`` renders a reading two
        seconds old and one ten minutes old identically — the same trap as the counter above, one
        level further out — and ``deafened_unknown`` counts probes that answered nothing and were
        **held through** rather than acted on, which is the failure rule made visible.
        """
        block = self._broadcast_fm() if self._broadcast_fm is not None else None
        deafened = None if block is None else bool(block.on)
        cadence = cadence_stats(self._broadcast_fm)
        return {
            "frames_in": self._frames_in,
            "dropped_rx_active": self._dropped_rx_active,
            "dropped_slot_busy": self._dropped_slot_busy,
            "dropped_dtmf_yield": self._dropped_dtmf_yield,
            "dropped_key_refused": self._dropped_key_refused,
            "relay_errors": self._relay_errors,
            "overs_keyed": self._overs_keyed,
            "dtmf_muted": self._dtmf_muted,
            "op_yielded": self._op_yielded,
            "rx_guarded": self._rx_guarded,
            "rx_deafened": self._rx_deafened,
            # Derived at read time, never stored: a stored copy is a reading old enough to be a lie.
            # `None` is the common case and it is the one that has to stay legible — see the
            # docstring above for why a bare count cannot carry it.
            "deafened": deafened,
            "deafened_reason": self._deafened_reason,
            "deafened_age_s": cadence["age_s"],
            "deafened_unknown": cadence["unknown"],
        }

    async def start(self) -> None:
        """Connect, subscribe to RF audio, and launch the bridge task(s). Idempotent."""
        if self._running:
            return
        self._loop = asyncio.get_running_loop()
        # Wire the received-audio sink before connecting so no early voice frame is missed.
        self._mumble.on_audio = self._on_mumble_audio
        self._mumble.connect()
        # RF -> Mumble: subscribe like a browser listener and hold a pump demand so the shared
        # reader runs even with nobody on `/audio/rx`.
        self._rx_sub = self._audio_hub.subscribe()
        if self._acquire_rx is not None:
            await self._acquire_rx()
        self._tasks = [asyncio.create_task(self._rx_to_mumble())]
        # The relay loop now exists, so the 97.113(b) hazard now exists — and only now is there any
        # reason to put probe frames on a serial link shared with tuning traffic (ADR 0163).
        start_cadence(self._broadcast_fm)
        if self._tx_to_rf:
            self._tx_queue = asyncio.Queue(maxsize=self._tx_queue_maxsize)
            self._tasks.append(asyncio.create_task(self._mumble_to_rf()))
        self._running = True

    async def stop(self) -> None:
        """Cancel the task(s), release the rx demand + talker slot, and disconnect. Idempotent."""
        if not self._running:
            return
        self._running = False
        stop_cadence(self._broadcast_fm)   # refcounted: D-STAR's crossband keeps it alive if running
        for task in self._tasks:
            task.cancel()
        # Bounded, concurrent join (ADR 0104): an unbounded `await task` here could hang shutdown
        # forever behind a task parked in a non-cancellable blocking call (send/executor) — one of
        # the two unbounded joins that could push SIGTERM past the service's stop budget into a
        # SIGKILL. Mirrors the ADR 0099-hardened D-STAR teardown; a still-parked task is abandoned
        # (daemon-side cleanup catches it at interpreter exit).
        if self._tasks:
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*self._tasks, return_exceptions=True), timeout=2.0
                )
        self._tasks = []
        self._mumble.on_audio = None
        if self._rx_sub is not None:
            self._audio_hub.unsubscribe(self._rx_sub)
            self._rx_sub = None
        if self._release_rx is not None:
            await self._release_rx()
        self._tx_queue = None
        self._mumble.disconnect()

    # --- RF -> Mumble --------------------------------------------------------------------

    def _deafened(self) -> bool:
        """Is this station hearing a broadcast station instead of its own channel? (ADR 0162)

        Checked **first** in both relay branches, above the RX guard. It is a standing legal
        condition rather than a timed latch, so it must not be shadowed by the guard's ordering: a
        frame that is both inside the turnaround window and broadcast FM should be counted as the
        thing an operator has to go and fix.

        Deliberately **not** folded into `_send_to_mumble`, which would be the tidier place and is
        the wrong one: `send_operator_audio` shares that helper, and muting there would silence the
        web operator's own microphone — which is not a retransmission of anything and has nothing to
        do with what the radio can hear.
        """
        if self._broadcast_fm is None:
            return False
        reason = relay_mute_reason(self._broadcast_fm())
        if reason is None:
            self._deafened_reason = None
            return False
        self._rx_deafened += 1
        self._deafened_reason = reason
        now = self._clock()
        # Once, then at most every 30 s. Silence on a link is indistinguishable from a dead link,
        # which is the fault class this repo keeps closing — but a line per frame would bury it.
        if self._rx_deafened == 1 or now - self._deafened_logged >= 30.0:
            self._deafened_logged = now
            log.warning(
                "mumble: RF relay muted — %s (%d frames so far)", reason, self._rx_deafened
            )
        return True

    async def _rx_to_mumble(self) -> None:
        assert self._rx_sub is not None
        if self._dtmf_mute is None:
            # No DTMF mute: the original zero-latency relay, byte for byte.
            while True:
                frame = await self._rx_sub.get()
                if self._deafened():
                    continue
                if self._rx_guard is not None and self._rx_guard.muted():
                    self._rx_guarded += 1
                    continue
                if self._operator_talk.muted():
                    self._op_yielded += 1
                    continue
                self._send_to_mumble(frame)
            return
        # DTMF mute (ADR 0049): decide per frame, in real time. If the tone detector sees DTMF
        # energy in this frame, arm the shared gate now (which also yields Mumble→RF keying); then,
        # if the gate is armed, drop the frame — no delay line, because the decision is made on the
        # very frame before it is sent. `note_digit` (multimon) may also arm the gate as a backstop.
        while True:
            frame = await self._rx_sub.get()
            if self._deafened():
                continue
            if self._rx_guard is not None and self._rx_guard.muted():
                self._rx_guarded += 1
                continue
            if self._tone_detector is not None and self._tone_detector.detect(frame):
                self._dtmf_mute.note_tone()
            if self._dtmf_mute.muted():
                self._dtmf_muted += 1
                continue
            # Yield the shared Mumble user to the web operator's mic (ADR 0050): while they talk,
            # withhold RF relay frames so the one channel user carries a single, ungarbled voice.
            if self._operator_talk.muted():
                self._op_yielded += 1
                continue
            self._send_to_mumble(frame)

    def send_operator_audio(self, pcm: bytes) -> None:
        """Send the web operator's mic audio to Mumble on the shared connection (ADR 0050).

        Arms the operator-talk yield so the RF→Mumble relay steps aside, then sends on the one Mumble
        user. Keys no RF (no ``TxSession``/arbiter); a no-op guarded send if the client is down.
        """
        self._operator_talk.mute_for(self._operator_hold)
        self._send_to_mumble(pcm)

    def _send_to_mumble(self, frame: bytes) -> None:
        try:
            self._mumble.send_audio(frame)
        except Exception:
            # A send fault must never kill the bridge task; the next frame retries.
            pass

    # --- Mumble -> RF --------------------------------------------------------------------

    def _on_mumble_audio(self, pcm: bytes) -> None:
        """Client-thread sink: hand the frame to the loop (thread-safe), never touch state here."""
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._deliver_inbound, pcm)

    def _deliver_inbound(self, pcm: bytes) -> None:
        """On-loop: fan received Mumble voice to the browser monitor (ADR 0050) and the RF drain.

        The hub publish runs for every entry (even receive-only), so the web UI can hear the channel;
        the RF enqueue is a no-op unless this entry bridges to RF (``_tx_queue`` created in ``start``).
        """
        if self._mumble_rx_hub is not None:
            self._mumble_rx_hub.publish(pcm)
        self._enqueue_tx(pcm)

    def _enqueue_tx(self, pcm: bytes) -> None:
        """On-loop: bounded drop-oldest enqueue of a received Mumble frame."""
        queue = self._tx_queue
        if queue is None:
            return
        self._frames_in += 1
        try:
            queue.put_nowait(pcm)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(pcm)

    async def _mumble_to_rf(self) -> None:
        assert self._tx_queue is not None
        session: TxSession | None = None
        try:
            while True:
                try:
                    pcm = await asyncio.wait_for(
                        self._tx_queue.get(), timeout=self._tx_hang
                    )
                except asyncio.TimeoutError:
                    # Mumble went quiet past the hang window: end the transmission.
                    session = self._end_session(session)
                    continue
                # Defer to a live RF signal being received — don't double onto it.
                if self._rx_active is not None and self._rx_active():
                    self._dropped_rx_active += 1
                    continue
                # Yield to an in-progress DTMF command (ADR 0049): if the operator is keying DTMF,
                # withhold this frame and drop PTT immediately so the (deaf-while-keyed) receiver
                # reopens for the rest of the command. Works under `squelch="off"`, where the
                # `rx_active` defer above is inert.
                if self._dtmf_mute is not None and self._dtmf_mute.muted():
                    self._dropped_dtmf_yield += 1
                    session = self._end_session(session)
                    continue
                if session is None:
                    # Share the single talker slot with the browser; refuse (drop) if it's busy.
                    if not self._tx_slot.try_acquire():
                        self._dropped_slot_busy += 1
                        continue
                    session = TxSession(
                        self._radio,
                        idle_timeout=self._tx_hang,
                        arbiter=self._arbiter,
                        station_id=self._station_id,
                        clock=self._clock,
                    )
                    fresh = True
                else:
                    fresh = False
                # Exception order is load-bearing (ADR 0153): the named catches must win, and the
                # broad backstop must go LAST. Reordering would silently reclassify a malformed
                # frame as a relay fault and lose a distinction that is already correct.
                try:
                    session.feed(pcm)
                except AudioFormatMismatch:
                    # A malformed frame from Mumble: end this over rather than key on garbage.
                    session = self._end_session(session)
                except RadioUnavailable as exc:
                    # The radio refused the key-up — it is demodulating AM and will not key its own
                    # PTT path (ADR 0150). An unhandled raise here killed this task for the life of
                    # the link, which is the fault the D-STAR bridge's arbiter catch already
                    # described: one frame must not take the relay down. Throttled, because this is
                    # a standing condition that recurs at frame rate until the operator clears it.
                    self._dropped_key_refused += 1
                    if self._dropped_key_refused == 1 or self._dropped_key_refused % 250 == 0:
                        log.warning(
                            "mumble: radio refused the key-up — RF audio dropped "
                            "(%d frames so far): %s",
                            self._dropped_key_refused,
                            exc,
                        )
                    session = self._end_session(session)
                except Exception:
                    # The backstop. A dead audio device or a yanked serial cable kills this loop
                    # exactly as the refusal did, so catching only the named exception would be a
                    # partial fix rather than a smaller one. Unthrottled and with a traceback: it
                    # is rare by construction, and the traceback is the only thing that identifies
                    # an unexpected fault.
                    self._relay_errors += 1
                    log.exception("mumble: relay error feeding RF — over ended, loop alive")
                    session = self._end_session(session)
                else:
                    if fresh:
                        # Counted only once the key-up actually succeeded (ADR 0153): `overs_keyed`
                        # is documented as transmissions the bridge KEYED, and incrementing it
                        # before the attempt reported overs that never happened — beside the
                        # refusal counter that pair would read as nonsense.
                        self._overs_keyed += 1
        finally:
            # Cancellation (stop) or any exit must drop PTT and free the slot.
            self._end_session(session)

    def _end_session(self, session: TxSession | None) -> None:
        """Close a keyed session and release the talker slot; returns ``None`` for reassignment.

        The release is a scope exit, not the statement after ``close()`` (ADR 0167). ``close()``
        drops PTT, and ADR 0166's dead serial reader made a raising ``ptt(False)`` a demonstrated
        event rather than a theoretical one — so the old ordering could strand the slot that this
        relay, the browser talker and the D-STAR bridge all share, from any of this method's six
        call sites. Worse, four of those are exception handlers and one is a ``finally``: a raise
        here also replaced the exception being handled and took the relay loop down with it, which
        is the exact fault ADR 0153 closed for ``feed``.

        ``except Exception`` and not ``BaseException``: a cancellation must still propagate (the
        loop is being stopped), and the ``finally`` frees the slot on that path too.
        """
        if session is not None:
            try:
                session.close()
            except Exception:
                # Logged, never swallowed silently — this is the only record that the transmitter
                # may not have been un-keyed. `DStarBridge` already suppresses here; the Mumble side
                # diverging is what ADR 0167 found.
                log.exception("mumble: failed to close the TX session — slot released anyway")
            finally:
                self._tx_slot.release()
        return None
