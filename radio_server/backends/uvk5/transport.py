"""Serial transport for the UV-K5 Quansheng Dock (ADR 0110, ADR 0111) — the I/O layer
under :mod:`.frames`.

This is the piece that touches a wire: it opens the AIOC serial port at 38400 8N1, runs a
daemon **reader thread** that feeds bytes through :class:`~.frames.Uvk5Decoder` and
dispatches decoded messages to blocked callers, and exposes a **request/reply** primitive
plus a link-liveness :meth:`connect`. It does not implement the ``Radio``/``CatRadio``
surface — the backend class that composes this transport with tuning logic is a later cycle.

Unlike the kv4p transport (flow-control window + sequence reconciler), the dock protocol is
a plain **request/reply** command protocol: no credits, no sequence numbers, no persisted
desired state. So this transport is deliberately simpler.

Like :class:`~radio_server.backends.aioc_baofeng.AiocBaofeng`, ``pyserial`` is the
``hardware`` optional extra, imported lazily so importing this module (and the whole test
suite) stays hardware-free; the constructor accepts an injected ``_serial_factory`` for unit
tests against the firmware-accurate fake.

Design facts, all read verbatim from the pins as a spec (firmware ``quansheng-dock-fw``
0.32.21q ``4375c3e…`` ``app/uart.c``; client ``QuanshengDock`` 0.32.21q ``851efa9…``
``Serial/Comms.cs``); see ADR 0111:

  1. **The dock does not stream at top level.** The firmware only replies to requests
     (``UART_HandleCommand``, uart.c:1042-1140); it emits unsolicited ``0xB5`` UI/DTMF
     packets only inside full-control mode (uart.c:728-733) or remote-UI mode. So
     :meth:`connect` cannot listen passively (as kv4p does) — it must **elicit**: send a
     benign register-read probe and wait for the reply, retransmitting until one arrives.
     Silence therefore means "no answer" — a timeout, not a normal steady state.

  2. **Hold DTR and RTS inactive before opening.** This AIOC serial line also carries PTT
     (DTR/RTS), exactly as the Baofeng backend — pulsing a control line at open would key
     the transmitter. We hold both low before ``open()``. Whether opening the port also
     resets/reboots the UV-K5 is unknowable offline; :meth:`connect`'s retransmit tolerates
     a boot race either way (verify on hardware — guardrail 1).

  3. **Replies carry a dummy CRC.** Firmware ``SendReply`` puts ``obf(0xFF 0xFF)`` where a
     command's CRC would be (uart.c:270-279), so the decoder runs ``validate_crc=False``.

Guardrail 2 (ADR 0002) holds: PTT is the AIOC serial control line, never a dock command.
This transport never asserts DTR/RTS; sharing the one AIOC handle between dock data and the
PTT line is a backend-class concern (verify on hardware — guardrail 1).
"""

from __future__ import annotations

import atexit
import logging
import threading
import time
from collections import deque
from typing import Callable

from .frames import (
    ReadRegisters,
    RegisterInfo,
    Uvk5Decoder,
    build_frame,
    parse_frame,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------------------
# Defaults (marked, guardrail 1 — verify against hardware)
# --------------------------------------------------------------------------------------

#: Dock UART line rate. 8N1 are pyserial's defaults, so only the baud is set. The stock
#: Quansheng speed (client ``Comms.cs:101``). VERIFY ON BENCH (guardrail 1).
DEFAULT_BAUD = 38400
#: Serial device. The AIOC enumerates as native CDC ``/dev/ttyACM0`` (matching the Baofeng
#: backend); the stable, reorder-proof path is ``/dev/serial/by-id/usb-*All-In-One-Cable*``.
#: VERIFY ON BENCH — the real path is hardware-specific (guardrail 1).
DEFAULT_SERIAL_PORT = "/dev/ttyACM0"
#: Seconds :meth:`connect` retransmits the register-read probe before raising. Opening the
#: port may reset the radio; a fresh connect can race a boot, so the budget spans it with
#: margin and the retransmits keep it responsive. VERIFY ON BENCH (guardrail 1).
DEFAULT_CONNECT_TIMEOUT = 10.0
#: Seconds a :meth:`request` waits for its reply before raising :class:`Uvk5Timeout`.
DEFAULT_REQUEST_TIMEOUT = 2.0
#: Seconds a blocking serial write waits before raising. Bounds a stuck write so a call
#: never hangs.
DEFAULT_WRITE_TIMEOUT = 2.0

#: BK4819 register read as the connect liveness probe — a read changes no radio state, and
#: ``0x0851`` is dispatched at top level (uart.c:1115), so it works without entering
#: full-control mode. ``0x30`` is a real tuning/control register. VERIFY ON BENCH.
_PROBE_REGISTER = 0x30
#: Seconds between connect-probe retransmits.
_ELICIT_RETRANSMIT_INTERVAL = 0.25

#: Read timeout (s): keeps a blocking ``read()`` returning periodically so the reader loop
#: can observe the stop flag. Only bounds idle latency.
_READ_TIMEOUT = 0.1
#: Bytes requested per ``read()``. A ceiling, not a floor — ``read`` returns what is ready.
_READ_SIZE = 4096
#: Bounded depth for unsolicited/unmatched messages (drop-oldest, never blocks the reader).
_INBOX_DEPTH = 256


class Uvk5Timeout(RuntimeError):
    """A blocking request or write timed out."""


class Uvk5Closed(RuntimeError):
    """The transport was closed while a request or write was in flight."""


_EXTRA_MSG = (
    "the UV-K5/Quansheng Dock backend needs the 'hardware' extra (pyserial): install with "
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

    This AIOC serial line also carries PTT (DTR/RTS), so pulsing a line at open would key
    the transmitter (ADR 0111; the Baofeng backend does the same). ``pyserial`` applies
    ``.dtr``/``.rts`` set before ``open()`` as the initial line state, so we set both low
    first and only then open.
    """
    serial = _load_serial()
    handle = serial.Serial()
    handle.port = port
    handle.baudrate = baud
    handle.timeout = _READ_TIMEOUT
    handle.write_timeout = DEFAULT_WRITE_TIMEOUT
    handle.dtr = False
    handle.rts = False
    handle.open()
    return handle


class Uvk5Transport:
    """Owns the AIOC serial handle, the reader thread, and the request/reply machinery."""

    def __init__(
        self,
        *,
        serial_port: str = DEFAULT_SERIAL_PORT,
        baud: int = DEFAULT_BAUD,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        obfuscate: bool = True,
        _serial_factory: Callable[[str, int], object] | None = None,
    ) -> None:
        self._serial = (_serial_factory or _default_serial_factory)(serial_port, baud)
        self._request_timeout = request_timeout
        self._obfuscate = obfuscate
        self._decoder = Uvk5Decoder(obfuscated=obfuscate, validate_crc=False)

        self._cond = threading.Condition()
        self._waiters: list[dict] = []
        self._inbox: deque = deque(maxlen=_INBOX_DEPTH)
        self._reader_error: Exception | None = None
        self._closed = False

        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, name="uvk5-reader", daemon=True)
        self._reader.start()
        # Never leave the port open if the process dies.
        atexit.register(self.close)

    # -- reader thread -----------------------------------------------------------------

    def _read_loop(self) -> None:
        """Runs on the daemon reader thread: read -> deframe -> dispatch, until stopped."""
        while not self._stop.is_set():
            try:
                chunk = self._serial.read(_READ_SIZE)
            except Exception as exc:  # SerialException et al. — surface it, don't wedge
                self._fail(exc)
                return
            if not chunk:
                continue  # read timeout (b"") — loop back and re-check the stop flag
            try:
                for payload in self._decoder.feed(chunk):
                    parsed = parse_frame(payload)
                    if parsed is not None:
                        self._dispatch(parsed)
            except Exception:  # a single malformed frame must not kill the reader
                logger.exception("uvk5: error dispatching frame")

    def _dispatch(self, msg: object) -> None:
        with self._cond:
            for waiter in self._waiters:
                if not waiter["done"] and waiter["match"](msg):
                    waiter["result"] = msg
                    waiter["done"] = True
                    self._cond.notify_all()
                    return
            self._inbox.append(msg)  # unsolicited / unmatched (bounded, drop-oldest)
            self._cond.notify_all()

    def _fail(self, exc: Exception) -> None:
        with self._cond:
            self._reader_error = exc
            self._cond.notify_all()
        logger.error("uvk5: reader thread stopped on %r", exc)

    def _raise_if_failed(self) -> None:
        if self._reader_error is not None:
            raise self._reader_error

    # -- writing -----------------------------------------------------------------------

    def send(self, msg) -> None:
        """Build a frame for a command message and write it — fire-and-forget (no reply)."""
        with self._cond:
            if self._closed:
                raise Uvk5Closed("transport closed")
            self._raise_if_failed()
        frame = build_frame(int(msg.COMMAND), msg.pack(), obfuscate_body=self._obfuscate)
        try:
            self._serial.write(frame)
        except Exception as exc:  # SerialTimeoutException et al.
            raise Uvk5Timeout(f"serial write failed: {exc!r}") from exc

    def request(self, msg, match: Callable[[object], bool], timeout: float | None = None):
        """Send *msg* and block until a dispatched message satisfies *match*, or raise.

        Registers the waiter **before** writing so a fast reply is never missed. Raises
        :class:`Uvk5Timeout` on the deadline, :class:`Uvk5Closed` if closed mid-wait, or the
        reader's stored error if the port died.
        """
        timeout = self._request_timeout if timeout is None else timeout
        waiter = {"match": match, "result": None, "done": False}
        with self._cond:
            if self._closed:
                raise Uvk5Closed("transport closed")
            self._raise_if_failed()
            self._waiters.append(waiter)
        try:
            self.send(msg)
            deadline = time.monotonic() + timeout
            with self._cond:
                while not waiter["done"]:
                    self._raise_if_failed()
                    if self._closed:
                        raise Uvk5Closed("transport closed")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise Uvk5Timeout(f"no matching reply within {timeout}s")
                    self._cond.wait(remaining)
                return waiter["result"]
        finally:
            with self._cond:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)

    # -- connect -----------------------------------------------------------------------

    def connect(self, timeout: float = DEFAULT_CONNECT_TIMEOUT) -> None:
        """Prove the link is alive by eliciting a register-read reply.

        The dock does not stream unsolicited traffic at top level, so we send a benign
        ``ReadRegisters`` probe and wait for a :class:`~.frames.RegisterInfo`, retransmitting
        until one arrives or the budget runs out. The retransmit tolerates a possible
        reset-on-open boot race (verify on hardware). The full enter-XVFO handshake
        (``0x0870`` + setup + readback) is the backend class's job.
        """
        deadline = time.monotonic() + timeout
        probe = ReadRegisters((_PROBE_REGISTER,))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Uvk5Timeout(
                    f"the UV-K5 never answered a register-read probe within {timeout}s "
                    "(radio off/asleep, wrong baud, or not running Quansheng Dock firmware)"
                )
            try:
                self.request(
                    probe,
                    lambda m: isinstance(m, RegisterInfo),
                    timeout=min(_ELICIT_RETRANSMIT_INTERVAL, remaining),
                )
                return
            except Uvk5Timeout:
                continue  # retransmit the probe

    # -- inbox (unsolicited / unmatched) -----------------------------------------------

    def drain_inbox(self) -> list:
        """Return and clear the buffered unsolicited/unmatched messages."""
        with self._cond:
            items = list(self._inbox)
            self._inbox.clear()
            return items

    # -- lifecycle ---------------------------------------------------------------------

    def close(self) -> None:
        """Stop the reader and close the port. Idempotent; safe at exit."""
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._cond.notify_all()
        self._stop.set()
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1.0)
        try:
            self._serial.close()
        except Exception:
            pass
        atexit.unregister(self.close)
