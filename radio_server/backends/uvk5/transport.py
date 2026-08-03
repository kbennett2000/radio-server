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

Like :class:`~radio_server.backends.aioc_baofeng.AiocBaofeng`, ``pyserial`` is part of the
``uvk5`` optional extra (serial + soundcard), imported lazily so importing this module (and the
whole test suite) stays hardware-free; the constructor accepts an injected ``_serial_factory``
for unit tests against the firmware-accurate fake.

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
import errno
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
    "the UV-K5/Quansheng Dock backend needs the 'uvk5' extra (pyserial + sounddevice): install "
    "with `pip install 'radio-server[uvk5]'`"
)


def _load_serial():
    try:
        import serial  # pyserial
    except ImportError as exc:  # pragma: no cover - exercised via the injected fake in tests
        raise RuntimeError(_EXTRA_MSG) from exc
    return serial


def apply_port_settings(handle, baud: int) -> None:
    """Put ``handle`` into the state this transport's reader thread requires.

    Shared with `AiocBaofeng`, which opens the AIOC handle itself for PTT and then hands the *same
    handle* to a transport for tuning. Factored out because getting it half-right is silent and
    baffling: the reader calls ``read(_READ_SIZE)``, and with pyserial's default ``timeout=None``
    that blocks until the full buffer arrives, so a 24-byte reply is simply never dispatched and
    every request times out against a radio that answered perfectly well.
    """
    handle.baudrate = baud
    handle.timeout = _READ_TIMEOUT
    handle.write_timeout = DEFAULT_WRITE_TIMEOUT


def _default_serial_factory(port: str, baud: int):
    """Open ``port`` at ``baud`` with DTR and RTS held **low from the moment it opens**.

    This AIOC serial line also carries PTT (DTR/RTS), so pulsing a line at open would key
    the transmitter (ADR 0111; the Baofeng backend does the same). ``pyserial`` applies
    ``.dtr``/``.rts`` set before ``open()`` as the initial line state, so we set both low
    first and only then open.

    Since ADR 0166 the port is also **claimed exclusively** once open, so a second process gets
    EBUSY instead of silently killing this reader — see `claim_port_exclusive`.
    """
    serial = _load_serial()
    handle = serial.Serial()
    handle.port = port
    apply_port_settings(handle, baud)
    handle.dtr = False
    handle.rts = False
    try:
        handle.open()
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.EBUSY:
            raise OSError(errno.EBUSY, port_busy_message(port, exc)) from exc
        raise
    claim_port_exclusive(handle)
    return handle


def claim_port_exclusive(handle) -> bool:
    """Ask the kernel to refuse further opens of ``handle``'s tty. Returns whether it took.

    **This is not what pyserial's ``exclusive=True`` does.** Measured on pyserial 3.5: ``TIOCEXCL``
    appears nowhere in it; ``serialposix.py`` calls ``fcntl.flock(LOCK_EX | LOCK_NB)``, which is
    *advisory* — it stops only another process that also flocks, and a plain
    ``serial.Serial(port)`` sails straight past it. Two docstrings in this repo claimed otherwise and
    were wrong (ADR 0166). ``TIOCEXCL`` is the mandatory one: a second ``open()`` gets **EBUSY**.

    Why this is worth an ioctl. The reader thread dies permanently on a concurrent open — a bench
    script, a ``doctor`` run — and until ADR 0166 nothing noticed. Refusing the port turns a silent
    kill into a loud failure *in the second process*, which is the one that can still do something
    about it.

    Two limits, both deliberate and both stated so nobody mistakes this for a guarantee:

    - **Root bypasses ``TIOCEXCL``.** A ``sudo``-ed script still gets in and still kills the reader.
      That is exactly why the liveness surface and the reconnect route still have to exist; this
      makes the common case loud, it does not make detection redundant.
    - The flag lives on the tty and clears when the last fd closes — **measured on a real USB serial
      device, including after ``SIGKILL``** (ADR 0166 B0), so a crashed service cannot lock the
      station out of its own radio. A pty says otherwise and is simply a bad model: its master keeps
      the slave alive, so the slave never reaches last-close.

    Returns ``False`` — never raises — when the handle has no real fd (every fake in the test suite)
    or the platform has no ``TIOCEXCL``. A port that cannot be claimed is still a usable port.
    """
    try:
        import fcntl
        import termios

        fcntl.ioctl(handle.fileno(), termios.TIOCEXCL)
        return True
    except Exception:  # noqa: BLE001 - fakes, non-tty handles, non-POSIX platforms
        return False


def port_busy_message(port: str, exc: BaseException) -> str:
    """Explain an EBUSY on ``port`` to whoever is about to see a traceback.

    Since ADR 0166 the running service claims the tty, so this is the *expected* answer for a bench
    script or a ``doctor`` run against a live station — the rule the docs already gave, now enforced.
    A bare "Device or resource busy" sends someone to a search engine; naming the remedy is the
    difference between a two-second fix and an afternoon.
    """
    return (
        f"{port} is held by another process: {exc}. The radio-server service claims the dock tty "
        f"exclusively while it runs (ADR 0166), so a bench script or `doctor` has to have it to "
        f"itself: `systemctl --user stop radio-server` first, and start it again afterwards."
    )


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
        #: **One frame on the wire at a time** (ADR 0163). ``_cond`` guards the waiter list, not
        #: the wire, and until this cycle nothing did: ``send`` writes outside it and two
        #: ``request`` calls could have frames in flight together. That was survivable only while
        #: every ``match`` was discriminated — ADR 0125's thread-safety argument for `PolledGate`
        #: rests entirely on `CatBusyGate` matching ``m.register == reg``. Broadcast FM has no such
        #: discriminator: the probe and the clear both match ``isinstance(m, BroadcastFmReply)``,
        #: so a poll's refusal can be handed to a key-up's clear, which reads it as "the radio
        #: refused to clear" and refuses the key-up — ADR 0161's defect, rebuilt by the cadence.
        #:
        #: The firmware wants this anyway: ``Dock_EnterFullControl`` blocks, so a frame arriving
        #: while it is busy is **dropped**, silently, since writes are fire-and-forget (ADR 0131).
        #: Serialising here turns two racing frames into two sequential ones.
        self._wire = threading.Lock()
        #: Round trips completed through :meth:`request`, ever. Monotonic, incremented **while the
        #: wire is still held** so it needs no lock of its own, and never reset — a caller measures
        #: an interval by differencing two snapshots (ADR 0177).
        #:
        #: It exists because `wire_busy` under-reports: sampling the lock at an instant misses an
        #: exchange that starts just after the sample and finishes across a key-up, which is exactly
        #: the case that damages a transmission. A delta over the key-up window catches it.
        self._exchanges = 0
        self._waiters: list[dict] = []
        self._inbox: deque = deque(maxlen=_INBOX_DEPTH)
        self._reader_error: Exception | None = None
        self._closed = False

        #: Monotonic reader generation (ADR 0099's guard, brought here by ADR 0166). Each spawned
        #: reader carries the generation current when it started; `reconnect` bumps it. `_dispatch`
        #: and `_fail` ignore a reader whose tag is stale, so a straggler from the superseded
        #: reader — which on a wedged port can outlive `reconnect`'s bounded join — can neither fill
        #: a reply slot nor store its own death in `_reader_error`. Without it a recovered transport
        #: reports itself broken the moment the old thread finally notices, and never looks healthy
        #: again.
        self._reader_gen = 0
        self._serial_port = serial_port
        self._baud = baud
        self._factory = _serial_factory or _default_serial_factory

        self._stop = threading.Event()
        self._reader = self._spawn_reader()
        # Never leave the port open if the process dies.
        atexit.register(self.close)

    def _spawn_reader(self) -> threading.Thread:
        """Start a reader tagged with the current generation, binding its inputs by value."""
        gen = self._reader_gen
        serial_handle, stop = self._serial, self._stop
        thread = threading.Thread(
            target=self._read_loop,
            args=(serial_handle, stop, gen),
            name=f"uvk5-reader-{gen}",
            daemon=True,
        )
        thread.start()
        return thread

    # -- liveness ----------------------------------------------------------------------

    @property
    def alive(self) -> bool:
        """Is the reader thread actually running? **Asks the thread, never the radio** (ADR 0166).

        Polling the radio to discover that the radio has stopped answering is the failure mode, not
        the detector — and it would be the one call guaranteed to hang. This is a local
        ``is_alive()`` and an attribute read, cheap enough to sit on the per-frame ``status()`` path
        and safe to call when the serial handle raises on every touch.

        A **closed** transport is not alive and is not a fault: teardown is deliberate, and
        reporting it as a dead reader would make every clean shutdown look like the defect this
        exists to catch. `reader_error` is what distinguishes them.
        """
        reader = self._reader
        return bool(reader is not None and reader.is_alive() and self._reader_error is None)

    @property
    def reader_error(self) -> Exception | None:
        """The exception that killed the reader, or ``None`` — including after a clean close."""
        return self._reader_error

    def reconnect(self) -> str:
        """Close the port and bring a fresh reader up. **One shot** — never a loop (ADR 0166).

        Returns ``"already_healthy"`` when the reader was running and nothing was done, or
        ``"reopened"`` on success. Raises whatever the reopen raised — the caller turns that into a
        503 with the reason, because an outcome of "failed" reported as a 200 is read as success by
        everything that checks a status code.

        **Why one shot and not a retry loop.** The concurrent-open case means another process holds
        this tty. A loop would spend the station's life fighting it for the port, which is a louder
        version of the race that caused the original defect. One attempt, an honest answer, and a
        human decides — while `TIOCEXCL` makes the collision much rarer in the first place.

        The bounded join is why the generation tag exists: a reader wedged in ``read()`` can outlive
        it, and a straggler that stores its death afterwards would mark the new transport broken.
        """
        with self._cond:
            if self._closed:
                raise Uvk5Closed("transport closed")
        if self.alive:
            return "already_healthy"

        old_stop, old_reader = self._stop, self._reader
        old_stop.set()
        try:
            self._serial.close()
        except Exception:  # noqa: BLE001 - the handle is already broken; that is why we are here
            pass
        if old_reader is not None and old_reader is not threading.current_thread():
            old_reader.join(timeout=1.0)

        handle = self._factory(self._serial_port, self._baud)  # may raise — the caller reports it
        with self._cond:
            self._reader_gen += 1          # sheds anything still running on the old generation
            self._serial = handle
            self._stop = threading.Event()
            self._decoder = Uvk5Decoder(obfuscated=self._obfuscate, validate_crc=False)
            self._reader_error = None
            self._waiters.clear()
            self._inbox.clear()
            self._cond.notify_all()
        self._reader = self._spawn_reader()
        logger.warning("uvk5: reader reopened on %s (generation %d)", self._serial_port, self._reader_gen)
        return "reopened"

    # -- reader thread -----------------------------------------------------------------

    def _read_loop(self, serial_handle, stop: threading.Event, gen: int) -> None:
        """Runs on the daemon reader thread: read -> deframe -> dispatch, until stopped.

        Its handle, stop flag and generation are bound by value at spawn, so a superseded reader
        keeps reading its own dead port rather than the live one a `reconnect` installed.
        """
        while not stop.is_set():
            if gen != self._reader_gen:
                return  # superseded by a reconnect — shed silently
            try:
                chunk = serial_handle.read(_READ_SIZE)
            except Exception as exc:  # SerialException et al. — surface it, don't wedge
                self._fail(exc, gen)
                return
            if not chunk:
                continue  # read timeout (b"") — loop back and re-check the stop flag
            try:
                for payload in self._decoder.feed(chunk):
                    parsed = parse_frame(payload)
                    if parsed is not None:
                        self._dispatch(parsed, gen)
            except Exception:  # a single malformed frame must not kill the reader
                logger.exception("uvk5: error dispatching frame")

    def _dispatch(self, msg: object, gen: int | None = None) -> None:
        with self._cond:
            if gen is not None and gen != self._reader_gen:
                return  # a straggler must never fill a live reply slot
            for waiter in self._waiters:
                if not waiter["done"] and waiter["match"](msg):
                    waiter["result"] = msg
                    waiter["done"] = True
                    self._cond.notify_all()
                    return
            self._inbox.append(msg)  # unsolicited / unmatched (bounded, drop-oldest)
            self._cond.notify_all()

    def _fail(self, exc: Exception, gen: int | None = None) -> None:
        with self._cond:
            if gen is not None and gen != self._reader_gen:
                return  # a superseded reader's death is not the live transport's problem
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

    def request(self, msg, match: Callable[[object], bool], timeout: float | None = None,
                *, wire_timeout: float | None = None):
        """Send *msg* and block until a dispatched message satisfies *match*, or raise.

        Holds :attr:`_wire` for the whole send-and-wait, so exactly one frame is outstanding and
        a reply can only reach the caller that asked for it. Registers the waiter **before**
        writing so a fast reply is never missed. Raises :class:`Uvk5Timeout` on the deadline,
        :class:`Uvk5Closed` if closed mid-wait, or the reader's stored error if the port died.

        ``wire_timeout`` is how long to wait for the *wire*, as opposed to for the reply.
        ``None`` — every caller written before ADR 0163, and every key-up — blocks, so the key-up
        path always gets its turn. ``0`` gives up immediately with :class:`Uvk5Timeout`, which is
        what the broadcast-FM cadence passes: a poll that cannot have the wire this round should
        skip and ask again, never queue behind a tune for a reading nothing is waiting on.
        """
        timeout = self._request_timeout if timeout is None else timeout
        if wire_timeout is None:
            got_wire = self._wire.acquire()
        elif wire_timeout > 0:
            got_wire = self._wire.acquire(timeout=wire_timeout)
        else:
            got_wire = self._wire.acquire(blocking=False)
        if not got_wire:
            raise Uvk5Timeout("the dock link is busy with another frame")
        try:
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
        finally:
            # Under `_wire`, so the increment is serialised by the lock it is counting.
            self._exchanges += 1
            self._wire.release()

    @property
    def exchanges(self) -> int:
        """Round trips completed through :meth:`request`, ever — see :attr:`_exchanges`."""
        return self._exchanges

    def wait_for_quiet(self, timeout: float) -> float | None:
        """Block until no frame is on the wire, up to *timeout*. Seconds waited, or `None` if not.

        A **barrier, not a held lock**: it takes the wire and gives it straight back, so on its own
        it guarantees nothing — another thread may take it the instant this returns. It is only
        meaningful to a caller that has already stopped new frames being started (`AiocBaofeng`
        sets its keying counter first). Written down because "the barrier makes the wire quiet"
        reads like a guarantee and is not one.

        Never blocks past *timeout*, because the caller is a key-up and a transmitter must not wait
        on a diagnostic (ADR 0177).
        """
        started = time.monotonic()
        if not self._wire.acquire(timeout=timeout):
            return None
        self._wire.release()
        return time.monotonic() - started

    def wire_busy(self) -> bool:
        """Is a frame on the wire **right now**? A lock-state read: no acquire, no I/O (ADR 0177).

        Safe to call from the keying path, which is the only reason it exists. `Lock.locked()` is an
        atomic state read that cannot block and cannot take the lock, so this can never become the
        thing it measures — the ADR 0176 rule about a check that does I/O to decide whether to do
        I/O applies just as much to a *measurement* of this wire as to a pause hook.

        **It is a lower bound.** An exchange starting immediately after this returns `False` is
        invisible to it and can still straddle a key-up, so a run of zeroes here is not on its own
        evidence that nothing raced. :attr:`exchanges` is the instrument that catches those.

        Deliberately not counting `send()` — that path is fire-and-forget and takes no wire lock, so
        the one direct caller (`reboot_radio`) is invisible here and is meant to be.
        """
        return self._wire.locked()

    def read_register(self, reg: int, *, timeout: float | None = None,
                      wire_timeout: float | None = None) -> int:
        """Read one BK4819 register: ``0x0851`` out, the matching ``0x0951`` back.

        Lives here rather than on a backend because **two** callers need it and they are on
        opposite sides of the backend split: `Uvk5Radio`, which owns the whole radio over the dock,
        and the tuner a `baofeng` backend attaches to the very same AIOC handle (ADR 0175). One
        implementation means one place where the match predicate can be wrong — and the predicate
        is load-bearing, because it is what makes concurrent reads safe: `m.register == reg`
        discriminates replies, which is the entire basis of `PolledGate`'s thread-safety argument
        (ADR 0125) and of the `_wire` note above.

        ``wire_timeout=0`` is for a cadence: skip this round rather than queue behind a tune for a
        reading nothing is waiting on. Every other caller leaves it ``None`` and blocks, so a
        key-up always gets its turn.
        """
        info = self.request(
            ReadRegisters((reg,)),
            lambda m: isinstance(m, RegisterInfo) and m.register == reg,
            timeout=timeout,
            wire_timeout=wire_timeout,
        )
        return info.value

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
