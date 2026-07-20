"""``DVDongleVocoder`` — the :class:`Vocoder` seam over the DV Dongle's AMBE2000 (ADR 0086).

Drives the DV Dongle (a DVSI AMBE2000 full-duplex vocoder behind an FTDI VCP) as a synchronous,
one-frame-at-a-time PCM<->AMBE codec. The serial machinery mirrors the kv4p transport
(:mod:`radio_server.backends.kv4p.transport`): ``pyserial`` imported lazily behind the ``hardware``
extra with a ``_serial_factory`` test seam; a daemon reader thread (read -> deframe -> dispatch)
bounded by a short read timeout and a stop :class:`~threading.Event`; a fatal-read path that wakes
every blocked caller; and an idempotent, best-effort :meth:`close`. The wire codec is the pure,
I/O-free :mod:`radio_server.vocoder.frames`.

**Bring-up is the risky unknown (guardrail 1).** The open handshake (query name) + session start and
the AMBE2000 D-STAR full-rate config bytes come from the g4klx/DummyRepeater reference read as a
spec; they are proven on the actual hardware by the ``doctor --vocoder-loopback`` self-test, not by
the fakes. Every port/baud/protocol constant is marked verify-against-hardware.

v1 is a clean synchronous query/reply per frame — the AMBE2000 is full-duplex, but a blocking
``encode``/``decode`` is enough and keeps the seam simple. A streamed full-duplex path is a later
concern if the live wiring needs it.

**Drive encode and decode as separate continuous streams — never interleave them frame by frame.**
The AMBE2000 is a *pipelined* full-duplex chip: a read reflects an input from several ticks earlier.
Encoding a whole stream then decoding a whole stream preserves order at a constant latency (bench-
confirmed: a staircase of tones round-trips with pitch correlation 1.00). Alternating
``decode(encode(frame))`` per frame instead feeds the chip's two pipelines dummy frames on the
opposite stream each tick and reads back the wrong result, corrupting anything time-varying
(correlation collapsed to ~0 with gross frequency errors — see ADR 0086 and ``doctor
--vocoder-loopback``). A real single-direction path (TX encodes, RX decodes) never interleaves, so
this is a self-test/duplex-caller hazard, not a limit of the seam.
"""

from __future__ import annotations

import atexit
import collections
import contextlib
import logging
import threading
import time

from ..audio import AudioFormatMismatch, AudioFrame
from .base import (
    AMBE_BYTES_PER_FRAME,
    PCM_BYTES_PER_FRAME,
    PCM_FORMAT,
    VocoderTimeout,
    VocoderUnavailable,
)
from . import frames

logger = logging.getLogger(__name__)

#: FTDI VCP device path. A per-hardware fact (guardrail 1): prefer a stable ``/dev/serial/by-id/*``
#: symlink over ``/dev/ttyUSB*`` (which reorders across reboots and collides with the radio's UART).
#: Verify against the dongle in hand; overridable via ``doctor --vocoder-port``.
DEFAULT_DVDONGLE_PORT = "/dev/ttyUSB1"

#: DV Dongle line rate. 230400 8N1 per the reference (``SERIAL_230400``). Verify against hardware.
DEFAULT_BAUD = 230400

#: Blocking-read timeout (s): bounds how long the reader waits before re-checking the stop flag.
_READ_TIMEOUT = 0.1

#: Blocking-WRITE timeout (s): a full/stuck FTDI write buffer must not park a codec exchange forever
#: (ADR 0091) — an unbounded write is one way the reflector→RF decode wedged with PTT asserted. pyserial
#: raises ``SerialTimeoutException`` past this, surfaced as a codec error the bridge already handles.
_WRITE_TIMEOUT = 1.0

#: Bytes requested per serial ``read`` — a ceiling, not a floor. One audio packet is 322 bytes.
_READ_SIZE = 512

#: Seconds a single ``encode``/``decode`` waits for its reply pair before :class:`VocoderTimeout`.
DEFAULT_REPLY_TIMEOUT = 2.0

#: Seconds the open/start handshake waits for each response before :class:`VocoderUnavailable`.
DEFAULT_HANDSHAKE_TIMEOUT = 5.0

#: Seconds ``close`` waits for the session-stop ack before tearing down anyway (never block teardown).
_STOP_ACK_TIMEOUT = 0.5

#: Seconds ``close`` waits to grab ``_io_lock`` for the courtesy ``REQ_STOP``. A live ``_recover`` can
#: hold the lock for seconds; teardown must never wait that long (ADR 0099), so past this we skip the
#: graceful stop and close the port directly (which itself errors out the recover's blocked I/O).
_CLOSE_LOCK_TIMEOUT = 0.5

#: How many times :meth:`DVDongleVocoder._recover` re-runs the open+handshake when waking a wedged
#: dongle. The AMBE2000's first open after being left in a bad state is flaky (name OK, start drops —
#: bench-observed), exactly like cold bring-up, so recovery retries the reopen a few times. ADR 0094.
_RECOVER_HANDSHAKE_ATTEMPTS = 3

#: The standard D-STAR "silence" AMBE frame (an AMBE-layer constant, same bytes as DSRP's terminator),
#: fed during a decode stream's flush to push the last in-flight real frames out of the chip's pipeline.
_SILENCE_AMBE = bytes([0x9E, 0x8D, 0x32, 0x88, 0x26, 0x1A, 0x3F, 0x61, 0xE8])

#: Decode-pipeline depth budget (frames) for the streaming decode path (ADR 0098): how many silence
#: frames a :class:`DecodeStream` flushes at over end to drain the AMBE2000's in-flight tail, and the
#: priming window before it expects output. Bench-measured L on the DV Dongle was ~5 (range 4-6); 8
#: sits comfortably above the observed max. Marked tunable default (guardrail 1) — re-measure per
#: dongle on the bench (the ADR 0098 prime/marker/flush decode-only method) if audio boundary artifacts
#: appear; over-estimating only adds a little key-up latency, never dropped frames.
DEFAULT_DECODE_LATENCY_FRAMES = 8

_EXTRA_MSG = (
    "pyserial not found; the DV Dongle vocoder needs the 'hardware' extra "
    "(pip install 'radio-server[hardware]'). The dongle is an FTDI serial device."
)


def _load_serial():
    try:
        import serial  # pyserial
    except ImportError as exc:  # pragma: no cover - exercised via the injected fake in tests
        raise VocoderUnavailable(_EXTRA_MSG) from exc
    return serial


def _default_serial_factory(port: str, baud: int):
    """Open ``port`` at ``baud`` for the DV Dongle (plain 8N1, no auto-reset lines to guard).

    Unlike the kv4p ESP32 boards, the DV Dongle's FTDI has no reset circuit on DTR/RTS, so they are
    left at their defaults. Opened ``exclusive`` (POSIX ``TIOCEXCL`` + advisory lock): the DV Dongle
    is shared between the two radio-server instances (ADR 0089), and the exclusive open is the
    cross-process arbiter — the second instance's open fails cleanly instead of both scribbling on the
    same tty. Wraps a pyserial failure as :class:`VocoderUnavailable` so a missing, busy, or
    already-held device is a clear error ("in use by the other radio"), not a stack trace.
    """
    serial = _load_serial()
    try:
        return serial.Serial(
            port=port, baudrate=baud, timeout=_READ_TIMEOUT, write_timeout=_WRITE_TIMEOUT, exclusive=True
        )
    except Exception as exc:  # SerialException: device absent / busy / held exclusively / bad path
        raise VocoderUnavailable(f"could not open the DV Dongle on {port}: {exc}") from exc


class DVDongleVocoder:
    """PCM<->AMBE via the DV Dongle. Implements the :class:`~radio_server.vocoder.base.Vocoder` seam.

    Args:
        port: FTDI device path (:data:`DEFAULT_DVDONGLE_PORT`).
        baud: Line rate (:data:`DEFAULT_BAUD`).
        reply_timeout: Per-frame reply deadline (:data:`DEFAULT_REPLY_TIMEOUT`).
        handshake_timeout: Per-response deadline for open/start (:data:`DEFAULT_HANDSHAKE_TIMEOUT`).
        connect: Run the open+start handshake in ``__init__`` (default). ``False`` opens the port and
            starts the reader but skips the handshake — for tests exercising the sequence explicitly.
        _serial_factory: Test seam — ``(port, baud) -> Serial-like`` with a blocking ``read``,
            ``write`` and ``close()``. Defaults to a real pyserial port.
        _clock: Monotonic clock seam (defaults to :func:`time.monotonic`) — lets a test drive the
            timeout path deterministically.

    Construction opens the port, starts the reader thread, and (unless ``connect=False``) handshakes;
    a handshake failure closes the port and raises :class:`VocoderUnavailable`.
    """

    def __init__(
        self,
        *,
        port: str = DEFAULT_DVDONGLE_PORT,
        baud: int = DEFAULT_BAUD,
        reply_timeout: float = DEFAULT_REPLY_TIMEOUT,
        handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT,
        decode_latency_frames: int = DEFAULT_DECODE_LATENCY_FRAMES,
        connect: bool = True,
        _serial_factory=None,
        _clock=time.monotonic,
    ) -> None:
        # Kept so `_recover` can rebuild the transport (close+reopen+re-handshake) to wake a wedged
        # dongle (ADR 0094) — the same factory/port/baud the constructor opened with.
        self._serial_factory = _serial_factory or _default_serial_factory
        self._port = port
        self._baud = baud
        self._serial = self._serial_factory(port, baud)
        self._reply_timeout = reply_timeout
        self._handshake_timeout = handshake_timeout
        self._decode_latency_frames = decode_latency_frames
        self._clock = _clock
        self._decoder = frames.DvDongleDecoder()

        # Reply hand-off: the reader thread fills the latest value in each single-value slot and
        # notifies; a waiter blocks on the condition with a monotonic deadline. Single-value slots are
        # bounded by construction — a synchronous exchange consumes each reply pair before the next.
        self._reply_cond = threading.Condition()
        self._ambe_reply: bytes | None = None
        self._audio_reply: bytes | None = None
        self._control_kind: frames.ResponseKind | None = None
        # While a streaming decode is open (ADR 0098) the reader appends decoded PCM here IN ORDER
        # instead of into the single-value `_audio_reply` slot — so no bursty pipeline reply is dropped
        # or reordered (the root cause of the garbled crossband decode). None = legacy per-frame mode.
        self._decode_fifo: collections.deque[bytes] | None = None

        # One exchange (or handshake step) at a time: serialises writers so two callers can't
        # interleave their request/reply pairs on the wire. Reentrant because `_recover` holds it
        # while re-running the handshake, which itself takes the lock (ADR 0094).
        self._io_lock = threading.RLock()

        self._stop = threading.Event()
        self._reader_error: Exception | None = None
        self._closed = False
        # Monotonic reader generation (ADR 0099). Each spawned reader is tagged with the generation
        # current when it started; a later `_recover` bumps it and rebinds. `_dispatch`/`_fail` ignore a
        # reader whose tag is stale, so a straggler from a superseded transport — which on a wedged
        # dongle can outlive `_recover`'s bounded join — can never fill a reply slot or clobber
        # `_reader_error` (the zombie-reader `TypeError` that turned a recoverable sleep into a lockup).
        self._reader_gen = 0

        self._reader = self._spawn_reader()
        atexit.register(self.close)

        if connect:
            try:
                self._handshake()
            except Exception:
                self.close()
                raise

    # --- reader thread --------------------------------------------------------

    def _spawn_reader(self) -> threading.Thread:
        """Start a reader bound to the CURRENT ``_serial`` + ``_stop`` + a fresh generation (ADR 0099).

        The thread reads its own ``serial``/``stop`` by value, never ``self.*``, so a later ``_recover``
        that reassigns ``self._serial``/``self._stop`` cannot make this reader read a *different*
        (reassigned) port — it only ever touches the handle it was born with. The generation tag lets
        ``_dispatch``/``_fail`` shed a straggler once it has been superseded.
        """
        self._reader_gen += 1
        gen = self._reader_gen
        reader = threading.Thread(
            target=self._read_loop,
            args=(self._serial, self._stop, gen),
            name="dvdongle-reader",
            daemon=True,
        )
        reader.start()
        return reader

    def _read_loop(self, serial, stop: threading.Event, gen: int) -> None:
        """Runs on the daemon reader thread: read -> deframe -> dispatch, until stopped.

        ``serial``/``stop``/``gen`` are bound at thread start (ADR 0099) — never re-read from ``self`` —
        so a ``_recover`` that rebuilds the transport cannot alias this reader onto the new port."""
        while not stop.is_set():
            if gen != self._reader_gen:
                return  # superseded by a _recover — stop touching shared state, let this thread die
            try:
                chunk = serial.read(_READ_SIZE)
            except Exception as exc:  # SerialException et al. — surface it, don't wedge silently
                self._fail(exc, gen)
                return
            if not chunk:
                continue  # read timeout (b"") — loop back and re-check the stop flag
            try:
                for packet in self._decoder.feed(chunk):
                    self._dispatch(packet, gen)
            except Exception:  # a single malformed packet must not kill the reader
                logger.exception("dvdongle: error dispatching packet")

    def _dispatch(self, packet: frames.Packet, gen: int) -> None:
        if gen != self._reader_gen:
            return  # a superseded reader must not fill reply slots for the live transport
        kind = frames.classify(packet)
        with self._reply_cond:
            if kind is frames.ResponseKind.AMBE:
                self._ambe_reply = frames.ambe_voice_frame(packet)
            elif kind is frames.ResponseKind.AUDIO:
                if self._decode_fifo is not None:
                    # Streaming decode (ADR 0098): keep every decoded frame, in arrival order.
                    self._decode_fifo.append(frames.audio_pcm(packet))
                else:
                    self._audio_reply = frames.audio_pcm(packet)
            elif kind is frames.ResponseKind.UNKNOWN:
                logger.debug(
                    "dvdongle: unknown packet type=%d len=%d", packet.type_bits, len(packet.payload)
                )
                return
            else:  # NAME / START / STOP / NOP control response
                self._control_kind = kind
            self._reply_cond.notify_all()

    def _fail(self, exc: Exception, gen: int) -> None:
        """Record a fatal read error and wake every blocked caller so they re-raise it.

        A straggler from a superseded generation (ADR 0099) is ignored: its port died *because*
        ``_recover`` closed it, and recording that would falsely fail the freshly-rebuilt transport."""
        if gen != self._reader_gen:
            return
        self._reader_error = exc
        logger.error("dvdongle: reader thread stopped on %r", exc)
        with self._reply_cond:
            self._reply_cond.notify_all()

    def _raise_if_failed(self) -> None:
        if self._reader_error is not None:
            raise VocoderUnavailable(
                f"DV Dongle reader thread stopped: {self._reader_error}"
            ) from self._reader_error

    # --- handshake ------------------------------------------------------------

    def _handshake(self) -> None:
        """Query the name (open) then start the streaming session, per the reference."""
        self._control_exchange(
            frames.REQ_NAME, frames.ResponseKind.NAME, "no name response (is a DV Dongle attached?)"
        )
        self._control_exchange(
            frames.REQ_START, frames.ResponseKind.START, "no response to the session-start request"
        )

    def _control_exchange(
        self, request: bytes, want: frames.ResponseKind, what: str, timeout: float | None = None
    ) -> None:
        """Write a control ``request`` and block until the ``want`` response arrives (or fail loud)."""
        with self._io_lock:
            with self._reply_cond:
                self._control_kind = None
            self._write(request)
            deadline = self._clock() + (self._handshake_timeout if timeout is None else timeout)
            with self._reply_cond:
                while self._control_kind is not want:
                    self._raise_if_failed()
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        raise VocoderUnavailable(f"DV Dongle handshake failed: {what}")
                    self._reply_cond.wait(remaining)

    # --- codec surface --------------------------------------------------------

    def encode(self, frame: AudioFrame) -> bytes:
        """Encode one 8 kHz / 160-sample PCM frame to a 9-byte AMBE voice frame."""
        if frame.format != PCM_FORMAT:
            raise AudioFormatMismatch(
                f"DVDongleVocoder encodes {PCM_FORMAT}, got a frame in {frame.format}"
            )
        if len(frame.samples) != PCM_BYTES_PER_FRAME:
            raise AudioFormatMismatch(
                f"encode expects a {PCM_BYTES_PER_FRAME}-byte frame, got {len(frame.samples)}"
            )
        # Send the encoder-config AMBE packet then the audio to encode; the dongle streams back an
        # AMBE result (the voice frame) plus an audio echo. Wait for both to keep the stream in sync.
        ambe, _audio = self._exchange(
            [frames.build_encode_config_packet(), frames.build_audio_packet(frame.samples)]
        )
        return ambe

    def decode(self, ambe: bytes) -> AudioFrame:
        """Decode one 9-byte AMBE voice frame to an 8 kHz / 160-sample PCM frame."""
        if len(ambe) != AMBE_BYTES_PER_FRAME:
            raise ValueError(f"decode expects a {AMBE_BYTES_PER_FRAME}-byte AMBE frame, got {len(ambe)}")
        # Send the AMBE (with the voice frame spliced into the decoder-config block) then a dummy
        # audio packet; the dongle streams back an AMBE echo plus the decoded PCM.
        _ambe, pcm = self._exchange(
            [frames.build_decode_ambe_packet(ambe), frames.build_decode_dummy_audio_packet()]
        )
        return AudioFrame(pcm, PCM_FORMAT)

    # --- streaming decode (ADR 0098) ------------------------------------------

    def open_decode_stream(self) -> "_DvDongleDecodeStream":
        """Open an ordered, per-over streaming decode session (the :class:`DecodeStream` seam).

        The AMBE2000 decode is pipelined and its replies are bursty, so the per-frame :meth:`decode`
        mis-pairs and drops frames when its output is keyed straight onto RF. A stream instead collects
        every decoded frame in an ordered FIFO and hands back correctly-sequenced audio, absorbing the
        constant pipeline latency with a flush at over end. One stream per inbound over; close it after.

        A stream that hit a wedge fails its over fast rather than recovering mid-flight (ADR 0099), so
        recovery happens HERE at the next over boundary: if the dongle is in a failed state (its reader
        died), wake it before starting the over. A dongle that will not wake raises — the bridge then
        falls back to the legacy per-frame decode (which recovers per frame) so the over is still safe.
        """
        if self._reader_error is not None:
            self._recover()  # heal a dongle left wedged by a prior over, at the safe over boundary
        with self._reply_cond:
            self._decode_fifo = collections.deque()
        return _DvDongleDecodeStream(self, self._decode_latency_frames)

    def _write_decode_frame(self, ambe: bytes) -> None:
        """Write one decode input pair (AMBE + dummy audio) — the chip clocks its pipeline one tick."""
        with self._io_lock:
            self._write(frames.build_decode_ambe_packet(ambe))
            self._write(frames.build_decode_dummy_audio_packet())

    def _drain_decoded(self, *, block: bool) -> list[AudioFrame]:
        """Pop all decoded PCM frames the reader has collected, in order. If ``block``, wait (bounded)
        for at least one when the FIFO is momentarily empty — so a caller past the priming window is not
        raced ahead of the reader; otherwise return whatever is ready (possibly empty)."""
        with self._reply_cond:
            fifo = self._decode_fifo
            if fifo is None:
                return []
            if block and not fifo:
                deadline = self._clock() + self._reply_timeout
                while not fifo:
                    self._raise_if_failed()
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        raise VocoderTimeout("decode stream: no PCM within the reply deadline")
                    self._reply_cond.wait(remaining)
            out = [AudioFrame(pcm, PCM_FORMAT) for pcm in fifo]
            fifo.clear()
            return out

    def _close_decode_stream(self) -> None:
        with self._reply_cond:
            self._decode_fifo = None

    def _exchange(self, packets: list[bytes]) -> tuple[bytes, bytes]:
        """Write ``packets`` and block for the AMBE+audio reply pair, recovering a wedged dongle once.

        The AMBE2000 sleeps after ~2-3 s idle and then stops responding — the frame times out
        (:class:`VocoderTimeout`) and, as the caller keeps feeding, the FTDI write buffer fills and
        writes start timing out too (the crossband wedge, bench-characterised). It does NOT self-wake;
        a full close+reopen+re-handshake reliably recovers it (bench-proven). So on a timeout we
        ``_recover`` **once** and retry the exchange; a second failure propagates (ADR 0094). A wedge
        during a live keyed over is otherwise the crossband's problem — the ADR 0092/0093 safety net
        still drops PTT, but recovering here keeps the over's audio flowing instead of dropping it.
        """
        try:
            return self._exchange_once(packets)
        except VocoderTimeout:
            self._recover()  # wake the sleeping/wedged chip, then retry the frame once
            return self._exchange_once(packets)

    def _exchange_once(self, packets: list[bytes]) -> tuple[bytes, bytes]:
        """Write ``packets`` then block for the AMBE+audio reply pair (bounded), returning both."""
        with self._io_lock:
            with self._reply_cond:
                self._ambe_reply = None
                self._audio_reply = None
            for packet in packets:
                self._write(packet)
            deadline = self._clock() + self._reply_timeout
            with self._reply_cond:
                while self._ambe_reply is None or self._audio_reply is None:
                    self._raise_if_failed()
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        raise VocoderTimeout(
                            "DV Dongle did not return a frame within "
                            f"{self._reply_timeout:.1f}s (ambe={self._ambe_reply is not None}, "
                            f"audio={self._audio_reply is not None})"
                        )
                    self._reply_cond.wait(remaining)
                return self._ambe_reply, self._audio_reply

    def _write(self, data: bytes) -> None:
        self._raise_if_failed()
        self._serial.write(data)

    def _recover(self) -> None:
        """Wake a wedged/asleep AMBE2000 by rebuilding the transport and session (ADR 0094).

        The chip goes unresponsive after ~2-3 s idle and will not self-wake; a close+reopen+
        re-handshake recovers it (bench-proven). Under the io lock so it can't race an exchange: stop
        the old reader and close its port, then rebuild the port, reader (a fresh generation — ADR
        0099), decoder and reply slots and re-handshake, retrying the flaky first open a few times.
        The old reader is bound to its own now-closed handle and shed by generation, so it can neither
        alias the new port nor clobber ``_reader_error`` even if it outlives the bounded join. Bails if
        the dongle is closed mid-recovery (a concurrent ``close`` on teardown). Raises
        :class:`VocoderUnavailable` if it cannot wake the dongle.
        """
        with self._io_lock:
            if self._closed:
                raise VocoderUnavailable("cannot recover a closed DV Dongle")
            # Tear the OLD transport down. The old reader is generation-shed, so this is best-effort.
            self._stop.set()
            with self._reply_cond:
                self._reply_cond.notify_all()
            with contextlib.suppress(Exception):
                self._serial.close()
            old_reader = self._reader
            if old_reader is not None and old_reader is not threading.current_thread():
                old_reader.join(timeout=1.5)
            # Rebuild + re-handshake, retrying the flaky first open (name OK / start drops).
            last_exc: Exception | None = None
            for _ in range(_RECOVER_HANDSHAKE_ATTEMPTS):
                if self._closed:  # a concurrent close() on teardown — stop reopening the port (ADR 0099)
                    raise VocoderUnavailable("DV Dongle closed during recovery")
                self._stop = threading.Event()
                self._reader_error = None
                self._decoder = frames.DvDongleDecoder()
                with self._reply_cond:
                    self._ambe_reply = None
                    self._audio_reply = None
                    self._control_kind = None
                    if self._decode_fifo is not None:
                        self._decode_fifo = collections.deque()  # pipeline reset: drop stale tail
                try:
                    self._serial = self._serial_factory(self._port, self._baud)
                except Exception as exc:  # port reopen failed — retry
                    last_exc = exc
                    continue
                self._reader = self._spawn_reader()
                try:
                    self._handshake()
                    logger.info("dvdongle: recovered a wedged dongle by close+reopen+re-handshake")
                    return
                except Exception as exc:  # handshake failed — tear down this attempt and retry
                    last_exc = exc
                    self._stop.set()
                    with self._reply_cond:
                        self._reply_cond.notify_all()
                    with contextlib.suppress(Exception):
                        self._serial.close()
                    if self._reader is not threading.current_thread():
                        self._reader.join(timeout=1.0)
            raise VocoderUnavailable(
                f"DV Dongle recovery failed after {_RECOVER_HANDSHAKE_ATTEMPTS} attempts: {last_exc}"
            )

    # --- lifecycle ------------------------------------------------------------

    def close(self) -> None:
        """Stop the session, stop the reader, close the port. Idempotent; safe at exit.

        Best-effort: a dead port or an unresponsive dongle must never make ``close`` raise or hang.
        The session-stop request is written and its ack waited for only briefly, then teardown
        proceeds regardless.

        Never wait on ``_io_lock`` for the courtesy ``REQ_STOP`` (ADR 0099): a live ``_recover`` can hold
        it for seconds, and blocking here stalled the crossband teardown ~15 s with PTT asserted. So
        ``_closed`` is set FIRST (a running ``_recover`` sees it and bails), the graceful stop is
        attempted only if the lock is free within ``_CLOSE_LOCK_TIMEOUT``, and the port is then closed
        regardless — closing it is what errors out an in-flight ``_recover``'s blocked I/O.
        """
        if self._closed:
            return
        self._closed = True  # set before touching the lock, so a running _recover bails at its next attempt
        if self._io_lock.acquire(timeout=_CLOSE_LOCK_TIMEOUT):
            try:
                with contextlib.suppress(Exception):
                    self._control_exchange(
                        frames.REQ_STOP, frames.ResponseKind.STOP, "no stop ack", timeout=_STOP_ACK_TIMEOUT
                    )
            finally:
                self._io_lock.release()
        # else: a _recover holds the lock — skip the graceful stop; closing the port below frees it.
        self._stop.set()
        with self._reply_cond:
            self._reply_cond.notify_all()
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1.0)
        try:
            self._serial.close()
        except Exception:
            pass
        atexit.unregister(self.close)


class _DvDongleDecodeStream:
    """One inbound over's ordered decode stream over :class:`DVDongleVocoder` (ADR 0098).

    Feeds each AMBE frame into the AMBE2000 and returns the decoded PCM the chip has clocked out, **in
    arrival order**, from the vocoder's ordered FIFO — so no bursty pipeline reply is dropped or
    reordered (the root cause of the garbled crossband decode). The chip is pipelined (~L frames
    latency, bench-measured ~5 on the DV Dongle), so the first ~L :meth:`decode` calls return nothing
    (the pipeline priming with pre-over silence, which the reflector→RF content gate drops anyway) and
    the last ~L real frames are recovered by :meth:`flush`, which clocks silence frames through to push
    the tail out. Not thread-safe: driven from the bridge's single-worker vocoder executor, one over at
    a time (the half-duplex latch serialises overs).
    """

    def __init__(self, voc: "DVDongleVocoder", latency: int) -> None:
        self._voc = voc
        self._latency = max(0, latency)
        self._fed = 0  # real AMBE frames fed via decode()
        self._emitted = 0  # decoded frames returned so far
        self._closed = False
        # Latched once a wedge (write timeout / dead reader) is hit (ADR 0099). The over then fails
        # FAST — every later decode()/flush() short-circuits instead of re-attempting a ~1 s serial
        # write per frame, which is what parked the inbound drain one frame at a time. The dongle is
        # healed on the NEXT over's open_decode_stream, not mid-flight.
        self._wedged = False
        # Starvation alarm (ADR 0108): the last time the stream made progress — a drain that returned
        # audio, or any call inside the priming window. The non-blocking drain (ADR 0107) removed the
        # blocking path that used to turn a silently-dead pipeline into a loud VocoderTimeout, so a
        # chip that eats input and returns nothing produced a full over of pure nothing with no error
        # anywhere. This clock restores the alarm without restoring the block.
        self._last_progress = voc._clock()

    def decode(self, ambe: bytes) -> list[AudioFrame]:
        if self._closed:
            raise VocoderUnavailable("decode() on a closed stream")
        if self._wedged:  # fail fast — no more per-frame 1 s write timeouts once wedged
            raise VocoderUnavailable("decode() on a wedged DV Dongle stream")
        if len(ambe) != AMBE_BYTES_PER_FRAME:
            raise ValueError(
                f"decode expects a {AMBE_BYTES_PER_FRAME}-byte AMBE frame, got {len(ambe)}"
            )
        try:
            self._voc._write_decode_frame(ambe)
            self._fed += 1
            # Drain opportunistically, NEVER block mid-over (ADR 0107): blocking per call locked the
            # drain loop to the reader's ~27 ms read-batch cadence — slower than the stream's 20 ms
            # frame time, so the bridge's rx queue overflowed and shredded long overs (the bench's
            # "starts clear, unintelligible by the end"). The wire itself paces the pipeline: input
            # arrives at 50 frames/s, the serial link drains ~62/s, and the OS write buffer bounds
            # any burst. A wedged write still raises here (1 s write timeout); a pipeline that eats
            # input but yields nothing trips the starvation alarm below (ADR 0108) — the dead-air
            # bound alone protects only PTT, not audio, and does nothing within a short over.
            out = self._voc._drain_decoded(block=False)
        except Exception:
            self._wedged = True  # a wedge — latch so the rest of the over fails fast, then re-raise
            raise
        # Starvation alarm (ADR 0108): past the priming window, a healthy pipeline yields ~one frame
        # per input within the wire round-trip. If nothing has emerged for a full reply_timeout while
        # we keep feeding, the pipeline is dead even though every write "succeeded" — fail LOUD like
        # the pre-ADR-0107 blocking drain did, so the bridge sees the over fail instead of silence.
        now = self._voc._clock()
        if out or self._fed <= self._latency:
            self._last_progress = now
        elif now - self._last_progress > self._voc._reply_timeout:
            self._wedged = True
            raise VocoderTimeout(
                f"decode stream starved: {self._fed} frames fed, nothing decoded for "
                f"{now - self._last_progress:.2f}s (reply timeout {self._voc._reply_timeout:.2f}s)"
            )
        self._emitted += len(out)
        return out

    def flush(self) -> list[AudioFrame]:
        """Drain the pipeline tail: clock silence frames through until every fed real frame has emerged."""
        if self._closed or self._wedged:
            return []  # a wedged stream has nothing drainable — never re-attempt the wedged write
        out: list[AudioFrame] = []
        try:
            for _ in range(self._latency):
                self._voc._write_decode_frame(_SILENCE_AMBE)
            # Collect until every real frame we fed has come out (the pipeline held ~L; we clocked
            # `latency` silence frames ≥ L above). A stalled chip raises VocoderTimeout via the block.
            while self._emitted < self._fed:
                got = self._voc._drain_decoded(block=True)
                if not got:
                    break
                out.extend(got)
                self._emitted += len(got)
            out.extend(self._voc._drain_decoded(block=False))  # sweep trailing silence still queued
        except Exception:
            # A wedge during the tail flush is not fatal to the over — the real audio already went out.
            # Latch and return what we drained so `_flush_and_end_rx` proceeds straight to the unkey.
            self._wedged = True
        return out

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._voc._close_decode_stream()
