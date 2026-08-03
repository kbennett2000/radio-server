"""Tests for the UV-K5 (Quansheng Dock) serial transport (ADR 0111).

Hardware-free: pyserial is never imported — the transport is driven through an injected
fake. The important fake is :class:`FirmwareFakeSerial`, which enforces the **firmware's
real acceptance rules** from the pinned ``uart.c`` (framing, length, obfuscation, command
CRC — dropping silently what the firmware drops) and replies with the dummy-CRC ``SendReply``
shape. That is the whole point: a transport that cannot actually speak the protocol surfaces
a loud timeout instead of a false-green pass (the kv4p lesson, ADR 0066).
"""

from __future__ import annotations

import builtins
import struct
import threading
import time

import pytest

from radio_server.backends.uvk5 import transport as tp
from radio_server.backends.uvk5.frames import (
    FLAG_TX_OK,
    BroadcastFmReply,
    BroadcastFmStatus,
    DockModulation,
    GpioInfo,
    ImHere,
    JetScanReply,
    ModulationStatus,
    ReadGpio,
    ReadRegisters,
    RegisterInfo,
    ScanReply,
    SetModulationReply,
    StockSetModulation,
    WriteGpio,
    WriteRegisters,
    build_frame,
    crc16,
)
from radio_server.backends.uvk5.transport import (
    Uvk5Closed,
    Uvk5Timeout,
    Uvk5Transport,
)

_XOR = bytes((0x16, 0x6C, 0x14, 0xE6, 0x2E, 0x91, 0x0D, 0x40,
              0x21, 0x35, 0xD5, 0x40, 0x13, 0x03, 0xE9, 0x80))


# ---------------------------------------------------------------------------------------
# Threading helpers
# ---------------------------------------------------------------------------------------


def run_bg(fn, *args, **kwargs):
    result: dict = {}

    def target():
        try:
            result["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - capture for the asserting thread
            result["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, result


def wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


# ---------------------------------------------------------------------------------------
# Layer 1 — a dumb pyserial-like pipe
# ---------------------------------------------------------------------------------------


class FakeSerialError(Exception):
    """Stands in for serial.SerialException (pyserial may be absent in the test env)."""


class FakeSerial:
    def __init__(self) -> None:
        self.port = None
        self.baudrate = None
        self.timeout = None
        self.write_timeout = None
        self.dtr = None
        self.rts = None
        self.writes: list[bytes] = []
        self.closed = False
        self.dead = False  # when True, write/close raise (a vanished device)
        import queue

        self._items: "queue.Queue[object]" = queue.Queue()

    def feed(self, item: object) -> None:
        self._items.put(item)

    def read(self, _size: int) -> bytes:
        import queue

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


# ---------------------------------------------------------------------------------------
# Layer 2 — a fake that accepts/replies like the pinned firmware (uart.c)
# ---------------------------------------------------------------------------------------


class FirmwareFakeSerial(FakeSerial):
    """Models ``UART_IsCommandAvailable`` + ``UART_HandleCommand`` + ``SendReply`` (uart.c).

    On every ``write`` it runs the firmware receive parser over the accumulated bytes: sync
    ``AB CD``, read the length, require the ``DC BA`` footer, honour the ``bIsEncrypted``
    toggle (starts True; a *pre-de-obfuscation* opcode of ``0x0514`` clears it, ``0x6902``
    sets it — exactly uart.c:1024-1028), de-obfuscate ``Size+2``, and **validate the command
    CRC**. Anything malformed is dropped silently, just like the firmware. Accepted commands
    dispatch per the pin; replies use the ``SendReply`` shape (obfuscated payload + a dummy
    ``obf(0xFF 0xFF)`` in the CRC slot).
    """

    def __init__(self) -> None:
        super().__init__()
        self.encrypted = True  # firmware default (uart.c:249)
        self.full_control = False  # set by 0x0870, cleared by 0x0871
        #: Seeded with the stock RX system-control word, which is what a healthy radio reads back
        #: on every idle probe (measured 0xBFF1 on both bands, ADR 0132). An empty register file
        #: answers 0 for reg 0x30, and 0 is a *broken* radio — the receiver disabled — so leaving
        #: it empty made every fake look like the fault `_seed_reg30` exists to repair.
        #: Reg 0x33 seeds to the firmware's own initial GPIO shadow (`gBK4819_GpioOutState =
        #: 0x9000`, bk4829.c:198) — the pin output-enable bits every real value carries.
        #:
        #: Reg 0x38/0x39 are the synthesiser, in 10 Hz units, and they get the same treatment for
        #: the same reason (ADR 0174): an empty file answers 0, and a radio whose synthesiser reads
        #: back 0 is one the host cannot key — so an unseeded fake modelled the *fault* rather than
        #: a working radio, and a test that keyed it would be refused for a reason it never meant
        #: to be about. 147.390 MHz, a value nothing else in the suite tunes to, so "it keyed on
        #: the seed" can never be mistaken for "it keyed on a tune". Clear them (or set them to
        #: something out of band) to model the radio ADR 0174 refuses.
        self.registers: dict[int, int] = {0x30: 0xBFF1, 0x33: 0x9000, 0x38: 0xE638, 0x39: 0x00E0}
        self.gpio: dict[tuple[int, int], int] = {}
        #: The demodulator the radio is on, as `0x0877` last set it. `None` is a radio nobody has
        #: told — which is what a real one is until a host says so, and not the same as "FM".
        self.modulation: int | None = None
        #: Is the BK1080 second receiver running? False is the state a healthy station is left in
        #: by `_assert_boot_broadcast_fm`; set True to model the radio that hears nothing on its
        #: own channel (ADR 0157).
        self.broadcast_fm_on = False
        self._rx = bytearray()
        #: Models the firmware's ``ENABLE_DOCK`` build flag: when False this is a STOCK radio that
        #: still answers the unguarded 0x0514 HELLO but ignores every 0x08xx dock command (ADR 0114).
        self.dock = True
        #: The 16-byte version string the 0x0515 HELLO reply carries (settable for version tests).
        self.hello_version = b"DOCK".ljust(16, b"\x00")
        #: When True, a write of reg 0x30 is dropped, so the keying read-back never returns 0xC1FE —
        #: models a radio that will not latch TX-enable (drives ``Uvk5KeyingError``).
        self.withhold_tx_confirm = False
        #: Models the F3 firmware RX-audio force-open (ADR 0120): when True, processing a 0x0870 sets
        #: REG_47=0x6142 (AF=FM/unmute) — the host-visible proxy the first-start dead-RX fix reads.
        #: Set False to model a pre-F3 build (REG_47 never comes alive).
        self.f3 = True
        #: Drops the first N 0x0870 frames (they still count as received), modelling a 0x0870 lost in
        #: the reset-on-open boot race — the firmware never runs the force-open (ADR 0122).
        self.drop_enter_hw = 0
        #: Count of 0x0870 frames the fake received (incl. dropped ones) — proves a re-send happened.
        self.enter_hw_count = 0

    # -- receive-side parser (uart.c:949-1040) -----------------------------------------

    def write(self, data: bytes) -> int:
        n = super().write(data)  # record + honour .dead
        self._rx += data
        self._consume()
        return n

    def _consume(self) -> None:
        buf = self._rx
        while True:
            start = buf.find(0xAB)
            if start < 0:
                buf.clear()
                return
            if start > 0:
                del buf[:start]  # firmware scans past non-preamble bytes
            if len(buf) < 4:
                return  # need preamble + length
            if buf[1] != 0xCD:
                del buf[:1]  # not CD after AB — advance and re-sync
                continue
            size = buf[2] | (buf[3] << 8)
            total = size + 8
            if size == 0 or size > 512:
                del buf[:2]  # bogus length — drop and resync
                continue
            if len(buf) < total:
                return  # incomplete frame — wait for more
            footer = 4 + size + 2
            if buf[footer] != 0xDC or buf[footer + 1] != 0xBA:
                del buf[:2]  # bad footer — drop and resync
                continue
            body = bytes(buf[4 : 4 + size + 2])
            del buf[:total]
            self._accept(size, body)

    def _accept(self, size: int, body: bytes) -> None:
        raw_opcode = body[0] | (body[1] << 8)  # read pre-de-obfuscation (uart.c:1024)
        if raw_opcode == 0x0514:
            self.encrypted = False
        elif raw_opcode == 0x6902:
            self.encrypted = True
        work = bytearray(body)
        if self.encrypted:
            for i in range(size + 2):
                work[i] ^= _XOR[i % 16]
        payload = bytes(work[:size])
        crc = work[size] | (work[size + 1] << 8)
        if crc16(payload) != crc:
            return  # firmware drops silently on CRC mismatch (uart.c:1039)
        self._dispatch(payload)

    # -- command dispatch (uart.c:1042-1140 + the 0x0870 full-control loop) -------------

    def _dispatch(self, payload: bytes) -> None:
        opcode = payload[0] | (payload[1] << 8)
        params = payload[4:]
        if opcode == 0x0514:  # HELLO / version — UNGUARDED (a stock radio answers too, uart.c)
            self._reply(0x0515, ImHere(self.hello_version, 0, 0, (0, 0, 0, 0)).pack())
            return
        if not self.dock:
            return  # a stock (ENABLE_DOCK=0) build ignores every 0x08xx dock command silently
        if opcode == 0x0851:  # read registers -> one RegisterInfo each
            for reg in ReadRegisters.unpack(params).registers:
                self._reply(0x0951, RegisterInfo(reg, self.registers.get(reg, 0)).pack())
        elif opcode == 0x0850:  # write registers -> no reply, update the store
            for reg, value in WriteRegisters.unpack(params).registers:
                if reg == 0x30 and self.withhold_tx_confirm:
                    continue  # model a radio that never latches TX-enable (read-back != 0xC1FE)
                self.registers[reg] = value
                # Per PAIR, not per frame. The firmware's dock hook runs inside this very loop
                # (`dock.c:61-68`), so a subclass modelling `Dock_ForceTx` / `Dock_EndTx` sees the
                # register file exactly as it stood at that pair — which is the only way to observe
                # what ORDER within a batch actually means. Applying the whole frame and then
                # checking would sample the un-key edge with the frequency already restored, i.e.
                # report the hazardous ordering as normal (ADR 0133).
                self._on_register_write(reg, value)
        elif opcode == 0x0861:  # read gpio -> one GpioInfo each
            for port, bit in ReadGpio.unpack(params).pins:
                self._reply(0x0961, GpioInfo(port, self.gpio.get((port, bit), bit)).pack())
        elif opcode == 0x0860:  # write gpio -> no reply, update the store
            for port, bit in WriteGpio.unpack(params).pins:
                self.gpio[(port, bit)] = bit
        elif opcode == 0x0888:  # jet scan -> one reply
            self._reply(0x0988, JetScanReply(tuple([0] * 16), tuple([0] * 16)).pack())
        elif opcode == 0x0808:  # spectrum scan -> one reply
            self._reply(0x0908, ScanReply(0, 0, bytes(100)).pack())
        elif opcode == 0x0803:  # screen dump: raw 0xEF + 1024 bytes, NOT a framed reply
            self.feed(bytes([0xEF]) + bytes(1024))
        elif opcode == 0x0870:  # enter full-control mode
            self.enter_hw_count += 1
            if self.drop_enter_hw > 0:
                self.drop_enter_hw -= 1
                return  # 0x0870 lost in the reset-on-open boot race — the F3 force-open never runs
            self.full_control = True
            if self.f3:
                self.registers[0x47] = 0x6142  # Dock_ForceRxAudioAlive: AF=FM/unmute (ADR 0120)
        elif opcode == 0x0879:  # drive the BK1080 second receiver -> one 0x087A reply (F8/F9)
            # The deployed station runs F9 and answers this — its `/status` carries a real
            # `broadcast_fm` block — so a fake that stayed silent was modelling a radio OLDER than
            # the one on the bench, and charging every test built on it a 3 s `SetVfoTuner` timeout
            # at construction for the privilege (`AiocBaofeng._assert_boot_broadcast_fm`).
            # `broadcast_fm_on` decides which leg: the OFF action always applies, and the probe
            # (an out-of-band TUNE, deliberately unbuildable as a real request) is refused
            # `ERR_OFF` when the receiver is off and `ERR_BAND` when it is on — that asymmetry is
            # the whole probe (ADR 0163), so it has to be modelled and not shortcut.
            action = params[0] if params else 0
            if action == 0:                                      # OFF — always applies
                self.broadcast_fm_on = False
                self._reply(0x087A, BroadcastFmReply(BroadcastFmStatus.APPLIED, 0, 0, 0, 0).pack())
            elif self.broadcast_fm_on:
                self._reply(0x087A, BroadcastFmReply(BroadcastFmStatus.ERR_BAND).pack())
            else:
                self._reply(0x087A, BroadcastFmReply(BroadcastFmStatus.ERR_OFF).pack())
        elif opcode == 0x0877:  # set the demodulator -> one 0x0878 reply (F7)
            # Modelled because a hybrid tuner states the demodulator at CONSTRUCTION
            # (`AiocBaofeng._assert_modulation`), so a fake that stays silent here costs every
            # baofeng-over-this-fake test a full 3 s `SetVfoTuner` timeout before its first
            # assertion — and reports the radio as pre-F7 while doing it. The reply carries the
            # modulation read back and `FLAG_TX_OK` on FM only, which is what the firmware does:
            # `RADIO_PrepareTX` disables TX on anything but FM in a build without
            # ENABLE_TX_WHEN_AM, and that flag is how a host learns it (ADR 0149/0150).
            want = params[0] if params else DockModulation.UNKNOWN
            if want in (DockModulation.FM, DockModulation.AM):
                self.modulation = want
                self._reply(0x0878, SetModulationReply(
                    ModulationStatus.APPLIED, want, want,
                    FLAG_TX_OK if want == DockModulation.FM else 0,
                ).pack())
            else:
                self._reply(0x0878, SetModulationReply(ModulationStatus.ERR_FIELD).pack())
        elif opcode == 0x0871:  # exit full-control mode
            self.full_control = False
        # 0x0872 (StockSetModulation): no reply at top level (the cycle-1 discrepancy); inside
        # full-control it is a set-modulation no-op — still no reply. 0x0801 (keypress): no
        # reply. Unknown opcodes: no reply.

    def _on_register_write(self, reg: int, value: int) -> None:
        """Hook fired after each register write lands. Inert here; see `ForceTxFake`."""

    # -- transmit-side (SendReply, uart.c:251-283) --------------------------------------

    def _reply(self, opcode: int, params: bytes) -> None:
        payload = struct.pack("<HH", opcode, len(params)) + params
        size = len(payload)
        if self.encrypted:
            obf = bytes(b ^ _XOR[i % 16] for i, b in enumerate(payload))
            pad = bytes((_XOR[size % 16] ^ 0xFF, _XOR[(size + 1) % 16] ^ 0xFF))
        else:
            obf = payload
            pad = b"\xff\xff"
        self.feed(b"\xab\xcd" + struct.pack("<H", size) + obf + pad + b"\xdc\xba")


def make_transport(fake: FakeSerial, **kwargs) -> Uvk5Transport:
    return Uvk5Transport(_serial_factory=lambda port, baud: fake, **kwargs)


# ---------------------------------------------------------------------------------------
# Connect
# ---------------------------------------------------------------------------------------


def test_connect_succeeds_against_firmware_fake():
    fake = FirmwareFakeSerial()
    fake.registers[tp._PROBE_REGISTER] = 0x1234
    transport = make_transport(fake)
    try:
        transport.connect(timeout=1.0)  # returns without raising == link alive
        assert any(b[:2] == b"\xab\xcd" for b in fake.writes)  # it actually wrote a probe
    finally:
        transport.close()


def test_connect_retransmits_and_tolerates_initial_silence():
    # The fake only starts answering after a short delay — connect must retransmit.
    fake = FirmwareFakeSerial()

    def arm():
        time.sleep(0.15)
        fake.registers[tp._PROBE_REGISTER] = 7  # already answered (defaults to 0) — this just
        # proves connect keeps probing; even without this the default 0 answer would satisfy it.

    threading.Thread(target=arm, daemon=True).start()
    transport = make_transport(fake)
    try:
        transport.connect(timeout=1.0)
        assert len(fake.writes) >= 1
    finally:
        transport.close()


def test_connect_times_out_when_frames_are_dropped():
    # The regression the old accept-anything fakes could never catch: the transport sends
    # plaintext while the firmware expects obfuscated, so every frame fails CRC and is
    # dropped — connect must surface a timeout, not hang or false-pass.
    fake = FirmwareFakeSerial()  # encrypted == True
    transport = make_transport(fake, obfuscate=False)
    try:
        with pytest.raises(Uvk5Timeout):
            transport.connect(timeout=0.4)
        assert len(fake.writes) >= 1  # it tried
    finally:
        transport.close()


# ---------------------------------------------------------------------------------------
# Pin fidelity
# ---------------------------------------------------------------------------------------


def test_write_then_read_register_round_trips_through_the_store():
    fake = FirmwareFakeSerial()
    transport = make_transport(fake)
    try:
        transport.send(WriteRegisters(((0x38, 0xABCD),)))
        reg = transport.request(ReadRegisters((0x38,)), lambda m: isinstance(m, RegisterInfo))
        assert reg == RegisterInfo(0x38, 0xABCD)
    finally:
        transport.close()


def test_set_modulation_0x0872_gets_no_reply():
    # 0x0872 is not in the top-level dispatch (uart.c:1098-1137) — a request for any reply
    # must time out.
    fake = FirmwareFakeSerial()
    transport = make_transport(fake)
    try:
        with pytest.raises(Uvk5Timeout):
            transport.request(StockSetModulation(1, 2), lambda m: True, timeout=0.3)
    finally:
        transport.close()


def test_bad_crc_command_is_dropped_by_the_fake():
    fake = FirmwareFakeSerial()
    good = build_frame(int(ReadRegisters((0x30,)).COMMAND), ReadRegisters((0x30,)).pack())
    fake.write(good)
    assert fake.read(4096) != b""  # a good frame produces a reply
    corrupt = bytearray(good)
    corrupt[5] ^= 0xFF  # flip a body byte -> CRC no longer matches
    fake.write(bytes(corrupt))
    assert fake.read(4096) == b""  # a bad-CRC frame produces nothing


def test_plaintext_hello_toggles_encryption_off_obfuscated_does_not():
    # Faithful to uart.c:1024: the toggle reads the opcode BEFORE de-obfuscation, so only a
    # plaintext 0x0514 flips encryption; an obfuscated one does not.
    fake = FirmwareFakeSerial()
    fake.write(build_frame(0x0514, struct.pack("<I", 0), obfuscate_body=False))
    assert fake.encrypted is False
    fake2 = FirmwareFakeSerial()
    fake2.write(build_frame(0x0514, struct.pack("<I", 0), obfuscate_body=True))
    assert fake2.encrypted is True


# ---------------------------------------------------------------------------------------
# Reader robustness
# ---------------------------------------------------------------------------------------


def test_reader_reassembles_a_frame_split_across_reads():
    fake = FakeSerial()
    reply = _register_info_reply(0x30, 0x55AA)
    fake.feed(reply[:3])  # partial frame
    fake.feed(reply[3:])  # remainder
    transport = make_transport(fake)
    try:
        assert wait_until(lambda: RegisterInfo(0x30, 0x55AA) in transport.drain_inbox())
    finally:
        transport.close()


def test_reader_survives_empty_reads_and_then_delivers():
    fake = FakeSerial()
    transport = make_transport(fake)
    try:
        time.sleep(0.05)  # only b"" reads so far
        fake.feed(_register_info_reply(0x31, 9))
        assert wait_until(lambda: any(isinstance(m, RegisterInfo) for m in transport._inbox))
    finally:
        transport.close()


def test_read_exception_wakes_a_blocked_request_and_re_raises():
    fake = FakeSerial()
    transport = make_transport(fake)
    try:
        thread, result = run_bg(
            transport.request, ReadRegisters((0x30,)), lambda m: isinstance(m, RegisterInfo)
        )
        assert wait_until(lambda: len(fake.writes) >= 1)  # request has sent + is waiting
        boom = FakeSerialError("device vanished")
        fake.feed(boom)
        thread.join(timeout=1.0)
        assert isinstance(result.get("error"), FakeSerialError)
        # A subsequent call re-raises the stored error too.
        with pytest.raises(FakeSerialError):
            transport.request(ReadRegisters((0x30,)), lambda m: True, timeout=0.2)
    finally:
        transport.close()


# ---------------------------------------------------------------------------------------
# Reset-safe open / missing pyserial
# ---------------------------------------------------------------------------------------


def test_default_factory_holds_lines_low_before_open(monkeypatch):
    events: list[tuple[str, object]] = []

    class Handle:
        def __setattr__(self, name, value):
            events.append((name, value))
            object.__setattr__(self, name, value)

        def open(self):
            events.append(("open", True))

    class FakeSerialModule:
        Serial = Handle

    monkeypatch.setattr(tp, "_load_serial", lambda: FakeSerialModule)
    tp._default_serial_factory("/dev/ttyACM0", 38400)
    open_at = next(i for i, e in enumerate(events) if e[0] == "open")
    dtr_at = next(i for i, e in enumerate(events) if e == ("dtr", False))
    rts_at = next(i for i, e in enumerate(events) if e == ("rts", False))
    assert dtr_at < open_at and rts_at < open_at


def test_missing_pyserial_gives_actionable_error(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "serial":
            raise ImportError("no pyserial here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match="uvk5.*extra"):
        tp._load_serial()


# ---------------------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------------------


def test_close_is_idempotent():
    fake = FakeSerial()
    transport = make_transport(fake)
    transport.close()
    transport.close()  # no raise
    assert fake.closed is True


def test_close_with_a_dead_port_does_not_raise():
    fake = FakeSerial()
    transport = make_transport(fake)
    fake.dead = True
    transport.close()  # swallows the port error


def test_request_after_close_raises_closed():
    fake = FakeSerial()
    transport = make_transport(fake)
    transport.close()
    with pytest.raises(Uvk5Closed):
        transport.request(ReadRegisters((0x30,)), lambda m: True, timeout=0.2)


def test_close_while_request_blocked_wakes_it():
    fake = FakeSerial()
    transport = make_transport(fake)
    thread, result = run_bg(
        transport.request, ReadRegisters((0x30,)), lambda m: isinstance(m, RegisterInfo)
    )
    assert wait_until(lambda: len(fake.writes) >= 1)
    transport.close()
    thread.join(timeout=1.0)
    assert isinstance(result.get("error"), Uvk5Closed)


# ---------------------------------------------------------------------------------------
# Helper: a well-formed RegisterInfo reply frame (SendReply shape, obfuscated dummy CRC)
# ---------------------------------------------------------------------------------------


def _register_info_reply(register: int, value: int) -> bytes:
    params = RegisterInfo(register, value).pack()
    payload = struct.pack("<HH", 0x0951, len(params)) + params
    size = len(payload)
    obf = bytes(b ^ _XOR[i % 16] for i, b in enumerate(payload))
    pad = bytes((_XOR[size % 16] ^ 0xFF, _XOR[(size + 1) % 16] ^ 0xFF))
    return b"\xab\xcd" + struct.pack("<H", size) + obf + pad + b"\xdc\xba"


# ---------------------------------------------------------------------------------------
# One frame in flight at a time (ADR 0163)
# ---------------------------------------------------------------------------------------
#
# The dock link had no wire lock: `self._cond` guards the waiter list, not the wire. `send()`
# writes outside it, and `_dispatch` gives a reply to the first not-done waiter whose `match`
# is true. That is safe only while every match is discriminated — ADR 0125's thread-safety
# argument turned entirely on `CatBusyGate` matching `m.register == reg`.
#
# Broadcast FM breaks that premise: `probe_broadcast_fm` and `clear_broadcast_fm` both match
# `isinstance(m, BroadcastFmReply)` and nothing else. Once anything polls, a key-up's clear can
# consume the poll's refusal, read it as "the radio refused to clear", and refuse the key-up —
# ADR 0161's defect, rebuilt by the poller. The fix is at the wire, not in the predicates,
# because the firmware is single-threaded anyway and drops frames that arrive while it is busy
# (ADR 0131).


def test_a_request_in_flight_stops_the_next_frame_going_out():
    """The wire carries one frame at a time; a second request waits its turn to *write*.

    Asserted on the writes rather than on which waiter won, because "the reply reached the
    right caller" can pass by luck on a machine that happens to schedule threads kindly. A
    frame that was never written cannot be answered by the wrong reply.
    """
    fake = FakeSerial()
    transport = Uvk5Transport(_serial_factory=lambda *_: fake)
    try:
        match = lambda m: isinstance(m, RegisterInfo)  # noqa: E731 - undiscriminated, on purpose
        thread_a, result_a = run_bg(
            transport.request, ReadRegisters((0x30,)), match, 3.0
        )
        assert wait_until(lambda: len(fake.writes) == 1), "the first frame never went out"

        thread_b, result_b = run_bg(
            transport.request, ReadRegisters((0x31,)), match, 3.0
        )
        time.sleep(0.15)  # long enough for an unlocked transport to have written it
        assert len(fake.writes) == 1, (
            "a second frame went onto the wire while the first was still outstanding"
        )

        fake.feed(_register_info_reply(0x30, 0xAAAA))
        thread_a.join(timeout=3.0)
        assert result_a.get("value") == RegisterInfo(0x30, 0xAAAA)

        assert wait_until(lambda: len(fake.writes) == 2), "the queued frame never went out"
        fake.feed(_register_info_reply(0x31, 0xBBBB))
        thread_b.join(timeout=3.0)
        assert result_b.get("value") == RegisterInfo(0x31, 0xBBBB)
    finally:
        transport.close()


def test_an_undiscriminated_reply_cannot_reach_the_wrong_caller():
    """The ADR 0161-defect regression, stated as the thing that actually goes wrong.

    Both callers match on type alone. Unlocked, both waiters are registered before either
    reply lands and the first reply goes to whoever registered first — which is not
    necessarily whoever asked for it.
    """
    fake = FakeSerial()
    transport = Uvk5Transport(_serial_factory=lambda *_: fake)
    try:
        match = lambda m: isinstance(m, RegisterInfo)  # noqa: E731
        thread_a, result_a = run_bg(transport.request, ReadRegisters((0x30,)), match, 3.0)
        assert wait_until(lambda: len(fake.writes) == 1)
        thread_b, result_b = run_bg(transport.request, ReadRegisters((0x31,)), match, 3.0)
        time.sleep(0.1)

        # One reply, for the frame that is actually in flight.
        fake.feed(_register_info_reply(0x30, 0x1234))
        thread_a.join(timeout=3.0)
        assert result_a.get("value") == RegisterInfo(0x30, 0x1234), (
            "the in-flight request did not get its own reply"
        )
        assert "value" not in result_b, "a caller that had not written yet consumed a reply"

        assert wait_until(lambda: len(fake.writes) == 2)
        fake.feed(_register_info_reply(0x31, 0x5678))
        thread_b.join(timeout=3.0)
        assert result_b.get("value") == RegisterInfo(0x31, 0x5678)
    finally:
        transport.close()


def test_wire_timeout_zero_gives_up_rather_than_queueing():
    """A poll must never queue behind a tune: it skips this round and asks again later.

    ``wire_timeout=None`` (the default, and every existing caller) keeps blocking, so the
    key-up path always wins the wire. Only the poller passes 0.
    """
    fake = FakeSerial()
    transport = Uvk5Transport(_serial_factory=lambda *_: fake)
    try:
        match = lambda m: isinstance(m, RegisterInfo)  # noqa: E731
        thread_a, _ = run_bg(transport.request, ReadRegisters((0x30,)), match, 3.0)
        assert wait_until(lambda: len(fake.writes) == 1)

        started = time.monotonic()
        with pytest.raises(Uvk5Timeout):
            transport.request(ReadRegisters((0x31,)), match, 3.0, wire_timeout=0.0)
        assert time.monotonic() - started < 1.0, "a non-blocking acquire waited anyway"
        assert len(fake.writes) == 1, "the skipped request still wrote a frame"

        fake.feed(_register_info_reply(0x30, 1))
        thread_a.join(timeout=3.0)
    finally:
        transport.close()
