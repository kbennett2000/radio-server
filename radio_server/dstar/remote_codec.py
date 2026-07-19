"""Pure wire codec for the ircDDBGateway **remote-control** protocol (ADR 0095) — no I/O.

This is a *different* protocol from DSRP (:mod:`radio_server.dstar.dsrp`). DSRP is the repeater<->gateway
audio link radio-server speaks as module A; this is the gateway's small **control** channel — the one the
``ircddbgatewayconfig`` tool uses — for linking/unlinking *any* module to a reflector and reading each
module's **confirmed** link state. radio-server needs it to drive the DVAP modules (B = 441.600, C =
441.000), which are separate ``dstarrepeater`` endpoints it otherwise can't see or control (ADR 0089).

This module is the frame layer only — building the client->gateway packets and parsing the
gateway->client replies. The socket, auth round-trips and timeouts live in
:mod:`radio_server.dstar.remote_client`.

Source of truth: an independent implementation from the public g4klx ``ircDDBGateway/Common/
RemoteProtocolHandler.{h,cpp}`` and ``Defs.h`` read purely as a **protocol specification** — tags,
offsets and enum orders are interop facts, not ported GPL code (the ADR 0086 stance).

Wire shape — every packet opens with a **3-byte ASCII tag**; all multi-byte integers are
**little-endian** (the wx ``*_SWAP_ON_BE`` convention: the wire is little-endian, and the gateway box is
x86/LE). Callsign fields are the 8-char space-padded D-STAR field (``LONG_CALLSIGN_LENGTH``)::

    client -> gateway
      login      "LIN"                                                     (tag only)
      hash       "SHA" <32-byte SHA256(random_bytes || password)>
      link       "LNK" <call:8> <reconnect:int32 LE> <reflector:8>
      unlink     "UNL" <call:8> <protocol:int32 LE>  <reflector:8>
      get-calls  "GCS"                                                     (tag only)
      get-rptr   "GRP" <call:8>
      logout     "LOG"

    gateway -> client
      random     "RND" <random:uint32 LE>
      ack        "ACK"
      nak        "NAK" <text\\0>
      callsigns  "CAL" { ('R'|'S') <call:8> } *
      repeater   "RPT" <call:8> <reconnect:int32> <reflector:8>
                       { <reflector:8> <protocol:int32> <linked:int32> <direction:int32> <dongle:int32> } *

The SHA256 input is the **four random bytes exactly as they arrived on the wire** followed by the
password bytes (the gateway hashes its own native LE bytes of the random, which equal the wire bytes) —
a judge-on-the-chip fact to confirm against the live gateway (guardrail 1).
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from enum import Enum, IntEnum

from .header import LONG_CALLSIGN_LEN

# --- packet tags (3 ASCII bytes) --------------------------------------------------------------

# client -> gateway
TAG_LOGIN = b"LIN"
TAG_HASH = b"SHA"
TAG_GET_CALLSIGNS = b"GCS"
TAG_GET_REPEATER = b"GRP"
TAG_LINK = b"LNK"
TAG_UNLINK = b"UNL"
TAG_LOGOUT = b"LOG"

# gateway -> client
TAG_RANDOM = b"RND"
TAG_ACK = b"ACK"
TAG_NAK = b"NAK"
TAG_CALLSIGNS = b"CAL"
TAG_REPEATER = b"RPT"

#: SHA256 digest length carried in a ``SHA`` packet.
HASH_LEN = 32

#: The ircDDBGateway remote-control UDP port (g4klx default). Marked: verify against the live gateway.
DEFAULT_REMOTE_PORT = 10022

#: One link record in an ``RPT`` reply: reflector(8) + 4 int32s (protocol, linked, direction, dongle).
_LINK_RECORD_LEN = LONG_CALLSIGN_LEN + 4 * 4


class Reconnect(IntEnum):
    """g4klx ``RECONNECT`` enum (``Defs.h``), in declared order — the value carried by ``LNK``/``RPT``."""

    NEVER = 0
    FIXED = 1
    MINS_5 = 2
    MINS_10 = 3
    MINS_15 = 4
    MINS_20 = 5
    MINS_25 = 6
    MINS_30 = 7
    MINS_60 = 8
    MINS_90 = 9
    MINS_120 = 10
    MINS_180 = 11


class Protocol(IntEnum):
    """g4klx ``DSTAR_PROTOCOL`` enum (``DStarDefines.h``), in declared order — the value in ``UNL``/``RPT``."""

    UNKNOWN = 0
    LOOPBACK = 1
    DEXTRA = 2
    DPLUS = 3
    DCS = 4


class Direction(IntEnum):
    """g4klx ``DIRECTION`` enum — whether a link was made by us (outgoing) or to us (incoming)."""

    INCOMING = 0
    OUTGOING = 1


def _field(text: str) -> bytes:
    """The 8-byte space-padded D-STAR callsign/reflector field (left-justified, truncated to fit)."""
    return text.encode("ascii", "replace")[:LONG_CALLSIGN_LEN].ljust(LONG_CALLSIGN_LEN, b" ")


def _read_field(raw: bytes) -> str:
    """Decode an 8-byte callsign/reflector field, dropping trailing pad spaces (content is left-justified)."""
    return raw.decode("ascii", "replace").rstrip(" ")


# --------------------------------------------------------------------------------------
# Build (client -> gateway)
# --------------------------------------------------------------------------------------

def build_login() -> bytes:
    """Build the login request (``LIN``) that asks the gateway for a random challenge."""
    return TAG_LOGIN


def build_hash(password: str, random: int) -> bytes:
    """Build the auth reply (``SHA``): ``SHA256(random_bytes || password)`` over the 32-byte digest.

    ``random`` is the value from the gateway's ``RND`` packet; its four **little-endian** bytes (the same
    bytes seen on the wire) are hashed ahead of the password bytes, matching the gateway's own check.
    """
    random_bytes = struct.pack("<I", random & 0xFFFFFFFF)
    digest = hashlib.sha256(random_bytes + password.encode("ascii", "replace")).digest()
    return TAG_HASH + digest


def build_link(repeater: str, reflector: str, reconnect: Reconnect = Reconnect.FIXED) -> bytes:
    """Build a link command (``LNK``): point ``repeater`` (e.g. ``"AE9S   B"``) at ``reflector`` (``"REF001 C"``)."""
    return TAG_LINK + _field(repeater) + struct.pack("<i", int(reconnect)) + _field(reflector)


def build_unlink(
    repeater: str, reflector: str = "", protocol: Protocol = Protocol.UNKNOWN
) -> bytes:
    """Build an unlink command (``UNL``) dropping ``repeater``'s current link (reflector/protocol optional)."""
    return TAG_UNLINK + _field(repeater) + struct.pack("<i", int(protocol)) + _field(reflector)


def build_get_callsigns() -> bytes:
    """Build a get-callsigns request (``GCS``) asking for the gateway's repeater/StarNet list."""
    return TAG_GET_CALLSIGNS


def build_get_repeater(repeater: str) -> bytes:
    """Build a get-repeater request (``GRP``) asking for one module's confirmed link state."""
    return TAG_GET_REPEATER + _field(repeater)


def build_logout() -> bytes:
    """Build a logout (``LOG``) ending the remote-control session."""
    return TAG_LOGOUT


# --------------------------------------------------------------------------------------
# Parse (gateway -> client)
# --------------------------------------------------------------------------------------

class RemoteKind(Enum):
    """What a parsed remote-control reply is."""

    RANDOM = "random"
    ACK = "ack"
    NAK = "nak"
    CALLSIGNS = "callsigns"
    REPEATER = "repeater"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RepeaterLink:
    """One active link in an ``RPT`` reply — the confirmed reflector connection for a module."""

    reflector: str
    protocol: Protocol
    linked: bool
    direction: Direction
    dongle: bool


@dataclass(frozen=True)
class CallsignEntry:
    """One entry of a ``CAL`` reply: a repeater (``kind == 'R'``) or StarNet group (``'S'``)."""

    kind: str
    callsign: str


@dataclass(frozen=True)
class RemoteMessage:
    """A parsed remote-control reply. Only the fields relevant to :attr:`kind` are populated."""

    kind: RemoteKind
    #: RANDOM: the 32-bit challenge to hash with the password.
    random: int = 0
    #: NAK: the gateway's error text.
    text: str = ""
    #: CALLSIGNS: the repeater / StarNet entries.
    callsigns: tuple[CallsignEntry, ...] = ()
    #: REPEATER: the queried module and its confirmed link state.
    repeater: str = ""
    reconnect: Reconnect = Reconnect.NEVER
    reflector: str = ""
    links: tuple[RepeaterLink, ...] = field(default_factory=tuple)


def _reconnect(value: int) -> Reconnect:
    try:
        return Reconnect(value)
    except ValueError:
        return Reconnect.NEVER


def _protocol(value: int) -> Protocol:
    try:
        return Protocol(value)
    except ValueError:
        return Protocol.UNKNOWN


def _direction(value: int) -> Direction:
    try:
        return Direction(value)
    except ValueError:
        return Direction.INCOMING


def _parse_repeater(packet: bytes) -> RemoteMessage:
    base = 3 + LONG_CALLSIGN_LEN + 4 + LONG_CALLSIGN_LEN
    if len(packet) < base:
        return RemoteMessage(RemoteKind.UNKNOWN)
    repeater = _read_field(packet[3 : 3 + LONG_CALLSIGN_LEN])
    (reconnect,) = struct.unpack_from("<i", packet, 3 + LONG_CALLSIGN_LEN)
    reflector = _read_field(packet[3 + LONG_CALLSIGN_LEN + 4 : base])

    links: list[RepeaterLink] = []
    off = base
    while off + _LINK_RECORD_LEN <= len(packet):
        link_reflector = _read_field(packet[off : off + LONG_CALLSIGN_LEN])
        proto, linked, direction, dongle = struct.unpack_from("<iiii", packet, off + LONG_CALLSIGN_LEN)
        links.append(
            RepeaterLink(
                reflector=link_reflector,
                protocol=_protocol(proto),
                linked=linked != 0,
                direction=_direction(direction),
                dongle=dongle != 0,
            )
        )
        off += _LINK_RECORD_LEN

    return RemoteMessage(
        RemoteKind.REPEATER,
        repeater=repeater,
        reconnect=_reconnect(reconnect),
        reflector=reflector,
        links=tuple(links),
    )


def _parse_callsigns(packet: bytes) -> RemoteMessage:
    entries: list[CallsignEntry] = []
    off = 3
    while off + 1 + LONG_CALLSIGN_LEN <= len(packet):
        kind = chr(packet[off])
        call = _read_field(packet[off + 1 : off + 1 + LONG_CALLSIGN_LEN])
        entries.append(CallsignEntry(kind=kind, callsign=call))
        off += 1 + LONG_CALLSIGN_LEN
    return RemoteMessage(RemoteKind.CALLSIGNS, callsigns=tuple(entries))


def parse(packet: bytes) -> RemoteMessage:
    """Parse one remote-control reply (never raises; a malformed packet is :attr:`RemoteKind.UNKNOWN`)."""
    if len(packet) < 3:
        return RemoteMessage(RemoteKind.UNKNOWN)
    tag = packet[:3]

    if tag == TAG_RANDOM and len(packet) >= 3 + 4:
        (random,) = struct.unpack_from("<I", packet, 3)
        return RemoteMessage(RemoteKind.RANDOM, random=random)
    if tag == TAG_ACK:
        return RemoteMessage(RemoteKind.ACK)
    if tag == TAG_NAK:
        return RemoteMessage(RemoteKind.NAK, text=packet[3:].split(b"\x00", 1)[0].decode("ascii", "replace"))
    if tag == TAG_REPEATER:
        return _parse_repeater(packet)
    if tag == TAG_CALLSIGNS:
        return _parse_callsigns(packet)
    return RemoteMessage(RemoteKind.UNKNOWN)
