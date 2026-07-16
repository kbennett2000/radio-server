"""The RF <-> Mumble bridge: the peer state machine (ADR 0041).

This wires a :class:`~radio_server.link.client.MumbleClient` to the RF radio using the seams that
already exist — it invents no new RX or TX mechanism:

- **RF -> Mumble.** Subscribe to the shared :class:`~radio_server.rx.hub.AudioHub` exactly like a
  browser ``/audio/rx`` listener; a loop task forwards each frame to ``mumble.send_audio``. The hub
  only ever carries gate-open, non-empty PCM, so the bridge relays squelched audio, and it goes
  quiet while the radio transmits (the pump skips ``receive()`` under the arbiter). The bridge holds
  an rx *demand* (``acquire_rx``/``release_rx``) so the shared pump runs even with no browser
  connected. When a :class:`~radio_server.link.mute.DtmfMuteGate` is injected (ADR 0045), this feed
  runs a short delay line behind live so decoded DTMF digits retroactively condemn the buffered
  tone — control tones never reach Mumble listeners.
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
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Awaitable, Callable

from ..audio import CANONICAL_FORMAT, AudioFormatMismatch
from ..backends import Radio
from ..tx import TxIdentifier, TxSession, TxSlot
from .client import DEFAULT_MUMBLE_TX_HANG, MumbleClient, MumbleStatus
from .mute import DtmfMuteGate

#: A clock returns seconds as a float (``time.monotonic`` by default) — injectable so the hang timer
#: and station-ID due-checks are exactly testable with a fake clock.
Clock = Callable[[], float]

#: Bounded hand-off queue depth for received Mumble voice (~1.3 s at 20 ms framing) before
#: drop-oldest, mirroring the audio hub / multimon write queue.
DEFAULT_TX_QUEUE_MAXSIZE = 64

#: Seconds of RF audio the bridge holds back from Mumble when DTMF mute is on (ADR 0045). Must
#: exceed multimon-ng's worst-case tone-onset→digit-report latency (detection needs ~40-100 ms of
#: tone, plus the ADR 0040 write-queue hop and the pump's next `receive()`), or the start of each
#: tone leaks to Mumble before the digit condemns the buffer. VERIFY AGAINST HARDWARE (guardrail
#: 1): bench-check with an HT dialing digits; bump to 0.4 if a leading blip is audible. The cost
#: is exactly this much added RF→Mumble latency.
DEFAULT_DTMF_MUTE_DELAY = 0.3


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
        mute_delay: float = DEFAULT_DTMF_MUTE_DELAY,
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
        # DTMF mute (ADR 0045): when a gate is injected, the RF→Mumble feed runs `mute_delay`
        # seconds behind live so a decoded digit can retroactively condemn the buffered tone.
        # None (the default) keeps the original zero-latency relay.
        self._dtmf_mute = dtmf_mute
        self._mute_delay = mute_delay
        self._delay_bytes = int(mute_delay * CANONICAL_FORMAT.rate * CANONICAL_FORMAT.frame_bytes)

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
        self._overs_keyed = 0

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
        """Mumble→RF counters for ``GET /link/status`` (ADR 0045).

        ``frames_in`` counts every frame received from Mumble; ``dropped_rx_active`` those dropped
        deferring to a live RF signal; ``dropped_slot_busy`` those dropped because the browser
        talker held the slot; ``overs_keyed`` how many transmissions the bridge keyed.
        """
        return {
            "frames_in": self._frames_in,
            "dropped_rx_active": self._dropped_rx_active,
            "dropped_slot_busy": self._dropped_slot_busy,
            "overs_keyed": self._overs_keyed,
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
        if self._tx_to_rf:
            self._tx_queue = asyncio.Queue(maxsize=self._tx_queue_maxsize)
            self._tasks.append(asyncio.create_task(self._mumble_to_rf()))
        self._running = True

    async def stop(self) -> None:
        """Cancel the task(s), release the rx demand + talker slot, and disconnect. Idempotent."""
        if not self._running:
            return
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
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

    async def _rx_to_mumble(self) -> None:
        assert self._rx_sub is not None
        if self._dtmf_mute is None:
            # No DTMF mute: the original zero-latency relay, byte for byte.
            while True:
                frame = await self._rx_sub.get()
                self._send_to_mumble(frame)
            return
        # DTMF mute (ADR 0045): run `mute_delay` seconds behind live. Frames sit in a delay line;
        # a decoded digit condemns everything buffered (the tone onset was published before
        # multimon could report it) and holds the feed muted. The idle flush is load-bearing: a
        # real squelch gate stops publishing between overs, and without the timeout the tail of
        # every over (up to `delay_bytes`) would stall here and play stale at the next over.
        buffer: deque[bytes] = deque()
        buffered = 0
        while True:
            try:
                frame = await asyncio.wait_for(self._rx_sub.get(), timeout=self._mute_delay)
            except asyncio.TimeoutError:
                # Hub went quiet for a full delay window: whatever is buffered is either older
                # than any digit still undecoded (safe to send) or already condemned.
                if self._dtmf_mute.muted():
                    buffer.clear()
                    buffered = 0
                else:
                    while buffer:
                        self._send_to_mumble(buffer.popleft())
                    buffered = 0
                continue
            if self._dtmf_mute.muted():
                buffer.clear()
                buffered = 0
                continue
            buffer.append(frame)
            buffered += len(frame)
            # Forward the surplus beyond the delay window, oldest first.
            while buffer and buffered - len(buffer[0]) >= self._delay_bytes:
                head = buffer.popleft()
                buffered -= len(head)
                self._send_to_mumble(head)

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
        loop.call_soon_threadsafe(self._enqueue_tx, pcm)

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
                if session is None:
                    # Share the single talker slot with the browser; refuse (drop) if it's busy.
                    if not self._tx_slot.try_acquire():
                        self._dropped_slot_busy += 1
                        continue
                    self._overs_keyed += 1
                    session = TxSession(
                        self._radio,
                        idle_timeout=self._tx_hang,
                        arbiter=self._arbiter,
                        station_id=self._station_id,
                        clock=self._clock,
                    )
                try:
                    session.feed(pcm)
                except AudioFormatMismatch:
                    # A malformed frame from Mumble: end this over rather than key on garbage.
                    session = self._end_session(session)
        finally:
            # Cancellation (stop) or any exit must drop PTT and free the slot.
            self._end_session(session)

    def _end_session(self, session: TxSession | None) -> None:
        """Close a keyed session and release the talker slot; returns ``None`` for reassignment."""
        if session is not None:
            session.close()
            self._tx_slot.release()
        return None
