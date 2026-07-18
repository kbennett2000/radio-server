"""Fake-serial tests for the kv4p HT transport (ADR 0061, ADR 0062).

No hardware and no 'hardware' extra: the transport's ``_serial_factory`` seam takes a fake
pyserial-like object, so the reader thread, flow-control window, and reconciler bookkeeping
are exercised entirely in-process. Blocking calls (``connect``, a credit-starved write, an
``await_applied``) are driven on background threads and fed device→host frames on the main
thread, mirroring how the firmware would answer.
"""

from __future__ import annotations

import dataclasses
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


# --------------------------------------------------------------------------------------
# Firmware-accurate fake: models SHIPPED v2.0.0.1 (3f0e809) acceptance, NOT e9935bd (ADR 0066).
#
# Shipped facts, read verbatim this cycle: `handleCommands` accepts a HostDesiredState iff
# `param_len == sizeof(HostDesiredState)` (no session, no sequence gate), then does a WHOLE-STRUCT
# memcpy over `desiredState`; `reconcileDesiredState` applies config to `appliedState` only on
# RADIO_CONFIG_VALID, persists `desiredState` to NVS UNCONDITIONALLY, and reports `deviceStateFlags()`
# = the WHOLE `desiredState.flags` word (no session mask). This fake models desiredState/appliedState/
# persistedState so the NVS-clobber regression (ADR 0066) is observable, and echoes the full flags word.
# --------------------------------------------------------------------------------------

#: The persistable subset `savePersistedRadioStateIfChanged` compares/writes (the flag bits, plus the
#: config fields handled separately below).
_PERSISTABLE_FLAGS = (
    HostStateFlag.HIGH_POWER
    | HostStateFlag.RSSI_ENABLED
    | HostStateFlag.FILTER_PRE
    | HostStateFlag.FILTER_HIGH
    | HostStateFlag.FILTER_LOW
    | HostStateFlag.TX_ALLOWED
)
_CONFIG_FIELDS = ("memory_id", "bw", "freq_tx", "freq_rx", "ctcss_tx", "squelch", "ctcss_rx")


class FirmwareFakeSerial(FakeSerial):
    """A FakeSerial that accepts/echoes/persists like the SHIPPED firmware (ADR 0066).

    ``accept_len`` is the payload length the "firmware" requires (default the real 22); set it to
    anything else to model a wire/struct-size mismatch — the fake then drops our (correct 22-byte)
    frames, so ``connect`` must time out. ``config`` seeds the operator's stored tuning/flags (as a
    ``HostDesiredState``) so a probe that clobbers NVS is observable via :attr:`persisted`.
    ``reporting`` starts the device with ``ENABLE_STATUS_REPORTS`` already on and emits an initial
    unsolicited report, exercising ``connect``'s passive (zero-write) path.
    """

    def __init__(
        self,
        *,
        accept_len: int = HostDesiredState.SIZE,
        config: HostDesiredState | None = None,
        reporting: bool = False,
    ) -> None:
        super().__init__()
        self._accept_len = accept_len
        self._decoder = KissDecoder()
        # Modeled device state (mirrors shipped desiredState / appliedState / persistedState).
        seed = config or HostDesiredState(
            sequence=0, memory_id=0, flags=0, bw=0, freq_tx=0.0, freq_rx=0.0,
            ctcss_tx=0, squelch=0, ctcss_rx=0,
        )
        self._desired = seed
        self._applied = {f: getattr(seed, f) for f in _CONFIG_FIELDS}  # config applied to the module
        self.persisted = self._persist_view(seed)  # what NVS holds
        if reporting:
            self._desired = dataclasses.replace(
                seed, flags=int(seed.flags) | int(HostStateFlag.ENABLE_STATUS_REPORTS)
            )
            self.feed(self._report())

    @staticmethod
    def _persist_view(state: HostDesiredState) -> dict:
        """The persistable subset NVS stores: config fields + the persistable flag bits."""
        view = {f: getattr(state, f) for f in _CONFIG_FIELDS}
        view["flags"] = int(HostStateFlag(state.flags) & _PERSISTABLE_FLAGS)
        return view

    def _report(self) -> bytes:
        """A DeviceState frame: appliedSequence + config from appliedState, flags = whole desired word."""
        state = DeviceState(
            applied_sequence=self._desired.sequence,
            memory_id=self._applied["memory_id"],
            flags=int(self._desired.flags),  # shipped deviceStateFlags(): the WHOLE desired flags word
            bw=self._applied["bw"],
            freq_tx=self._applied["freq_tx"],
            freq_rx=self._applied["freq_rx"],
            ctcss_tx=self._applied["ctcss_tx"],
            squelch=self._applied["squelch"],
            ctcss_rx=self._applied["ctcss_rx"],
            radio_module_status=0,
            mode=0,
            last_error=0,
            latest_rssi=0,
        )
        return build_vendor_frame(SndCommand.DEVICE_STATE, state.pack())

    def emit_boot_hello(self) -> None:
        """Queue a boot HELLO whose embedded DeviceState has ENABLE_STATUS_REPORTS == 0 (captured at boot)."""
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
            self._apply(HostDesiredState.unpack(parsed.payload))
        return n

    def _apply(self, incoming: HostDesiredState) -> None:
        """Mirror handleCommands + reconcileDesiredState: whole-struct memcpy, conditional retune,
        UNCONDITIONAL persist, then a report iff ENABLE_STATUS_REPORTS is set (ADR 0066)."""
        self._desired = incoming  # whole-struct memcpy
        if HostStateFlag(incoming.flags) & HostStateFlag.RADIO_CONFIG_VALID:
            self._applied = {f: getattr(incoming, f) for f in _CONFIG_FIELDS}  # retune appliedState
        self.persisted = self._persist_view(incoming)  # unconditional NVS write (the clobber vector)
        if HostStateFlag(incoming.flags) & HostStateFlag.ENABLE_STATUS_REPORTS:
            self.feed(self._report())


def a_config(**overrides) -> HostDesiredState:
    """An operator's stored radio config: 146.520 MHz simplex, TX enabled, high power (ADR 0066)."""
    base = dict(
        sequence=0, memory_id=3,
        flags=int(
            HostStateFlag.RADIO_CONFIG_VALID
            | HostStateFlag.HIGH_POWER
            | HostStateFlag.RSSI_ENABLED
            | HostStateFlag.TX_ALLOWED
        ),
        bw=1, freq_tx=146.520, freq_rx=146.520, ctcss_tx=0, squelch=1, ctcss_rx=0,
    )
    base.update(overrides)
    return HostDesiredState(**base)


def test_connect_passive_reads_a_streaming_board_without_writing():
    """A board already streaming reports is read with ZERO writes — fully non-destructive (ADR 0066)."""
    fake = FirmwareFakeSerial(config=a_config(sequence=5), reporting=True)
    before = dict(fake.persisted)
    transport = make_transport(fake)
    try:
        state = transport.connect(timeout=2.0)
        assert state.applied_sequence == 5
        assert state.freq_rx == pytest.approx(146.520)
        assert len(fake.writes) == 0  # the whole point: connect touched the board not at all
        assert fake.persisted == before  # NVS untouched
        assert transport.window_size == tp.DEFAULT_WINDOW_SIZE  # no HELLO -> the marked default
        # The counter synced to the reported appliedSequence: the next real send lands at +1.
        assert transport.send_desired_state(neutral()) == 6
    finally:
        transport.close()


def test_connect_elicit_restores_the_stored_frequency_never_clobbers_it():
    """On a reports-off board, connect elicits then RESTORES the tuning the elicit zeroed (ADR 0066).

    The old neutral probe persisted freq 0.0 + tx_allowed=false permanently. The new connect must leave
    the operator's frequency intact; TX_ALLOWED is left safely cleared (unrecoverable once overwritten)."""
    fake = FirmwareFakeSerial(config=a_config())  # reports OFF: connect must write to see anything
    transport = make_transport(fake)
    try:
        state = transport.connect(timeout=2.0)
        assert DeviceStateFlag(state.flags) & DeviceStateFlag.ENABLE_STATUS_REPORTS
        # The frequency survived the elicit's zero-clobber (restored from the device's own reply)...
        assert fake.persisted["freq_rx"] == pytest.approx(146.520)
        assert fake.persisted["freq_tx"] == pytest.approx(146.520)
        # ...TX_ALLOWED did NOT (unrecoverable) and is left safely off; power/RSSI at boot defaults.
        assert not (fake.persisted["flags"] & int(HostStateFlag.TX_ALLOWED))
        assert fake.persisted["flags"] & int(HostStateFlag.HIGH_POWER)
        assert fake.persisted["flags"] & int(HostStateFlag.RSSI_ENABLED)
    finally:
        transport.close()


def test_close_does_not_clobber_the_stored_config():
    """close()'s PTT-off reconcile echoes the last state (not zeros), so NVS is not clobbered (ADR 0066)."""
    fake = FirmwareFakeSerial(config=a_config(sequence=9), reporting=True)
    transport = make_transport(fake)
    transport.connect(timeout=2.0)  # passive: no writes yet
    transport.close()
    # After close, the persisted frequency is still the operator's, and PTT is not requested.
    assert fake.persisted["freq_rx"] == pytest.approx(146.520)
    assert not (int(fake._desired.flags) & int(HostStateFlag.PTT_REQUESTED))


def test_connect_proves_round_trip_and_ignores_boot_hello():
    """connect() must not latch a boot HELLO; it completes only on a state echoing ENABLE_STATUS_REPORTS.

    A just-reset board: the HELLO's embedded state (flag clear) must NOT satisfy the passive listen; the
    elicit + restore then complete the round trip and the HELLO is still adopted for window/module."""
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
# TX telemetry (ADR 0069 — the bench-bring-up measurement rig)
# --------------------------------------------------------------------------------------


def test_tx_stats_counts_audio_frames_and_encoded_bytes():
    fake = FakeSerial()
    transport = make_transport(fake, window_size=4096)
    try:
        transport.send_tx_audio(b"\x01\x02\x03")  # opus 3 bytes, +9 envelope -> 12 on-wire
        transport.send_tx_audio(b"\x04\x05\x06\x07\x08")  # opus 5 bytes -> 14 on-wire
        stats = transport.tx_stats
        assert stats.frames == 2
        assert stats.opus_bytes_sum == 8
        assert stats.opus_bytes_min == 3 and stats.opus_bytes_max == 5
        assert stats.wire_bytes_sum == 12 + 14  # escaped body + FENDs, no escape bytes in payloads
        assert stats.blocked_frames == 0  # 4096-byte window never ran dry
        assert stats.min_credits == 4096 - 12  # lowest credits seen at a write entry (before frame 2)
    finally:
        transport.close()


def test_tx_stats_records_a_blocked_frame_when_the_window_starves():
    fake = FakeSerial()
    packet = b"\x01\x02\x03"
    wire = len(build_vendor_frame(RcvCommand.HOST_TX_AUDIO, packet))
    transport = make_transport(fake, window_size=wire)  # room for exactly one frame
    try:
        transport.send_tx_audio(packet)  # spends the whole window; credits now 0
        thread, result = run_bg(transport.send_tx_audio, packet)  # must block on zero credit
        assert not wait_until(lambda: len(fake.writes) >= 2, timeout=0.2)
        fake.feed(window_frame(wire))  # the device refunds the encoded bytes
        thread.join(2.0)
        assert not thread.is_alive() and "error" not in result
        stats = transport.tx_stats
        assert stats.frames == 2
        assert stats.blocked_frames == 1  # only the second frame had to wait for credit
        assert stats.min_credits == 0  # the pool bottomed out
    finally:
        transport.close()


def test_reset_tx_stats_zeroes_the_counters():
    fake = FakeSerial()
    transport = make_transport(fake, window_size=4096)
    try:
        transport.send_tx_audio(b"\x01\x02\x03")
        assert transport.tx_stats.frames == 1
        transport.reset_tx_stats()
        s = transport.tx_stats
        assert s.frames == 0 and s.opus_bytes_sum == 0 and s.min_credits is None
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
