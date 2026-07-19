"""Pure wire codec for the DSRP repeater<->gateway protocol (ADR 0087) — no I/O.

DSRP ("D-Star Repeater Protocol") is the UDP link a G4KLX DStarRepeater speaks to an ircDDBGateway.
radio-server plays the **repeater** side: it *registers* an endpoint, *polls* to stay alive, and sends
a stream as one *header* packet followed by *data* packets carrying AMBE voice + slow-data, closing
with an end-marked frame. The gateway replies in kind (header + data) plus text/status packets. This
module is the frame layer only — building and parsing the ``"DSRP"``-tagged packets; the socket,
reader thread, and timers live in :mod:`radio_server.dstar.client`.

Source of truth: an independent implementation from g4klx ``DStarRepeater/Common/
RepeaterProtocolHandler.cpp`` (GPL-2) read purely as a **protocol specification** — packet tags and
offsets are interop facts, not ported code (the ADR 0086 / kv4p stance). The radio header inside a
header packet is built/parsed by :mod:`radio_server.dstar.header`.

Packet shapes (all begin with the 4 ASCII bytes ``"DSRP"`` then a 1-byte type)::

    register  0x0B : "DSRP" 0B  <name...> 00
    poll      0x0A : "DSRP" 0A  <text...> 00
    header    0x20 : "DSRP" 20  id_hi id_lo 00      <41-byte radio header>          (49 bytes)
    data      0x21 : "DSRP" 21  id_hi id_lo seq err <12-byte DV frame>              (21 bytes)

``seq`` runs 0..0x14 (a 21-frame superframe) then wraps; its ``0x40`` bit marks the final frame.
Gateway->repeater also uses ``0x00``/``0x01`` (slow text) and ``0x04 xx`` (status) packets.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .header import RADIO_HEADER_LEN

#: Every DSRP packet opens with these 4 bytes.
MAGIC = b"DSRP"

# Packet types (repeater -> gateway).
TYPE_POLL = 0x0A
TYPE_REGISTER = 0x0B
TYPE_HEADER = 0x20
TYPE_DATA = 0x21

# Packet types (gateway -> repeater), beyond the shared header/data.
TYPE_TEXT = 0x00
TYPE_TEMPTEXT = 0x01
TYPE_STATUS = 0x04

#: The ``0x40`` bit in a data packet's sequence byte marks the last frame of a stream.
END_BIT = 0x40

#: The sequence number resets to 0 each superframe; g4klx wraps after 0x14 (21 frames).
SEQ_MAX = 0x14

# --------------------------------------------------------------------------------------
# DV frame geometry: 9-byte AMBE voice + 3-byte slow-data
# --------------------------------------------------------------------------------------

#: Bytes of a packed AMBE voice frame (kept in sync with ``vocoder.base.AMBE_BYTES_PER_FRAME``).
VOICE_FRAME_LEN = 9
#: Bytes of slow-data per frame (header/GPS/text, or the sync pattern at superframe start).
SLOW_DATA_LEN = 3
#: A DV frame is the voice frame followed by the slow-data (g4klx ``DV_FRAME_LENGTH_BYTES`` = 12).
DV_FRAME_LEN = VOICE_FRAME_LEN + SLOW_DATA_LEN

#: The slow-data sync pattern that opens every superframe (g4klx ``DATA_SYNC_BYTES``). Its presence
#: resets the sequence to 0; the gateway regenerates it at ``seq == 0`` and rejects it elsewhere.
DATA_SYNC = bytes([0x55, 0x2D, 0x16])

#: Benign slow-data filler for non-sync frames (no message). The gateway relays it untouched; audio
#: is unaffected. Real radios send scrambled header/text here — out of scope for a voice bridge.
SLOW_DATA_FILLER = bytes(SLOW_DATA_LEN)

#: The "null" AMBE voice frame that fills the terminating frame (g4klx ``NULL_AMBE_DATA_BYTES``).
NULL_AMBE = bytes([0x9E, 0x8D, 0x32, 0x88, 0x26, 0x1A, 0x3F, 0x61, 0xE8])


# --------------------------------------------------------------------------------------
# Build (repeater -> gateway)
# --------------------------------------------------------------------------------------

def build_register(name: str) -> bytes:
    """Build a register packet (``0x0B``) naming this endpoint; NUL-terminated, like g4klx."""
    return MAGIC + bytes([TYPE_REGISTER]) + name.encode("ascii", "replace") + b"\x00"


def build_poll(text: str = "") -> bytes:
    """Build a poll / keep-alive packet (``0x0A``); NUL-terminated text (may be empty)."""
    return MAGIC + bytes([TYPE_POLL]) + text.encode("ascii", "replace") + b"\x00"


def build_header_packet(radio_header: bytes, session_id: int) -> bytes:
    """Build a header packet (``0x20``) wrapping a 41-byte radio header under a 16-bit session id."""
    if len(radio_header) != RADIO_HEADER_LEN:
        raise ValueError(f"radio header is {RADIO_HEADER_LEN} bytes, got {len(radio_header)}")
    sid = session_id & 0xFFFF
    return MAGIC + bytes([TYPE_HEADER, sid >> 8, sid & 0xFF, 0x00]) + radio_header


def build_dv_frame(ambe: bytes, slow_data: bytes = SLOW_DATA_FILLER) -> bytes:
    """Assemble a 12-byte DV frame from a 9-byte AMBE voice frame and 3 bytes of slow-data."""
    if len(ambe) != VOICE_FRAME_LEN:
        raise ValueError(f"AMBE voice frame is {VOICE_FRAME_LEN} bytes, got {len(ambe)}")
    if len(slow_data) != SLOW_DATA_LEN:
        raise ValueError(f"slow-data is {SLOW_DATA_LEN} bytes, got {len(slow_data)}")
    return ambe + slow_data


def build_data_packet(
    dv_frame: bytes, session_id: int, seq_no: int, *, end: bool = False, errors: int = 0
) -> bytes:
    """Build a data packet (``0x21``) carrying one DV frame; ``end`` sets the terminator bit."""
    if len(dv_frame) != DV_FRAME_LEN:
        raise ValueError(f"DV frame is {DV_FRAME_LEN} bytes, got {len(dv_frame)}")
    sid = session_id & 0xFFFF
    seq = (seq_no & 0x3F) | (END_BIT if end else 0)
    return MAGIC + bytes([TYPE_DATA, sid >> 8, sid & 0xFF, seq, errors & 0xFF]) + dv_frame


def slow_data_for_seq(seq_no: int) -> bytes:
    """The slow-data bytes for a given sequence: the sync pattern at ``seq == 0``, else filler."""
    return DATA_SYNC if seq_no == 0 else SLOW_DATA_FILLER


def next_seq(seq_no: int) -> int:
    """Advance a sequence number, wrapping 0x14 -> 0 (the 21-frame superframe)."""
    return 0 if seq_no >= SEQ_MAX else seq_no + 1


# --------------------------------------------------------------------------------------
# Parse (gateway -> repeater)
# --------------------------------------------------------------------------------------

class MessageKind(Enum):
    """What a parsed DSRP packet is."""

    HEADER = "header"
    DATA = "data"
    TEXT = "text"
    STATUS = "status"
    REGISTER = "register"
    POLL = "poll"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DsrpMessage:
    """A parsed DSRP packet. Only the fields relevant to :attr:`kind` are populated."""

    kind: MessageKind
    session_id: int = 0
    #: HEADER: the raw 41-byte radio header (parse with :func:`radio_server.dstar.header.parse_header`).
    radio_header: bytes = b""
    #: DATA: sequence number (end bit stripped), the end flag, and the 12-byte DV frame.
    seq_no: int = 0
    end: bool = False
    dv_frame: bytes = b""
    errors: int = 0
    #: TEXT/STATUS: the decoded string payload.
    text: str = ""


def voice_frame(dv_frame: bytes) -> bytes:
    """The 9-byte AMBE voice frame at the front of a 12-byte DV frame."""
    return dv_frame[:VOICE_FRAME_LEN]


def parse(packet: bytes) -> DsrpMessage:
    """Parse one DSRP packet (never raises; a malformed packet is :attr:`MessageKind.UNKNOWN`)."""
    if len(packet) < 5 or packet[:4] != MAGIC:
        return DsrpMessage(MessageKind.UNKNOWN)
    ptype = packet[4]

    if ptype == TYPE_HEADER and len(packet) >= 8 + RADIO_HEADER_LEN:
        return DsrpMessage(
            MessageKind.HEADER,
            session_id=(packet[5] << 8) | packet[6],
            radio_header=packet[8 : 8 + RADIO_HEADER_LEN],
        )
    if ptype == TYPE_DATA and len(packet) >= 9 + DV_FRAME_LEN:
        seq = packet[7]
        return DsrpMessage(
            MessageKind.DATA,
            session_id=(packet[5] << 8) | packet[6],
            seq_no=seq & 0x3F,
            end=bool(seq & END_BIT),
            errors=packet[8],
            dv_frame=packet[9 : 9 + DV_FRAME_LEN],
        )
    if ptype == TYPE_TEXT or ptype == TYPE_TEMPTEXT:
        return DsrpMessage(MessageKind.TEXT, text=packet[5:].split(b"\x00", 1)[0].decode("ascii", "replace"))
    if ptype == TYPE_STATUS:
        return DsrpMessage(MessageKind.STATUS, text=packet[5:].split(b"\x00", 1)[0].decode("ascii", "replace"))
    if ptype == TYPE_REGISTER:
        return DsrpMessage(MessageKind.REGISTER, text=packet[5:].split(b"\x00", 1)[0].decode("ascii", "replace"))
    if ptype == TYPE_POLL:
        return DsrpMessage(MessageKind.POLL, text=packet[5:].split(b"\x00", 1)[0].decode("ascii", "replace"))
    return DsrpMessage(MessageKind.UNKNOWN)
