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
from .header import build_voice_header

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

#: How many NULL_AMBE data frames a link/unlink command burst carries between its header and the
#: end frame. Default 0 (header + terminator only) — the gateway latches routing off the header.
#: Marked tunable default (guardrail 1): if a bare header does not trigger the gateway's link logic,
#: raise this up the fallback ladder (a few silence frames → a full 21-frame superframe) without a
#: code change. See ADR 0088.
DEFAULT_COMMAND_FRAMES = 0


class DStarBridge:
    """Bridge RF audio to/from a D-STAR reflector via the gateway (ADR 0087). A pure DI object.

    Start/stop are idempotent (mirroring the Mumble bridge). ``start`` opens the gateway client,
    subscribes to the audio hub, raises an rx demand, and launches the drain/encode tasks; ``stop``
    cancels them, releases the demand + slot, and closes the client and vocoder executor.
    """

    def __init__(
        self,
        gateway: GatewayClient,
        radio: Radio,
        vocoder: Vocoder,
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
    ) -> None:
        if clock is None:
            import time

            clock = time.monotonic
        self._gateway = gateway
        self._radio = radio
        self._vocoder = vocoder
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

        self._running = False
        # The half-duplex latch: "idle" | "rx" (reflector inbound) | "tx" (RF outbound). Mutated only
        # on the event loop, so cooperative scheduling keeps it consistent without a lock.
        self._mode = "idle"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._rx_sub = None
        self._rx_queue: asyncio.Queue[dsrp.DsrpMessage] | None = None
        self._rx_session: TxSession | None = None  # the open reflector→RF keying session, if any
        self._tasks: list[asyncio.Task] = []
        self._vox_pool: ThreadPoolExecutor | None = None
        # Outbound (RF→reflector) reframing buffer of 8 kHz PCM and the running session id / sequence.
        self._tx_pcm = bytearray()
        self._tx_session_id = 0
        self._tx_seq = 0
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
        """Open the gateway client, subscribe to RF audio, and launch the bridge tasks. Idempotent."""
        if self._running:
            return
        self._loop = asyncio.get_running_loop()
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
        self._running = True

    async def stop(self) -> None:
        """Cancel the tasks, release the demand + slot, and close the client/executor. Idempotent."""
        if not self._running:
            return
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
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
        self._mode = "idle"

    async def _encode(self, frame: AudioFrame) -> bytes:
        assert self._vox_pool is not None
        return await self._loop.run_in_executor(self._vox_pool, self._vocoder.encode, frame)

    async def _decode(self, ambe: bytes) -> AudioFrame:
        assert self._vox_pool is not None
        return await self._loop.run_in_executor(self._vox_pool, self._vocoder.decode, ambe)

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
            self._tx_slot.try_acquire()
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
        """Close the reflector→RF session, release the slot, and drop the latch."""
        if self._rx_session is not None:
            self._rx_session.close()
            self._tx_slot.release()
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
                    self._end_tx()
                    continue
                # Defer to an inbound reflector stream — one talker at a time.
                if self._mode == "rx":
                    self._tx_dropped_busy += 1
                    continue
                if self._mode == "idle":
                    self._open_tx()
                await self._feed_rf(pcm)
        finally:
            self._end_tx()

    def _alloc_session_id(self) -> int:
        """A per-stream session id in 1..0xFFFF (0 is reserved), derived without a clock so tests
        are deterministic: a running counter, wrapped."""
        self._session_counter = (self._session_counter + 1) % 0xFFFF
        return self._session_counter + 1

    def _open_tx(self) -> None:
        """Open an outbound reflector stream: latch TX, send the header, reset the sequence."""
        self._mode = "tx"
        self._tx_overs += 1
        self._tx_session_id = self._alloc_session_id()
        self._tx_seq = 0
        self._tx_pcm = bytearray()
        header = build_voice_header(callsign=self._callsign, module=self._module, ur=self._ur_call)
        self._gateway.send_header(header, self._tx_session_id)

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

    def _end_tx(self) -> None:
        """Close an outbound reflector stream with the terminating (null-AMBE, end-bit) frame."""
        if self._mode != "tx":
            return
        end_dv = dsrp.build_dv_frame(dsrp.NULL_AMBE, dsrp.slow_data_for_seq(self._tx_seq))
        self._gateway.send_data(end_dv, self._tx_session_id, self._tx_seq, end=True)
        self._tx_pcm = bytearray()
        self._mode = "idle"

    # --- Browser operator -> reflector ---------------------------------------------------
    # The browser-mic TX source (the D-STAR twin of ``MumbleBridge.send_operator_audio``). It drives
    # the SAME encode→DSRP outbound pipeline as ``_rf_to_reflector`` but sourced from the WebSocket
    # instead of the RF ``audio_hub``. The WS endpoint owns the over: the first frame opens it and a
    # WS idle-timeout/disconnect calls ``end_operator_over``. **Must be awaited serially** (one
    # ``dstar_talk_slot`` holder) — concurrent calls would corrupt the shared ``_tx_*`` reframe state.
    # Do NOT run this while ``rx_to_reflector`` is also live (``_rf_to_reflector`` would fight for the
    # same TX latch/state); the dedicated browser instance sets ``rx_to_reflector=False``.

    async def send_operator_audio(self, pcm48: bytes) -> None:
        """Feed one canonical (48 kHz) browser-mic frame toward the reflector, opening the over lazily.

        Drops the frame (counted) if a reflector stream currently holds the RX latch — one talker at a
        time, and the vocoder is busy decoding inbound audio (the ADR 0086 no-interleave rule).
        """
        if self._mode == "rx":
            self._tx_dropped_busy += 1
            return
        if self._mode == "idle":
            self._open_tx()
        await self._feed_rf(pcm48)

    def end_operator_over(self) -> None:
        """Close the browser operator's outbound over (the terminator). A no-op unless mode is TX."""
        self._end_tx()

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
        if self._mode != "idle":
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
