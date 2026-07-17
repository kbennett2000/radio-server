"""Pure wire-codec tests for the kv4p HT backend (ADR 0061).

No I/O, no hardware: KISS (un)framing, the vendor envelope, and the on-wire struct
codecs. The ``SIZE`` assertions are the load-bearing check that our ``struct`` format
strings match the firmware's ``[[gnu::packed]]`` layouts (protocol.h at the pinned SHA);
the round-trips prove the codecs are inverse.
"""

from __future__ import annotations

import struct

import pytest

from radio_server.backends.kv4p.frames import (
    HOST_STATE_GLOBAL_FLAG_MASK,
    HOST_STATE_SESSION_FLAG_MASK,
    KISS_CMD_DATA,
    KISS_CMD_SETHARDWARE,
    KISS_FEND,
    KISS_FESC,
    KISS_MAX_FRAME_SIZE,
    KISS_TFEND,
    KISS_TFESC,
    KV4P_PROTOCOL_VERSION,
    PROTO_MTU,
    Ax25Frame,
    DeviceState,
    DeviceStateFlag,
    Hello,
    HostDesiredState,
    HostStateFlag,
    KissDecoder,
    RcvCommand,
    SndCommand,
    Version,
    VendorFrame,
    WindowUpdate,
    build_kiss_frame,
    build_vendor_frame,
    parse_frame,
)


# --------------------------------------------------------------------------------------
# Struct layout: calcsize against the documented field list + round-trips
# --------------------------------------------------------------------------------------


def test_struct_sizes_match_packed_layout():
    # Derived byte-for-byte from protocol.h (all [[gnu::packed]], RfModuleType=uint8).
    assert Version.SIZE == 17
    assert HostDesiredState.SIZE == 22
    assert DeviceState.SIZE == 26
    assert Hello.SIZE == 43
    assert WindowUpdate.SIZE == 4
    # No implicit padding: SIZE is exactly the sum of the field widths.
    assert Version.SIZE == struct.calcsize(Version._FORMAT)
    assert HostDesiredState.SIZE == struct.calcsize(HostDesiredState._FORMAT)
    assert DeviceState.SIZE == struct.calcsize(DeviceState._FORMAT)
    assert Hello.SIZE == Version.SIZE + DeviceState.SIZE
    assert WindowUpdate.SIZE == struct.calcsize(WindowUpdate._FORMAT)


def test_version_roundtrip():
    v = Version(
        ver=0x0102,
        radio_module_status=ord("R"),
        window_size=4096,
        rf_module_type=1,
        min_radio_freq=136.0,
        max_radio_freq=174.0,
        features=0b101,
    )
    data = v.pack()
    assert len(data) == Version.SIZE
    assert Version.unpack(data) == v


def test_host_desired_state_roundtrip():
    s = HostDesiredState(
        sequence=42,
        memory_id=-1,
        flags=int(HostStateFlag.PTT_REQUESTED | HostStateFlag.RX_AUDIO_OPEN),
        bw=1,
        freq_tx=146.5,
        freq_rx=146.5,
        ctcss_tx=12,
        squelch=3,
        ctcss_rx=0,
    )
    data = s.pack()
    assert len(data) == HostDesiredState.SIZE
    assert HostDesiredState.unpack(data) == s


def test_device_state_roundtrip_and_flags():
    s = DeviceState(
        applied_sequence=42,
        memory_id=7,
        flags=int(DeviceStateFlag.SQUELCHED | DeviceStateFlag.RX_AUDIO_OPEN),
        bw=1,
        freq_tx=146.5,
        freq_rx=146.5,
        ctcss_tx=12,
        squelch=3,
        ctcss_rx=0,
        radio_module_status=ord("A"),
        mode=1,
        last_error=0,
        latest_rssi=200,
    )
    data = s.pack()
    assert len(data) == DeviceState.SIZE
    got = DeviceState.unpack(data)
    assert got == s
    # SQUELCHED is the real busy line that makes audio.squelch="cat" valid (ADR 0061).
    assert DeviceStateFlag(got.flags) & DeviceStateFlag.SQUELCHED


def test_hello_roundtrip_nested():
    v = Version(1, ord("R"), 2048, 0, 136.0, 174.0, 0)
    d = DeviceState(1, 0, 0, 1, 146.0, 146.0, 0, 0, 0, ord("A"), 1, 0, 100)
    hello = Hello(v, d)
    data = hello.pack()
    assert len(data) == Hello.SIZE
    assert Hello.unpack(data) == hello


def test_window_update_roundtrip():
    w = WindowUpdate(size=1234)
    assert WindowUpdate.unpack(w.pack()) == w


def test_hello_unpack_rejects_wrong_length():
    with pytest.raises(ValueError):
        Hello.unpack(b"\x00" * (Hello.SIZE - 1))


def test_flag_masks_partition_host_state():
    # protocol.h:102-114 — the two masks are disjoint and the next cycle needs the split.
    assert HOST_STATE_SESSION_FLAG_MASK & HOST_STATE_GLOBAL_FLAG_MASK == 0
    assert HostStateFlag.RX_AUDIO_OPEN in HOST_STATE_SESSION_FLAG_MASK
    assert HostStateFlag.PTT_REQUESTED in HOST_STATE_GLOBAL_FLAG_MASK


def test_command_enum_values():
    assert RcvCommand.HOST_DESIRED_STATE == 0x0D
    assert RcvCommand.HOST_TX_AUDIO == 0x0C
    assert SndCommand.HELLO == 0x06
    assert SndCommand.DEVICE_STATE == 0x0B
    assert SndCommand.WINDOW_UPDATE == 0x09


# --------------------------------------------------------------------------------------
# KISS encode/escape round-trips through the streaming decoder
# --------------------------------------------------------------------------------------


def _decode_one(data: bytes) -> list[bytes]:
    return KissDecoder().feed(data)


def test_encode_decode_roundtrip_plain_payload():
    frame = build_kiss_frame(KISS_CMD_SETHARDWARE, b"hello")
    assert _decode_one(frame) == [bytes((KISS_CMD_SETHARDWARE,)) + b"hello"]


def test_escaping_fend_fesc_and_both_back_to_back():
    payload = bytes((KISS_FEND, KISS_FESC, KISS_FEND, KISS_FESC, 0x41))
    frame = build_kiss_frame(KISS_CMD_SETHARDWARE, payload)
    # The escapes must be present on the wire and absent after decode.
    assert KISS_TFEND in frame and KISS_TFESC in frame
    decoded = _decode_one(frame)
    assert decoded == [bytes((KISS_CMD_SETHARDWARE,)) + payload]


def test_vendor_frame_roundtrip():
    wire = build_vendor_frame(RcvCommand.HOST_DESIRED_STATE, b"\x01\x02\x03")
    (frame,) = _decode_one(wire)
    parsed = parse_frame(frame)
    assert parsed == VendorFrame(int(RcvCommand.HOST_DESIRED_STATE), b"\x01\x02\x03")


def test_vendor_frame_carrying_a_struct_roundtrips():
    state = HostDesiredState(9, -1, int(HostStateFlag.PTT_REQUESTED), 1, 146.0, 146.0, 0, 0, 0)
    wire = build_vendor_frame(RcvCommand.HOST_DESIRED_STATE, state.pack())
    (frame,) = _decode_one(wire)
    parsed = parse_frame(frame)
    assert isinstance(parsed, VendorFrame)
    assert parsed.command == RcvCommand.HOST_DESIRED_STATE
    assert HostDesiredState.unpack(parsed.payload) == state


# --------------------------------------------------------------------------------------
# Streaming decode
# --------------------------------------------------------------------------------------


def test_one_frame_split_across_three_chunks():
    frame = build_vendor_frame(RcvCommand.HOST_TX_AUDIO, b"abcdef")
    dec = KissDecoder()
    a, b, c = frame[:3], frame[3:7], frame[7:]
    out = dec.feed(a) + dec.feed(b) + dec.feed(c)
    assert out == [bytes((KISS_CMD_SETHARDWARE,)) + b"KV4P" + bytes(
        (KV4P_PROTOCOL_VERSION, int(RcvCommand.HOST_TX_AUDIO))
    ) + b"abcdef"]


def test_two_frames_in_one_chunk():
    f1 = build_vendor_frame(RcvCommand.HOST_TX_AUDIO, b"one")
    f2 = build_vendor_frame(RcvCommand.HOST_DESIRED_STATE, b"two")
    out = KissDecoder().feed(f1 + f2)
    assert len(out) == 2
    assert parse_frame(out[0]) == VendorFrame(int(RcvCommand.HOST_TX_AUDIO), b"one")
    assert parse_frame(out[1]) == VendorFrame(int(RcvCommand.HOST_DESIRED_STATE), b"two")


def test_leading_boot_banner_garbage_is_discarded():
    banner = b"kv4p-ht ready, firmware 1.0\r\n"  # plaintext before any FEND
    frame = build_vendor_frame(RcvCommand.HOST_DESIRED_STATE, b"ok")
    out = KissDecoder().feed(banner + frame)
    assert out == [bytes((KISS_CMD_SETHARDWARE,)) + b"KV4P" + bytes(
        (KV4P_PROTOCOL_VERSION, int(RcvCommand.HOST_DESIRED_STATE))
    ) + b"ok"]


def test_unknown_escape_drops_only_that_frame_and_resyncs():
    # FESC followed by a byte that is neither TFEND nor TFESC → drop the current frame.
    bad = bytes((KISS_FEND, KISS_CMD_SETHARDWARE, KISS_FESC, 0x00, 0x99, KISS_FEND))
    good = build_vendor_frame(RcvCommand.HOST_DESIRED_STATE, b"ok")
    out = KissDecoder().feed(bad + good)
    # Only the good frame survives; the decoder resynced at the next FEND.
    assert len(out) == 1
    assert parse_frame(out[0]) == VendorFrame(int(RcvCommand.HOST_DESIRED_STATE), b"ok")


def test_empty_frames_between_fends_yield_nothing():
    # Back-to-back FENDs (idle fill) produce no frames.
    frame = build_kiss_frame(KISS_CMD_SETHARDWARE, b"x")
    out = KissDecoder().feed(bytes((KISS_FEND, KISS_FEND)) + frame + bytes((KISS_FEND,)))
    assert out == [bytes((KISS_CMD_SETHARDWARE,)) + b"x"]


def test_oversize_frame_dropped_not_truncated():
    # A payload past KISS_MAX_FRAME_SIZE must be dropped whole, and a following valid
    # frame must still decode.
    huge = b"\x41" * (KISS_MAX_FRAME_SIZE + 10)
    oversize = build_kiss_frame(KISS_CMD_SETHARDWARE, huge)
    good = build_vendor_frame(RcvCommand.HOST_DESIRED_STATE, b"ok")
    out = KissDecoder().feed(oversize + good)
    assert len(out) == 1
    assert parse_frame(out[0]) == VendorFrame(int(RcvCommand.HOST_DESIRED_STATE), b"ok")


# --------------------------------------------------------------------------------------
# Frame dispatch
# --------------------------------------------------------------------------------------


def test_data_frame_parses_as_ax25_and_is_not_a_vendor_command():
    ax25 = b"\x82\xa0\xb4payload"
    frame = build_kiss_frame(KISS_CMD_DATA, ax25)
    (decoded,) = _decode_one(frame)
    parsed = parse_frame(decoded)
    assert parsed == Ax25Frame(ax25)
    assert not isinstance(parsed, VendorFrame)


def test_empty_data_frame_is_dropped():
    (decoded,) = _decode_one(build_kiss_frame(KISS_CMD_DATA, b""))
    # Firmware only dispatches DATA when 0 < len <= PROTO_MTU.
    assert parse_frame(decoded) is None


def test_nonzero_kiss_port_is_dropped():
    # High nibble of the command byte is the port; anything but port 0 is ignored.
    command_byte = (0x1 << 4) | KISS_CMD_SETHARDWARE
    frame = build_kiss_frame(command_byte, b"KV4P" + bytes((KV4P_PROTOCOL_VERSION, 0x0D)))
    (decoded,) = _decode_one(frame)
    assert parse_frame(decoded) is None


def test_vendor_bad_prefix_ignored_not_raised():
    frame = build_kiss_frame(KISS_CMD_SETHARDWARE, b"XXXX" + bytes((KV4P_PROTOCOL_VERSION, 0x0D)))
    (decoded,) = _decode_one(frame)
    assert parse_frame(decoded) is None


def test_vendor_wrong_protocol_version_ignored_not_raised():
    frame = build_kiss_frame(KISS_CMD_SETHARDWARE, b"KV4P" + bytes((0x02, 0x0D)))
    (decoded,) = _decode_one(frame)
    assert parse_frame(decoded) is None


def test_vendor_too_short_for_header_ignored():
    frame = build_kiss_frame(KISS_CMD_SETHARDWARE, b"KV4")  # < KV4P_VENDOR_HEADER_LEN
    (decoded,) = _decode_one(frame)
    assert parse_frame(decoded) is None


def test_unknown_kiss_command_is_dropped():
    frame = build_kiss_frame(0x0A, b"whatever")  # neither DATA nor SETHARDWARE
    (decoded,) = _decode_one(frame)
    assert parse_frame(decoded) is None


def test_parse_frame_on_empty_returns_none():
    assert parse_frame(b"") is None


def test_max_size_data_payload_is_accepted():
    # Exactly PROTO_MTU bytes of DATA payload is the largest AX.25 packet accepted.
    ax25 = b"\x41" * PROTO_MTU
    (decoded,) = _decode_one(build_kiss_frame(KISS_CMD_DATA, ax25))
    assert parse_frame(decoded) == Ax25Frame(ax25)
