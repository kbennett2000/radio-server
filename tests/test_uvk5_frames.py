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
    SetModulationReply,
    StockSetModulation,
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
    assert StockSetModulation.SIZE == 4
    assert SetModulation.SIZE == 1
    assert SetModulationReply.SIZE == 4
    assert JetScan.SIZE == 12
    assert ImHere.SIZE == 36
    assert ScanReply.SIZE == 102
    assert RegisterInfo.SIZE == 4
    assert GpioInfo.SIZE == 2
    assert JetScanReply.SIZE == 96
    # SIZE is exactly the format width — no implicit padding beyond the explicit `xx`.
    for cls in (Hello, KeyPress, GetScreen, Scan, StockSetModulation, JetScan, ImHere,
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
        StockSetModulation(length=1, mode=2),
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


# --- 0x0873 SetVfo (fork extension, F6) ------------------------------------------------
#
# This is the only command whose effect is meant to OUTLIVE the dock session, which changes
# what "wrong" costs. A register write that goes astray is undone by the next 0x0871; a VFO
# written wrong is what the radio transmits on after the host walks away — and the firmware
# has no reply channel to complain on, so a bad frame is refused in silence. The byte vector
# below is the same one pinned in the firmware's own host tests
# (uvk5v3-f1-build/tests/host/test_dock.c), so the two sides cannot drift apart unnoticed.


def test_setvfo_packs_the_exact_bytes_the_firmware_test_expects():
    """K0PRA 448.525: receive on the output, transmit 5 MHz down, 100.0 Hz, wide, high."""
    got = f.SetVfo(
        rx_hz=448_525_000, offset_hz=5_000_000, ctcss_tenths=1000,
        direction=f.OFFSET_SUB, narrow=0, power=2,
    ).pack()
    assert got == bytes(
        [0xC8, 0xF2, 0xBB, 0x1A, 0x40, 0x4B, 0x4C, 0x00, 0xE8, 0x03, 0x02, 0x00, 0x02]
    )
    assert len(got) == f.SetVfo.SIZE == 13


def test_setvfo_round_trips():
    original = f.SetVfo(448_525_000, 5_000_000, 1000, f.OFFSET_SUB, 0, 2)
    assert f.SetVfo.unpack(original.pack()) == original


def test_setvfo_dispatches_by_opcode():
    original = f.SetVfo(445_800_000)
    payload = struct.pack("<HH", DockCommand.SET_VFO, f.SetVfo.SIZE) + original.pack()
    assert parse_frame(payload) == original


def test_setvfo_does_not_collide_with_the_stock_modulation_command():
    """0x0872 is stock CMD_0872_t. Reusing it would make a documented command mean something
    else on this radio, and silently reinterpret anyone else's frame as a channel change."""
    assert DockCommand.SET_VFO == 0x0873
    assert DockCommand.STOCK_SET_MODULATION == 0x0872


def test_the_fork_modulation_command_is_0x0877_and_the_stock_one_is_still_0x0872():
    """Both, pinned together, because the interesting failure is them drifting into each other.

    `SetModulation` is the fork's F7 command and `StockSetModulation` is the classic Dock's
    undispatched `CMD_0872_t`. They share a concept, a near-identical name, and nothing else — one
    reaches a radio and the other never has. A rename that quietly re-pointed either at the other's
    opcode would send a channel-changing frame to a radio expecting something else, and no test that
    checked only one of them would notice.

    **0x0877 and not 0x0875.** ADR 0111:52 records the classic Dock's extended set as "0x0872
    modulation, 0x0873/4 backlight, 0x0875/6 AM emulation" — so the obvious next pair is claimed.
    That is the census check `0x0873`'s own allocation skipped (ADR 0140 reasoned about `0x0872`
    alone and took a pair the same list had spoken for); that one shipped and cannot be walked back,
    which is exactly why this one is pinned here (ADR 0150).
    """
    assert DockCommand.SET_MODULATION == 0x0877
    assert DockCommand.SET_MODULATION_REPLY == 0x0878
    assert DockCommand.STOCK_SET_MODULATION == 0x0872
    assert SetModulation.COMMAND == 0x0877
    assert StockSetModulation.COMMAND == 0x0872


def test_tx_hz_matches_the_firmware_offset_arithmetic():
    """Mirrors RADIO_ApplyOffset. A caller can assert where it is about to transmit BEFORE
    keying, which on a repeater input is the difference between the machine and its output."""
    assert f.SetVfo(448_525_000, 5_000_000, direction=f.OFFSET_SUB).tx_hz == 443_525_000
    assert f.SetVfo(147_000_000, 600_000, direction=f.OFFSET_ADD).tx_hz == 147_600_000
    assert f.SetVfo(445_800_000).tx_hz == 445_800_000


def test_simplex_needs_no_offset_and_no_tone():
    simplex = f.SetVfo(445_800_000)
    assert simplex.direction == f.OFFSET_NONE
    assert simplex.ctcss_tenths == 0
    assert f.SetVfo.unpack(simplex.pack()) == simplex


@pytest.mark.parametrize(
    "kwargs, why",
    [
        ({"direction": 7}, "an unknown direction would transmit somewhere unintended"),
        ({"narrow": 5}, "bandwidth is wide or narrow, nothing else"),
        ({"power": 9}, "power is off the end of the radio's scale"),
        ({"rx_hz": 0}, "a zero frequency is not a channel"),
        ({"ctcss_tenths": 1234}, "not a tone the radio's own table contains"),
        ({"direction": 1, "offset_hz": 0}, "a duplex direction with no offset is simplex in disguise"),
    ],
)
def test_setvfo_refuses_what_the_firmware_would_silently_drop(kwargs, why):
    """The firmware refuses these too, but without a reply — so a caller that got the
    encoding wrong would see success and a radio that never moved. Fail at the mistake."""
    base = {"rx_hz": 445_800_000}
    base.update(kwargs)
    with pytest.raises(ValueError):
        f.SetVfo(**base)


def test_the_lowest_and_highest_real_ctcss_tones_are_accepted():
    """67.0 and 254.1 Hz are the ends of dcs.c CTCSS_Options; rejecting either would make
    perfectly ordinary repeaters unreachable."""
    assert f.SetVfo(445_800_000, ctcss_tenths=670).ctcss_tenths == 670
    assert f.SetVfo(445_800_000, ctcss_tenths=2541).ctcss_tenths == 2541


def test_every_tone_an_operator_can_configure_is_sendable_to_the_radio():
    """The 38 tones a preset accepts must all exist in the radio's own table.

    These are two independently-maintained tables — `presets.CTCSS_TONES` (the public EIA
    set, in Hz) and the mirror of the firmware's `CTCSS_Options` (in tenths). If they ever
    disagree, an operator configures a perfectly ordinary tone, the preset loads clean, and
    the channel silently transmits with no tone at all — which looks exactly like a repeater
    that will not open. Cheaper to assert it here than to debug it on the air.
    """
    from radio_server.presets import CTCSS_TONES

    missing = sorted(hz for hz in CTCSS_TONES if round(hz * 10) not in f.CTCSS_TENTHS)
    assert not missing, f"preset tones the radio has no code for: {missing}"


# --- 0x0877 SetModulation / 0x0878 (fork extension, F7) --------------------------------
#
# The two golden vectors below are the CROSS-REPO ARTIFACT. They are transcribed from the
# firmware fork's own host harness (`tests/host/test_dock.c`, cases 25 and 26, at the merged
# F7 commit) — the only byte-exact oracle either side has — and then re-derived here from
# this file's independent reference implementation, which is a different implementation of
# the documented framing than `frames.py`. Nothing checks the two repos stay in step
# automatically (ADR 0148 left that unguarded), so this cross-check IS the check.


#: `0x0877` carrying DOCK_MOD_AM, with a real CRC-16/XMODEM — commands carry one.
GOLDEN_SET_MODULATION_AM = bytes(
    (0xAB, 0xCD, 0x05, 0x00, 0x61, 0x64, 0x15, 0xE6, 0x2F, 0x11, 0xD5, 0xDC, 0xBA)
)

#: `0x0878` APPLIED / AM / raw=1 / flags=0, with the firmware's DUMMY `obf(0xFF 0xFF)` in the
#: CRC slot. `flags = 0` is not an oversight in the fixture: AM applies and **cannot transmit**
#: on this build, which is exactly the case a host most needs to read correctly.
GOLDEN_SET_MODULATION_REPLY_AM = bytes(
    (0xAB, 0xCD, 0x08, 0x00, 0x6E, 0x64, 0x10, 0xE6,
     0x2E, 0x90, 0x0C, 0x40, 0xDE, 0xCA, 0xDC, 0xBA)
)


def test_golden_set_modulation_command_matches_the_firmware_byte_for_byte():
    """The frame this server puts on the wire, against the fork's own vector.

    Catches the failures a round-trip cannot see, because a round-trip is happy with any
    self-consistent encoding: a mis-sized `param_len`, a byte-swapped opcode, a CRC taken over
    the obfuscated bytes instead of the plaintext.
    """
    got = f.SetModulation(f.DockModulation.AM).to_frame()
    assert got == GOLDEN_SET_MODULATION_AM
    # Same bytes from an independently written framer, not from `frames.py`.
    assert got == _ref_frame(0x0877, bytes([1]))
    # And the payload the firmware will decode: opcode LE, param_len 1, one modulation byte.
    # Decoded with CRC validation, which is the rule the firmware applies to a COMMAND.
    (payload,) = Uvk5Decoder(validate_crc=True).feed(got)
    assert payload == bytes((0x77, 0x08, 0x01, 0x00, 0x01))


def test_golden_set_modulation_reply_decodes_to_what_the_radio_reported():
    """The reply the radio sends, decoded through the real decoder — the direction that matters.

    A reply is only ever *read* here, so the contract is "these exact bytes off the wire produce
    this state", not "we can rebuild them". Note the frame is accepted with the dummy CRC the
    firmware's `SendReply` writes, which is why the decoder does not validate it on replies.
    """
    assert GOLDEN_SET_MODULATION_REPLY_AM == _reply_frame(0x0878, bytes([0, 1, 1, 0]))

    (payload,) = Uvk5Decoder().feed(GOLDEN_SET_MODULATION_REPLY_AM)
    reply = parse_frame(payload)
    assert isinstance(reply, SetModulationReply)
    assert reply.status is f.ModulationStatus.APPLIED
    assert reply.ok
    assert reply.name == "AM"
    assert reply.raw == 1
    # The load-bearing one: AM applied, and the radio will NOT key its own PTT path.
    assert reply.tx_ok is False


def test_a_refused_reply_names_no_modulation_and_never_reads_as_fm():
    """`0xFF`, not `0` — and this is the one place a literal copy of `0x0874` would ship a bug.

    `SetVfoReply` blanks its frequencies to zero on a refusal because 0 Hz is obviously not a
    channel. Zero here **is** `DockModulation.FM`, so the same trick would answer a refusal with a
    plausible claim that the radio is on the one modulation that can transmit. The firmware forces
    `0xFF`; this asserts the decoder reports it as "unknown" rather than folding it onto a name.
    """
    refused = SetModulationReply.unpack(bytes((f.ModulationStatus.ERR_SHORT, 0xFF, 0xFF, 0)))
    assert not refused.ok
    assert refused.name is None
    assert refused.tx_ok is False
    # Derived here rather than transcribed (the fork asserts fields, not bytes, for this case).
    assert refused.to_frame() != GOLDEN_SET_MODULATION_REPLY_AM


def test_an_unnameable_modulation_decodes_to_none_rather_than_a_neighbour():
    """A build with `ENABLE_BYP_RAW_DEMODULATORS` has demodulators this wire cannot name, and
    `raw`'s numbering moves with that flag. Decoding by position would map one onto FM or AM."""
    exotic = SetModulationReply.unpack(bytes((0, 0xFF, 4, 0x01)))
    assert exotic.ok            # the radio DID apply something
    assert exotic.name is None  # ...but not something we can name
    assert exotic.raw == 4      # diagnostic only — never branched on
    assert exotic.tx_ok is True


def test_an_unknown_status_still_decodes_and_still_reports_not_applied():
    """A later firmware may add a status. The caller must still learn it was not `APPLIED`
    rather than have the decode raise in its face."""
    reply = SetModulationReply.unpack(bytes((99, 0xFF, 0xFF, 0)))
    assert reply.status == 99
    assert not reply.ok


@pytest.mark.parametrize(
    "value, why",
    [
        (2, "USB's number is reserved but the firmware refuses the value at F7"),
        (9, "not a modulation at all"),
        (0xFF, "the unknown sentinel is reply-only, never a request"),
        (-1, "not a byte"),
    ],
)
def test_set_modulation_refuses_rather_than_clamps(value, why):
    """Refused at the call site, where the stack points at the bug — not three layers away over
    the air as an `ERR_FIELD` that says only that the radio did not like something. A clamp would
    be worse than either: a radio quietly listening to the wrong thing."""
    with pytest.raises(ValueError):
        f.SetModulation(value)


def test_set_modulation_round_trips_and_names_itself():
    for name, value in f.MODULATION_VALUES.items():
        msg = f.SetModulation(value)
        assert f.SetModulation.unpack(msg.pack()) == msg
        assert msg.name == name


# --- EEPROM access + reset (ADR 0141) -----------------------------------------------------
#
# These write to the operator's radio, so the guards matter more than the codec. `CMD_051D` writes
# whole 8-byte chunks — `EEPROM_WriteBuffer(Offset + i*8, &Data[i*8])` — so a short or unaligned
# payload does not write less, it writes the neighbouring bytes with whatever happened to follow.
# Corrupting a settings block on someone's handheld is not a test failure you find in CI.


def test_eeprom_read_round_trips_and_pins_the_struct_size():
    original = f.EepromRead(offset=0x0E70, size=16, timestamp=0x12345678)
    assert original.SIZE == 8
    assert f.EepromRead.unpack(original.pack()) == original


def test_eeprom_read_reply_carries_exactly_the_bytes_it_claims():
    reply = f.EepromReadReply(offset=0x0E70, size=4, data=b"\x01\x02\x03\x04")
    assert f.EepromReadReply.unpack(reply.pack()).data == b"\x01\x02\x03\x04"


def test_eeprom_read_reply_refuses_a_truncated_body():
    """A short reply means a dropped frame, not zeros. Padding it out would fabricate EEPROM
    contents, and the very next step writes them back."""
    with pytest.raises(ValueError):
        f.EepromReadReply.unpack(struct.pack("<HBB", 0x0E70, 16, 0) + b"\x01\x02")


def test_eeprom_write_refuses_a_payload_that_is_not_a_whole_chunk():
    """The firmware writes 8 bytes at a time. A 1-byte payload does not update 1 byte — it updates
    8, with 7 bytes of whatever followed in the buffer."""
    with pytest.raises(ValueError):
        f.EepromWrite(offset=0x0E78, data=b"\x00", timestamp=1)
    with pytest.raises(ValueError):
        f.EepromWrite(offset=0x0E78, data=b"", timestamp=1)


def test_eeprom_write_refuses_an_unaligned_offset():
    """Writes land at offset + i*8, so starting mid-chunk straddles two of them."""
    with pytest.raises(ValueError):
        f.EepromWrite(offset=0x0E7C, data=b"\x00" * 8, timestamp=1)


def test_eeprom_write_round_trips_a_whole_chunk():
    original = f.EepromWrite(offset=0x0E78, data=bytes(range(8)), timestamp=0xDEADBEEF)
    assert f.EepromWrite.unpack(original.pack()) == original


def test_dual_watch_byte_sits_inside_the_chunk_we_write():
    """0xA00C is index 4 of the 8-byte chunk at 0xA008. If these ever disagree the write would
    modify the wrong setting on a real radio — squelch, or the battery saver.

    The addresses are 0xA0xx and NOT the classic 0x0E7x: settings.c uses raw flash on this tree and
    eeprom_compat maps 0xA000 identity, while 0x0E70 lands in the channel region and reads as 0xFF —
    indistinguishable, from the host, from a settings block full of defaults."""
    assert f.EEPROM_SETTINGS_BLOCK == 0xA000
    assert f.EEPROM_SETTINGS_CHUNK == 0xA008
    assert f.EEPROM_DUAL_WATCH - f.EEPROM_SETTINGS_CHUNK == 4
    assert 0 <= f.EEPROM_DUAL_WATCH - f.EEPROM_SETTINGS_CHUNK < f.EEPROM_CHUNK


def test_reset_takes_no_parameters():
    assert f.Reset().pack() == b""
    with pytest.raises(ValueError):
        f.Reset.unpack(b"\x00")


def test_eeprom_frames_dispatch_by_opcode():
    payload = struct.pack("<HH", DockCommand.EEPROM_READ, f.EepromRead.SIZE) + f.EepromRead(
        offset=0x0E70, size=16, timestamp=7
    ).pack()
    got = parse_frame(payload)
    assert isinstance(got, f.EepromRead)
    assert got.offset == 0x0E70
