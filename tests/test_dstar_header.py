"""The 41-byte D-STAR radio header + CRC-16/X-25 (ADR 0087) — pure, no I/O.

Pins the byte layout and the checksum against the g4klx reference facts: ``table[1] == 0x1189``,
the on-wire field order (RPT1 module, RPT2 gateway), the command-letter URCALL convention, and the
``FF FF`` checksum-skip sentinel the gateway honours.
"""

from __future__ import annotations

from radio_server.dstar import header


def test_crc_table_matches_g4klx():
    # The reflected CCITT (0x8408) table entry the reference documents.
    assert header._CRC_TABLE[1] == 0x1189
    assert len(header._CRC_TABLE) == 256


def test_crc_is_deterministic_and_two_bytes():
    body = header.build_voice_header(callsign="AE9S", module="A")[:-2]
    crc = header.crc16_x25(body)
    assert 0 <= crc <= 0xFFFF
    # Same input -> same CRC (a pure function of the header body).
    assert header.crc16_x25(body) == crc


def test_format_callsign_places_module_in_the_last_slot():
    assert header.format_callsign("AE9S", "G") == b"AE9S   G"
    assert header.format_callsign("AE9S", "A") == b"AE9S   A"
    assert header.format_callsign("AE9S") == b"AE9S    "
    assert len(header.format_callsign("AE9S", "B")) == header.LONG_CALLSIGN_LEN


def test_urcall_command_letter_is_right_justified():
    assert header.format_urcall("E") == b"       E"  # echo command
    assert header.format_urcall("U") == b"       U"  # unlink command
    assert header.format_urcall("CQCQCQ") == b"CQCQCQ  "  # routing target, left-justified
    assert header.format_urcall("REF001CL") == b"REF001CL"  # a reflector link, verbatim


def test_build_and_parse_round_trips_the_fields():
    h = header.build_voice_header(callsign="AE9S", module="A", ur="E", my2="INFO")
    assert len(h) == header.RADIO_HEADER_LEN
    rh = header.parse_header(h)
    assert rh.rpt1 == "AE9S   A"  # departure module (on-wire slot 1)
    assert rh.rpt2 == "AE9S   G"  # gateway (on-wire slot 2)
    assert rh.ur == "       E"
    assert rh.my1 == "AE9S    "
    assert rh.my2 == "INFO"
    assert rh.flags == (0, 0, 0)
    assert rh.checksum_ok


def test_parse_rejects_a_corrupt_checksum_but_still_returns_fields():
    h = bytearray(header.build_voice_header(callsign="AE9S", module="A"))
    h[-1] ^= 0xFF  # corrupt the CRC high byte
    rh = header.parse_header(bytes(h))
    assert not rh.checksum_ok
    assert rh.rpt1 == "AE9S   A"  # fields still decode


def test_parse_accepts_the_ff_ff_checksum_bypass():
    body = header.build_voice_header(callsign="AE9S", module="A")[:-2]
    bypass = bytes(body) + header.CHECKSUM_BYPASS
    rh = header.parse_header(bypass)
    assert rh.checksum_ok  # FF FF is the gateway's "accept without verifying" sentinel


def test_build_header_takes_preformatted_fields():
    h = header.build_header(
        rpt1=header.format_callsign("AE9S", "A"),
        rpt2=header.format_callsign("AE9S", "G"),
        ur=header.format_urcall("CQCQCQ"),
        my1=header.format_callsign("AE9S"),
    )
    assert header.parse_header(h).my2 == "    "  # default 4-space suffix
