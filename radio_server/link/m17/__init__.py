"""M17 wire format — base-40 callsigns, reflector control packets, and the stream frame (ADR 0050).

A pure, stdlib-only codec of M17 bytes: no sockets, no ``Link`` backend, no ``radio_server``
imports. The UDP client and the ``M17Link`` that consume this are later cycles. See ADR 0050 for
the sources (the M17 spec and mrefd) and the malformed-input rule (parse → ``None``, build → raise).
"""

from __future__ import annotations

from .callsign import (
    ADDRESS_BYTES,
    ALPHABET,
    BROADCAST,
    EMPTY,
    MAX_CALLSIGN_LEN,
    STANDARD_MAX,
    CallsignError,
    decode_callsign,
    encode_callsign,
)
from .crc import crc16
from .packet import (
    ACKN,
    CONN,
    CONTROL_MAGICS,
    DISC,
    LSTN,
    NACK,
    PAYLOAD_BYTES,
    PING,
    PONG,
    STREAM_FRAME_SIZE,
    STREAM_MAGIC,
    META_BYTES,
    ControlPacket,
    StreamFrame,
    build_ackn,
    build_conn,
    build_disc,
    build_lstn,
    build_nack,
    build_ping,
    build_pong,
    build_stream,
    parse_control,
    parse_stream,
)

__all__ = [
    # callsign
    "ALPHABET",
    "ADDRESS_BYTES",
    "MAX_CALLSIGN_LEN",
    "STANDARD_MAX",
    "EMPTY",
    "BROADCAST",
    "CallsignError",
    "encode_callsign",
    "decode_callsign",
    # crc
    "crc16",
    # control packets
    "CONN",
    "ACKN",
    "NACK",
    "DISC",
    "PING",
    "PONG",
    "LSTN",
    "CONTROL_MAGICS",
    "ControlPacket",
    "build_conn",
    "build_lstn",
    "build_disc",
    "build_ping",
    "build_pong",
    "build_ackn",
    "build_nack",
    "parse_control",
    # stream frame
    "STREAM_MAGIC",
    "STREAM_FRAME_SIZE",
    "META_BYTES",
    "PAYLOAD_BYTES",
    "StreamFrame",
    "build_stream",
    "parse_stream",
]
