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

Two firmware facts drive the design (source read verbatim as a spec, not ported — kv4p-ht
GPL-3.0 @ the shipped release **v2.0.0.1, ``3f0e809baa02a946c3f0602681303f600c321d31``**,
``kv4p_ht_esp32_wroom_32/kv4p_ht_esp32_wroom_32.ino``; was the unreleased ``e9935bd…``, ADR 0064).
The shipped device has **no sessions, no sequence gate, no flag mask** (all ``e9935bd``-only):
``handleCommands`` accepts a ``HOST_DESIRED_STATE`` iff ``param_len == sizeof(HostDesiredState)`` (22)
and then does a **whole-struct** ``memcpy`` over ``desiredState`` followed by ``reconcileDesiredState()``
(a wrong length is dropped *silently*). ``reconcileDesiredState`` persists ``desiredState`` to NVS
**unconditionally** and ``deviceStateFlags()`` reports the **whole** ``desiredState.flags`` word back;
status reports fire on-dirty **and** periodically, both gated on ``ENABLE_STATUS_REPORTS`` (ADR 0066).

  1. **Connect without clobbering the board (ADR 0066).** Because any host frame overwrites and
     *persists* the device's entire desired state, a naive "neutral zeros" probe permanently zeroes the
     operator's stored frequency and clears ``TX_ALLOWED`` in NVS. :meth:`connect` is therefore
     **passive-first**: it listens for an unsolicited ``DeviceState`` (a board already streaming reports
     needs no write) and, only if none comes, sends an elicit ``HostDesiredState``
     (``ENABLE_STATUS_REPORTS`` on, ``RADIO_CONFIG_VALID`` off so it never retunes), **retransmitting**
     it until the device echoes the flag (a single probe can be lost to a reset-on-open race or a dropped
     write — the silent ``param_len`` gate gives no error), then **restores** the tuning it read back
     with safe flag defaults. It syncs our counter to the reported ``appliedSequence``. A HELLO, if one
     arrives, is a bonus (its windowSize/module/freq range are adopted); it is never a precondition, and
     its embedded state (captured at boot with ``ENABLE_STATUS_REPORTS`` clear) never completes the
     handshake.

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
    DeviceStateFlag,
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
#: Seconds :meth:`connect`'s elicit phase retransmits+waits for the echoed ``DeviceState`` before raising.
DEFAULT_CONNECT_TIMEOUT = 2.0
#: Seconds :meth:`connect` listens *passively* first — a board already streaming reports (a server
#: reconnect, or the app attached) is read with **zero** writes. Must exceed the firmware's
#: ``DEVICE_STATE_REPORT_INTERVAL_MS`` so a periodic report is reliably caught; that value is not in a
#: header we mirror, so this is a **marked default, verify-on-source/bench** (guardrail 1).
DEFAULT_PASSIVE_WINDOW = 0.6
#: Seconds between elicit-probe retransmits (a lost probe draws no error — the ``param_len`` gate is silent).
_ELICIT_RETRANSMIT_INTERVAL = 0.25
#: Seconds :meth:`connect` waits for the config-restoring write to apply after a successful elicit.
_RESTORE_ACK_TIMEOUT = 1.0
#: Seconds :meth:`close` waits to confirm the PTT-off reconcile applied before tearing down
#: regardless. Deliberately short — shutdown must never hang on a device that stopped answering.
_CLOSE_ACK_TIMEOUT = 0.5
#: RX-audio hand-off queue depth (one Opus packet per slot). Bounded + drop-oldest so a slow consumer
#: never blocks the reader thread (the ``MultimonStream`` idiom, ``audio/dtmf.py``). Sized for the
#: retired ADPCM path (~64 blk/s); narrowband VBR Opus is far lighter (~25 frames/s, well under the
#: ADPCM ~89 kbit/s that shaped ADR 0062), so this depth has ample headroom — revisit against real
#: bench numbers if RX latency ever matters (ADR 0065).
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


@dataclasses.dataclass
class TxStats:
    """TX-audio counters for one keying — the bench-bring-up measurement rig (ADR 0069).

    The whole transmit side rode marked verify-on-bench guesses (window size, ``tx_lead``, the
    encoded-frame size) with no way to *read* what actually went over the wire. These counters make
    a keyed ``doctor`` run quantitative: ``opus_bytes_*`` is the encoded packet size (the codec's
    output), ``wire_bytes_sum`` is what the flow-control window actually spends (escaped + FENDs), and
    ``blocked_frames`` / ``min_credits`` say whether the credit window ever became the bottleneck.
    Reset per key-up (:meth:`Kv4pTransport.reset_tx_stats`) so each keying reads clean.
    """

    frames: int = 0  # TX-audio packets sent this keying
    opus_bytes_sum: int = 0  # encoded Opus payload bytes (pre-KISS-escape)
    opus_bytes_min: int | None = None
    opus_bytes_max: int | None = None
    wire_bytes_sum: int = 0  # on-wire escaped + FEND-delimited bytes (what credits are spent on)
    blocked_frames: int = 0  # writes that had to wait for window credit (window was the bottleneck)
    min_credits: int | None = None  # lowest credit level observed at a write (how close to starving)

    def record_audio(self, opus_len: int, wire_len: int) -> None:
        """Fold one TX-audio frame's encoded size into the counters."""
        self.frames += 1
        self.opus_bytes_sum += opus_len
        self.wire_bytes_sum += wire_len
        self.opus_bytes_min = opus_len if self.opus_bytes_min is None else min(self.opus_bytes_min, opus_len)
        self.opus_bytes_max = opus_len if self.opus_bytes_max is None else max(self.opus_bytes_max, opus_len)


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
        self._tx_stats = TxStats()  # per-keying TX-audio counters; guarded by _credit_cond (ADR 0069)

        # Reconciler: `_sequence` is the last sequence number we sent (or synced to); the next
        # send is `_sequence + 1`. `_applied_sequence` echoes the device's last-applied.
        self._state_cond = threading.Condition()
        self._sequence = 0
        self._applied_sequence = 0
        self._device_state: DeviceState | None = None
        self._state_epoch = 0  # bumped on every DeviceState — connect waits on it

        # Link flags are kept asserted on *every* outgoing frame for the life of the connection.
        # This is not a firmware "session mask" (that is e9935bd-only, ADR 0066) — shipped firmware
        # memcpy's the whole flags word, so dropping ENABLE_STATUS_REPORTS on a later frame would turn
        # reports *off*. Keeping it here is what holds the report stream open across every send.
        self._link_flags = HostStateFlag(0)

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
            if self._tx_stats.min_credits is None or self._credits < self._tx_stats.min_credits:
                self._tx_stats.min_credits = self._credits  # how low the pool got at a write (ADR 0069)
            blocked = False
            while self._credits < need:
                blocked = True
                self._raise_if_failed()
                if self._closed:
                    raise Kv4pClosed("transport closed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Kv4pTimeout(
                        f"no window credit for a {need}-byte frame after {self._write_timeout}s"
                    )
                self._credit_cond.wait(remaining)
            if blocked:
                self._tx_stats.blocked_frames += 1  # the window was the bottleneck for this frame
            self._credits -= need
        self._serial.write(built)

    def _raise_if_failed(self) -> None:
        if self._reader_error is not None:
            raise self._reader_error

    # --- reconciler -----------------------------------------------------------

    def send_desired_state(self, state: HostDesiredState) -> int:
        """Assign the next sequence, OR in the link flags, encode, and write. Returns the seq.

        The caller supplies the desired state (config/PTT/power/filters); the sequence and the
        connection-lifetime link flags (status reports) are owned here (ADR 0066).
        """
        self._raise_if_failed()
        self._sequence += 1
        seq = self._sequence
        outgoing = dataclasses.replace(
            state, sequence=seq, flags=int(state.flags) | int(self._link_flags)
        )
        self._write_frame(build_vendor_frame(RcvCommand.HOST_DESIRED_STATE, outgoing.pack()))
        return seq

    def send_tx_audio(self, packet: bytes) -> None:
        """Send one Opus audio packet as ``HOST_TX_AUDIO`` through the flow-control window.

        TX audio is the bulk of the link, so it rides the same encoded-byte credit window as every
        other frame: this blocks until the window has room and raises :class:`Kv4pTimeout` rather than
        overrunning the device buffer. The reconciler's sequence bookkeeping does not apply — audio
        frames carry no sequence. One packet per frame (Opus, ADR 0065); it is opaque here.
        """
        self._raise_if_failed()
        built = build_vendor_frame(RcvCommand.HOST_TX_AUDIO, packet)
        with self._credit_cond:
            self._tx_stats.record_audio(len(packet), len(built))  # bench telemetry (ADR 0069)
        self._write_frame(built)

    @property
    def tx_stats(self) -> TxStats:
        """A stable snapshot of this keying's TX-audio counters (ADR 0069)."""
        with self._credit_cond:
            return dataclasses.replace(self._tx_stats)

    @property
    def window_size(self) -> int:
        """The effective flow-control window in encoded bytes (HELLO-adjusted if one arrived)."""
        with self._credit_cond:
            return self._window_size

    def reset_tx_stats(self) -> None:
        """Zero the TX-audio counters — called per key-up so each keying reads clean."""
        with self._credit_cond:
            self._tx_stats = TxStats()

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
        """Passive-first, clobber-safe handshake (ADR 0066); return the device's state.

        Shipped firmware overwrites and *persists* its whole desired state on any host frame, so a
        "neutral zeros" probe would permanently zero the operator's stored frequency and clear
        ``TX_ALLOWED``. This handshake avoids that:

        1. **Passive.** Listen (no write) for an unsolicited ``DeviceState`` echoing
           ``ENABLE_STATUS_REPORTS`` — a board already streaming reports (a server reconnect, or the app
           attached) is fully visible and is read with **zero** writes; sync the counter and return.
        2. **Elicit.** Otherwise send an elicit ``HostDesiredState`` (``ENABLE_STATUS_REPORTS`` on,
           ``RADIO_CONFIG_VALID`` off so it never retunes), **retransmitting** it every
           ``_ELICIT_RETRANSMIT_INTERVAL`` until the device echoes the flag or ``timeout`` — a single
           probe can be lost to a reset-on-open race or a dropped write, and the firmware's
           ``param_len == 22`` gate fails silently.
        3. **Restore.** Rewrite the tuning the elicit read back (freq/CTCSS/bw/memory, sourced from the
           device's ``appliedState``) with safe flag defaults (``RADIO_CONFIG_VALID | HIGH_POWER |
           RSSI_ENABLED``; ``TX_ALLOWED`` stays clear — never silently re-enable TX), undoing the
           elicit's zero-clobber of the stored frequency.

        The acknowledgement proof is the echoed ``ENABLE_STATUS_REPORTS``: shipped ``deviceStateFlags()``
        reports the whole ``desiredState.flags`` word, so a state produced *because our probe applied*
        carries the flag; a boot HELLO's embedded state (captured with the flag clear) never does, so it
        can never be mistaken for a round trip.
        """
        # Kept asserted on every subsequent frame so the report stream stays open (see __init__).
        self._link_flags |= HostStateFlag.ENABLE_STATUS_REPORTS

        # 1. Passive: a board already streaming reports needs no write at all. Capped by the caller's
        # timeout so a deliberately short connect stays short.
        state = self._wait_for_ack(min(DEFAULT_PASSIVE_WINDOW, timeout))
        if state is not None:
            with self._state_cond:
                self._sequence = state.applied_sequence
            logger.debug("kv4p connect: passive — board already reporting, no write sent")
            return state

        # 2. Elicit: retransmit the probe until the device echoes the flag or the budget runs out.
        deadline = time.monotonic() + timeout
        state = None
        while state is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Kv4pTimeout(
                    f"the device never acknowledged a host frame within {timeout}s — the "
                    f"HostDesiredState is not landing (board unpowered/asleep, or the firmware "
                    f"protocol does not match this build)"
                )
            self.send_desired_state(_ELICIT_STATE)
            state = self._wait_for_ack(min(_ELICIT_RETRANSMIT_INTERVAL, remaining))

        # 3. Restore: rewrite the operator's tuning (elicit zeroed it in NVS) with safe flag defaults.
        with self._state_cond:
            self._sequence = state.applied_sequence
        seq = self.send_desired_state(self._restore_state(state))
        return self.await_applied(seq, timeout=_RESTORE_ACK_TIMEOUT)

    def _wait_for_ack(self, window: float) -> DeviceState | None:
        """Wait up to ``window`` s for a DeviceState echoing ``ENABLE_STATUS_REPORTS``; else ``None``."""
        deadline = time.monotonic() + window
        with self._state_cond:
            while not self._session_acknowledged():
                self._raise_if_failed()
                if self._closed:
                    raise Kv4pClosed("transport closed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._state_cond.wait(remaining)
            return self._device_state

    def _session_acknowledged(self) -> bool:
        """True once a DeviceState echoes ``ENABLE_STATUS_REPORTS`` — proof a host frame was applied.

        Called only under ``self._state_cond``. Shipped ``deviceStateFlags()`` copies the whole
        ``desiredState.flags`` word (no session mask — ADR 0066), so this bit is present exactly when the
        device's current desired state has reports on. A boot HELLO's embedded state (flag clear) returns
        False, so it can never be mistaken for a round trip."""
        state = self._device_state
        return state is not None and bool(
            DeviceStateFlag(state.flags) & DeviceStateFlag.ENABLE_STATUS_REPORTS
        )

    def _restore_state(self, state: DeviceState) -> HostDesiredState:
        """A HostDesiredState that echoes ``state``'s tuning with safe flag defaults (ADR 0066).

        Used after an elicit (which zeroed the device's desired state) to rewrite the operator's real
        frequency/CTCSS back to NVS. ``TX_ALLOWED`` and filters stay clear (fail-safe — the operator's
        originals are unrecoverable once the elicit overwrote ``desiredState``); ``HIGH_POWER`` /
        ``RSSI_ENABLED`` match the firmware's own boot defaults. ``ENABLE_STATUS_REPORTS`` is added by
        :meth:`send_desired_state` from the link flags.
        """
        safe = HostStateFlag.RADIO_CONFIG_VALID | HostStateFlag.HIGH_POWER | HostStateFlag.RSSI_ENABLED
        return HostDesiredState(
            sequence=0,  # assigned by send_desired_state
            memory_id=state.memory_id,
            flags=int(safe),
            bw=state.bw,
            freq_tx=state.freq_tx,
            freq_rx=state.freq_rx,
            ctcss_tx=state.ctcss_tx,
            squelch=state.squelch,
            ctcss_rx=state.ctcss_rx,
        )

    def _ptt_off_echo(self, state: DeviceState) -> HostDesiredState:
        """Echo ``state`` back with ``PTT_REQUESTED`` cleared and the device-only flag bits stripped.

        Used by :meth:`close` for a safe shutdown that does **not** clobber NVS: it reproduces the
        device's own last desired state (config + operational flags) minus PTT, so
        ``persistedRadioStateMatchesDesired`` holds and no zeros are persisted (ADR 0066).
        """
        device_only = (
            DeviceStateFlag.PHYS_PTT_DOWN | DeviceStateFlag.TX_ACTIVE | DeviceStateFlag.SQUELCHED
        )
        host_flags = int(state.flags) & ~int(device_only) & ~int(HostStateFlag.PTT_REQUESTED)
        return HostDesiredState(
            sequence=0,  # assigned by send_desired_state
            memory_id=state.memory_id,
            flags=host_flags,
            bw=state.bw,
            freq_tx=state.freq_tx,
            freq_rx=state.freq_rx,
            ctcss_tx=state.ctcss_tx,
            squelch=state.squelch,
            ctcss_rx=state.ctcss_rx,
        )

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
        # Best-effort safe shutdown BEFORE we tear down: echo the device's last known state with
        # PTT cleared (NOT a zeros write — that would clobber the operator's stored frequency and
        # TX_ALLOWED in NVS, ADR 0066), confirmed applied. If we never saw a state, nothing was ever
        # keyed and there is nothing to reconcile. Swallow everything — a dead port or a credit-starved
        # window must never make close() raise or hang past the write timeout.
        last = self._device_state
        if last is not None:
            try:
                seq = self.send_desired_state(self._ptt_off_echo(last))
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


#: The elicit probe (ADR 0066): zeros with no RADIO_CONFIG_VALID, so applying it never retunes the
#: radio (``appliedState`` keeps the real freq, which the reply then reports for the restore step).
#: ENABLE_STATUS_REPORTS is added from the link flags by send_desired_state. Only ever sent when a
#: passive listen found no already-streaming board; connect() restores the tuning immediately after.
_ELICIT_STATE = HostDesiredState(
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
