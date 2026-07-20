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

        self._reader = threading.Thread(
            target=self._read_loop, name="dvdongle-reader", daemon=True
        )
        self._reader.start()
        atexit.register(self.close)

        if connect:
            try:
                self._handshake()
            except Exception:
                self.close()
                raise

    # --- reader thread --------------------------------------------------------

    def _read_loop(self) -> None:
        """Runs on the daemon reader thread: read -> deframe -> dispatch, until stopped."""
        while not self._stop.is_set():
            try:
                chunk = self._serial.read(_READ_SIZE)
            except Exception as exc:  # SerialException et al. — surface it, don't wedge silently
                self._fail(exc)
                return
            if not chunk:
                continue  # read timeout (b"") — loop back and re-check the stop flag
            try:
                for packet in self._decoder.feed(chunk):
                    self._dispatch(packet)
            except Exception:  # a single malformed packet must not kill the reader
                logger.exception("dvdongle: error dispatching packet")

    def _dispatch(self, packet: frames.Packet) -> None:
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

    def _fail(self, exc: Exception) -> None:
        """Record a fatal read error and wake every blocked caller so they re-raise it."""
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
        """
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
        and **join** the old reader before touching any shared attribute (the reader reads
        ``self._serial``/``self._stop`` by reference — reassigning them under a live reader would race),
        then rebuild the port, reader, decoder and reply slots and re-handshake — retrying the flaky
        first open a few times, exactly as cold bring-up does. Raises :class:`VocoderUnavailable` if it
        cannot wake the dongle.
        """
        with self._io_lock:
            if self._closed:
                raise VocoderUnavailable("cannot recover a closed DV Dongle")
            # Tear the OLD transport down fully before reassigning anything the reader touches.
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
                self._reader = threading.Thread(
                    target=self._read_loop, name="dvdongle-reader", daemon=True
                )
                self._reader.start()
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
        """
        if self._closed:
            return
        self._closed = True
        try:
            self._control_exchange(
                frames.REQ_STOP, frames.ResponseKind.STOP, "no stop ack", timeout=_STOP_ACK_TIMEOUT
            )
        except Exception:
            pass  # never block teardown on a stop ack
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

    def decode(self, ambe: bytes) -> list[AudioFrame]:
        if self._closed:
            raise VocoderUnavailable("decode() on a closed stream")
        if len(ambe) != AMBE_BYTES_PER_FRAME:
            raise ValueError(
                f"decode expects a {AMBE_BYTES_PER_FRAME}-byte AMBE frame, got {len(ambe)}"
            )
        self._voc._write_decode_frame(ambe)
        self._fed += 1
        # During the priming window the chip has not clocked out a frame per input yet, so don't block;
        # past it the chip yields ~one frame per input, so wait (bounded) to keep the stream paced with
        # the reader and the FIFO from growing unboundedly.
        out = self._voc._drain_decoded(block=self._fed > self._latency)
        self._emitted += len(out)
        return out

    def flush(self) -> list[AudioFrame]:
        """Drain the pipeline tail: clock silence frames through until every fed real frame has emerged."""
        if self._closed:
            return []
        for _ in range(self._latency):
            self._voc._write_decode_frame(_SILENCE_AMBE)
        out: list[AudioFrame] = []
        # Collect until every real frame we fed has come out (the pipeline held ~L; we clocked `latency`
        # silence frames ≥ L through above). A stalled chip raises VocoderTimeout via the bounded block.
        while self._emitted < self._fed:
            got = self._voc._drain_decoded(block=True)
            if not got:
                break
            out.extend(got)
            self._emitted += len(got)
        out.extend(self._voc._drain_decoded(block=False))  # sweep any trailing silence still queued
        return out

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._voc._close_decode_stream()
