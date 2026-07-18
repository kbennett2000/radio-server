"""TX pacer for the kv4p HT (ADR 0082) — keeps the transmitter fed while keyed-but-idle.

The kv4p is a **frame-push** backend: :meth:`Kv4pHt.transmit` sends an Opus frame only when it is
called, and nothing goes out between calls. The AIOC backend, by contrast, opens a *continuous*
sounddevice output stream that emits silence whenever ``transmit`` is not writing
(:mod:`..aioc_baofeng`), so the radio's TX audio buffer is never starved while keyed. That gap is
the "Max Headroom" bug: over Mumble, the bridge holds PTT across the ``mumble.tx_hang`` quiet window
(``link/bridge.py``) but stops delivering audio, so the kv4p sends nothing while still keyed — the
firmware's TX buffer underruns and the SA818 loops its last ~0.5 s of speech.

This pacer gives the kv4p the AIOC's contract: **while keyed, a continuous frame stream reaches the
radio — real audio when available, encoded silence otherwise.** It is a *single coherent sender* so
it can never race the flow-control window or double a frame: during a held key the pacer thread is
the *only* thing that pushes to the :class:`~.audio.TxAudioEncoder` and calls ``send_tx_audio``.
:meth:`Kv4pHt.transmit` (on the asyncio loop) only hands PCM to :meth:`enqueue`; the pacer thread
drains it one frame per ~40 ms slot.

Why a daemon thread and not an asyncio task (as :class:`ScanRunner`/``RxPump`` are): the ``Radio``
surface (``ptt``/``transmit``) is synchronous and the pacer must keep firing every frame interval
even while the bridge's own async task is parked in ``wait_for(queue.get(), timeout=tx_hang)``. A
backend-layer daemon thread mirrors the transport's own reader thread (:mod:`.transport`) and the
AIOC's background output stream. Policy lives in the injected-clock :meth:`tick`, tested directly
with a deterministic clock (each ``tick()`` = one advanced frame interval); the thread loop is a
trivial sleep-and-tick, the repo's ``tick()``-policy house style (:class:`ScanEngine`).

Scope: the pacer runs **only** during a held key (``ptt(True)`` → ``ptt(False)``). The one-shot
``transmit()`` path self-keys, sends the whole clip, and drops immediately, so it never holds the
key idle and never starves — it is left exactly as it was.
"""

from __future__ import annotations

import logging
import threading
import time

from ...audio import AudioFrame
from .audio import FRAME_BYTES, FRAME_MS, TxAudioEncoder
from .transport import Kv4pClosed, Kv4pTimeout

logger = logging.getLogger(__name__)

#: One frame interval in seconds — the Opus frame cadence (40 ms; :data:`.audio.FRAME_MS`). The
#: pacer emits exactly one frame per slot at this rate. A fixed protocol constant, not an operator
#: knob (unlike the ``kv4p.*`` config surface), so it is a module default, not a setting.
FRAME_INTERVAL_SECONDS = FRAME_MS / 1000.0

#: One second of encoded silence is ~5–10 B/frame, so a bounded PCM jitter buffer of a few hundred
#: ms is ample headroom while staying current. At real time the drain (one frame per 40 ms) matches
#: a real-time producer, so the buffer sits near empty; the bound only bites if a producer briefly
#: outpaces the drain, and then drop-oldest keeps latency bounded (the ``link/bridge`` ``_enqueue_tx``
#: and transport RX-deque idiom). 16 frames ≈ 640 ms.
DEFAULT_MAX_BUFFER_BYTES = FRAME_BYTES * 16

#: A canonical 40 ms silence frame, encoded fresh each idle slot through the same encoder as real
#: audio (so it decodes the way the board expects). Zeros are ``tx_gain``-invariant, so the ADR 0080
#: gain is neither applied nor double-applied to silence.
_SILENCE_FRAME = AudioFrame(b"\x00" * FRAME_BYTES)


class _TxPacer:
    """Paces one frame per ~40 ms slot to the kv4p while keyed: real audio if buffered, else silence.

    Owns the per-keying :class:`~.audio.TxAudioEncoder` for the whole held key. :meth:`enqueue` is
    called from the asyncio loop by ``transmit()``; :meth:`tick` runs on the daemon thread started
    by :meth:`start`. Exactly one Opus packet leaves per :meth:`tick`.

    Args:
        encoder: the keying's :class:`TxAudioEncoder` (already built in ``_key_on``; carries the
            ADR 0080 ``tx_gain`` and any lead-in remainder). The pacer is its sole user while keyed.
        send: ``transport.send_tx_audio`` — thread-safe (guarded by the transport credit window), so
            the pacer thread and any lingering caller cannot corrupt each other.
        frame_interval: seconds per slot (:data:`FRAME_INTERVAL_SECONDS`).
        max_buffer_bytes: PCM jitter-buffer bound (:data:`DEFAULT_MAX_BUFFER_BYTES`); over it,
            drop-oldest.
        clock / sleep: injected time seams (default :func:`time.monotonic` / a stop-aware wait), so
            the loop can be driven deterministically in tests — though the cadence tests call
            :meth:`tick` directly instead.
    """

    def __init__(
        self,
        encoder: TxAudioEncoder,
        send,
        *,
        frame_interval: float = FRAME_INTERVAL_SECONDS,
        max_buffer_bytes: int = DEFAULT_MAX_BUFFER_BYTES,
        clock=time.monotonic,
    ) -> None:
        self._encoder = encoder
        self._send = send
        self._frame_interval = frame_interval
        self._max_buffer_bytes = max_buffer_bytes
        self._clock = clock

        self._buf = bytearray()
        self._lock = threading.Lock()
        self._dropped_bytes = 0

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- producer side (asyncio loop thread) ----------------------------------

    def enqueue(self, pcm: bytes) -> None:
        """Append PCM for the pacer to drain; drop-oldest if over the buffer bound.

        Non-blocking (unlike the pre-pacer direct write, which could stall the event loop up to the
        transport write timeout on a credit-starved window). The bounded drop-oldest buffer is what
        now protects against a producer briefly outpacing the 40 ms drain.
        """
        if not pcm:
            return
        with self._lock:
            self._buf.extend(pcm)
            overflow = len(self._buf) - self._max_buffer_bytes
            if overflow > 0:
                del self._buf[:overflow]  # drop-oldest — stay current
                self._dropped_bytes += overflow

    @property
    def dropped_bytes(self) -> int:
        """PCM bytes discarded to the buffer bound (telemetry for a producer outpacing the drain)."""
        with self._lock:
            return self._dropped_bytes

    # --- consumer side (pacer thread, or a test driving it directly) ----------

    def tick(self) -> None:
        """Send exactly one frame this slot: a real frame if one is buffered, else encoded silence.

        Slices exactly :data:`FRAME_BYTES` from the *buffer* before encoding, so a producer that
        enqueued a non-frame-multiple (Mumble delivers 20 ms / 960-sample frames) can never cause a
        partial or double push: each ``push`` here is fed exactly 1920 samples, and the encoder's
        accumulator is always ``< FRAME_SAMPLES`` (the 0.5 s lead-in leaves a standing ~960-sample
        remainder), so ``push`` returns exactly one packet per tick. Send errors from a shutdown or
        credit-starve race are swallowed so the thread never dies with PTT still asserted.
        """
        with self._lock:
            if len(self._buf) >= FRAME_BYTES:
                chunk = bytes(self._buf[:FRAME_BYTES])
                del self._buf[:FRAME_BYTES]
            else:
                chunk = None
        frame = AudioFrame(chunk) if chunk is not None else _SILENCE_FRAME
        self._emit(self._encoder.push(frame))

    def _emit(self, packets) -> None:
        for packet in packets:
            try:
                self._send(packet)
            except Kv4pClosed:
                self._stop.set()  # transport gone — stop pacing rather than spin on the error
                return
            except Kv4pTimeout:
                # Credit-starved window (e.g. the device stopped draining): skip this frame rather
                # than kill the thread with PTT asserted. The next slot tries again.
                logger.debug("kv4p pacer: dropped a frame on a credit-starved window")

    # --- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Start the daemon pacer thread (idempotent)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="kv4p-tx-pacer", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            started = self._clock()
            self.tick()
            elapsed = self._clock() - started
            self._stop.wait(max(0.0, self._frame_interval - elapsed))

    def stop(self) -> None:
        """Signal the thread and join it (idempotent). Does NOT flush — see :meth:`flush_tail`.

        The join is bounded by the transport write timeout if the thread is parked in a blocking
        write; a daemon thread means teardown can never hang the process past that.
        """
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    def flush_tail(self) -> None:
        """Drain any final buffered PCM and the encoder tail — caller thread, AFTER :meth:`stop`.

        Runs only once the pacer thread has joined, so the encoder is never touched by two threads.
        Pushes the sub-frame remainder then :meth:`TxAudioEncoder.flush` (zero-pads and encodes the
        last partial frame), so nothing keyed is clipped — the same tail-flush the pre-pacer
        ``_key_off`` did.
        """
        with self._lock:
            tail = bytes(self._buf)
            self._buf.clear()
        if tail:
            self._emit(self._encoder.push(AudioFrame(tail)))
        self._emit(self._encoder.flush())
