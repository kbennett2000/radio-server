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

from ..arbiter import ArbiterStateError
from ..audio import CANONICAL_FORMAT, AudioFormatMismatch, AudioFrame, resample, to_canonical
from ..backends import Radio
from ..tx import TxIdentifier, TxSession, TxSlot
from ..vocoder.base import PCM_BYTES_PER_FRAME, PCM_FORMAT, PCM_RATE, StreamingVocoder, Vocoder
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

#: Hard ceiling (seconds) on a single reflector→RF crossband over — the content-independent stuck-key
#: backstop (ADR 0097). Armed at key-up, never reset per frame, so it bounds a *continuous* keyed over
#: (a lost end frame / a decode emitting continuous garbage) that the per-frame idle deadline and the
#: level gate can't close. Chosen below the global TOT (`DEFAULT_TX_TOT` 180 s, ADR 0090) so a runaway
#: over closes here first; 0 disables. Marked tunable default (guardrail 1): long enough not to clip a
#: legitimate long over, short enough that junk can't sit on the air.
DEFAULT_DSTAR_MAX_OVER = 60.0

#: Seconds a keyed reflector→RF over may carry gate-failing (dead-air) decode before it is cut
#: (ADR 0106). This is the CONTENT-silence bound, deliberately much longer than speech-pause
#: timescale: over liveness itself follows frame ARRIVAL (`TxSession.touch`), so a talker's pause
#: keeps the carrier up like a real repeater, and this bound only reaps a stream that keeps
#: sending frames but has decoded to nothing for a long time (a lost end-bit trickle, garbage
#: silence). Sits well below `max_over`/TOT. 0 disables. Marked tunable default (guardrail 1).
DEFAULT_DSTAR_DEAD_AIR = 10.0

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
        rx_gate: Callable[[AudioFrame], bool] | None = None,
        max_over: float = 0.0,
        dead_air: float = DEFAULT_DSTAR_DEAD_AIR,
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
        # Per-frame level gate on the DECODED reflector→RF audio (ADR 0097/0106). The gate governs
        # *keying and feeding*: only frames it passes reach `TxSession.feed`, so dead air never keys a
        # fresh over. It no longer governs *liveness* — that follows frame ARRIVAL (`TxSession.touch`
        # in the DATA branch), because content-based liveness cut the over at every speech pause and
        # chopped real voice into phrase-sized fragments (ADR 0106, bench 2026-07-20: 43 s of speech
        # arrived as 9 overs with the inter-phrase frames discarded). The stuck-key cases content
        # liveness existed for are covered by `dead_air` (below) + `max_over` + the TOT.
        # None = ungated (a bare bridge in tests keeps the old shape). Symmetric with `_rf_gate`.
        self._rx_gate = rx_gate
        # Absolute ceiling (seconds) on a single reflector→RF over, armed at key-up and — unlike the idle
        # deadline — NOT reset per frame (ADR 0097): the content-independent backstop for a continuous
        # stream the level gate can't idle out (loud garbage). Sits below the global TOT (ADR 0090), so a
        # runaway over closes here first. 0 disables. Verify on the bench (guardrail 1).
        self._max_over = max_over
        self._rx_over_deadline: float | None = None
        # Content-silence bound (ADR 0106): a keyed over whose decode has not passed the gate for this
        # many seconds is cut even though frames keep arriving — the dead-air/garbage reaper that
        # replaced content-based liveness, at a timescale (10 s default) no speech pause reaches.
        self._dead_air = dead_air
        self._last_gate_pass = 0.0
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
        # Per-over ordered decode stream (ADR 0098) when the vocoder supports it — fixes the pipelined
        # AMBE2000's dropped/mis-ordered frames that garbled the crossband. `_decode_streaming` is set
        # from the vocoder's capability on start; None stream = between overs (or a non-streaming fake).
        self._decode_streaming = False
        self._rx_decode_stream = None
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
        self._rx_arbiter_conflicts = 0  # decoded frames dropped because the shared arbiter was held
        self._rx_reheaders = 0  # same-stream header re-sends absorbed without cutting the over (ADR 0105)
        self._rx_stream_id: int | None = None  # DSRP session id of the over currently inbound
        # ADR 0106 — cut-cause + continuity observability. `_last_rx_stream_id` outlives a mid-stream
        # cut (idle/timeout/dead-air/watchdog) so a still-flowing stream can re-latch from its next
        # DATA frame instead of waiting for a gateway re-header (which dropped up to ~0.5 s of voice
        # per cut); it is cleared on a genuine end (end-bit) and on teardown so trailing frames of a
        # finished stream can never ghost-key a new over.
        self._last_rx_stream_id: int | None = None
        # Enqueue-side seq continuity (ADR 0107): tracked BEFORE the queue can drop, so
        # `rx_seq_lost` is true upstream loss and `rx_queue_drops` is our own backpressure.
        self._enq_stream_id: int | None = None
        self._enq_prev_seq: int | None = None
        self._rx_seq_lost = 0  # frames missing from DSRP seq discontinuities (network loss upstream)
        self._rx_queue_drops = 0  # inbound messages shed by the bounded queue's drop-oldest (local)
        # Decode throughput probe accumulators (ADR 0107) — logged every 500 decodes.
        self._decode_probe_ms = 0.0
        self._decode_probe_n = 0
        self._rx_relatches = 0  # overs reopened from a DATA frame after a mid-stream cut
        self._rx_idle_cuts = 0  # overs cut because frames stopped arriving (or the loop was parked)
        self._rx_stream_cuts = 0  # overs cut by the queue-quiet timeout (stream death, no end-bit)
        self._rx_dead_air_cuts = 0  # overs cut by the content-silence bound (`dead_air`)
        # Set while decodes are RAISING (ADR 0092/0106): arriving frames then stop refreshing the
        # over's liveness, so a wedged-but-still-fed vocoder cannot hold a dead carrier keyed.
        self._rx_decode_failing = False
        # Per-over log-line bases, snapshotted at over open (ADR 0106).
        self._rx_over_opened = 0.0
        self._rx_over_frame_base = 0
        self._rx_over_reheader_base = 0
        self._rx_over_seq_base = 0
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
            # The SHARED radio arbiter's view alongside the bridge's own latch (ADR 0102): the two
            # can legitimately diverge (browser TX holds the arbiter while the bridge idles), and
            # during the 2026-07-20 jam the divergence was the diagnosis — status said idle while
            # the arbiter was stuck transmitting. Surfacing both closes that observability gap.
            "arbiter": str(self._arbiter.mode),
            "rx_frames": self._rx_frames,
            "rx_overs": self._rx_overs,
            "rx_dropped_busy": self._rx_dropped_busy,
            "rx_arbiter_conflicts": self._rx_arbiter_conflicts,
            "rx_reheaders": self._rx_reheaders,
            # ADR 0106 — the cut-cause/continuity ledger. Together these localize any remaining
            # chop numerically: seq_lost ⇒ frames never arrived (network); cuts+relatches ⇒ the
            # bridge fragmented a live stream; all-zero with one over per key-up ⇒ downstream.
            "rx_seq_lost": self._rx_seq_lost,
            "rx_queue_drops": self._rx_queue_drops,
            "rx_relatches": self._rx_relatches,
            "rx_idle_cuts": self._rx_idle_cuts,
            "rx_stream_cuts": self._rx_stream_cuts,
            "rx_dead_air_cuts": self._rx_dead_air_cuts,
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
        # Use the ordered streaming-decode path (ADR 0098) if this vocoder offers it; a plain Vocoder
        # (a test fake, a non-pipelined codec) falls back to the legacy per-frame decode unchanged.
        self._decode_streaming = isinstance(vocoder, StreamingVocoder)
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
                # Independent PTT-safety watchdog (ADR 0092). The `_reflector_to_rf` loop can PARK in
                # a blocking vocoder decode (`run_in_executor`), and while parked its own loop-top
                # idle check can never run — so a wedged decode would hold PTT keyed until the TOT
                # (the real-hardware stuck-key). This task lives on the event loop, never in the
                # executor, so it keeps checking the idle deadline and drops PTT even while the decode
                # loop is parked.
                self._tasks.append(asyncio.create_task(self._rx_watchdog()))
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

        Ordered so PTT can never survive a teardown (ADR 0091/0099): the reflector→RF loop can be parked
        in a blocking vocoder decode, and the vocoder's ``close()`` can itself take seconds (its
        ``_io_lock`` may be held by a live ``_recover``). So (1) drop PTT DIRECTLY via ``_force_unkey``
        FIRST — a codec-independent ``radio.ptt(False)`` that never waits on the vocoder; then (2) wake a
        parked decode/encode by closing the vocoder, but OFF the event loop with a bounded wait so a
        slow/wedged ``close()`` can never stall SIGTERM, the joins, or the (already done) unkey; then
        (3) bound each task join so a still-wedged worker can never hang teardown. Before ADR 0099 the
        vocoder was closed first, synchronously on the loop, which stalled the unkey ~15 s behind a
        wedged ``_recover`` — the transmitter stayed keyed until SIGKILL (the re-proof stuck-key).
        """
        # (1) The load-bearing unkey — first, direct, independent of the (possibly parked/wedged) vocoder.
        self._force_unkey()
        for task in self._tasks:
            task.cancel()
        # (2) Unblock a parked decode/encode so the cancel is deliverable — but never on the event-loop
        # thread: run close() in the default executor and bound it, so a wedged close() cannot stall us.
        vocoder = self._vocoder
        if vocoder is not None:
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(None, vocoder.close),
                    timeout=self._tx_hang + 2.0,
                )
        # (3) Join, but never wait forever on a worker still wedged in the executor — and join
        # CONCURRENTLY under ONE bound (ADR 0104): the previous per-task sequential waits compounded
        # to 4 x (tx_hang + 2 s) ≈ 12 s of worst-case stop budget, overran the service unit's
        # TimeoutStopSec, and the resulting SIGKILL severed the DV Dongle mid-operation (the wedge).
        if self._tasks:
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
                await asyncio.wait_for(
                    asyncio.gather(*self._tasks, return_exceptions=True),
                    timeout=self._tx_hang + 2.0,
                )
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
        self._close_decode_stream()  # drop any open per-over decode stream (ADR 0098)
        if self._rx_session is not None:
            with contextlib.suppress(Exception):
                self._rx_session.close()  # radio.ptt(False) + arbiter.release_tx()
            if self._rx_slot_held:
                with contextlib.suppress(Exception):
                    self._tx_slot.release()
                self._rx_slot_held = False
            self._rx_session = None
        self._rx_over_deadline = None  # disarm the hard per-over cap (ADR 0097)
        # Belt-and-suspenders (ADR 0092): drop PTT DIRECTLY, independent of the session-close path.
        # Even with `TxSession.close` hardened to always unkey, teardown must never depend on it — a
        # direct `ptt(False)` here is the last, unconditional guarantee the transmitter is unkeyed.
        with contextlib.suppress(Exception):
            self._radio.ptt(False)
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
        """On-loop: bounded drop-oldest enqueue of an inbound reflector message.

        Sequence continuity is tracked HERE, before the queue can drop anything (ADR 0107):
        `rx_seq_lost` then measures ONLY frames the network never delivered. Counting it after the
        queue (as ADR 0106 first did) booked our own drop-oldest as "upstream loss" — the counter
        meant to separate network from local conflated them. Our own backpressure is counted
        separately as `rx_queue_drops`.
        """
        if msg.kind is dsrp.MessageKind.DATA:
            if msg.session_id != self._enq_stream_id:
                self._enq_stream_id = msg.session_id
                self._enq_prev_seq = None  # a different stream: seq continuity restarts
            if msg.end:
                # The end frame carries its own sequence handling (g4klx) — a "gap" to it would book
                # phantom loss on every clean over end; it also terminates the stream's continuity.
                self._enq_prev_seq = None
            else:
                prev, self._enq_prev_seq = self._enq_prev_seq, msg.seq_no
                if prev is not None:
                    self._rx_seq_lost += (msg.seq_no - prev - 1) % (dsrp.SEQ_MAX + 1)
        queue = self._rx_queue
        if queue is None:
            return
        try:
            queue.put_nowait(msg)
        except asyncio.QueueFull:
            self._rx_queue_drops += 1  # our own backpressure, named as such (ADR 0107)
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(msg)

    async def _rx_watchdog(self) -> None:
        """The load-bearing PTT-safety watchdog (ADR 0092), run as its OWN task so it can drop PTT
        even when ``_reflector_to_rf`` is parked in a blocking vocoder decode.

        The ADR 0091 idle check lives at the top of the ``_reflector_to_rf`` loop, but that loop
        ``await``s each decode in the single-worker vocoder executor — and a wedged DV Dongle decode
        parks it there far longer than ``tx_hang``. While parked, the loop-top check never runs, so
        the over never closes and the transmitter sits keyed (dead air) until the TOT — the exact
        real-hardware stuck-key the dummy-load test exposed. This task only ever ``await``s
        ``asyncio.sleep`` (never the executor), so the event loop keeps scheduling it while the decode
        loop is parked; it closes an idle keyed over directly. A healthy over refreshes the deadline
        on every fed frame (``TxSession.idle_elapsed``), so it is never cut short.
        """
        interval = max(self._tx_hang / 2.0, 0.05)
        while True:
            await asyncio.sleep(interval)
            if self._rx_session is not None and (
                self._rx_session.idle_elapsed()
                or self._over_expired()
                or self._dead_air_elapsed()
            ):
                self._end_rx("watchdog")

    def _over_expired(self) -> bool:
        """True iff a hard per-over cap is armed and this reflector→RF over has run past it (ADR 0097).

        Content-independent and NOT reset per frame (the idle deadline is) — so it bounds a *continuous*
        inbound stream the level gate can't idle out (loud garbage that keeps the gate open). The
        absolute ceiling, set below the global TOT (ADR 0090) so a runaway over closes here first."""
        return self._rx_over_deadline is not None and self._clock() >= self._rx_over_deadline

    def _dead_air_elapsed(self) -> bool:
        """True iff a keyed over's decode has not passed the level gate for ``dead_air`` seconds.

        The ADR 0106 content-silence bound: liveness itself follows frame arrival, so this is the
        only content-based cut left — at a timescale (default 10 s) no speech pause reaches, it
        reaps a stream that keeps sending frames but has decoded to nothing (a lost end-bit
        trickle, garbage silence) without holding PTT to the over-cap/TOT."""
        return (
            self._dead_air > 0
            and self._rx_session is not None
            and self._clock() - self._last_gate_pass >= self._dead_air
        )

    async def _reflector_to_rf(self) -> None:
        assert self._rx_queue is not None
        try:
            while True:
                # In-loop PTT checks (ADR 0091/0106): `idle_elapsed` now measures frame ARRIVAL
                # (`touch` in the DATA branch), so it means the stream stopped (or this loop was
                # parked in a wedged decode) — never a talker's pause. The over-cap and dead-air
                # bounds are the content-independent/content-silence backstops. Cut WITH the tail
                # flushed — these are live-audio paths, not teardown.
                if self._rx_session is not None:
                    if self._over_expired():
                        await self._flush_and_end_rx("over-cap")
                    elif self._rx_session.idle_elapsed():
                        await self._flush_and_end_rx("idle")
                    elif self._dead_air_elapsed():
                        await self._flush_and_end_rx("dead-air")
                try:
                    msg = await asyncio.wait_for(self._rx_queue.get(), timeout=self._tx_hang)
                except asyncio.TimeoutError:
                    # Inbound went quiet past the hang: stream death (or a lost end-bit) — close
                    # the over with its decode tail flushed rather than stranded (ADR 0106).
                    await self._flush_and_end_rx("stream-quiet")
                    continue

                if msg.kind is dsrp.MessageKind.HEADER:
                    # An inbound stream header. Take the latch unless RF is outbound to the reflector.
                    if self._mode == "tx":
                        self._rx_dropped_busy += 1
                        continue
                    if self._mode == "rx" and msg.session_id == self._rx_stream_id:
                        # The gateway RE-SENDS the stream header periodically mid-stream (late-join
                        # resync). Treating each re-send as a new over cut the live session every
                        # ~0.7 s — unkey (the pacer discards its queue), re-key, a fresh 0.5 s TX
                        # lead-in — so FM transmitted almost pure lead-in silence while the browser
                        # lost each cut's stranded decode tail: the "12-fragment shredder"
                        # (ADR 0105, bench 2026-07-20: ONE key-up arrived as 12 overs / 186 frames).
                        # Same session id while an over is open = the SAME over: absorb it.
                        self._rx_reheaders += 1
                        continue
                    # A genuinely new stream (or no over open). Close any prior over first — WITH the
                    # decode-pipeline tail flushed onto RF; the bare _end_rx here stranded the last
                    # ~latency frames of the old stream (ADR 0105). _flush_and_end_rx no-ops cleanly
                    # when nothing is open.
                    await self._flush_and_end_rx("new-stream")
                    self._latch_over(msg.session_id)
                    self._report_activity(msg.radio_header, "rx")  # who's talking on the reflector
                    continue

                if msg.kind is dsrp.MessageKind.DATA:
                    if self._mode == "tx":
                        self._rx_dropped_busy += 1
                        continue
                    if self._mode == "idle":
                        # A mid-stream cut (idle/stream-quiet/dead-air/watchdog) closed the over while
                        # the stream kept flowing. Re-latch from the DATA frame itself instead of
                        # waiting for the gateway's next ~0.5 s re-header — that wait discarded every
                        # frame in between (bench 2026-07-20: 27 voice frames binned in one test run,
                        # an audible hole at each phrase boundary; ADR 0106). Only the SAME stream id
                        # may re-latch — a genuine end-bit cleared it, so a finished stream can never
                        # ghost-key a new over from trailing frames — and only while the decode
                        # pipeline is healthy: after a failing-decode cut (ADR 0092), resumption
                        # waits for the next HEADER, whose fresh decode stream may recover.
                        if msg.session_id != self._last_rx_stream_id or self._rx_decode_failing:
                            self._rx_dropped_busy += 1
                            continue
                        self._latch_over(msg.session_id)
                        self._rx_relatches += 1
                    await self._play_ambe(dsrp.voice_frame(msg.dv_frame))
                    if self._rx_session is not None and not self._rx_decode_failing:
                        # Liveness follows the PROCESSED stream (ADR 0106): a frame that arrived and
                        # decoded refreshes the idle deadline even when its content is below the gate
                        # (a talker's pause), so pauses no longer cut the over — the refresh moved
                        # here from `feed`. Two escapes keep the ADR 0092 stuck-key contract: a
                        # wedged decode PARKS this loop before this line (stamps stop, the watchdog
                        # fires), and a RAISING decode sets `_rx_decode_failing` (this frame does not
                        # refresh, so idle fires within ~tx_hang).
                        self._rx_session.touch()
                    if msg.end:
                        await self._flush_and_end_rx("end")  # drain the tail, then close the over
        finally:
            self._end_rx()

    def _latch_over(self, session_id: int) -> None:
        """Latch an inbound over: mode, stream ids, decode pipeline, counters, per-over log bases."""
        self._mode = "rx"
        self._rx_stream_id = session_id
        self._last_rx_stream_id = session_id
        self._open_decode_stream()  # fresh ordered decode pipeline for this over (ADR 0098)
        self._rx_decode_failing = False  # a fresh over/pipeline gets a clean liveness slate
        self._rx_overs += 1
        self._rx_over_opened = self._clock()
        self._rx_over_frame_base = self._rx_frames
        self._rx_over_reheader_base = self._rx_reheaders
        self._rx_over_seq_base = self._rx_seq_lost

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
            # Arm the hard per-over ceiling (ADR 0097) — content-independent, never reset per frame.
            self._rx_over_deadline = (
                self._clock() + self._max_over if self._max_over > 0 else None
            )
        return self._rx_session

    def _open_decode_stream(self) -> None:
        """Open a fresh ordered decode stream for this over if the vocoder supports it (ADR 0098)."""
        if self._decode_streaming and self._rx_decode_stream is None and self._vocoder is not None:
            with contextlib.suppress(Exception):
                self._rx_decode_stream = self._vocoder.open_decode_stream()

    def _close_decode_stream(self) -> None:
        """Close and drop the per-over decode stream (no tail flush — the safety/teardown path)."""
        stream = self._rx_decode_stream
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.close()
            self._rx_decode_stream = None

    async def _decode_frames(self, ambe: bytes) -> list[AudioFrame]:
        """Decode one AMBE frame to 0..n ordered 8 kHz PCM frames.

        Streaming (ADR 0098) yields ordered frames from the pipeline FIFO (empty while priming, or a
        small burst once primed); the legacy per-frame path yields exactly one. Runs on the single
        vocoder executor either way."""
        stream = self._rx_decode_stream
        started = self._clock()
        if stream is not None:
            out = await self._loop.run_in_executor(self._vox_pool, stream.decode, ambe)
        else:
            out = [await self._decode(ambe)]
        # Permanent lightweight throughput probe (ADR 0107): the bench reads sustained decode cost
        # straight from the journal. Anything approaching 20 ms/frame means the queue is filling.
        self._decode_probe_ms += (self._clock() - started) * 1000.0
        self._decode_probe_n += 1
        if self._decode_probe_n >= 500:
            queue = self._rx_queue
            log.info(
                "dstar: decode probe: avg %.1f ms/frame over %d frames, rx_queue depth %d, queue_drops %d",
                self._decode_probe_ms / self._decode_probe_n,
                self._decode_probe_n,
                queue.qsize() if queue is not None else -1,
                self._rx_queue_drops,
            )
            self._decode_probe_ms = 0.0
            self._decode_probe_n = 0
        return out

    async def _play_ambe(self, ambe: bytes) -> None:
        """Decode one AMBE frame and key the resulting (0..n, ordered) PCM frames onto RF."""
        try:
            pcm_frames = await self._decode_frames(ambe)
            # A completed decode (even an empty priming burst) is a healthy pipeline: arrivals may
            # refresh the over's liveness again (ADR 0106 — see the touch site in the drain loop).
            self._rx_decode_failing = False
        except Exception:
            # A RAISING decode is not a quiet decode (ADR 0092/0106): while decodes fail, arriving
            # DATA must not keep the over alive, or a wedged vocoder holds a dead carrier keyed —
            # the stuck-key incident. The touch site checks this flag; liveness then starves and
            # the idle watchdog unkeys within ~tx_hang, exactly the pre-0106 contract.
            self._rx_decode_failing = True
            log.exception("dstar: AMBE decode failed")
            return
        for pcm8 in pcm_frames:
            self._emit_rx_pcm(pcm8)

    def _emit_rx_pcm(self, pcm8: AudioFrame) -> None:
        """Resample one decoded 8 kHz frame to canonical and key it onto RF (+ the monitor hub)."""
        # A slow/wedged decode can park us long enough that the independent PTT watchdog (or a teardown)
        # already closed this over. Feeding now would RE-KEY the transmitter right after it was safely
        # unkeyed — the stuck-key by another name. Drop the stale frame instead (ADR 0092).
        if self._mode != "rx":
            return
        frame48 = to_canonical(pcm8)
        self._rx_frames += 1
        # The browser monitor hears the raw decode either way — publish before the content gate so the
        # listen path is unchanged (and an operator can still hear a garbled decode to diagnose it).
        if self._dstar_rx_hub is not None:
            self._dstar_rx_hub.publish(frame48.samples)
        # The gate governs KEYING and FEEDING, not liveness (ADR 0106): a below-threshold (dead-air /
        # garbage-silence) frame does not feed the session, so silence never keys a fresh over — but the
        # over's idle deadline follows frame ARRIVAL (`touch` in the drain loop), so a talker's pause no
        # longer cuts it. The gate-pass clock stamped here drives the `dead_air` bound instead: long
        # content silence on a keyed over still gets reaped. Ungated bridges feed as before.
        if self._rx_gate is not None and not self._rx_gate(AudioFrame(frame48.samples, CANONICAL_FORMAT)):
            return
        self._last_gate_pass = self._clock()
        session = self._open_rx_session()
        try:
            session.feed(frame48.samples)
        except AudioFormatMismatch:
            pass
        except ArbiterStateError:
            # The shared arbiter is held by another keyer (or stuck from an earlier fault). An
            # unhandled raise here would kill the drain loop — the whole crossband — over one
            # contended frame. Drop the frame and count it instead; `feed` re-attempts the key-up
            # per frame, so the over keys up on its own the moment the arbiter frees (ADR 0102).
            self._rx_arbiter_conflicts += 1
            if self._rx_arbiter_conflicts == 1 or self._rx_arbiter_conflicts % 250 == 0:
                log.warning(
                    "dstar: arbiter busy (%s) — reflector audio dropped (%d conflicts so far)",
                    self._arbiter.mode,
                    self._rx_arbiter_conflicts,
                )

    async def _flush_and_end_rx(self, cause: str = "end") -> None:
        """Clean over end: drain the decode pipeline's tail onto RF, then close the over (ADR 0098)."""
        stream = self._rx_decode_stream
        if stream is not None and self._mode == "rx":
            try:
                tail = await self._loop.run_in_executor(self._vox_pool, stream.flush)
            except Exception:
                log.exception("dstar: decode flush failed")
                tail = []
            for pcm8 in tail:
                self._emit_rx_pcm(pcm8)
        self._end_rx(cause)

    def _end_rx(self, cause: str = "teardown") -> None:
        """Close the reflector→RF session, release the slot (only if we hold it), and drop the latch.

        ``cause`` is the ADR 0106 cut ledger: ``"end"`` (end-bit), ``"new-stream"`` (a different
        talker's header took over), ``"idle"`` (arrivals stopped / loop parked), ``"stream-quiet"``
        (queue timeout), ``"dead-air"`` (content-silence bound), ``"over-cap"`` (ADR 0097 ceiling),
        ``"watchdog"`` (ADR 0092 safety task), ``"teardown"``.
        """
        was_open = self._mode == "rx" or self._rx_session is not None
        self._close_decode_stream()
        if self._rx_session is not None:
            with contextlib.suppress(Exception):
                self._rx_session.close()
            if self._rx_slot_held:
                self._tx_slot.release()
                self._rx_slot_held = False
            self._rx_session = None
        self._rx_over_deadline = None  # disarm the hard per-over cap (ADR 0097)
        self._rx_stream_id = None  # a later same-id header re-latches as a fresh over (ADR 0105)
        if cause in ("end", "teardown"):
            # A finished (or torn-down) stream must never re-latch from trailing DATA frames —
            # only mid-stream cuts keep the id so a still-flowing stream can resume (ADR 0106).
            self._last_rx_stream_id = None
        if self._mode == "rx":
            self._mode = "idle"
        if not was_open:
            return
        if cause in ("idle", "watchdog"):
            self._rx_idle_cuts += 1
        elif cause == "stream-quiet":
            self._rx_stream_cuts += 1
        elif cause == "dead-air":
            self._rx_dead_air_cuts += 1
        # One line per over for the bench: how long, how continuous, and why it ended.
        log.info(
            "dstar: over closed (%s): %d frames / %.1f s, %d reheaders, %d seq-lost",
            cause,
            self._rx_frames - self._rx_over_frame_base,
            self._clock() - self._rx_over_opened,
            self._rx_reheaders - self._rx_over_reheader_base,
            self._rx_seq_lost - self._rx_over_seq_base,
        )

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
