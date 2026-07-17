"""Serial transport for the kv4p HT (ADR 0061, ADR 0062) — the I/O layer under `frames.py`.

This is the piece that finally touches a wire: it opens the CP210x/CH340 UART at 115200
8N1, runs a daemon **reader thread** that feeds bytes through :class:`~.frames.KissDecoder`
and dispatches decoded frames to their sinks, tracks the **flow-control window** in
*encoded* bytes, and owns the **reconciler's sequence bookkeeping**
(:meth:`send_desired_state` / :meth:`await_applied`). It does not implement the
``Radio``/``CatRadio`` surface — the ``Kv4pHt`` backend class that composes this transport
with :mod:`.audio` and :mod:`.frames` is a later cycle.

Like :class:`~radio_server.backends.aioc_baofeng.AiocBaofeng`, ``pyserial`` is the
``hardware`` optional extra and is imported lazily, so importing this module (and the whole
test suite) stays hardware-free; the constructor accepts an injected ``_serial_factory`` for
unit tests.

Two firmware facts drive the design (ADR 0062; source read as a spec, not ported — kv4p-ht
GPL-3.0 @ ``e9935bd37e7505f70ae7023c78fe6a714be90be9``,
``kv4p_ht_esp32_wroom_32/kv4p_ht_esp32_wroom_32.ino``):

  1. **Connect by syncing ``DeviceState.appliedSequence``, never by waiting for a HELLO.**
     The USB session's HELLO fires once at the end of ``setup()``; a host attaching to an
     already-running device never sees it, and the device's ``sequence`` is RAM-only and
     monotonic *within a boot* — so a fresh host counting from 1 against a running ESP32 is
     silently ignored (`incoming.sequence > desiredState.sequence`). :meth:`connect` sends a
     probe ``HostDesiredState`` with ``ENABLE_STATUS_REPORTS`` set — the firmware applies
     *session* flags unconditionally, *before* the sequence comparison, so the probe still
     turns on status reports and triggers a ``DeviceState`` push even with a stale sequence —
     then syncs our counter to the reported ``appliedSequence``. A HELLO, if one arrives, is
     a bonus (its windowSize/module/freq range are adopted); it is never a precondition.

  2. **Hold DTR and RTS inactive before opening.** On ESP32 boards DTR/RTS drive the
     auto-reset circuit (EN / GPIO0); pyserial asserting them at open can reset the device or
     drop it into the bootloader. We hold both low before ``open()`` (the AIOC shape, for a
     different reason) and deliberately do **not** reset-to-get-a-HELLO — that would reboot
     the radio on every server restart, and the appliedSequence sync makes it unnecessary.

Guardrail 2 (ADR 0002) holds trivially: PTT is a flag inside ``HostDesiredState``, set by
:meth:`send_desired_state`; there is no command path to key over.
"""

from __future__ import annotations

import atexit
import dataclasses
import logging
import threading
import time
from collections import deque

from .frames import (
    Ax25Frame,
    DeviceState,
    Hello,
    HostDesiredState,
    HostStateFlag,
    RcvCommand,
    SndCommand,
    VendorFrame,
    WindowUpdate,
    build_vendor_frame,
    KissDecoder,
    parse_frame,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------------------
# Defaults (marked, guardrail 1 — verify against hardware)
# --------------------------------------------------------------------------------------

#: kv4p UART line rate. 8N1 are pyserial's defaults, so only the baud is set.
DEFAULT_BAUD = 115200
#: Flow-control window, in *encoded* bytes: the firmware's ``USB_BUFFER_SIZE``. VERIFY ON
#: BENCH (guardrail 1) — the device never tells us this unless a HELLO arrives.
DEFAULT_WINDOW_SIZE = 2048
#: Serial device. The CP210x/CH340 enumerates as ``/dev/ttyUSB0`` (unlike the AIOC's native
#: CDC ``/dev/ttyACM0``); the stable, reorder-proof path is ``/dev/serial/by-id/*``. VERIFY
#: ON BENCH — the real path/name is hardware-specific (guardrail 1).
DEFAULT_SERIAL_PORT = "/dev/ttyUSB0"
#: Seconds a blocking write waits for enough window credits before raising :class:`Kv4pTimeout`.
DEFAULT_WRITE_TIMEOUT = 2.0
#: Seconds :meth:`connect` waits for the probe's ``DeviceState`` before raising.
DEFAULT_CONNECT_TIMEOUT = 2.0
#: Seconds :meth:`close` waits to confirm the PTT-off reconcile applied before tearing down
#: regardless. Deliberately short — shutdown must never hang on a device that stopped answering.
_CLOSE_ACK_TIMEOUT = 0.5
#: RX-audio hand-off queue depth (ADPCM payloads). Bounded + drop-oldest so a slow consumer
#: never blocks the reader thread (the ``MultimonStream`` idiom, ``audio/dtmf.py``).
DEFAULT_RX_AUDIO_DEPTH = 256

#: Read timeout (s): keeps a blocking ``read()`` returning periodically so the reader loop can
#: observe the stop flag. Larger reads are drained in one call; this only bounds idle latency.
_READ_TIMEOUT = 0.1
#: Bytes requested per ``read()``. A ceiling, not a floor — ``read`` returns whatever is ready.
_READ_SIZE = 4096

#: Device debug frames -> stdlib logging levels (there is no TRACE level; it folds into DEBUG).
_DEBUG_LEVELS: dict[SndCommand, int] = {
    SndCommand.DEBUG_ERROR: logging.ERROR,
    SndCommand.DEBUG_WARN: logging.WARNING,
    SndCommand.DEBUG_INFO: logging.INFO,
    SndCommand.DEBUG_DEBUG: logging.DEBUG,
    SndCommand.DEBUG_TRACE: logging.DEBUG,
}


class Kv4pTimeout(RuntimeError):
    """A blocking write ran out of window credits, or a reconciler wait timed out."""


class Kv4pClosed(RuntimeError):
    """The transport was closed while a write or wait was in flight."""


# --------------------------------------------------------------------------------------
# Serial factory (the DI seam; RF/reset-safe open)
# --------------------------------------------------------------------------------------

_EXTRA_MSG = (
    "the kv4p backend needs the 'hardware' extra (pyserial): install with "
    "`pip install 'radio-server[hardware]'`"
)


def _load_serial():
    try:
        import serial  # pyserial
    except ImportError as exc:  # pragma: no cover - exercised via the injected fake in tests
        raise RuntimeError(_EXTRA_MSG) from exc
    return serial


def _default_serial_factory(port: str, baud: int):
    """Open ``port`` at ``baud`` with DTR and RTS held **low from the moment it opens**.

    On ESP32 boards those lines drive the auto-reset circuit, so pulsing them at open can
    reset the device (ADR 0062, Decision 2). ``pyserial`` applies ``.dtr``/``.rts`` set before
    ``open()`` as the initial line state, so we set both low first and only then open.
    """
    serial = _load_serial()
    handle = serial.Serial()
    handle.port = port
    handle.baudrate = baud
    handle.timeout = _READ_TIMEOUT
    handle.dtr = False
    handle.rts = False
    handle.open()
    return handle


class Kv4pTransport:
    """Serial transport for the kv4p HT.

    Args:
        serial_port: UART device (:data:`DEFAULT_SERIAL_PORT`).
        baud: Line rate (:data:`DEFAULT_BAUD`).
        window_size: Initial flow-control credits in encoded bytes (:data:`DEFAULT_WINDOW_SIZE`);
            replaced if a HELLO advertises a different size.
        write_timeout: Seconds a credit-starved write waits before raising (:class:`Kv4pTimeout`).
        rx_audio_depth: Bounded RX-audio queue depth (drop-oldest).
        _serial_factory: Test seam — ``(port, baud) -> Serial-like`` with a blocking ``read``,
            ``write``, writable ``.dtr``/``.rts`` and ``.close()``. Defaults to a real pyserial
            port opened with both control lines held low.

    Construction opens the port and starts the reader thread but does **not** connect — call
    :meth:`connect` to run the appliedSequence handshake.
    """

    def __init__(
        self,
        *,
        serial_port: str = DEFAULT_SERIAL_PORT,
        baud: int = DEFAULT_BAUD,
        window_size: int = DEFAULT_WINDOW_SIZE,
        write_timeout: float = DEFAULT_WRITE_TIMEOUT,
        rx_audio_depth: int = DEFAULT_RX_AUDIO_DEPTH,
        _serial_factory=None,
    ) -> None:
        self._serial = (_serial_factory or _default_serial_factory)(serial_port, baud)
        self._write_timeout = write_timeout
        self._decoder = KissDecoder()

        # Flow control: credits are encoded bytes the device buffer can still hold. A write
        # spends len(frame) (escaped, FENDs included — build_vendor_frame returns the on-wire
        # bytes, so len() *is* the encoded length); a WINDOW_UPDATE refunds the same count.
        self._credit_cond = threading.Condition()
        self._window_size = window_size
        self._credits = window_size

        # Reconciler: `_sequence` is the last sequence number we sent (or synced to); the next
        # send is `_sequence + 1`. `_applied_sequence` echoes the device's last-applied.
        self._state_cond = threading.Condition()
        self._sequence = 0
        self._applied_sequence = 0
        self._device_state: DeviceState | None = None
        self._state_epoch = 0  # bumped on every DeviceState — connect waits on it

        # Session flags ride *every* outgoing frame (reset per connection); global flags come
        # from the caller's desired state and persist. See the frames.py mask split.
        self._session_flags = HostStateFlag(0)

        # Hardware identity, only known if a HELLO arrives (fresh boot).
        self._hello: Hello | None = None

        # RX audio hand-off: bounded, drop-oldest, single-writer (the reader thread).
        self._rx_audio: deque[bytes] = deque(maxlen=rx_audio_depth)
        self._rx_drops = 0

        self._stop = threading.Event()
        self._reader_error: Exception | None = None
        self._closed = False

        self._reader = threading.Thread(target=self._read_loop, name="kv4p-reader", daemon=True)
        self._reader.start()
        # Never leave the port open (or the radio keyed) if the process dies.
        atexit.register(self.close)

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
                for frame in self._decoder.feed(chunk):
                    self._dispatch(frame)
            except Exception:  # a single malformed frame must not kill the reader
                logger.exception("kv4p: error dispatching frame")

    def _fail(self, exc: Exception) -> None:
        """Record a fatal read error and wake every blocked caller so they re-raise it."""
        self._reader_error = exc
        logger.error("kv4p: reader thread stopped on %r", exc)
        with self._credit_cond:
            self._credit_cond.notify_all()
        with self._state_cond:
            self._state_cond.notify_all()

    def _dispatch(self, frame: bytes) -> None:
        parsed = parse_frame(frame)
        if parsed is None:
            return  # non-zero port / unknown KISS command / malformed vendor frame
        if isinstance(parsed, Ax25Frame):
            # Separate dispatch path (future text-over-RF); inert here — never a vendor sink.
            logger.debug("kv4p: AX.25 frame, %d bytes (inert)", len(parsed.payload))
            return
        self._dispatch_vendor(parsed)

    def _dispatch_vendor(self, vf: VendorFrame) -> None:
        try:
            command = SndCommand(vf.command)
        except ValueError:
            logger.debug(
                "kv4p: unknown device command 0x%02x, %d bytes", vf.command, len(vf.payload)
            )
            return

        if command == SndCommand.RX_AUDIO:
            self._push_rx_audio(vf.payload)
        elif command == SndCommand.DEVICE_STATE:
            self._on_device_state(vf.payload)
        elif command == SndCommand.HELLO:
            self._on_hello(vf.payload)
        elif command == SndCommand.WINDOW_UPDATE:
            self._on_window_update(vf.payload)
        elif command in _DEBUG_LEVELS:
            text = vf.payload.decode("utf-8", "replace")
            logger.log(_DEBUG_LEVELS[command], "kv4p device: %s", text)
        else:  # e.g. UNKNOWN — nothing to route
            logger.debug("kv4p: unhandled device command %s, %d bytes", command, len(vf.payload))

    def _push_rx_audio(self, payload: bytes) -> None:
        # Single writer (this thread); count a drop when the bounded deque is already full.
        if self._rx_audio.maxlen is not None and len(self._rx_audio) == self._rx_audio.maxlen:
            self._rx_drops += 1
        self._rx_audio.append(payload)

    def _on_device_state(self, payload: bytes) -> None:
        state = DeviceState.unpack(payload)
        with self._state_cond:
            self._device_state = state
            self._applied_sequence = state.applied_sequence
            self._state_epoch += 1
            self._state_cond.notify_all()

    def _on_hello(self, payload: bytes) -> None:
        hello = Hello.unpack(payload)
        self._hello = hello
        # A HELLO is authoritative for the window size; adopt it and reconcile live credits by
        # the delta (a HELLO normally precedes any host write on a fresh boot, so credits are
        # still at the seeded ceiling and the delta is exact).
        new_window = hello.version.window_size
        with self._credit_cond:
            self._credits += new_window - self._window_size
            self._window_size = new_window
            self._credit_cond.notify_all()
        # The HELLO also carries an initial DeviceState — treat it like a state report.
        with self._state_cond:
            self._device_state = hello.device_state
            self._applied_sequence = hello.device_state.applied_sequence
            self._state_epoch += 1
            self._state_cond.notify_all()

    def _on_window_update(self, payload: bytes) -> None:
        update = WindowUpdate.unpack(payload)
        with self._credit_cond:
            self._credits += update.size  # encoded-byte refund (protocol.h _encodedFrameLen)
            self._credit_cond.notify_all()

    # --- flow-controlled write ------------------------------------------------

    def _write_frame(self, built: bytes) -> None:
        """Block until the window has room for these encoded bytes, then write them."""
        need = len(built)  # the on-wire (escaped, FEND-delimited) length — what the device acks
        deadline = time.monotonic() + self._write_timeout
        with self._credit_cond:
            while self._credits < need:
                self._raise_if_failed()
                if self._closed:
                    raise Kv4pClosed("transport closed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Kv4pTimeout(
                        f"no window credit for a {need}-byte frame after {self._write_timeout}s"
                    )
                self._credit_cond.wait(remaining)
            self._credits -= need
        self._serial.write(built)

    def _raise_if_failed(self) -> None:
        if self._reader_error is not None:
            raise self._reader_error

    # --- reconciler -----------------------------------------------------------

    def send_desired_state(self, state: HostDesiredState) -> int:
        """Assign the next sequence, OR in the session flags, encode, and write. Returns the seq.

        The caller supplies the *global* desired state (config/PTT/power/filters); the sequence
        and the session flags (RX audio / status reports) are owned here.
        """
        self._raise_if_failed()
        self._sequence += 1
        seq = self._sequence
        outgoing = dataclasses.replace(
            state, sequence=seq, flags=int(state.flags) | int(self._session_flags)
        )
        self._write_frame(build_vendor_frame(RcvCommand.HOST_DESIRED_STATE, outgoing.pack()))
        return seq

    def send_tx_audio(self, block: bytes) -> None:
        """Send one ADPCM audio block as ``HOST_TX_AUDIO`` through the flow-control window.

        TX audio is the bulk of the link (≈ 77% of the line), so it rides the same encoded-byte
        credit window as every other frame: this blocks until the window has room and raises
        :class:`Kv4pTimeout` rather than overrunning the device buffer. The reconciler's
        sequence bookkeeping does not apply — audio frames carry no sequence.
        """
        self._raise_if_failed()
        self._write_frame(build_vendor_frame(RcvCommand.HOST_TX_AUDIO, block))

    def await_applied(self, seq: int, timeout: float) -> DeviceState:
        """Wait until the device reports having applied at least ``seq``; return its DeviceState."""
        deadline = time.monotonic() + timeout
        with self._state_cond:
            while self._applied_sequence < seq:
                self._raise_if_failed()
                if self._closed:
                    raise Kv4pClosed("transport closed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Kv4pTimeout(f"device did not apply sequence {seq} within {timeout}s")
                self._state_cond.wait(remaining)
            assert self._device_state is not None  # applied_sequence only moves with a state
            return self._device_state

    def connect(self, timeout: float = DEFAULT_CONNECT_TIMEOUT) -> DeviceState:
        """Run the appliedSequence handshake (ADR 0062, Decision 1); return the device's state.

        Sends a probe desired state with status reports enabled — which the firmware honours
        regardless of the (still-unknown) sequence — waits for the resulting DeviceState, then
        syncs our counter so the next real send is ``appliedSequence + 1``.
        """
        # Turn on status reports for this session; the flag then rides every subsequent frame.
        self._session_flags |= HostStateFlag.ENABLE_STATUS_REPORTS
        with self._state_cond:
            start_epoch = self._state_epoch
        # A neutral probe: no RADIO_CONFIG_VALID, so even if applied it reconfigures nothing.
        self.send_desired_state(_NEUTRAL_STATE)
        deadline = time.monotonic() + timeout
        with self._state_cond:
            while self._state_epoch == start_epoch:
                self._raise_if_failed()
                if self._closed:
                    raise Kv4pClosed("transport closed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Kv4pTimeout(f"no DeviceState from the device within {timeout}s")
                self._state_cond.wait(remaining)
            state = self._device_state
            assert state is not None
            # Sync: the next send_desired_state (+1) lands just above what the device has applied.
            self._sequence = state.applied_sequence
        return state

    # --- accessors (for the future backend) -----------------------------------

    def read_audio(self) -> bytes | None:
        """Pop the oldest queued RX-audio payload, or ``None`` if the queue is empty."""
        try:
            return self._rx_audio.popleft()
        except IndexError:
            return None

    @property
    def rx_audio_drops(self) -> int:
        """Count of RX-audio payloads dropped because the bounded queue was full."""
        return self._rx_drops

    @property
    def device_state(self) -> DeviceState | None:
        """The most recent DeviceState the device reported, or ``None`` before the first."""
        return self._device_state

    @property
    def hello(self) -> Hello | None:
        """The HELLO the device sent on a fresh boot, or ``None`` if none was seen."""
        return self._hello

    @property
    def window_size(self) -> int:
        """The current flow-control window (the HELLO's if adopted, else the seeded default)."""
        return self._window_size

    # --- lifecycle ------------------------------------------------------------

    def close(self) -> None:
        """Reconcile PTT off, stop the reader, close the port. Idempotent; safe at exit.

        The safe state here is a *reconciled flag*, not a dropped control line (there is no line
        to drop). Best-effort: if the port or reader is already gone, teardown proceeds anyway.
        """
        if self._closed:
            return
        # Best-effort safe shutdown BEFORE we tear down: a desired state with PTT_REQUESTED clear
        # (flags = 0 | session flags), confirmed applied. Swallow everything — a dead port or a
        # credit-starved window must never make close() raise or hang past the write timeout.
        try:
            seq = self.send_desired_state(_NEUTRAL_STATE)
            self.await_applied(seq, timeout=_CLOSE_ACK_TIMEOUT)
        except Exception:
            pass

        self._closed = True
        self._stop.set()
        with self._credit_cond:
            self._credit_cond.notify_all()
        with self._state_cond:
            self._state_cond.notify_all()
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1.0)
        try:
            self._serial.close()
        except Exception:
            pass
        atexit.unregister(self.close)


#: A do-nothing desired state: neutral global fields, no RADIO_CONFIG_VALID and no PTT_REQUESTED,
#: so the firmware reconfigures nothing if it applies it. Used for the connect probe and the
#: close-time PTT-clear; the sequence and session flags are filled in by send_desired_state.
_NEUTRAL_STATE = HostDesiredState(
    sequence=0,
    memory_id=0,
    flags=0,
    bw=0,
    freq_tx=0.0,
    freq_rx=0.0,
    ctcss_tx=0,
    squelch=0,
    ctcss_rx=0,
)
