"""Pure framing tests for the DV Dongle wire codec (ADR 0086) — no hardware, no I/O.

Proves the length/type header math against the g4klx reference constants, the AMBE config blocks,
the voice-frame splice, and the streaming deframer's chunk reassembly and desync resync.
"""

from __future__ import annotations

import pytest

from radio_server.vocoder import frames as F


def test_header_length_type_encoding_matches_reference():
    # The reference constants, decoded via length = word & 0x1FFF, type = word >> 13.
    assert F.build_encode_config_packet()[:2] == bytes([0x32, 0xA0])  # len 50, type 5 (AMBE)
    assert F.build_audio_packet(bytes(F.AUDIO_PAYLOAD_LEN))[:2] == bytes([0x42, 0x81])  # len 322, type 4
    assert F.REQ_NAME == bytes([0x04, 0x20, 0x01, 0x00])  # len 4, type 1
    assert F.REQ_START == bytes([0x05, 0x00, 0x18, 0x00, 0x01])
    assert F.RESP_NAME == bytes([0x0E, 0x00, 0x01, 0x00]) + b"DV Dongle\x00"


def test_build_packet_round_trips_through_decoder():
    payload = bytes(range(10))
    packet = F._build_packet(F.TYPE_AMBE, payload)
    decoded = F.DvDongleDecoder().feed(packet)
    assert len(decoded) == 1
    assert decoded[0].type_bits == F.TYPE_AMBE
    assert decoded[0].payload == payload
    assert decoded[0].raw == packet


def test_build_packet_rejects_oversize_length():
    with pytest.raises(ValueError):
        F._build_packet(0, bytes(F.LENGTH_MASK))  # + 2 header bytes overflows the 13-bit field


def test_ambe_config_blocks_are_48_bytes_and_differ_only_at_offset_22_23():
    assert len(F.AMBE_ENC_PARAMS) == F.AMBE_PAYLOAD_LEN == 48
    assert len(F.AMBE_DEC_PARAMS) == F.AMBE_PAYLOAD_LEN == 48
    assert F.AMBE_ENC_PARAMS[22:24] == bytes([0x04, 0xF0])
    assert F.AMBE_DEC_PARAMS[22:24] == bytes([0x20, 0x80])
    diff = [i for i in range(48) if F.AMBE_ENC_PARAMS[i] != F.AMBE_DEC_PARAMS[i]]
    assert diff == [22, 23]


def test_decode_ambe_packet_splices_voice_frame_at_offset_24():
    voice = bytes(range(1, F.VOICE_FRAME_LEN + 1))
    packet = F.build_decode_ambe_packet(voice)
    parsed = F.DvDongleDecoder().feed(packet)[0]
    assert F.classify(parsed) is F.ResponseKind.AMBE
    assert F.ambe_voice_frame(parsed) == voice
    # The config prefix (bytes before the voice frame) is the decoder block's, untouched.
    assert parsed.payload[: F.AMBE_VOICE_OFFSET] == F.AMBE_DEC_PARAMS[: F.AMBE_VOICE_OFFSET]


def test_build_audio_packet_carries_pcm_verbatim():
    pcm = bytes((i * 7) % 256 for i in range(F.AUDIO_PAYLOAD_LEN))
    parsed = F.DvDongleDecoder().feed(F.build_audio_packet(pcm))[0]
    assert F.classify(parsed) is F.ResponseKind.AUDIO
    assert F.audio_pcm(parsed) == pcm


@pytest.mark.parametrize("bad_len", [0, 9, 47, 49, 320])
def test_build_ambe_packet_rejects_wrong_payload_length(bad_len):
    with pytest.raises(ValueError):
        F.build_ambe_packet(bytes(bad_len))


def test_build_audio_packet_rejects_wrong_length():
    with pytest.raises(ValueError):
        F.build_audio_packet(bytes(319))


def test_build_decode_ambe_packet_rejects_wrong_voice_frame_length():
    with pytest.raises(ValueError):
        F.build_decode_ambe_packet(bytes(8))


def test_streaming_decoder_reassembles_across_arbitrary_chunk_boundaries():
    stream = (
        F.RESP_NAME
        + F.RESP_START
        + F.build_encode_config_packet()
        + F.build_audio_packet(bytes(F.AUDIO_PAYLOAD_LEN))
        + F.RESP_NOP
    )
    decoder = F.DvDongleDecoder()
    packets = []
    for i in range(0, len(stream), 3):  # 3-byte dribble splits every packet
        packets += decoder.feed(stream[i : i + 3])
    kinds = [F.classify(p) for p in packets]
    assert kinds == [
        F.ResponseKind.NAME,
        F.ResponseKind.START,
        F.ResponseKind.AMBE,
        F.ResponseKind.AUDIO,
        F.ResponseKind.NOP,
    ]


def test_streaming_decoder_resyncs_past_garbage():
    # A byte with an implausible header (length 1, below the 2-byte minimum) must be dropped, and the
    # decoder recover on the next valid packet rather than wedge.
    garbage = bytes([0x01, 0x00])  # length 1 -> desync, dropped byte-by-byte
    decoder = F.DvDongleDecoder()
    packets = decoder.feed(garbage + F.RESP_START)
    assert [F.classify(p) for p in packets] == [F.ResponseKind.START]


def test_classify_unknown_for_unrecognised_control_packet():
    parsed = F.DvDongleDecoder().feed(F._build_packet(F.TYPE_CONTROL_RESP, bytes([0x99, 0x99])))[0]
    assert F.classify(parsed) is F.ResponseKind.UNKNOWN
