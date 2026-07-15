"""M17 CRC-16 (ADR 0050): the four published spec test vectors, exactly.

M17's CRC is a non-standard variant (poly 0x5935, init 0xFFFF, MSB-first, non-reflected), so it
is pinned here against the specification's own vectors — a bit-order slip fails a test rather than
silently corrupting every stream frame.
"""

from radio_server.link.m17.crc import crc16


def test_spec_test_vectors():
    assert crc16(b"") == 0xFFFF
    assert crc16(b"A") == 0x206E
    assert crc16(b"123456789") == 0x772B
    assert crc16(bytes(range(256))) == 0x1C31


def test_message_plus_its_crc_checksums_to_zero():
    # The standard trailing-CRC property: appending the big-endian CRC makes the whole checksum 0.
    msg = b"the quick brown fox"
    framed = msg + crc16(msg).to_bytes(2, "big")
    assert crc16(framed) == 0
