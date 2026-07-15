"""M17 reflector control packets and the stream frame (ADR 0050).

Two packet families cross the wire between a client and an mrefd reflector:

- **Control packets** — a four-byte ASCII magic optionally followed by the sender's callsign and,
  for a link request, a module letter: ``CONN`` / ``ACKN`` / ``NACK`` / ``DISC`` / ``PING`` /
  ``PONG`` / ``LSTN``. ``LSTN`` is the listen-only request — the same shape as ``CONN`` — that
  enables a zero-credential listening tier (the ``LISTEN_ONLY`` capability of ADR 0041).
- **The stream frame** — the 54-byte ``M17 `` packet carrying one 40 ms slice of a transmission:
  StreamID, the LSF (DST, SRC, TYPE, META), a frame number, a 16-byte Codec2 payload, and a CRC.
  The layout and offsets were read from mrefd's ``packet.cpp`` (guardrail 1).

**Malformed-input rule (ADR 0050).** A reflector is an untrusted peer, so the ``parse_*``
functions return ``None`` on *any* malformation — wrong length, unknown magic, or a failed CRC —
never a half-parsed object and never an exception on the keying path. The ``build_*`` functions
operate on local input, where bad data is a programming error, and so raise by name
(:class:`~radio_server.link.m17.callsign.CallsignError` or :class:`ValueError` naming the field).

The 16-byte stream payload is treated as **opaque bytes** here; decoding it to audio via the
ADR-0049 Codec2 seam is the ``M17Link`` cycle's job.
"""

from __future__ import annotations

from dataclasses import dataclass

from .callsign import ADDRESS_BYTES, decode_callsign, encode_callsign
from .crc import crc16

# --- magics ------------------------------------------------------------------

CONN = b"CONN"
ACKN = b"ACKN"
NACK = b"NACK"
DISC = b"DISC"
PING = b"PING"
PONG = b"PONG"
LSTN = b"LSTN"
STREAM_MAGIC = b"M17 "  # note the trailing space

#: Every control magic this module builds/parses (the stream magic is separate).
CONTROL_MAGICS: frozenset[bytes] = frozenset({CONN, ACKN, NACK, DISC, PING, PONG, LSTN})

_MAGIC_LEN = 4

# --- stream-frame geometry (mrefd packet.cpp, isstream branch) ----------------

STREAM_FRAME_SIZE = 54
META_BYTES = 14
PAYLOAD_BYTES = 16

_OFF_STREAM_ID = 4
_OFF_DST = 6
_OFF_SRC = 12
_OFF_TYPE = 18
_OFF_META = 20
_OFF_FN = 34
_OFF_PAYLOAD = 36
_OFF_CRC = 52  # CRC covers bytes [0:52]

#: The frame number's top bit signals the last frame of a transmission; the low 15 bits index it.
LAST_FRAME_BIT = 0x8000
FRAME_INDEX_MASK = 0x7FFF


# --- parsed forms -------------------------------------------------------------


@dataclass(frozen=True)
class ControlPacket:
    """A parsed reflector control packet.

    ``kind`` is the ASCII magic (e.g. ``"CONN"``). ``callsign`` is the sender's decoded callsign
    when the packet carries one (``None`` for the bare ``ACKN`` / ``NACK`` / ``DISC``-ack forms).
    ``module`` is the requested module letter for ``CONN`` / ``LSTN`` (``None`` otherwise).
    """

    kind: str
    callsign: str | None = None
    module: str | None = None


@dataclass(frozen=True)
class StreamFrame:
    """A parsed 54-byte ``M17 `` stream frame.

    ``src`` is the LSF source callsign — *the talker*: M17 has no directory, so "who is
    transmitting right now" is carried in every frame, and this is where it is read out (ADR 0050).
    ``dst`` and ``src`` are decoded callsigns (``None`` for a non-base-40 address such as
    BROADCAST). ``frame_number`` is the 15-bit index and ``last`` its end-of-stream flag.
    ``payload`` is the opaque 16-byte Codec2 slice.
    """

    stream_id: int
    dst: str | None
    src: str | None
    frame_type: int
    meta: bytes
    frame_number: int
    payload: bytes
    last: bool


# --- helpers ------------------------------------------------------------------


def _module_byte(module: str) -> bytes:
    if len(module) != 1 or not ("A" <= module <= "Z"):
        raise ValueError(f"module must be a single letter 'A'-'Z'; got {module!r}")
    return module.encode("ascii")


def _address(value: str | bytes) -> bytes:
    """Encode a callsign string, or accept an already-6-byte address (e.g. BROADCAST)."""
    if isinstance(value, (bytes, bytearray)):
        if len(value) != ADDRESS_BYTES:
            raise ValueError(
                f"an address given as bytes must be {ADDRESS_BYTES} bytes; got {len(value)}"
            )
        return bytes(value)
    return encode_callsign(value)


def _u16(name: str, value: int, maximum: int = 0xFFFF) -> int:
    if not (0 <= value <= maximum):
        raise ValueError(f"{name} must be in 0..{maximum}; got {value}")
    return value


# --- control builders ---------------------------------------------------------


def build_conn(callsign: str, module: str) -> bytes:
    """Build an 11-byte ``CONN`` link request: magic + sender callsign + module letter."""
    return CONN + encode_callsign(callsign) + _module_byte(module)


def build_lstn(callsign: str, module: str) -> bytes:
    """Build an 11-byte ``LSTN`` listen-only request — the zero-credential listening tier."""
    return LSTN + encode_callsign(callsign) + _module_byte(module)


def build_disc(callsign: str) -> bytes:
    """Build a 10-byte client-initiated ``DISC`` (disconnect) request."""
    return DISC + encode_callsign(callsign)


def build_ping(callsign: str) -> bytes:
    """Build a 10-byte ``PING`` keep-alive carrying the sender's callsign."""
    return PING + encode_callsign(callsign)


def build_pong(callsign: str) -> bytes:
    """Build a 10-byte ``PONG`` keep-alive (identical purpose to ``PING``)."""
    return PONG + encode_callsign(callsign)


def build_ackn() -> bytes:
    """Build the bare 4-byte ``ACKN`` acknowledgement."""
    return bytes(ACKN)


def build_nack() -> bytes:
    """Build the bare 4-byte ``NACK`` refusal."""
    return bytes(NACK)


# --- control parser -----------------------------------------------------------


def parse_control(data: bytes) -> ControlPacket | None:
    """Parse a reflector control packet, or ``None`` if malformed (untrusted-peer rule).

    Returns ``None`` for an unknown magic or any length that does not match the magic. ``CONN`` /
    ``LSTN`` are 11 bytes (callsign + module); ``PING`` / ``PONG`` are 10 (callsign); ``DISC`` is
    10 with a callsign or the bare 4-byte reflector ack; ``ACKN`` / ``NACK`` are the bare 4 bytes.
    """
    if len(data) < _MAGIC_LEN:
        return None
    magic = bytes(data[:_MAGIC_LEN])
    if magic not in CONTROL_MAGICS:
        return None
    kind = magic.decode("ascii")

    if magic in (CONN, LSTN):
        if len(data) != 11:
            return None
        return ControlPacket(
            kind,
            callsign=decode_callsign(data[_MAGIC_LEN : _MAGIC_LEN + ADDRESS_BYTES]),
            module=chr(data[10]),
        )

    if magic in (PING, PONG):
        if len(data) != 10:
            return None
        return ControlPacket(kind, callsign=decode_callsign(data[_MAGIC_LEN:10]))

    if magic == DISC:
        if len(data) == 10:
            return ControlPacket(kind, callsign=decode_callsign(data[_MAGIC_LEN:10]))
        if len(data) == _MAGIC_LEN:  # bare reflector-side disconnect ack
            return ControlPacket(kind)
        return None

    # ACKN / NACK: bare magic only.
    if len(data) != _MAGIC_LEN:
        return None
    return ControlPacket(kind)


# --- stream frame -------------------------------------------------------------


def build_stream(
    stream_id: int,
    dst: str | bytes,
    src: str | bytes,
    frame_type: int,
    meta: bytes,
    frame_number: int,
    payload: bytes,
    last: bool = False,
) -> bytes:
    """Build a 54-byte ``M17 `` stream frame with a valid trailing CRC.

    ``dst`` / ``src`` are callsign strings (or an already-6-byte address such as ``BROADCAST``).
    ``meta`` must be 14 bytes and ``payload`` 16; ``frame_number`` is the 15-bit index and ``last``
    sets the end-of-stream bit. Raises :class:`ValueError` (or :class:`CallsignError`) naming the
    offending field on any invalid local input.
    """
    if len(meta) != META_BYTES:
        raise ValueError(f"meta must be {META_BYTES} bytes; got {len(meta)}")
    if len(payload) != PAYLOAD_BYTES:
        raise ValueError(f"payload must be {PAYLOAD_BYTES} bytes; got {len(payload)}")
    _u16("stream_id", stream_id)
    _u16("frame_type", frame_type)
    _u16("frame_number", frame_number, FRAME_INDEX_MASK)

    fn_field = frame_number | (LAST_FRAME_BIT if last else 0)
    body = (
        STREAM_MAGIC
        + stream_id.to_bytes(2, "big")
        + _address(dst)
        + _address(src)
        + frame_type.to_bytes(2, "big")
        + bytes(meta)
        + fn_field.to_bytes(2, "big")
        + bytes(payload)
    )
    assert len(body) == _OFF_CRC  # layout invariant: everything before the CRC is 52 bytes
    return body + crc16(body).to_bytes(2, "big")


def parse_stream(data: bytes) -> StreamFrame | None:
    """Parse a 54-byte ``M17 `` stream frame, or ``None`` if malformed (untrusted-peer rule).

    Returns ``None`` unless the length is exactly 54, the magic is ``M17 ``, and the trailing CRC
    checks out. The returned :class:`StreamFrame` surfaces the LSF source callsign as ``src`` (the
    talker) and exposes the 16-byte ``payload`` opaquely.
    """
    if len(data) != STREAM_FRAME_SIZE or bytes(data[:_MAGIC_LEN]) != STREAM_MAGIC:
        return None
    if crc16(data[:_OFF_CRC]) != int.from_bytes(data[_OFF_CRC:], "big"):
        return None

    fn_field = int.from_bytes(data[_OFF_FN : _OFF_FN + 2], "big")
    return StreamFrame(
        stream_id=int.from_bytes(data[_OFF_STREAM_ID : _OFF_STREAM_ID + 2], "big"),
        dst=decode_callsign(data[_OFF_DST : _OFF_DST + ADDRESS_BYTES]),
        src=decode_callsign(data[_OFF_SRC : _OFF_SRC + ADDRESS_BYTES]),
        frame_type=int.from_bytes(data[_OFF_TYPE : _OFF_TYPE + 2], "big"),
        meta=bytes(data[_OFF_META : _OFF_META + META_BYTES]),
        frame_number=fn_field & FRAME_INDEX_MASK,
        payload=bytes(data[_OFF_PAYLOAD : _OFF_PAYLOAD + PAYLOAD_BYTES]),
        last=bool(fn_field & LAST_FRAME_BIT),
    )
