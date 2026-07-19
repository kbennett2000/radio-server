"""The D-STAR <-> RF bridge: a half-duplex peer state machine (ADR 0087).

This wires a :class:`~radio_server.dstar.client.GatewayClient` and a
:class:`~radio_server.vocoder.base.Vocoder` to the RF radio using the seams that already exist — the
same shape as the Mumble bridge (ADR 0041), with AMBE + DSRP framing in place of Opus + Mumble:

- **Reflector -> RF.** Inbound DSRP header/data arrive on the client's reader thread; the sinks hop
  onto the event loop (``call_soon_threadsafe``) into a bounded, drop-oldest queue. A drain task
  decodes each AMBE frame through the vocoder, resamples 8 kHz -> 48 kHz at the edge
  (:func:`~radio_server.audio.resample.to_canonical`), and keys the radio through a
  :class:`~radio_server.tx.session.TxSession` sharing the single :class:`~radio_server.tx.session.TxSlot`
  and the same ``station_id`` (Part 97 auto-ID). The stream's end frame (or a hang timeout) drops PTT.
- **RF -> reflector.** Subscribe to the shared :class:`~radio_server.rx.hub.AudioHub` like a browser
  listener, holding an rx demand so the pump runs with no browser. A loop task reframes each 48 kHz
  frame down to 8 kHz / 20 ms, encodes it, and sends one DSRP header then the AMBE data frames,
  closing with the end frame after ``tx_hang`` of silence.

**Half-duplex by a mode latch (IDLE / RX / TX).** D-STAR carries one talker at a time and the RF side
is simplex, so the bridge uses the single vocoder chip in **one direction at a time**: while a
reflector stream is inbound it does not simultaneously encode RF outbound, and vice versa. This is
also the ADR 0086 safety rule — the AMBE2000 must never have encode and decode *interleaved per
frame*; the latch guarantees it. The blocking vocoder runs on a dedicated single-worker executor so
it never stalls the event loop, and the latch keeps its two pipelines from ever running at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor

from ..audio import CANONICAL_FORMAT, AudioFormatMismatch, AudioFrame, resample, to_canonical
from ..backends import Radio
from ..tx import TxIdentifier, TxSession, TxSlot
from ..vocoder.base import PCM_BYTES_PER_FRAME, PCM_FORMAT, PCM_RATE, Vocoder
from . import dsrp
from .client import GatewayClient, GatewayStatus
from .header import build_voice_header, parse_header

log = logging.getLogger(__name__)

Clock = Callable[[], float]

#: Bounded hand-off queue depth for inbound reflector frames (~1.3 s at 20 ms) before drop-oldest,
#: mirroring the Mumble bridge's tx queue and the audio hub.
DEFAULT_RX_QUEUE_MAXSIZE = 64

#: Seconds of RF silence after which an outbound over is closed (the end frame sent, PTT of the
#: reflector stream dropped). Marked tunable default (guardrail 1) — the RF→reflector hang.
DEFAULT_DSTAR_TX_HANG = 1.0

#: Echo/command destination: URCALL = "E" addresses the gateway's echo test.
ECHO_URCALL = "E"

#: Seconds between idle keepalive decodes that keep the DV Dongle AMBE2000 warm. The chip goes
#: unresponsive after ~2-3 s of no codec traffic (bench-measured; ADR 0087/0088), so the FIRST inbound
#: reflector frame after a quiet spell would otherwise time out and the over would be lost. A cheap
#: decode of NULL_AMBE every keepalive interval — run ONLY while idle, so it never interleaves with a
#: live encode/decode stream (the ADR 0086 hazard) — keeps the chip primed. 0 disables. Marked tunable
#: default (guardrail 1): comfortably under the measured ~2 s sleep floor.
DEFAULT_VOCODER_KEEPALIVE = 1.2

#: How many NULL_AMBE data frames a link/unlink command burst carries between its header and the
#: end frame. Default 0 (header + terminator only) — the gateway latches routing off the header.
#: Marked tunable default (guardrail 1): if a bare header does not trigger the gateway's link logic,
#: raise this up the fallback ladder (a few silence frames → a full 21-frame superframe) without a
#: code change. See ADR 0088.
DEFAULT_COMMAND_FRAMES = 0


class DStarBridge:
    """Bridge RF audio to/from a D-STAR reflector via the gateway (ADR 0087/0089). A pure DI object.

    Start/stop are idempotent (mirroring the Mumble bridge). ``start`` **creates the vocoder from its
    factory (opening the DV Dongle with an exclusive serial lock)**, opens the gateway client,
    subscribes to the audio hub, raises an rx demand, and launches the drain/encode tasks; ``stop``
    cancels them, releases the demand + slot, and closes the client and vocoder.

    **Start/stop follow the reflector link (ADR 0089).** The DV Dongle is a single physical resource
    shared between the two radio instances (AIOC + kv4p), so the bridge does *not* hold it while idle:
    :class:`~radio_server.dstar.manager.DStarLinkManager` calls ``start`` on connect and ``stop`` on
    disconnect. If the other instance already holds the dongle, the factory raises
    :class:`~radio_server.vocoder.base.VocoderUnavailable` (the exclusive open fails) and ``start``
    propagates it — the manager surfaces "in use by the other radio". The vocoder is created *first*
    in ``start`` so a busy dongle fails before any other resource is opened.
    """

    def __init__(
        self,
        gateway: GatewayClient,
        radio: Radio,
        vocoder_factory: Callable[[], Vocoder],
        *,
        arbiter,
        tx_slot: TxSlot,
        audio_hub,
        callsign: str,
        module: str,
        acquire_rx: Callable[[], Awaitable[None]] | None = None,
        release_rx: Callable[[], Awaitable[None]] | None = None,
        station_id: TxIdentifier | None = None,
        tx_to_rf: bool = True,
        rx_to_reflector: bool = True,
        tx_hang: float = DEFAULT_DSTAR_TX_HANG,
        ur_call: str = "CQCQCQ",
        clock: Clock | None = None,
        rx_active: Callable[[], bool] | None = None,
        rx_queue_maxsize: int = DEFAULT_RX_QUEUE_MAXSIZE,
        dstar_rx_hub=None,
        command_frames: int = DEFAULT_COMMAND_FRAMES,
        vocoder_keepalive: float = DEFAULT_VOCODER_KEEPALIVE,
        on_activity: Callable[[dict], None] | None = None,
        rf_gate: Callable[[AudioFrame], bool] | None = None,
    ) -> None:
        if clock is None:
            import time

            clock = time.monotonic
        self._gateway = gateway
        self._radio = radio
        self._vocoder_factory = vocoder_factory
        self._vocoder: Vocoder | None = None
        self._arbiter = arbiter
        self._tx_slot = tx_slot
        self._audio_hub = audio_hub
        self._callsign = callsign
        self._module = module
        self._acquire_rx = acquire_rx
        self._release_rx = release_rx
        self._station_id = station_id
        self._tx_to_rf = tx_to_rf
        self._rx_to_reflector = rx_to_reflector
        self._tx_hang = tx_hang
        self._ur_call = ur_call
        self._clock = clock
        self._rx_active = rx_active
        self._rx_queue_maxsize = rx_queue_maxsize
        self._dstar_rx_hub = dstar_rx_hub
        self._command_frames = command_frames
        self._vocoder_keepalive = vocoder_keepalive
        self._on_activity = on_activity
        # Per-frame level gate on the RF→reflector crossband (ADR 0091): keys the reflector only on real
        # RF audio, never on receiver hiss — independent of the global `audio.squelch`. None = ungated
        # (the historical behaviour; a bare bridge in tests keeps the old shape).
        self._rf_gate = rf_gate
        self._last_vox = clock()

        self._running = False
        # The half-duplex latch: "idle" | "rx" (reflector inbound) | "tx" (RF outbound). Mutated only
        # on the event loop, so cooperative scheduling keeps it consistent without a lock.
        self._mode = "idle"
        # Who owns an outbound (TX) over: None | "rf" (the crossband RF pump) | "op" (the browser mic).
        # With both TX sources live (ADR 0089) the latch alone is not enough — the second source must
        # not feed the first's open over. Only the owner feeds/closes; the other drops while busy.
        self._tx_source: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._rx_sub = None
        self._rx_queue: asyncio.Queue[dsrp.DsrpMessage] | None = None
        self._rx_session: TxSession | None = None  # the open reflector→RF keying session, if any
        self._rx_slot_held = False  # whether the rx session actually acquired the shared TxSlot
        self._tasks: list[asyncio.Task] = []
        self._vox_pool: ThreadPoolExecutor | None = None
        # Outbound (RF→reflector) reframing buffer of 8 kHz PCM and the running session id / sequence.
        self._tx_pcm = bytearray()
        self._tx_session_id = 0
        self._tx_seq = 0
        # Wall-clock of the last outbound frame fed to the reflector — the deadline the RF pump uses to
        # reap a stale over (a leaked "op" over whose browser WS died without end_operator_over).
        self._last_tx_feed = 0.0
        # Monotonic per-stream session-id allocator (voice overs and command bursts share it; the
        # latch serializes streams in time, so the id need only be nonzero and varying).
        self._session_counter = 0

        # Observability — every direction's frames land in a counted bucket.
        self._rx_frames = 0  # reflector AMBE frames decoded to RF
        self._rx_overs = 0  # reflector streams keyed onto RF
        self._rx_dropped_busy = 0  # inbound frames dropped while TX held the latch
        self._tx_frames = 0  # RF frames encoded to the reflector
        self._tx_overs = 0  # outbound streams opened to the reflector
        self._tx_dropped_busy = 0  # RF frames dropped while RX held the latch

    @property
    def running(self) -> bool:
        return self._running

    @property
    def mode(self) -> str:
        """The current half-duplex mode: ``"idle"``, ``"rx"``, or ``"tx"``."""
        return self._mode

    def status(self) -> GatewayStatus:
        """The gateway link snapshot, for status reporting."""
        return self._gateway.status()

    def tx_stats(self) -> dict:
        """Bridge frame counters for status reporting (both directions)."""
        return {
            "mode": self._mode,
            "rx_frames": self._rx_frames,
            "rx_overs": self._rx_overs,
            "rx_dropped_busy": self._rx_dropped_busy,
            "tx_frames": self._tx_frames,
            "tx_overs": self._tx_overs,
            "tx_dropped_busy": self._tx_dropped_busy,
        }

    async def start(self) -> None:
        """Acquire the DV Dongle + gateway endpoint and launch the bridge tasks (ADR 0089). Idempotent.

        Creates the vocoder from its factory **first** — the exclusive DV Dongle open raises
        :class:`~radio_server.vocoder.base.VocoderUnavailable` if the other radio instance already
        holds it, before any other resource is opened. On any later failure the vocoder is closed so a
        half-open bridge never lingers.
        """
        if self._running:
            return
        self._loop = asyncio.get_running_loop()
        # Open the shared, exclusive resource first; a busy dongle fails here and nothing else opens.
        vocoder = self._vocoder_factory()
        self._vocoder = vocoder
        try:
            self._vox_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dstar-vocoder")
            # Wire the inbound sinks before starting so no early frame is missed.
            self._gateway.on_header = self._on_gateway_header
            self._gateway.on_data = self._on_gateway_data
            self._gateway.start()
            self._tasks = []
            if self._tx_to_rf:
                self._rx_queue = asyncio.Queue(maxsize=self._rx_queue_maxsize)
                self._tasks.append(asyncio.create_task(self._reflector_to_rf()))
            if self._rx_to_reflector:
                self._rx_sub = self._audio_hub.subscribe()
                if self._acquire_rx is not None:
                    await self._acquire_rx()
                self._tasks.append(asyncio.create_task(self._rf_to_reflector()))
            # Keep the vocoder warm for inbound decode whenever we bridge reflector audio in (tx_to_rf).
            if self._tx_to_rf and self._vocoder_keepalive > 0:
                self._last_vox = self._clock()
                self._tasks.append(asyncio.create_task(self._keepalive_loop()))
        except Exception:
            # Roll back a half-open start so the dongle is released for the other instance to retry.
            await self._teardown()
            raise
        self._running = True

    async def stop(self) -> None:
        """Cancel the tasks, release the demand + slot, and close the gateway + vocoder. Idempotent."""
        if not self._running:
            return
        self._running = False
        await self._teardown()

    async def _teardown(self) -> None:
        """Cancel tasks and release every held resource — the shared teardown for stop and start-rollback.

        Ordered so PTT can never survive a teardown (ADR 0091): the reflector→RF loop can be parked in a
        blocking vocoder decode when we tear down, so (1) close the vocoder FIRST — its ``close()``
        wakes a waiting ``_exchange`` (``notify_all``), letting the cancelled task actually finish; (2)
        drop PTT DIRECTLY via ``_force_unkey`` rather than relying on that loop's ``finally`` (which
        can't run while the task is parked); (3) bound each task join so a still-wedged worker can never
        hang the teardown — PTT is already down by then.
        """
        for task in self._tasks:
            task.cancel()
        # (1) Unblock a parked decode/encode before joining, so the cancel is deliverable.
        if self._vocoder is not None:
            with contextlib.suppress(Exception):
                self._vocoder.close()
        # (2) The load-bearing unkey — independent of the (possibly parked) reflector→RF loop.
        self._force_unkey()
        # (3) Join, but never wait forever on a worker still wedged in the executor.
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
                await asyncio.wait_for(task, timeout=self._tx_hang + 2.0)
        self._tasks = []
        self._gateway.on_header = None
        self._gateway.on_data = None
        if self._rx_sub is not None:
            self._audio_hub.unsubscribe(self._rx_sub)
            self._rx_sub = None
            if self._release_rx is not None:
                await self._release_rx()
        self._rx_queue = None
        self._gateway.close()
        if self._vox_pool is not None:
            self._vox_pool.shutdown(wait=False)
            self._vox_pool = None
        self._vocoder = None  # already closed above
        self._mode = "idle"
        self._tx_source = None

    def _force_unkey(self) -> None:
        """Drop PTT and clear the latch directly — the teardown/safety unkey that never relies on the
        ``_reflector_to_rf`` loop (which can be parked in a blocking decode). Idempotent."""
        if self._rx_session is not None:
            with contextlib.suppress(Exception):
                self._rx_session.close()  # radio.ptt(False) + arbiter.release_tx()
            if self._rx_slot_held:
                with contextlib.suppress(Exception):
                    self._tx_slot.release()
                self._rx_slot_held = False
            self._rx_session = None
        self._mode = "idle"
        self._tx_source = None

    async def _encode(self, frame: AudioFrame) -> bytes:
        assert self._vox_pool is not None and self._vocoder is not None
        try:
            return await self._loop.run_in_executor(self._vox_pool, self._vocoder.encode, frame)
        finally:
            self._last_vox = self._clock()

    async def _decode(self, ambe: bytes) -> AudioFrame:
        assert self._vox_pool is not None and self._vocoder is not None
        try:
            return await self._loop.run_in_executor(self._vox_pool, self._vocoder.decode, ambe)
        finally:
            self._last_vox = self._clock()

    async def _keepalive_loop(self) -> None:
        """Keep the AMBE2000 warm while idle so the first inbound frame decodes instead of timing out.

        Runs a cheap NULL_AMBE decode whenever the codec has been idle for ~the keepalive interval and
        the bridge is in IDLE mode — never during a live RX/TX over (a real stream keeps the chip warm,
        and injecting a decode mid-over would be the ADR 0086 interleave hazard). A keepalive that
        itself times out (chip already asleep) is swallowed — the next tick re-primes it.
        """
        while True:
            await asyncio.sleep(self._vocoder_keepalive)
            if self._mode != "idle":
                continue
            if self._clock() - self._last_vox < self._vocoder_keepalive * 0.5:
                continue  # a real over used the chip recently — no need to poke it
            try:
                await self._decode(dsrp.NULL_AMBE)
            except Exception:
                # A timeout/error here just means we'll re-prime next tick; do not spam the log.
                pass

    # --- Reflector -> RF -----------------------------------------------------------------

    def _on_gateway_header(self, msg: dsrp.DsrpMessage) -> None:
        """Client-thread sink: hand the header to the loop (thread-safe)."""
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._enqueue_rx, msg)

    def _on_gateway_data(self, msg: dsrp.DsrpMessage) -> None:
        """Client-thread sink: hand a data frame to the loop (thread-safe)."""
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._enqueue_rx, msg)

    def _enqueue_rx(self, msg: dsrp.DsrpMessage) -> None:
        """On-loop: bounded drop-oldest enqueue of an inbound reflector message."""
        queue = self._rx_queue
        if queue is None:
            return
        try:
            queue.put_nowait(msg)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(msg)

    async def _reflector_to_rf(self) -> None:
        assert self._rx_queue is not None
        try:
            while True:
                # Independent PTT watchdog (ADR 0091): if we're keyed but haven't fed RF within tx_hang
                # — failing decodes, or a lost end-bit while inbound DATA still trickles so the queue
                # never idles — close the over anyway. Without this the transmitter can stay keyed
                # indefinitely (the stuck-key incident). The loop still cycles on inbound DATA, so this
                # check fires; `feed` refreshes the deadline, so a healthy over is never cut.
                if self._rx_session is not None and self._rx_session.idle_elapsed():
                    self._end_rx()
                try:
                    msg = await asyncio.wait_for(self._rx_queue.get(), timeout=self._tx_hang)
                except asyncio.TimeoutError:
                    self._end_rx()  # inbound went quiet past the hang: close the over
                    continue

                if msg.kind is dsrp.MessageKind.HEADER:
                    # A new inbound stream. Take the latch unless RF is outbound to the reflector.
                    if self._mode == "tx":
                        self._rx_dropped_busy += 1
                        continue
                    self._end_rx()  # close any prior over first
                    self._mode = "rx"
                    self._rx_overs += 1
                    self._report_activity(msg.radio_header, "rx")  # who's talking on the reflector
                    continue

                if msg.kind is dsrp.MessageKind.DATA:
                    if self._mode != "rx":
                        self._rx_dropped_busy += 1
                        continue
                    await self._play_ambe(dsrp.voice_frame(msg.dv_frame))
                    if msg.end:
                        self._end_rx()
        finally:
            self._end_rx()

    def _open_rx_session(self) -> TxSession:
        """Lazily open the reflector→RF keying session on the first decoded frame."""
        if self._rx_session is None:
            # Remember whether we actually got the shared slot, so `_end_rx`/`_force_unkey` release
            # ONLY a slot we own — never one a concurrent browser-TX talker holds (ADR 0091).
            self._rx_slot_held = self._tx_slot.try_acquire()
            self._rx_session = TxSession(
                self._radio,
                idle_timeout=self._tx_hang,
                arbiter=self._arbiter,
                station_id=self._station_id,
                clock=self._clock,
            )
        return self._rx_session

    async def _play_ambe(self, ambe: bytes) -> None:
        """Decode one AMBE frame, resample to canonical, and key it onto RF (+ optional monitor hub)."""
        try:
            pcm8 = await self._decode(ambe)
        except Exception:
            log.exception("dstar: AMBE decode failed")
            return
        frame48 = to_canonical(pcm8)
        self._rx_frames += 1
        if self._dstar_rx_hub is not None:
            self._dstar_rx_hub.publish(frame48.samples)
        session = self._open_rx_session()
        with contextlib.suppress(AudioFormatMismatch):
            session.feed(frame48.samples)

    def _end_rx(self) -> None:
        """Close the reflector→RF session, release the slot (only if we hold it), and drop the latch."""
        if self._rx_session is not None:
            with contextlib.suppress(Exception):
                self._rx_session.close()
            if self._rx_slot_held:
                self._tx_slot.release()
                self._rx_slot_held = False
            self._rx_session = None
        if self._mode == "rx":
            self._mode = "idle"

    # --- RF -> reflector -----------------------------------------------------------------

    async def _rf_to_reflector(self) -> None:
        assert self._rx_sub is not None
        try:
            while True:
                try:
                    pcm = await asyncio.wait_for(self._rx_sub.get(), timeout=self._tx_hang)
                except asyncio.TimeoutError:
                    self._reap_stale_tx()  # silence: close our own stale over (rf, or a leaked op over)
                    continue
                # Signal-gate the crossband so it keys the reflector only on real RF audio, never on
                # receiver hiss — independent of the global audio.squelch (ADR 0091). A below-threshold
                # (silence) frame closes our over on the gate-close edge and never opens one; the gate's
                # own hang bridges word gaps so the over doesn't chatter.
                if self._rf_gate is not None and not self._rf_gate(AudioFrame(pcm, CANONICAL_FORMAT)):
                    self._reap_stale_tx()
                    continue
                # Defer to an inbound reflector stream, or to the browser mic if it holds TX — one
                # talker at a time (ADR 0089): first source to open the over owns it; the other drops.
                if self._mode == "rx" or (self._mode == "tx" and self._tx_source != "rf"):
                    self._tx_dropped_busy += 1
                    continue
                if self._mode == "idle":
                    self._open_tx("rf")
                await self._feed_rf(pcm)
        finally:
            self._end_tx("rf")

    def _reap_stale_tx(self) -> None:
        """Close our own outbound over on RF silence — the crossband's own "rf" over, OR a leaked "op"
        over whose browser WS died without calling ``end_operator_over``. Guarded by a no-feed deadline
        so a live over is never cut; a no-op unless we currently own an outbound over."""
        if self._mode != "tx":
            return
        if self._clock() - self._last_tx_feed >= self._tx_hang:
            self._end_tx(self._tx_source)

    def _alloc_session_id(self) -> int:
        """A per-stream session id in 1..0xFFFF (0 is reserved), derived without a clock so tests
        are deterministic: a running counter, wrapped."""
        self._session_counter = (self._session_counter + 1) % 0xFFFF
        return self._session_counter + 1

    def _open_tx(self, source: str) -> None:
        """Open an outbound reflector stream: latch TX to ``source``, send the header, reset the seq."""
        self._mode = "tx"
        self._tx_source = source
        self._tx_overs += 1
        self._tx_session_id = self._alloc_session_id()
        self._tx_seq = 0
        self._tx_pcm = bytearray()
        self._last_tx_feed = self._clock()  # arm the stale-over deadline for this fresh over
        header = build_voice_header(callsign=self._callsign, module=self._module, ur=self._ur_call)
        self._gateway.send_header(header, self._tx_session_id)
        self._report_activity(None, "tx")  # our own over onto the reflector (mic or crossband)

    async def _feed_rf(self, pcm48: bytes) -> None:
        """Reframe a 48 kHz RF frame to 8 kHz / 20 ms chunks, encode, and send DSRP data frames."""
        down = resample(AudioFrame(pcm48, CANONICAL_FORMAT), PCM_RATE)
        self._tx_pcm += down.samples
        while len(self._tx_pcm) >= PCM_BYTES_PER_FRAME:
            chunk = bytes(self._tx_pcm[:PCM_BYTES_PER_FRAME])
            del self._tx_pcm[:PCM_BYTES_PER_FRAME]
            try:
                ambe = await self._encode(AudioFrame(chunk, PCM_FORMAT))
            except Exception:
                log.exception("dstar: AMBE encode failed")
                continue
            dv = dsrp.build_dv_frame(ambe, dsrp.slow_data_for_seq(self._tx_seq))
            self._gateway.send_data(dv, self._tx_session_id, self._tx_seq)
            self._tx_frames += 1
            self._tx_seq = dsrp.next_seq(self._tx_seq)
            self._last_tx_feed = self._clock()  # refresh the stale-over deadline on real outbound audio

    def _end_tx(self, source: str) -> None:
        """Close ``source``'s outbound over with the terminating (null-AMBE, end-bit) frame.

        Guarded by ownership: a no-op unless we are in TX **and** ``source`` owns the current over — so
        the RF pump's silence-timeout can't tear down the browser's live over, and vice versa.
        """
        if self._mode != "tx" or self._tx_source != source:
            return
        end_dv = dsrp.build_dv_frame(dsrp.NULL_AMBE, dsrp.slow_data_for_seq(self._tx_seq))
        self._gateway.send_data(end_dv, self._tx_session_id, self._tx_seq, end=True)
        self._tx_pcm = bytearray()
        self._mode = "idle"
        self._tx_source = None

    # --- Browser operator -> reflector ---------------------------------------------------
    # The browser-mic TX source (the D-STAR twin of ``MumbleBridge.send_operator_audio``). It drives
    # the SAME encode→DSRP outbound pipeline as ``_rf_to_reflector`` but sourced from the WebSocket
    # instead of the RF ``audio_hub``. The WS endpoint owns the over: the first frame opens it and a
    # WS idle-timeout/disconnect calls ``end_operator_over``. **Must be awaited serially** (one
    # ``dstar_talk_slot`` holder) — concurrent calls would corrupt the shared ``_tx_*`` reframe state.
    # It coexists with the ``_rf_to_reflector`` crossband pump (ADR 0089): the ``_tx_source`` owner
    # latch means whichever opens the over first ("op" here, "rf" there) owns it and the other drops
    # while it is live, so their frames never interleave into one DSRP session.

    async def send_operator_audio(self, pcm48: bytes) -> None:
        """Feed one canonical (48 kHz) browser-mic frame toward the reflector, opening the over lazily.

        Drops the frame (counted) if the bridge isn't linked (not running), if a reflector stream holds
        the RX latch, or if the crossband RF pump currently owns the TX over — one talker at a time
        (the ADR 0086 no-interleave rule, the ADR 0089 shared-TX latch).
        """
        if not self._running:
            return
        if self._mode == "rx" or (self._mode == "tx" and self._tx_source != "op"):
            self._tx_dropped_busy += 1
            return
        if self._mode == "idle":
            self._open_tx("op")
        await self._feed_rf(pcm48)

    def end_operator_over(self) -> None:
        """Close the browser operator's outbound over. A no-op unless the operator owns the TX over."""
        self._end_tx("op")

    def _report_activity(self, radio_header: bytes | None, direction: str) -> None:
        """Fire the activity callback with the callsign parsed from an inbound/outbound radio header.

        Best-effort: a malformed header never disturbs the audio path. The app layer enriches the
        entry (reflector, timestamp) and pushes it to the web UI as an ``activity`` event (ADR 0089).
        """
        if self._on_activity is None:
            return
        mycall = self._callsign
        ur = ""
        if radio_header is not None:
            try:
                hdr = parse_header(radio_header)
                mycall = (hdr.my1 or "").strip() or self._callsign
                ur = (hdr.ur or "").strip()
            except Exception:
                return
        with contextlib.suppress(Exception):
            self._on_activity({"mycall": mycall, "ur": ur, "dir": direction})

    # --- Reflector link control ----------------------------------------------------------

    def send_link_command(self, urcall: str) -> bool:
        """Emit a one-shot D-STAR stream carrying a routing/link command in the header's URCALL.

        The standard D-STAR way to link/unlink a reflector: a header addressed to e.g. ``"REF001CL"``
        (link REF001 module C) or ``"       U"`` (module-wide unlink), then a minimal valid stream.
        The burst carries only ``NULL_AMBE`` so it never touches the vocoder chip.

        This is **synchronous and idle-gated on purpose**: the ``_mode`` latch is mutated only at
        synchronous points on the event loop, so a no-``await`` burst is atomic under cooperative
        scheduling — it can neither corrupt nor be corrupted by an in-flight over. Returns ``False``
        (the caller surfaces "busy") if the bridge is not idle; the operator retries.
        """
        if not self._running or self._mode != "idle":
            return False
        session_id = self._alloc_session_id()
        header = build_voice_header(callsign=self._callsign, module=self._module, ur=urcall)
        self._gateway.send_header(header, session_id)
        seq = 0
        for _ in range(self._command_frames):
            dv = dsrp.build_dv_frame(dsrp.NULL_AMBE, dsrp.slow_data_for_seq(seq))
            self._gateway.send_data(dv, session_id, seq)
            seq = dsrp.next_seq(seq)
        end_dv = dsrp.build_dv_frame(dsrp.NULL_AMBE, dsrp.slow_data_for_seq(seq))
        self._gateway.send_data(end_dv, session_id, seq, end=True)
        return True
