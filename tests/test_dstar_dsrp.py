"""The DSRP repeater<->gateway wire codec (ADR 0087) — pure, no I/O.

Round-trips every packet type against its reference byte layout and exercises the sequence /
end-bit / superframe-sync rules.
"""

from __future__ import annotations

from radio_server.dstar import dsrp, header

HEADER = header.build_voice_header(callsign="AE9S", module="A", ur="E")
AMBE = bytes(range(9))


def test_register_packet():
    pkt = dsrp.build_register("A")
    assert pkt.startswith(dsrp.MAGIC) and pkt[4] == dsrp.TYPE_REGISTER and pkt.endswith(b"\x00")
    msg = dsrp.parse(pkt)
    assert msg.kind is dsrp.MessageKind.REGISTER and msg.text == "A"


def test_poll_packet():
    pkt = dsrp.build_poll("A")
    assert pkt[4] == dsrp.TYPE_POLL
    assert dsrp.parse(pkt).kind is dsrp.MessageKind.POLL


def test_header_packet_round_trip():
    pkt = dsrp.build_header_packet(HEADER, 0x1234)
    assert len(pkt) == 8 + header.RADIO_HEADER_LEN and pkt[4] == dsrp.TYPE_HEADER
    msg = dsrp.parse(pkt)
    assert msg.kind is dsrp.MessageKind.HEADER
    assert msg.session_id == 0x1234
    assert msg.radio_header == HEADER


def test_header_packet_rejects_wrong_length_header():
    try:
        dsrp.build_header_packet(HEADER[:-1], 1)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for a short radio header")


def test_data_packet_round_trip_with_sync_slow_data():
    dv = dsrp.build_dv_frame(AMBE, dsrp.slow_data_for_seq(0))
    assert dv[dsrp.VOICE_FRAME_LEN :] == dsrp.DATA_SYNC  # sync at superframe start
    pkt = dsrp.build_data_packet(dv, 0x1234, 0)
    msg = dsrp.parse(pkt)
    assert msg.kind is dsrp.MessageKind.DATA
    assert msg.session_id == 0x1234
    assert msg.seq_no == 0
    assert not msg.end
    assert dsrp.voice_frame(msg.dv_frame) == AMBE


def test_data_packet_end_bit():
    dv = dsrp.build_dv_frame(dsrp.NULL_AMBE)
    pkt = dsrp.build_data_packet(dv, 7, 5, end=True)
    msg = dsrp.parse(pkt)
    assert msg.end
    assert msg.seq_no == 5  # end bit stripped from the reported sequence
    assert dsrp.voice_frame(msg.dv_frame) == dsrp.NULL_AMBE


def test_non_sync_frames_use_filler_not_sync():
    assert dsrp.slow_data_for_seq(0) == dsrp.DATA_SYNC
    assert dsrp.slow_data_for_seq(1) == dsrp.SLOW_DATA_FILLER
    assert dsrp.DATA_SYNC != dsrp.SLOW_DATA_FILLER


def test_sequence_wraps_at_superframe():
    assert dsrp.next_seq(0) == 1
    assert dsrp.next_seq(dsrp.SEQ_MAX) == 0
    # A full superframe is 21 frames (0..0x14).
    seqs = []
    s = 0
    for _ in range(22):
        seqs.append(s)
        s = dsrp.next_seq(s)
    assert seqs[:22] == list(range(0, 0x15)) + [0]


def test_build_rejects_wrong_frame_sizes():
    for bad in (b"", AMBE[:-1], AMBE + b"\x00"):
        try:
            dsrp.build_dv_frame(bad)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for a mis-sized AMBE frame")


def test_parse_of_garbage_is_unknown():
    assert dsrp.parse(b"").kind is dsrp.MessageKind.UNKNOWN
    assert dsrp.parse(b"XXXX\x20").kind is dsrp.MessageKind.UNKNOWN  # wrong magic
    assert dsrp.parse(dsrp.MAGIC + bytes([0x99])).kind is dsrp.MessageKind.UNKNOWN  # unknown type


def test_gateway_text_and_status_parse():
    text = dsrp.MAGIC + bytes([dsrp.TYPE_TEXT]) + b"LINKED  REF001 C\x00"
    assert dsrp.parse(text).kind is dsrp.MessageKind.TEXT
    status = dsrp.MAGIC + bytes([dsrp.TYPE_STATUS, 0x00]) + b"status\x00"
    assert dsrp.parse(status).kind is dsrp.MessageKind.STATUS
