"""Tests for the UV-K5 (Quansheng Dock) wire codec (ADR 0110).

Pure, no I/O. The ``SIZE`` assertions are the load-bearing check that our ``struct``
formats match the firmware's C struct layouts (uart.c:65-227); the round-trips prove the
codecs are inverse. The framing golden vectors are **hand-derived from the documented
framing** (crc.c / Comms.cs), computed here by an independent reference and anchored to
concrete literals — none are copied out of the GPL client tree.
"""

from __future__ import annotations

import struct

import pytest

from radio_server.backends.uvk5 import frames as f
from radio_server.backends.uvk5.frames import (
    FOOTER,
    OBFUSCATION,
    PREAMBLE,
    DockCommand,
    GetScreen,
    GpioInfo,
    Hello,
    ImHere,
    JetScan,
    JetScanReply,
    KeyPress,
    RawMessage,
    ReadGpio,
    ReadRegisters,
    RegisterInfo,
    Scan,
    ScanReply,
    SetModulation,
    Uvk5Decoder,
    WriteGpio,
    WriteRegisters,
    build_frame,
    crc16,
    obfuscate,
    parse_frame,
)


# ---------------------------------------------------------------------------------------
# Independent reference for golden derivation (a deliberately different implementation
# of the documented steps — not frames.py, not the GPL tree).
# ---------------------------------------------------------------------------------------

_XOR = bytes((0x16, 0x6C, 0x14, 0xE6, 0x2E, 0x91, 0x0D, 0x40,
              0x21, 0x35, 0xD5, 0x40, 0x13, 0x03, 0xE9, 0x80))


def _ref_crc(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if (crc & 0x8000) else (crc << 1) & 0xFFFF
    return crc


def _ref_frame(command: int, params: bytes, obf: bool = True) -> bytes:
    payload = struct.pack("<HH", command, len(params)) + params
    body = payload + struct.pack("<H", _ref_crc(payload))
    if obf:
        body = bytes(b ^ _XOR[i % 16] for i, b in enumerate(body))
    return b"\xab\xcd" + struct.pack("<H", len(payload)) + body + b"\xdc\xba"


def _reply_frame(command: int, params: bytes) -> bytes:
    """Model the firmware ``SendReply`` (uart.c:251-283): the two bytes before the footer
    are ``obf(0xFF 0xFF)`` — a dummy, not a real CRC."""
    payload = struct.pack("<HH", command, len(params)) + params
    size = len(payload)
    obf_payload = bytes(b ^ _XOR[i % 16] for i, b in enumerate(payload))
    pad = bytes((_XOR[(size + 0) % 16] ^ 0xFF, _XOR[(size + 1) % 16] ^ 0xFF))
    return b"\xab\xcd" + struct.pack("<H", size) + obf_payload + pad + b"\xdc\xba"


# ---------------------------------------------------------------------------------------
# Struct sizes
# ---------------------------------------------------------------------------------------


def test_struct_sizes_match_c_layout():
    # Derived byte-for-byte from the CMD_/REPLY_ structs in uart.c (param region only).
    assert Hello.SIZE == 4
    assert KeyPress.SIZE == 6
    assert GetScreen.SIZE == 4
    assert Scan.SIZE == 14
    assert SetModulation.SIZE == 4
    assert JetScan.SIZE == 12
    assert ImHere.SIZE == 36
    assert ScanReply.SIZE == 102
    assert RegisterInfo.SIZE == 4
    assert GpioInfo.SIZE == 2
    assert JetScanReply.SIZE == 96
    # SIZE is exactly the format width — no implicit padding beyond the explicit `xx`.
    for cls in (Hello, KeyPress, GetScreen, Scan, SetModulation, JetScan, ImHere,
                ScanReply, RegisterInfo, GpioInfo, JetScanReply):
        assert cls.SIZE == struct.calcsize(cls._FORMAT)


# ---------------------------------------------------------------------------------------
# Fixed-struct round-trips
# ---------------------------------------------------------------------------------------


def test_fixed_struct_round_trips():
    cases = [
        Hello(timestamp=0x12345678),
        KeyPress(key=0x0D, padding=0, timestamp=0),
        KeyPress(key=0x2A, padding=0xFF, timestamp=0xDEADBEEF),
        GetScreen(timestamp=0),
        Scan(mid_freq=145_500_000, width=100_000, density=128, timestamp=0x12345678),
        SetModulation(length=1, mode=2),
        JetScan(start_freq=430_000_000, end_freq=440_000_000, step=12_500),
        RegisterInfo(register=0x38, value=0xBEEF),
        GpioInfo(gpio=5, bit=3),
        ImHere(version=b"DOCK-0.32".ljust(16, b"\x00"), has_custom_aes_key=0,
               in_lock_screen=1, challenge=(1, 2, 3, 0xFFFFFFFF)),
        ScanReply(length=100, sync=7, signals=bytes(range(100))),
        JetScanReply(freqs=tuple(range(16)), sigs=tuple(range(100, 116))),
    ]
    for msg in cases:
        data = msg.pack()
        assert len(data) == type(msg).SIZE
        assert type(msg).unpack(data) == msg


def test_variable_struct_round_trips():
    wr = WriteRegisters(registers=((0x38, 0x1234), (0x39, 0x0009), (0x33, 0xA5A5)))
    assert WriteRegisters.unpack(wr.pack()) == wr
    assert wr.pack()[:2] == struct.pack("<H", 3)  # Length = pair count

    rr = ReadRegisters(registers=(0x38, 0x39, 0x30, 0x33))
    assert ReadRegisters.unpack(rr.pack()) == rr
    assert rr.pack()[:2] == struct.pack("<H", 4)

    wg = WriteGpio(pins=((0, 3), (4, 1)))  # set A.3, clear B.1
    assert WriteGpio.unpack(wg.pack()) == wg

    rg = ReadGpio(pins=((0, 3), (1, 2), (2, 0)))
    assert ReadGpio.unpack(rg.pack()) == rg

    # Empty collections are valid (Length 0).
    assert WriteRegisters.unpack(WriteRegisters(()).pack()) == WriteRegisters(())


def test_fixed_unpack_rejects_wrong_length():
    with pytest.raises(ValueError):
        Hello.unpack(b"\x00" * (Hello.SIZE - 1))
    with pytest.raises(ValueError):
        ImHere.unpack(b"\x00" * (ImHere.SIZE + 1))


def test_variable_unpack_rejects_truncated_body():
    good = WriteRegisters(((0x38, 1), (0x39, 2))).pack()
    with pytest.raises(ValueError):
        WriteRegisters.unpack(good[:-1])  # claims 2 pairs, one byte short


# ---------------------------------------------------------------------------------------
# CRC and obfuscation
# ---------------------------------------------------------------------------------------


def test_crc16_is_xmodem():
    # The universal CRC-16/XMODEM check value anchors our impl to the standard.
    assert crc16(b"123456789") == 0x31C3
    assert crc16(b"") == 0x0000


def test_obfuscate_is_self_inverse_and_matches_table():
    assert obfuscate(bytes(16)) == OBFUSCATION  # 0 ^ table == table
    assert obfuscate(b"\x00") == b"\x16"
    blob = bytes(range(256))
    assert obfuscate(obfuscate(blob)) == blob


# ---------------------------------------------------------------------------------------
# Framing golden vectors (hand-derived; literals computed from the documented steps)
# ---------------------------------------------------------------------------------------


def test_build_frame_golden_keypress():
    # KeyPress(key=0x0D): payload = 0108 0600 0d0000000000, CRC-16 = 0x8832, whole
    # payload+CRC obfuscated, wrapped AB CD <len> … DC BA. Literal computed by hand from
    # the crc.c / Comms.cs framing spec.
    golden = bytes.fromhex("abcd0a00176412e623910d402135e7c8dcba")
    got = build_frame(DockCommand.KEYPRESS, struct.pack("<BBI", 0x0D, 0, 0))
    assert got == golden
    assert got == KeyPress(key=0x0D).to_frame()


def test_build_frame_golden_hello_plaintext():
    # HELLO is the one exchange the firmware runs unobfuscated (uart.c:1024-1035).
    golden = bytes.fromhex("abcd08001405040078563412259ddcba")
    got = build_frame(DockCommand.HELLO, struct.pack("<I", 0x12345678), obfuscate_body=False)
    assert got == golden
    assert got == Hello(0x12345678).to_frame(obfuscate_body=False)


def test_build_frame_matches_independent_reference():
    for cmd, params in [
        (DockCommand.SCAN, Scan(145_000_000, 100_000, 64, 0).pack()),
        (DockCommand.JET_SCAN, JetScan(430_000_000, 440_000_000, 25_000).pack()),
        (DockCommand.WRITE_REGISTERS, WriteRegisters(((0x38, 0x1234), (0x39, 9))).pack()),
    ]:
        assert build_frame(cmd, params) == _ref_frame(cmd, params)
        assert build_frame(cmd, params, obfuscate_body=False) == _ref_frame(cmd, params, obf=False)


def test_build_frame_structure():
    frame = build_frame(DockCommand.KEYPRESS, struct.pack("<BBI", 1, 0, 0))
    assert frame[:2] == PREAMBLE
    assert frame[-2:] == FOOTER
    # length field = payload length = 4 (inner header) + 6 (params)
    assert struct.unpack("<H", frame[2:4])[0] == 10
    assert len(frame) == 10 + f.FRAME_OVERHEAD


def test_build_frame_rejects_oversize_payload():
    with pytest.raises(ValueError):
        build_frame(DockCommand.WRITE_REGISTERS, b"\x00" * f.MAX_PAYLOAD_SIZE)


# ---------------------------------------------------------------------------------------
# Streaming decode / resync
# ---------------------------------------------------------------------------------------


def test_decode_single_frame_round_trips_to_message():
    frame = KeyPress(key=0x0D, timestamp=0x11223344).to_frame()
    (payload,) = Uvk5Decoder().feed(frame)
    assert parse_frame(payload) == KeyPress(key=0x0D, padding=0, timestamp=0x11223344)


def test_decode_frame_split_across_chunks():
    frame = Scan(145_000_000, 100_000, 64, 0).to_frame()
    dec = Uvk5Decoder()
    out = dec.feed(frame[:3]) + dec.feed(frame[3:9]) + dec.feed(frame[9:])
    assert len(out) == 1
    assert parse_frame(out[0]) == Scan(145_000_000, 100_000, 64, 0)


def test_leading_garbage_is_discarded_then_frame_syncs():
    frame = KeyPress(key=1).to_frame()
    out = Uvk5Decoder().feed(b"\x00\xff\xab\x12garbage" + frame)
    assert len(out) == 1
    assert parse_frame(out[0]) == KeyPress(key=1)


def test_bad_footer_frame_dropped_and_stream_resyncs():
    good = KeyPress(key=2).to_frame()
    bad = bytearray(KeyPress(key=9).to_frame())
    bad[-1] = 0x00  # corrupt the footer's second byte
    out = Uvk5Decoder().feed(bytes(bad) + good)
    assert len(out) == 1
    assert parse_frame(out[0]) == KeyPress(key=2)


def test_oversize_length_dropped_not_buffered_then_resyncs():
    good = KeyPress(key=3).to_frame()
    # A frame header claiming an impossible payload length must be dropped, not buffered.
    oversize = PREAMBLE + struct.pack("<H", f.MAX_PAYLOAD_SIZE + 1) + b"\x00" * 4
    out = Uvk5Decoder().feed(oversize + good)
    assert len(out) == 1
    assert parse_frame(out[0]) == KeyPress(key=3)


def test_zero_length_frame_dropped_then_resyncs():
    good = KeyPress(key=4).to_frame()
    zero = PREAMBLE + struct.pack("<H", 0) + FOOTER
    out = Uvk5Decoder().feed(zero + good)
    assert len(out) == 1
    assert parse_frame(out[0]) == KeyPress(key=4)


def test_malformed_input_never_raises():
    dec = Uvk5Decoder()
    for chunk in (b"", b"\xab", b"\xab\xcd", bytes(range(256)) * 4, b"\xab\xcd\xff\xff"):
        assert isinstance(dec.feed(chunk), list)  # no exception


def test_reply_dummy_crc_accepted_by_default_rejected_when_validating():
    # Firmware replies carry obf(0xFF 0xFF) in the CRC slot (uart.c:270-279), not a real
    # CRC. The default decoder accepts them; a CRC-validating decoder rejects them.
    reply = _reply_frame(DockCommand.REGISTER_INFO, RegisterInfo(0x38, 0xBEEF).pack())
    (payload,) = Uvk5Decoder().feed(reply)
    assert parse_frame(payload) == RegisterInfo(0x38, 0xBEEF)
    assert Uvk5Decoder(validate_crc=True).feed(reply) == []


def test_validate_crc_accepts_real_crc_command():
    # A command carries a real CRC, so a validating decoder keeps it.
    frame = KeyPress(key=7).to_frame()
    (payload,) = Uvk5Decoder(validate_crc=True).feed(frame)
    assert parse_frame(payload) == KeyPress(key=7)


def test_reset_clears_partial_frame():
    dec = Uvk5Decoder()
    dec.feed(KeyPress(key=1).to_frame()[:5])  # mid-frame
    dec.reset()
    out = dec.feed(KeyPress(key=8).to_frame())
    assert len(out) == 1
    assert parse_frame(out[0]) == KeyPress(key=8)


# ---------------------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------------------


def test_parse_frame_dispatches_known_opcodes():
    msgs = [
        Hello(0x12345678), KeyPress(key=1), Scan(1, 2, 3, 4),
        WriteRegisters(((0x38, 1),)), ReadRegisters((0x38,)),
        WriteGpio(((0, 1),)), ReadGpio(((0, 1),)),
        RegisterInfo(0x38, 9), GpioInfo(1, 0),
    ]
    for msg in msgs:
        payload = f._INNER_HEADER.pack(type(msg).COMMAND, len(msg.pack())) + msg.pack()
        assert parse_frame(payload) == msg


def test_parse_frame_unknown_opcode_returns_rawmessage():
    payload = struct.pack("<HH", 0x9999, 2) + b"\xaa\xbb"
    got = parse_frame(payload)
    assert got == RawMessage(command=0x9999, param_len=2, params=b"\xaa\xbb")


def test_parse_frame_bad_length_falls_back_to_rawmessage():
    # Known opcode but params that do not fit the struct → RawMessage, not a raise.
    payload = struct.pack("<HH", DockCommand.REGISTER_INFO, 1) + b"\x01"
    got = parse_frame(payload)
    assert isinstance(got, RawMessage)
    assert got.command == DockCommand.REGISTER_INFO


def test_parse_frame_too_short_returns_none():
    assert parse_frame(b"\x01\x08") is None
    assert parse_frame(b"") is None
