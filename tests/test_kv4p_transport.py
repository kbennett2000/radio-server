"""Fake-serial tests for the kv4p HT transport (ADR 0061, ADR 0062).

No hardware and no 'hardware' extra: the transport's ``_serial_factory`` seam takes a fake
pyserial-like object, so the reader thread, flow-control window, and reconciler bookkeeping
are exercised entirely in-process. Blocking calls (``connect``, a credit-starved write, an
``await_applied``) are driven on background threads and fed device→host frames on the main
thread, mirroring how the firmware would answer.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

import pytest

from radio_server.backends.kv4p import transport as tp
from radio_server.backends.kv4p.frames import (
    DeviceState,
    DeviceStateFlag,
    Hello,
    HostDesiredState,
    HostStateFlag,
    KissDecoder,
    KISS_CMD_DATA,
    RcvCommand,
    SndCommand,
    VendorFrame,
    Version,
    WindowUpdate,
    build_kiss_frame,
    build_vendor_frame,
    parse_frame,
)
from radio_server.backends.kv4p.transport import Kv4pTimeout, Kv4pTransport


# --------------------------------------------------------------------------------------
# Fakes and helpers
# --------------------------------------------------------------------------------------


class FakeSerialError(Exception):
    """Stands in for ``serial.SerialException`` (pyserial may be absent in the test env)."""


class FakeSerial:
    """A dumb pyserial-like pipe: ``feed`` device→host items, records host→device ``writes``.

    ``read`` serves the fed queue (bytes to return, or an exception instance to raise) and
    returns ``b""`` on a timeout — exactly the two edge cases the reader must tolerate.
    """

    def __init__(self) -> None:
        self.port = None
        self.baudrate = None
        self.timeout = None
        self.dtr = None
        self.rts = None
        self.writes: list[bytes] = []
        self.closed = False
        self.dead = False  # when True, write/close raise (simulates a vanished device)
        self._items: "queue.Queue[object]" = queue.Queue()

    def feed(self, item: object) -> None:
        """Enqueue a device→host chunk (``bytes``) or an exception for ``read`` to raise."""
        self._items.put(item)

    def read(self, _size: int) -> bytes:
        try:
            item = self._items.get(timeout=self.timeout or 0.05)
        except queue.Empty:
            return b""  # read timeout
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, bytes)
        return item

    def write(self, data: bytes) -> int:
        if self.dead:
            raise FakeSerialError("port is gone")
        self.writes.append(bytes(data))
        return len(data)

    def close(self) -> None:
        if self.dead:
            raise FakeSerialError("port is gone")
        self.closed = True


def make_transport(fake: FakeSerial, **kwargs) -> Kv4pTransport:
    return Kv4pTransport(_serial_factory=lambda port, baud: fake, **kwargs)


def wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def run_bg(fn, *args, **kwargs):
    """Run a blocking transport call on a thread; return (thread, result-dict)."""
    result: dict[str, object] = {}

    def target():
        try:
            result["value"] = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - the test inspects whatever it raised
            result["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, result


def _device_state(applied_sequence: int = 0, flags: int = 0) -> DeviceState:
    return DeviceState(
        applied_sequence=applied_sequence,
        memory_id=0,
        flags=flags,
        bw=0,
        freq_tx=0.0,
        freq_rx=0.0,
        ctcss_tx=0,
        squelch=0,
        ctcss_rx=0,
        radio_module_status=0,
        mode=0,
        last_error=0,
        latest_rssi=0,
    )


def state_frame(applied_sequence: int = 0, flags: int = 0) -> bytes:
    payload = _device_state(applied_sequence, flags).pack()
    return build_vendor_frame(SndCommand.DEVICE_STATE, payload)


def window_frame(size: int) -> bytes:
    return build_vendor_frame(SndCommand.WINDOW_UPDATE, WindowUpdate(size).pack())


def hello_frame(
    *, window_size: int, rf_module_type: int, min_freq: float, max_freq: float
) -> bytes:
    version = Version(
        ver=1,
        radio_module_status=0,
        window_size=window_size,
        rf_module_type=rf_module_type,
        min_radio_freq=min_freq,
        max_radio_freq=max_freq,
        features=0,
    )
    return build_vendor_frame(SndCommand.HELLO, Hello(version, _device_state()).pack())


def neutral() -> HostDesiredState:
    return HostDesiredState(
        sequence=0, memory_id=0, flags=0, bw=0, freq_tx=0.0, freq_rx=0.0,
        ctcss_tx=0, squelch=0, ctcss_rx=0,
    )


def decode_host_states(writes: list[bytes]) -> list[HostDesiredState]:
    """Deframe the transport's on-wire writes back into the HostDesiredStates it sent."""
    decoder = KissDecoder()
    states: list[HostDesiredState] = []
    for chunk in writes:
        for frame in decoder.feed(chunk):
            parsed = parse_frame(frame)
            if isinstance(parsed, VendorFrame) and parsed.command == RcvCommand.HOST_DESIRED_STATE:
                states.append(HostDesiredState.unpack(parsed.payload))
    return states


# --------------------------------------------------------------------------------------
# Reset-safe open (ADR 0062, Decision 2)
# --------------------------------------------------------------------------------------


def test_default_factory_holds_lines_low_before_open(monkeypatch):
    events: list[tuple[str, object]] = []

    class Handle:
        @property
        def dtr(self):
            return self._dtr

        @dtr.setter
        def dtr(self, value):
            self._dtr = value
            events.append(("dtr", value))

        @property
        def rts(self):
            return self._rts

        @rts.setter
        def rts(self, value):
            self._rts = value
            events.append(("rts", value))

        def open(self):
            events.append(("open", None))

    monkeypatch.setattr(tp, "_load_serial", lambda: type("Mod", (), {"Serial": Handle}))
    tp._default_serial_factory("/dev/ttyUSB0", 115200)

    # Both control lines are driven low, and open() happens strictly after both (the ESP32
    # auto-reset guard: never pulse DTR/RTS on open).
    assert ("dtr", False) in events and ("rts", False) in events
    assert events.index(("open", None)) > events.index(("dtr", False))
    assert events.index(("open", None)) > events.index(("rts", False))


def test_missing_pyserial_gives_actionable_error(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "serial", None)
    with pytest.raises(RuntimeError, match="hardware.*extra"):
        tp._default_serial_factory("/dev/ttyUSB0", 115200)


# --------------------------------------------------------------------------------------
# Connect handshake (ADR 0062, Decision 1)
# --------------------------------------------------------------------------------------


def test_connect_syncs_to_applied_sequence_without_hello():
    fake = FakeSerial()
    transport = make_transport(fake)
    try:
        thread, result = run_bg(transport.connect, timeout=2.0)
        # The probe is written before connect waits; only then does the (still-running) device
        # answer with its current appliedSequence — the probe's own stale sequence aside. The state
        # must echo ENABLE_STATUS_REPORTS (the session flag we sent) or connect keeps waiting: that
        # echo is the proof the probe actually landed, not a boot HELLO (ADR 0062).
        assert wait_until(lambda: len(fake.writes) >= 1)
        fake.feed(state_frame(applied_sequence=5, flags=int(HostStateFlag.ENABLE_STATUS_REPORTS)))
        thread.join(2.0)
        assert not thread.is_alive()
        assert "error" not in result, result.get("error")

        state = result["value"]
        assert state.applied_sequence == 5
        assert transport.window_size == tp.DEFAULT_WINDOW_SIZE  # no HELLO -> the marked default
        assert transport.hello is None

        # The counter is synced so the next real send lands at appliedSequence + 1.
        assert transport.send_desired_state(neutral()) == 6

        sent = decode_host_states(fake.writes)
        # The probe carried ENABLE_STATUS_REPORTS (a session flag rides every frame).
        assert HostStateFlag.ENABLE_STATUS_REPORTS & sent[0].flags
        assert HostStateFlag.ENABLE_STATUS_REPORTS & sent[-1].flags
    finally:
        transport.close()


def test_connect_adopts_hello_over_defaults():
    fake = FakeSerial()
    transport = make_transport(fake)
    try:
        thread, result = run_bg(transport.connect, timeout=2.0)
        assert wait_until(lambda: len(fake.writes) >= 1)
        # A fresh boot: a HELLO arrives and is authoritative for window/module/freq range. But a
        # HELLO's embedded state has session flags == 0, so it must NOT complete the handshake on
        # its own (it would falsely report a green round trip). connect keeps waiting until a real
        # DeviceState echoes ENABLE_STATUS_REPORTS.
        fake.feed(hello_frame(window_size=1024, rf_module_type=1, min_freq=430.0, max_freq=440.0))
        assert wait_until(lambda: transport.hello is not None)
        assert thread.is_alive()  # the boot HELLO alone did NOT satisfy connect
        fake.feed(state_frame(applied_sequence=2, flags=int(HostStateFlag.ENABLE_STATUS_REPORTS)))
        thread.join(2.0)
        assert not thread.is_alive() and "error" not in result

        assert transport.window_size == 1024  # HELLO adopted for window/module/freq
        assert transport.hello is not None
        assert transport.hello.version.rf_module_type == 1
        assert transport.hello.version.min_radio_freq == pytest.approx(430.0)
        assert transport.hello.version.max_radio_freq == pytest.approx(440.0)
    finally:
        transport.close()


def test_sequence_counter_never_regresses_below_applied():
    fake = FakeSerial()
    transport = make_transport(fake)
    try:
        thread, _ = run_bg(transport.connect, timeout=2.0)
        assert wait_until(lambda: len(fake.writes) >= 1)
        fake.feed(state_frame(applied_sequence=5, flags=int(HostStateFlag.ENABLE_STATUS_REPORTS)))
        thread.join(2.0)

        for _ in range(3):
            transport.send_desired_state(neutral())

        sent = decode_host_states(fake.writes)
        seqs = [s.sequence for s in sent]
        # Strictly increasing overall...
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        # ...and every post-sync send exceeds what the device had applied (5). The lone probe
        # (seqs[0]) is deliberately stale — the firmware ignores its state but honours the flags.
        assert all(s > 5 for s in seqs[1:])
    finally:
        transport.close()


# --------------------------------------------------------------------------------------
# Firmware-accurate fake: models the device's real acceptance rule (guardrail 6 regression).
#
# The old fakes echoed whatever we sent, so a whole suite could pass against a backend that had
# never actually talked to the device. This one mirrors two firmware facts: a HostDesiredState
# whose payload length != sizeof(HostDesiredState) is dropped silently (the firmware's
# `if (param_len == sizeof(...))` with no else), and a DeviceState echoes the incoming SESSION
# flags (`flags |= sessionFlags & HOST_STATE_SESSION_FLAG_MASK`) only once a frame is accepted.
# --------------------------------------------------------------------------------------

_SESSION_MASK = HostStateFlag.RX_AUDIO_OPEN | HostStateFlag.ENABLE_STATUS_REPORTS


class FirmwareFakeSerial(FakeSerial):
    """A FakeSerial that accepts/echoes like the real firmware.

    ``accept_len`` is the payload length the "firmware" requires (default the real 22); set it to
    anything else to model a wire/struct-size mismatch — the fake then drops our (correct 22-byte)
    frames, exactly as a device on a different protocol would, so ``connect`` must time out.
    """

    def __init__(self, *, accept_len: int = HostDesiredState.SIZE) -> None:
        super().__init__()
        self._accept_len = accept_len
        self._decoder = KissDecoder()

    def emit_boot_hello(self) -> None:
        """Queue a boot HELLO whose embedded DeviceState has session flags == 0 (captured at boot)."""
        self.feed(hello_frame(window_size=1024, rf_module_type=0, min_freq=144.0, max_freq=148.0))

    def emit_rx_audio(self, packet: bytes) -> None:
        """Queue an ``RX_AUDIO`` vendor frame carrying one Opus packet, as the firmware sends it.

        The firmware emits exactly one Opus packet per ``COMMAND_RX_AUDIO`` KISS frame (ADR 0064/0065);
        this models that so the full RX chain (deframe → queue → :class:`RxAudioDecoder`) is exercised.
        """
        self.feed(build_vendor_frame(SndCommand.RX_AUDIO, packet))

    def write(self, data: bytes) -> int:
        n = super().write(data)  # record + honour .dead
        for frame in self._decoder.feed(bytes(data)):
            parsed = parse_frame(frame)
            if not (isinstance(parsed, VendorFrame)
                    and parsed.command == RcvCommand.HOST_DESIRED_STATE):
                continue
            if len(parsed.payload) != self._accept_len:
                continue  # wrong length -> firmware drops it silently, no DeviceState back
            incoming = HostDesiredState.unpack(parsed.payload)
            echoed = int(HostStateFlag(incoming.flags) & _SESSION_MASK)
            self.feed(state_frame(applied_sequence=incoming.sequence, flags=echoed))
        return n


def test_connect_proves_round_trip_and_ignores_boot_hello():
    """connect() must not latch a boot HELLO; it completes only on a state echoing the session flag."""
    fake = FirmwareFakeSerial()
    fake.emit_boot_hello()  # in flight before/around the probe, like a just-reset board
    transport = make_transport(fake)
    try:
        state = transport.connect(timeout=2.0)
        # Returned state proves the probe landed: ENABLE_STATUS_REPORTS was echoed back.
        assert DeviceStateFlag(state.flags) & DeviceStateFlag.ENABLE_STATUS_REPORTS
        assert transport.hello is not None  # the HELLO was still adopted for window/module
    finally:
        transport.close()


def test_connect_times_out_when_frame_is_dropped_for_wrong_length():
    """If the firmware drops our HostDesiredState (here: a size mismatch), connect fails LOUD.

    This is the regression the old fakes could never catch: a backend that cannot actually talk to
    the device now surfaces a timeout instead of a false-green handshake."""
    fake = FirmwareFakeSerial(accept_len=HostDesiredState.SIZE + 1)  # our 22-byte frame is rejected
    fake.emit_boot_hello()  # boot data arrives, but no frame is ever accepted
    transport = make_transport(fake)
    try:
        with pytest.raises(Kv4pTimeout):
            transport.connect(timeout=0.4)
    finally:
        transport.close()


def test_connect_times_out_on_boot_data_only():
    """A device that sends a HELLO but never acknowledges a host frame -> loud timeout, not a pass."""
    fake = FakeSerial()
    fake.feed(hello_frame(window_size=2048, rf_module_type=0, min_freq=144.0, max_freq=148.0))
    transport = make_transport(fake)
    try:
        with pytest.raises(Kv4pTimeout):
            transport.connect(timeout=0.4)
        assert transport.hello is not None  # the HELLO arrived; it just isn't a round-trip proof
    finally:
        transport.close()


def _opus_or_skip():
    """Make libopus loadable (ADR 0056/0057), then import opuslib or skip if it isn't installed."""
    from radio_server.link._opus import ensure_opus_loadable

    ensure_opus_loadable()
    return pytest.importorskip("opuslib")


def _encode_opus_packet(opuslib, *, samples: int) -> bytes:
    from radio_server.backends.kv4p.audio import OPUS_CHANNELS, OPUS_RATE

    enc = opuslib.Encoder(OPUS_RATE, OPUS_CHANNELS, opuslib.APPLICATION_AUDIO)
    enc.max_bandwidth = opuslib.BANDWIDTH_NARROWBAND
    return enc.encode(b"\x00" * (samples * 2), samples)


def test_firmware_rx_audio_opus_decodes_to_one_canonical_frame():
    """An RX_AUDIO Opus packet from the firmware-accurate fake round-trips to one 1920-sample frame.

    Exercises the full RX chain end to end: firmware KISS frame → transport deframe/queue →
    :meth:`read_audio` → :class:`RxAudioDecoder` (ADR 0065).
    """
    opuslib = _opus_or_skip()
    from radio_server.backends.kv4p.audio import FRAME_BYTES, FRAME_SAMPLES, RxAudioDecoder

    fake = FirmwareFakeSerial()
    transport = make_transport(fake)
    try:
        fake.emit_rx_audio(_encode_opus_packet(opuslib, samples=FRAME_SAMPLES))
        popped: list[bytes] = []

        def _pop() -> bool:
            packet = transport.read_audio()
            if packet is not None:
                popped.append(packet)
            return bool(popped)

        assert wait_until(_pop)
        frame = RxAudioDecoder().push(popped[0])
        assert len(frame.samples) == FRAME_BYTES  # one 40 ms packet -> one 1920-sample frame
    finally:
        transport.close()


def test_firmware_rx_audio_corrupt_packet_is_dropped_not_fatal():
    """A corrupt Opus packet off the wire is dropped by the decoder — the reader/consumer survives."""
    _opus_or_skip()
    from radio_server.backends.kv4p.audio import RxAudioDecoder

    fake = FirmwareFakeSerial()
    transport = make_transport(fake)
    try:
        fake.emit_rx_audio(b"\xff\xff\xff")  # corrupted stream
        popped: list[bytes] = []

        def _pop() -> bool:
            packet = transport.read_audio()
            if packet is not None:
                popped.append(packet)
            return bool(popped)

        assert wait_until(_pop)
        frame = RxAudioDecoder().push(popped[0])
        assert frame.samples == b""  # dropped, no raise
    finally:
        transport.close()


# --------------------------------------------------------------------------------------
# Flow control — encoded-byte accounting
# --------------------------------------------------------------------------------------


def _fend_heavy_frame() -> bytes:
    # A payload of raw FEND bytes: each escapes to two bytes, so the on-wire frame is far
    # longer than the payload — the whole point of counting *encoded* bytes.
    built = build_vendor_frame(RcvCommand.HOST_TX_AUDIO, b"\xc0" * 100)
    assert len(built) > 200  # proof the escaping blew the length up past the raw payload
    return built


def test_write_spends_encoded_not_payload_bytes():
    fake = FakeSerial()
    built = _fend_heavy_frame()
    transport = make_transport(fake, window_size=len(built) + 50)
    try:
        transport._write_frame(built)
        # Credits fall by the escaped on-wire length (len(built)), NOT the 100-byte payload.
        assert transport._credits == 50
    finally:
        transport.close()


def test_write_blocks_at_zero_credits_and_resumes_on_window_update():
    fake = FakeSerial()
    built = _fend_heavy_frame()
    transport = make_transport(fake, window_size=len(built))
    try:
        transport._write_frame(built)  # spends the whole window; credits now 0
        assert transport._credits == 0

        thread, result = run_bg(transport._write_frame, built)
        # It must be blocked — no second frame reaches the wire yet.
        assert not wait_until(lambda: len(fake.writes) >= 2, timeout=0.2)

        fake.feed(window_frame(len(built)))  # the device refunds the encoded bytes
        thread.join(2.0)
        assert not thread.is_alive() and "error" not in result
        assert len(fake.writes) == 2
    finally:
        transport.close()


def test_write_times_out_without_credits():
    fake = FakeSerial()
    transport = make_transport(fake, window_size=0, write_timeout=0.05)
    try:
        with pytest.raises(Kv4pTimeout):
            transport._write_frame(_fend_heavy_frame())
    finally:
        transport.close()


# --------------------------------------------------------------------------------------
# Dispatch routing
# --------------------------------------------------------------------------------------


def test_dispatch_routes_each_command(caplog):
    fake = FakeSerial()
    transport = make_transport(fake, window_size=100)
    try:
        fake.feed(build_vendor_frame(SndCommand.RX_AUDIO, b"\x01\x02\x03"))
        assert wait_until(lambda: transport.read_audio() == b"\x01\x02\x03")

        fake.feed(state_frame(applied_sequence=7))
        assert wait_until(lambda: transport.device_state is not None)
        assert transport.device_state.applied_sequence == 7

        before = transport._credits
        fake.feed(window_frame(500))
        assert wait_until(lambda: transport._credits == before + 500)

        with caplog.at_level(logging.WARNING, logger=tp.__name__):
            fake.feed(build_vendor_frame(SndCommand.DEBUG_WARN, b"low battery"))
            assert wait_until(lambda: "low battery" in caplog.text)

        # An unknown device command is dropped without disturbing the audio queue.
        fake.feed(build_vendor_frame(0x42, b"junk"))
        time.sleep(0.05)
        assert transport.read_audio() is None
    finally:
        transport.close()


def test_data_frame_is_inert_and_never_reaches_the_vendor_path():
    fake = FakeSerial()
    transport = make_transport(fake)
    try:
        fake.feed(build_kiss_frame(KISS_CMD_DATA, b"an AX.25 packet"))
        # Followed by a real DeviceState we CAN observe, to know the DATA frame was processed.
        fake.feed(state_frame(applied_sequence=3))
        assert wait_until(lambda: transport.device_state is not None)
        # The DATA frame reached neither the audio queue nor the state sink.
        assert transport.read_audio() is None
        assert transport.device_state.applied_sequence == 3
    finally:
        transport.close()


# --------------------------------------------------------------------------------------
# Reader-thread robustness
# --------------------------------------------------------------------------------------


def test_reader_survives_mid_frame_chunk_boundary():
    fake = FakeSerial()
    transport = make_transport(fake)
    try:
        frame = state_frame(applied_sequence=9)
        cut = len(frame) // 2
        fake.feed(frame[:cut])  # a frame split across two reads
        fake.feed(frame[cut:])
        assert wait_until(lambda: transport.device_state is not None)
        assert transport.device_state.applied_sequence == 9
    finally:
        transport.close()


def test_reader_survives_empty_read():
    fake = FakeSerial()
    transport = make_transport(fake)
    try:
        fake.feed(b"")  # a zero-length read must not stop the loop
        fake.feed(state_frame(applied_sequence=4))
        assert wait_until(lambda: transport.device_state is not None)
        assert transport.device_state.applied_sequence == 4
    finally:
        transport.close()


def test_reader_surfaces_serial_exception_to_a_blocked_waiter():
    fake = FakeSerial()
    transport = make_transport(fake)
    try:
        # A waiter blocks on a sequence that will never be applied.
        thread, result = run_bg(transport.await_applied, 999, 5.0)
        assert wait_until(lambda: thread.is_alive())

        boom = FakeSerialError("cable yanked")
        fake.feed(boom)  # the reader raises on read -> surfaces the error, wakes the waiter

        thread.join(2.0)
        assert not thread.is_alive()  # it woke rather than wedging silently
        assert result.get("error") is boom
        # A fresh call re-raises the same stored error rather than hanging.
        with pytest.raises(FakeSerialError):
            transport.send_desired_state(neutral())
    finally:
        transport.close()


# --------------------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------------------


def test_close_twice_is_a_no_op():
    fake = FakeSerial()
    transport = make_transport(fake, write_timeout=0.05)
    transport.close()
    assert fake.closed
    transport.close()  # second close must not raise


def test_close_with_a_dead_port_does_not_raise():
    fake = FakeSerial()
    transport = make_transport(fake, write_timeout=0.05)
    fake.dead = True  # write() and close() now raise — the device vanished
    transport.close()  # best-effort teardown swallows it
