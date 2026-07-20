"""AiocBaofeng — audio-only UV-5R backend (audio/PTT via NA6D AIOC cable, no CAT).

The real hardware backend (ADR 0029). Audio in/out is the AIOC's USB sound card
(``sounddevice``/ALSA); PTT is the AIOC's serial control line (``pyserial``). There is no CAT:
frequency is set by hand on the radio, so this backend advertises only :data:`SHARED_CAPS` and
the API rejects tuning operations (guardrail 3).

Keying discipline (guardrail 2): PTT is a serial control line (DTR by default; DTR-vs-RTS is the
one empirical fact — verify on hardware, guardrail 1), NEVER a CAT ``TX`` command. Two keying
shapes share one backend:

  * **One-shot** (station ID, service TTS, REST ``/transmit``): a single ``transmit(clip)`` self-keys
    — assert the line, play the whole clip, drain, drop the line. The caller never touches ``ptt()``.
  * **Streaming** (``TxSession`` / ``/audio/tx``): an explicit ``ptt(True)`` holds the line across
    many ``transmit(frame)`` calls, then ``ptt(False)`` drops it. While the line is held, ``transmit``
    only queues the frame for the keying's pacer thread (ADR 0102) — non-blocking, and it must not
    drop the key between frames. All blocking device writes happen on the pacer thread, never on the
    caller: a blocking write on the asyncio event loop was what starved the D-STAR decode pipeline
    into clicking and froze the on-loop watchdogs.

The distinguishing state is :attr:`_keyed` (held by ``ptt(True)``): ``transmit`` self-keys only when
it is not already keyed.

Hardware deps (``pyserial``, ``sounddevice``) are the ``hardware`` optional extra and are lazily
imported here, so ``import radio_server.backends`` and the CI test suite stay hardware-free — the
constructor accepts injected fakes (``_serial_factory`` / ``_audio``) for unit tests. ``sounddevice``
additionally needs the system ``libportaudio2`` library (out-of-band, like ``multimon-ng``).

Known limitation (ADR 0029): ``receive()`` blocks ~one block (~20 ms) and is called directly on the
event loop by ``RxPump``; moving it to a thread executor is a deferred follow-up.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import threading
import time
from collections import deque
from enum import StrEnum

from ..audio import CANONICAL_FORMAT, AudioFormatMismatch, AudioFrame
from .base import SHARED_CAPS, Capability, RadioStatus

logger = logging.getLogger(__name__)


class PttLine(StrEnum):
    """Which serial control line keys PTT on the AIOC. **DTR** is the default — confirmed on the
    bench (cycle 29, `python -m radio_server.doctor --key-test`): on this NA6D AIOC + UV-5R, DTR keys
    the transmitter and RTS does not. Kept configurable because it is a per-hardware fact (guardrail
    1). ``pyserial`` exposes both as writable ``.rts`` / ``.dtr`` attributes."""

    RTS = "rts"
    DTR = "dtr"


#: AIOC PTT serial device. ``/dev/ttyACM0`` is the enumeration default; the stable, reorder-proof
#: path is ``/dev/serial/by-id/usb-*All-In-One-Cable*`` — prefer it in a multi-device setup.
DEFAULT_SERIAL_PORT = "/dev/ttyACM0"
#: The default PTT line: DTR, confirmed on the bench (cycle 29). RTS did not key this AIOC.
DEFAULT_PTT_LINE = PttLine.DTR
#: The AIOC USB sound card, as sounddevice/PortAudio name it. sounddevice matches a device by
#: integer index or by a (case-insensitive) substring of its PortAudio name — NOT by a raw ALSA
#: string like ``hw:CARD=AllInOneCable``. PortAudio names the raw ALSA device
#: ``All-In-One-Cable: USB Audio (hw:2,0)``; the substring ``"All-In-One-Cable: USB"`` targets that
#: (the low-latency raw device) unambiguously, where a bare ``"All-In-One-Cable"`` would also match
#: the PulseAudio/PipeWire-wrapped copies of the same card. Empirically opens + reads 48 kHz here;
#: ``python -m radio_server.doctor`` prints the exact index/name to use if this doesn't resolve.
DEFAULT_INPUT_DEVICE = "All-In-One-Cable: USB"
DEFAULT_OUTPUT_DEVICE = "All-In-One-Cable: USB"
#: Frames per capture/playback block: 960 = 20 ms at the canonical 48 kHz. VERIFY AGAINST HARDWARE
#: (guardrail 1) — trades latency against xrun robustness on the real codec.
DEFAULT_BLOCKSIZE = 960
#: Seconds of silence transmitted immediately after PTT keys up, before any real audio (the "TX
#: lead-in" / PTT head delay). A UV-5R's transmitter — and the receiving radio's squelch — take a few
#: hundred ms to come up after the line is asserted; without a lead-in the first fraction of a second
#: of speech goes out before the RF path is established and is clipped over the air. 0.5 s matches the
#: clip observed on the bench. Per-hardware (guardrail 1): bench-tune, or set 0 to disable.
DEFAULT_TX_LEAD_SECONDS = 0.5
#: Bound on PCM buffered ahead of the playback device (ADR 0102). Producers are real-time paced
#: (decode/browser/Mumble frames arrive at ~playback rate) so the buffer normally holds the 0.5 s
#: lead-in at key-up and then hovers near empty; the bound only bites if a producer briefly bursts
#: ahead, and then drop-oldest keeps TX latency bounded (the kv4p pacer / link-bridge idiom).
DEFAULT_TX_BUFFER_SECONDS = 2.0

_EXTRA_MSG = (
    "the AIOC/Baofeng backend needs the 'hardware' extra (pyserial + sounddevice): "
    "install with `pip install 'radio-server[hardware]'` (and the system libportaudio2)"
)


def _load_serial():
    try:
        import serial  # pyserial
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch in tests
        raise RuntimeError(_EXTRA_MSG) from exc
    return serial


def _default_serial_factory(port: str):
    """Open ``port`` with both control lines held **low from the moment it opens**.

    RF-safety (guardrail): some drivers pulse RTS/DTR on open (the Arduino-reset footgun), which
    would momentarily key the transmitter. ``pyserial`` applies ``.rts``/``.dtr`` set before
    ``open()`` as the initial line state, so we set both low first and only then open.
    """
    serial = _load_serial()
    handle = serial.Serial()
    handle.port = port
    handle.dtr = False
    handle.rts = False
    handle.open()
    return handle


class _AiocTxPacer:
    """Owns every blocking write to one keying's playback stream, on a daemon thread (ADR 0102).

    Producers (the asyncio event loop: ``TxSession.feed``, the D-STAR bridge, Mumble) call
    :meth:`enqueue` — non-blocking, bounded, drop-oldest by whole chunks — and the writer thread
    performs the blocking ``RawOutputStream.write`` calls, letting the device clock the drain.
    Unlike the kv4p ``_TxPacer`` (an Opus-encoder-owning slot timer that must synthesize silence),
    this pacer needs no cadence of its own: the AIOC's continuous output stream already emits
    silence when idle, and the blocking write paces the thread naturally.

    Chunk boundaries are preserved (a deque of ``bytes``), so the device sees the caller's frame
    shape; the byte bound is enforced across chunks with drop-oldest + ``dropped_bytes`` telemetry.

    RF-safety (ADR 0093 carried forward): a failed ``write`` on this thread invokes ``on_error``
    (the backend's key-off) after discarding the queue — a dying audio device must never hold the
    transmitter keyed. One pacer per physical keying; :meth:`stop` discards and joins.
    """

    def __init__(self, stream, *, max_buffer_bytes: int, on_error=None) -> None:
        self._stream = stream
        self._max = max_buffer_bytes
        self._on_error = on_error
        self._chunks: deque[bytes] = deque()
        self._buffered = 0  # bytes across _chunks
        self._cond = threading.Condition()
        self._writing = False  # a chunk is mid-write on the pacer thread
        self._stopped = False
        self._dropped = 0
        self._error: Exception | None = None  # the write failure that stopped the pacer, if any
        self._thread = threading.Thread(target=self._run, name="aioc-tx-pacer", daemon=True)
        self._thread.start()

    # --- producer side (event loop, or any transmit() caller) -----------------

    def enqueue(self, pcm: bytes) -> None:
        """Queue one PCM chunk for the writer thread; never blocks, drop-oldest over the bound."""
        if not pcm:
            return
        with self._cond:
            if self._stopped:
                return
            self._chunks.append(bytes(pcm))
            self._buffered += len(pcm)
            while self._buffered > self._max and len(self._chunks) > 1:
                old = self._chunks.popleft()
                self._buffered -= len(old)
                self._dropped += len(old)
            self._cond.notify_all()

    def wait_drained(self, timeout: float) -> bool:
        """Block until every queued chunk has been written (the one-shot contract). False on timeout."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._chunks or self._writing:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True

    @property
    def dropped_bytes(self) -> int:
        """PCM bytes discarded to the buffer bound (telemetry: a producer outpaced the device)."""
        with self._cond:
            return self._dropped

    @property
    def error(self) -> Exception | None:
        """The write failure that stopped this pacer, if any (the one-shot path re-raises it)."""
        with self._cond:
            return self._error

    # --- consumer side (writer thread) ----------------------------------------

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._chunks and not self._stopped:
                    self._cond.wait()
                if self._stopped:
                    return
                chunk = self._chunks.popleft()
                self._buffered -= len(chunk)
                self._writing = True
            try:
                self._stream.write(chunk)
            except Exception as exc:
                # The device died mid-write (unplug, xrun storm, closed stream). Discard, stop, and
                # unkey via on_error — the write failure must never strand the transmitter keyed.
                # The exception is kept for the one-shot path to re-raise (its blocking contract
                # includes surfacing playback failure to the caller).
                with self._cond:
                    self._error = exc
                    self._writing = False
                    self._stopped = True
                    self._chunks.clear()
                    self._buffered = 0
                    self._cond.notify_all()
                if self._on_error is not None:
                    with contextlib.suppress(Exception):
                        self._on_error()
                return
            with self._cond:
                self._writing = False
                if not self._chunks:
                    self._cond.notify_all()

    # --- lifecycle -------------------------------------------------------------

    def stop(self) -> None:
        """Discard everything queued and stop the thread (idempotent; safe from any thread).

        Deliberately does NOT drain: stop is the un-key path, and queued audio playing on after
        unkey is the "long FM tail" this pacer exists to kill. The join is bounded — worst case the
        thread is parked in one blocking chunk write against a stream the backend is closing.
        """
        with self._cond:
            already = self._stopped
            self._stopped = True
            self._chunks.clear()
            self._buffered = 0
            self._cond.notify_all()
        if already:
            return
        if self._dropped:
            logger.debug("aioc pacer: dropped %d buffered PCM bytes over the bound", self._dropped)
        thread = self._thread
        if thread is not threading.current_thread():
            thread.join(timeout=5.0)


class AiocBaofeng:
    """The UV-5R backend: USB-audio TX/RX + serial-line PTT, no CAT.

    Args:
        serial_port: PTT serial device (e.g. ``/dev/ttyACM0``).
        ptt_line: Which control line keys PTT — ``"dtr"`` (default, bench-confirmed cycle 29) or
            ``"rts"``. Empirical (guardrail 1); flip if the bench key-test shows the other line.
        input_device / output_device: ALSA device names for capture / playback (the AIOC card).
        blocksize: Frames per capture/playback block (:data:`DEFAULT_BLOCKSIZE`).
        tx_lead_seconds: Silence played right after PTT keys up, before real audio
            (:data:`DEFAULT_TX_LEAD_SECONDS`); prevents the transmitter/squelch key-up race from
            clipping the start of speech. 0 disables.
        _serial_factory: Test seam — ``(port) -> Serial-like`` with writable ``.rts``/``.dtr`` and
            ``.close()``. Defaults to opening a real ``pyserial`` port (lines held low on open).
        _audio: Test seam — a ``sounddevice``-like module exposing ``RawInputStream`` /
            ``RawOutputStream``. Defaults to the real (lazily imported) ``sounddevice``.
    """

    backend_name = "baofeng"

    def __init__(
        self,
        *,
        serial_port: str = DEFAULT_SERIAL_PORT,
        ptt_line: str = DEFAULT_PTT_LINE,
        input_device: str | int = DEFAULT_INPUT_DEVICE,
        output_device: str | int = DEFAULT_OUTPUT_DEVICE,
        blocksize: int = DEFAULT_BLOCKSIZE,
        tx_lead_seconds: float = DEFAULT_TX_LEAD_SECONDS,
        _serial_factory=None,
        _audio=None,
    ) -> None:
        try:
            self._ptt_line = PttLine(str(ptt_line).lower())
        except ValueError as exc:
            choices = ", ".join(m.value for m in PttLine)
            raise ValueError(f"ptt_line={ptt_line!r} is not one of: {choices}") from exc

        self._input_device = input_device
        self._output_device = output_device
        self._blocksize = blocksize
        # Precompute the TX lead-in as a raw silent-PCM byte count once (0 disables). Written to the
        # playback stream right after the line is asserted, so real audio starts only once the radio
        # is on the air — see _key_on().
        self._lead_bytes = round(CANONICAL_FORMAT.rate * float(tx_lead_seconds)) * CANONICAL_FORMAT.frame_bytes
        self._audio_mod = _audio  # None -> lazily import real sounddevice on first stream open

        # Open the serial handle now (the real backend needs the device present) and force BOTH
        # lines low, so construction can never leave the transmitter keyed (guardrail).
        self._serial = (_serial_factory or _default_serial_factory)(serial_port)
        self._serial.rts = False
        self._serial.dtr = False

        self._capture = None  # opened lazily on first receive()
        self._playback = None  # open only while the line is asserted
        self._pacer = None  # per-keying writer thread owning all playback writes (ADR 0102)
        self._keyed = False  # True while ptt(True) holds the line across frames (streaming)
        self._transmitting = False  # reflects the line being asserted (one-shot or held)
        self._closed = False
        # Never leave the radio keyed if the process dies mid-transmission.
        atexit.register(self.close)

    # --- audio plumbing -------------------------------------------------------

    def _sd(self):
        """The sounddevice-like module (injected fake, or the real library, lazily imported)."""
        if self._audio_mod is None:
            try:
                import sounddevice
            except (ImportError, OSError) as exc:  # OSError: PortAudio lib not found (libportaudio2)
                raise RuntimeError(_EXTRA_MSG) from exc
            self._audio_mod = sounddevice
        return self._audio_mod

    def _open_capture(self):
        stream = self._sd().RawInputStream(
            samplerate=CANONICAL_FORMAT.rate,
            blocksize=self._blocksize,
            device=self._input_device,
            channels=CANONICAL_FORMAT.channels,
            dtype="int16",
        )
        stream.start()
        return stream

    def _drop_line(self) -> None:
        """Drive the PTT line low — the unconditional un-key primitive, RF-safety's floor.

        Never guarded on ``_keyed`` or any tracked state, so a desynced flag can never leave the
        transmitter stranded keyed. A bare serial ``setattr`` (no drain, no stream teardown), so it
        cannot block or raise on the audio path. Every un-key route ends here (ADR 0093).
        """
        setattr(self._serial, self._ptt_line.value, False)
        self._transmitting = False

    def _key_on(self) -> None:
        """Open the playback stream, start its pacer, assert the PTT line, queue the TX lead-in.

        Ordering is an RF-safety invariant. The stream opens FIRST: if opening the audio device
        fails, the line is never asserted (a failed key-up must not leave the transmitter keyed).
        With the pacer (ADR 0102) no blocking write happens here any more: the 0.5 s lead-in is
        *enqueued* (cannot raise, cannot block), so the atomic-undo guard narrows to the line-assert
        itself. A lead-in (or any) write that later fails on the pacer thread unkeys via the pacer's
        ``on_error`` → :meth:`_key_off` — the ADR 0093 stranded-key guard, moved with the write.
        """
        stream = self._sd().RawOutputStream(
            samplerate=CANONICAL_FORMAT.rate,
            blocksize=self._blocksize,
            device=self._output_device,
            channels=CANONICAL_FORMAT.channels,
            dtype="int16",
        )
        stream.start()
        max_buffer = round(
            CANONICAL_FORMAT.rate * DEFAULT_TX_BUFFER_SECONDS
        ) * CANONICAL_FORMAT.frame_bytes
        # The bound must always admit the lead-in slug plus headroom for real-time producers.
        pacer = _AiocTxPacer(
            stream,
            max_buffer_bytes=max(max_buffer, self._lead_bytes * 2),
            on_error=self._key_off,
        )
        try:
            setattr(self._serial, self._ptt_line.value, True)
            self._transmitting = True
        except Exception:
            # Atomic key-up: undo everything, so a partial failure never strands the transmitter keyed.
            with contextlib.suppress(Exception):
                self._drop_line()
            pacer.stop()
            with contextlib.suppress(Exception):
                stream.stop()
            with contextlib.suppress(Exception):
                stream.close()
            raise
        # TX lead-in (guardrail 1): with the line asserted, queue a fixed slug of silence so the
        # transmitter and the far-end squelch are fully up before real audio plays — otherwise the
        # first fraction of a second of speech is clipped over the air. Fires exactly once per
        # physical key-up (backs both one-shot transmit() and streaming ptt(True)).
        if self._lead_bytes:
            pacer.enqueue(b"\x00" * self._lead_bytes)
        self._playback = stream
        self._pacer = pacer

    def _key_off(self) -> None:
        """Drop the PTT line FIRST, then stop the pacer (discarding), then close the stream.

        RF-safety inversion of the original drain-then-drop (ADR 0029 → ADR 0093): dropping the
        transmitter must NEVER depend on the audio-stream teardown succeeding. A drain that blocks, or
        a ``stop()``/``close()`` that raises on an xrun'd/starved stream, must not keep the line
        asserted — the exact way the crossband stranded the transmitter keyed. So the line goes low
        immediately and unconditionally (``_drop_line``); the pacer then discards anything still
        queued (ADR 0102 — buffered audio playing on after unkey was the "long FM tail") and the
        stream is torn down best-effort. Idempotent; also the pacer's write-failure ``on_error``.
        """
        self._drop_line()
        pacer, self._pacer = self._pacer, None
        if pacer is not None:
            pacer.stop()
        stream, self._playback = self._playback, None
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.stop()
            with contextlib.suppress(Exception):
                stream.close()

    # --- shared surface -------------------------------------------------------

    def transmit(self, audio: AudioFrame) -> None:
        if audio.format != CANONICAL_FORMAT:
            raise AudioFormatMismatch(
                f"radio accepts {CANONICAL_FORMAT}, got a frame in {audio.format}"
            )
        if self._keyed:
            # Streaming: ptt(True) already holds the line and started the pacer — queue the frame
            # and return at once. The blocking device write happens on the pacer thread (ADR 0102),
            # never on the caller (the event loop). A pacer torn down by a device failure swallows
            # the frame; the failure path has already unkeyed.
            pacer = self._pacer
            if pacer is not None:
                pacer.enqueue(audio.samples)
            return
        # One-shot: self-key for exactly the duration of this clip. The blocking contract stands —
        # callers (station ID, TTS, /transmit) rely on "returns once the clip has been played" — so
        # wait for the pacer to drain the lead-in + clip before dropping the key.
        byte_rate = CANONICAL_FORMAT.rate * CANONICAL_FORMAT.frame_bytes
        duration = (self._lead_bytes + len(audio.samples)) / byte_rate
        self._key_on()
        try:
            pacer = self._pacer
            if pacer is not None:
                pacer.enqueue(audio.samples)
                pacer.wait_drained(duration + 2.0)
                error = pacer.error
                if error is not None:
                    raise error  # playback failed — surface it (the pacer already unkeyed)
        finally:
            self._key_off()

    def receive(self) -> AudioFrame:
        if self._capture is None:
            self._capture = self._open_capture()
        data, _overflowed = self._capture.read(self._blocksize)
        # An xrun (overflow) is not fatal — the samples we did get are still valid audio.
        return AudioFrame(bytes(data), CANONICAL_FORMAT)

    def ptt(self, on: bool) -> None:
        if on:
            if not self._keyed:
                self._key_on()
                self._keyed = True
        else:
            # Unconditional un-key (ADR 0093): ptt(False) is the caller's safety lever — the watchdog,
            # the bridge teardown, the REST /ptt off — and it must ALWAYS drive the line low, even if
            # we believe we are not keyed (a key-up that failed after asserting the line, or any flag
            # desync). `_key_off` drops the line first, then tears down any stream. Idempotent.
            self._keyed = False
            self._key_off()

    def status(self) -> RadioStatus:
        # No hardware busy/COS line on the UV-5R (ADR 0015): busy is always False here; RX gating is
        # software VAD (audio.squelch=audio), not a carrier-detect the radio reports. CAT fields stay
        # None. `transmitting` tracks whether the PTT line is currently asserted.
        return RadioStatus(
            backend=self.backend_name,
            transmitting=self._transmitting,
            busy=False,
        )

    def capabilities(self) -> frozenset[Capability]:
        return SHARED_CAPS

    # --- lifecycle ------------------------------------------------------------

    def close(self) -> None:
        """Drop the line, close both streams and the serial handle. Idempotent; safe at exit."""
        if self._closed:
            return
        self._closed = True
        try:
            self._key_off()  # drops the line and closes playback if keyed
        except Exception:
            pass
        # Belt-and-suspenders: force both lines low even if _key_off did not run cleanly.
        try:
            self._serial.rts = False
            self._serial.dtr = False
        except Exception:
            pass
        if self._capture is not None:
            try:
                self._capture.stop()
                self._capture.close()
            except Exception:
                pass
            self._capture = None
        try:
            self._serial.close()
        except Exception:
            pass
        atexit.unregister(self.close)
