"""The 41-byte D-STAR radio header: callsign fields + CRC-16/X-25 (ADR 0087) — pure, no I/O.

A D-STAR transmission opens with a *radio header* naming the route: three flag bytes, then the four
8-character callsigns and one 4-character suffix, closed by a 2-byte checksum. radio-server, acting as
a homebrew-repeater endpoint, builds this header to open an outbound stream to the gateway and parses
the one the gateway sends back.

Source of truth: an independent implementation from g4klx ``DStarRepeater/Common/HeaderData.cpp`` +
``RepeaterProtocolHandler.cpp`` + ``CCITTChecksumReverse.cpp`` (GPL-2) read purely as a **protocol
specification** — byte offsets and a standard CRC are interop facts, not ported code (the ADR 0086 /
kv4p stance).

**On-wire field order (the load-bearing fact, verify-against-hardware — guardrail 1).** The 41 bytes
are laid out as g4klx DStarRepeater puts them on the DSRP wire::

    [0]      flag1        [1] flag2        [2] flag3
    [3:11]   RPT2  — the gateway callsign,                    e.g. "AE9S   G"
    [11:19]  RPT1  — the departure repeater module callsign,  e.g. "AE9S   A"
    [19:27]  UR    — the destination / command, e.g. "CQCQCQ  " or "       E" (echo)
    [27:35]  MY1   — the transmitting station callsign,       e.g. "AE9S    "
    [35:39]  MY2   — the 4-char station suffix, e.g. "INFO"
    [39:41]  CRC   — CRC-16/X-25 over bytes [0:39], little-endian

This is the standard ICOM D-STAR header order (RPT2 before RPT1), matching how g4klx ``CHeaderData``'s
raw constructor reads it: on-wire slot-1 (offset 3) into ``rptCall2`` (the gateway) and slot-2 (offset
11) into ``rptCall1`` (the module). **Bench-confirmed (ADR 0087): the gateway identifies the incoming
repeater by RPT1 = the module in slot-2** — sending the module in slot-1 makes it log "Header received
from unknown repeater". The gateway also accepts ``FF FF`` as a "skip checksum" sentinel, so a correct
CRC is belt-and-suspenders; we compute it anyway.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Total bytes of a D-STAR radio header, checksum included (g4klx ``RADIO_HEADER_LENGTH_BYTES``).
RADIO_HEADER_LEN = 41

#: A full callsign field is 8 characters; the station suffix (MY2) is 4 (g4klx
#: ``LONG_CALLSIGN_LENGTH`` / ``SHORT_CALLSIGN_LENGTH``).
LONG_CALLSIGN_LEN = 8
SHORT_CALLSIGN_LEN = 4

#: Field offsets within the 41-byte header (see the module docstring for the on-wire order). The
#: on-air order is RPT2 (gateway) first at offset 3, then RPT1 (module) at offset 11 — the gateway
#: matches the incoming repeater by RPT1, so the module must land in slot-2 (bench-confirmed, ADR 0087).
_OFF_FLAGS = 0
_OFF_RPT2 = 3
_OFF_RPT1 = 11
_OFF_UR = 19
_OFF_MY1 = 27
_OFF_MY2 = 35
_OFF_CRC = 39

#: The two checksum bytes the gateway treats as "accept without verifying" (g4klx readHeader).
CHECKSUM_BYPASS = b"\xff\xff"


# --------------------------------------------------------------------------------------
# CRC-16/X-25 (reflected CCITT, init 0xFFFF, xorout 0xFFFF) — the D-STAR header checksum
# --------------------------------------------------------------------------------------

def _build_table() -> tuple[int, ...]:
    """The 256-entry reflected CRC table for polynomial 0x8408 (reverse of CCITT 0x1021).

    Matches g4klx ``CCITTChecksumReverse`` byte-for-byte (e.g. ``table[1] == 0x1189``).
    """
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
        table.append(crc)
    return tuple(table)


_CRC_TABLE = _build_table()


def crc16_x25(data: bytes) -> int:
    """CRC-16/X-25 of ``data`` (init 0xFFFF, reflected, xorout 0xFFFF) — the D-STAR header checksum."""
    crc = 0xFFFF
    for b in data:
        crc = (crc >> 8) ^ _CRC_TABLE[(crc ^ b) & 0xFF]
    return (~crc) & 0xFFFF


def _checksum_bytes(header_body: bytes) -> bytes:
    """The 2 little-endian checksum bytes for the first 39 bytes of a header."""
    crc = crc16_x25(header_body)
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])


# --------------------------------------------------------------------------------------
# Callsign formatting
# --------------------------------------------------------------------------------------

def format_callsign(callsign: str, module: str = "") -> bytes:
    """Format a callsign into an 8-byte D-STAR field: left-justified, space-padded, module at index 7.

    ``format_callsign("AE9S", "G")`` -> ``b"AE9S   G"`` (gateway); ``format_callsign("AE9S", "A")``
    -> ``b"AE9S   A"`` (module A); ``format_callsign("AE9S")`` -> ``b"AE9S    "`` (bare call). A module
    longer than one char, or a callsign that would collide with the module slot, is truncated to fit.
    """
    call = callsign.upper()[:LONG_CALLSIGN_LEN]
    field = bytearray(b" " * LONG_CALLSIGN_LEN)
    field[: len(call)] = call.encode("ascii", "replace")
    if module:
        field[LONG_CALLSIGN_LEN - 1] = ord(module.upper()[:1])
        # Keep the callsign clear of the module slot.
        if len(call) >= LONG_CALLSIGN_LEN:
            field[: LONG_CALLSIGN_LEN - 1] = call[: LONG_CALLSIGN_LEN - 1].encode("ascii", "replace")
    return bytes(field)


def _pad(text: str, length: int) -> bytes:
    """Right-pad ``text`` with spaces (or truncate) to exactly ``length`` ASCII bytes."""
    return text.encode("ascii", "replace")[:length].ljust(length, b" ")


def format_urcall(ur: str) -> bytes:
    """Format an 8-byte URCALL field, honouring D-STAR's command-letter convention.

    A single-letter command (echo ``"E"``, info ``"I"``, unlink ``"U"``) goes in the **last** position
    — ``"       E"`` — while a routing target like ``"CQCQCQ"`` or a reflector link ``"REF001CL"`` is
    left-justified. The heuristic: a 1-char UR is right-justified, everything else left-justified.
    """
    text = ur.upper().encode("ascii", "replace")[:LONG_CALLSIGN_LEN]
    if len(text) == 1:
        return text.rjust(LONG_CALLSIGN_LEN, b" ")
    return text.ljust(LONG_CALLSIGN_LEN, b" ")


# --------------------------------------------------------------------------------------
# Build / parse
# --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class RadioHeader:
    """A decoded D-STAR radio header. Callsign fields are the raw 8/4-char strings (spaces kept)."""

    rpt1: str  # departure module (on-wire slot 1)
    rpt2: str  # gateway (on-wire slot 2)
    ur: str
    my1: str
    my2: str
    flags: tuple[int, int, int] = (0, 0, 0)
    #: True when the on-wire checksum verified (or was the ``FF FF`` bypass); False on a bad CRC.
    checksum_ok: bool = True


def build_header(
    *,
    rpt1: bytes,
    rpt2: bytes,
    ur: bytes,
    my1: bytes,
    my2: bytes = b"    ",
    flags: tuple[int, int, int] = (0, 0, 0),
) -> bytes:
    """Build the 41-byte radio header from pre-formatted callsign fields (see :func:`format_callsign`).

    ``rpt1`` is the departure module (on-wire slot 1), ``rpt2`` the gateway (slot 2). Each callsign is
    padded/truncated to its field width, and the trailing 2 bytes carry the CRC-16/X-25 of the first 39.
    """
    body = bytearray(RADIO_HEADER_LEN - 2)
    body[_OFF_FLAGS + 0] = flags[0] & 0xFF
    body[_OFF_FLAGS + 1] = flags[1] & 0xFF
    body[_OFF_FLAGS + 2] = flags[2] & 0xFF
    body[_OFF_RPT1 : _OFF_RPT1 + LONG_CALLSIGN_LEN] = rpt1[:LONG_CALLSIGN_LEN].ljust(LONG_CALLSIGN_LEN, b" ")
    body[_OFF_RPT2 : _OFF_RPT2 + LONG_CALLSIGN_LEN] = rpt2[:LONG_CALLSIGN_LEN].ljust(LONG_CALLSIGN_LEN, b" ")
    body[_OFF_UR : _OFF_UR + LONG_CALLSIGN_LEN] = ur[:LONG_CALLSIGN_LEN].ljust(LONG_CALLSIGN_LEN, b" ")
    body[_OFF_MY1 : _OFF_MY1 + LONG_CALLSIGN_LEN] = my1[:LONG_CALLSIGN_LEN].ljust(LONG_CALLSIGN_LEN, b" ")
    body[_OFF_MY2 : _OFF_MY2 + SHORT_CALLSIGN_LEN] = my2[:SHORT_CALLSIGN_LEN].ljust(SHORT_CALLSIGN_LEN, b" ")
    return bytes(body) + _checksum_bytes(bytes(body))


def build_voice_header(
    *,
    callsign: str,
    module: str,
    ur: str = "CQCQCQ",
    my2: str = "INFO",
    gateway_module: str = "G",
) -> bytes:
    """Convenience: build a voice header for ``callsign`` transmitting on ``module``.

    RPT1 = ``callsign`` + ``module`` (departure), RPT2 = ``callsign`` + ``gateway_module`` (the gateway),
    MY1 = ``callsign``. ``ur="E"`` addresses the gateway's echo test (padded to ``"       E"``).
    """
    return build_header(
        rpt1=format_callsign(callsign, module),
        rpt2=format_callsign(callsign, gateway_module),
        ur=format_urcall(ur),
        my1=format_callsign(callsign),
        my2=_pad(my2, SHORT_CALLSIGN_LEN),
    )


def parse_header(data: bytes) -> RadioHeader:
    """Parse a 41-byte (or 39-byte, checksum-less) radio header into a :class:`RadioHeader`.

    A trailing ``FF FF`` checksum is accepted as the gateway's skip sentinel; any other mismatch sets
    ``checksum_ok=False`` but still returns the parsed fields (the caller decides whether to trust it).
    """
    if len(data) < RADIO_HEADER_LEN - 2:
        raise ValueError(f"radio header is at least {RADIO_HEADER_LEN - 2} bytes, got {len(data)}")

    def field(off: int, length: int) -> str:
        return data[off : off + length].decode("ascii", "replace")

    checksum_ok = True
    if len(data) >= RADIO_HEADER_LEN:
        on_wire = data[_OFF_CRC : _OFF_CRC + 2]
        checksum_ok = on_wire == CHECKSUM_BYPASS or on_wire == _checksum_bytes(data[: RADIO_HEADER_LEN - 2])
    return RadioHeader(
        rpt1=field(_OFF_RPT1, LONG_CALLSIGN_LEN),
        rpt2=field(_OFF_RPT2, LONG_CALLSIGN_LEN),
        ur=field(_OFF_UR, LONG_CALLSIGN_LEN),
        my1=field(_OFF_MY1, LONG_CALLSIGN_LEN),
        my2=field(_OFF_MY2, SHORT_CALLSIGN_LEN),
        flags=(data[0], data[1], data[2]),
        checksum_ok=checksum_ok,
    )
