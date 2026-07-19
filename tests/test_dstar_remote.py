"""The gateway remote-control seam (ADR 0095): codec byte-layouts + Mock/Udp clients over a fake socket.

No real network and no gateway: the UDP client is driven with an injected ``_socket_factory`` so the
login handshake and request/reply round-trips are exactly testable (the mock-first discipline; the
`test_dstar_client.py` fake-transport pattern). Wire constants are asserted here for regression; their
agreement with a *real* gateway is the hardware-phase bench check (ADR 0095, guardrail 1).
"""

from __future__ import annotations

import hashlib
import struct

import pytest

from radio_server.dstar import remote_codec as rc
from radio_server.dstar.remote_client import (
    MockRemoteControlClient,
    RemoteAuthError,
    RemoteTimeout,
    UdpRemoteControlClient,
)
from radio_server.dstar.remote_codec import Direction, Protocol, Reconnect, RemoteKind

MODULE_B = "AE9S   B"
REF = "REF001 C"


def _rpt(repeater: str, reflector: str, *, linked: bool = True) -> bytes:
    """Hand-assemble an ``RPT`` reply with a single link record, for parse tests."""
    pkt = rc.TAG_REPEATER + rc._field(repeater) + struct.pack("<i", Reconnect.FIXED) + rc._field(reflector)
    if reflector:
        pkt += rc._field(reflector) + struct.pack(
            "<iiii", Protocol.DPLUS, 1 if linked else 0, Direction.OUTGOING, 1
        )
    return pkt


# --------------------------------------------------------------------------------------
# Codec — build
# --------------------------------------------------------------------------------------


def test_link_command_byte_layout():
    pkt = rc.build_link(MODULE_B, REF, Reconnect.FIXED)
    assert pkt[:3] == rc.TAG_LINK
    assert pkt[3:11] == b"AE9S   B"
    assert struct.unpack_from("<i", pkt, 11)[0] == int(Reconnect.FIXED)  # reconnect is little-endian
    assert pkt[15:23] == b"REF001 C"
    assert len(pkt) == 3 + 8 + 4 + 8


def test_unlink_command_byte_layout():
    pkt = rc.build_unlink(MODULE_B)
    assert pkt[:3] == rc.TAG_UNLINK
    assert pkt[3:11] == b"AE9S   B"
    assert struct.unpack_from("<i", pkt, 11)[0] == int(Protocol.UNKNOWN)
    assert pkt[15:23] == b"        "  # blank reflector => 8 spaces


def test_hash_is_sha256_of_random_bytes_then_password():
    expected = rc.TAG_HASH + hashlib.sha256(struct.pack("<I", 0xAABBCCDD) + b"secret").digest()
    assert rc.build_hash("secret", 0xAABBCCDD) == expected
    assert len(rc.build_hash("secret", 1)) == 3 + rc.HASH_LEN


def test_login_and_getters_are_tag_only():
    assert rc.build_login() == rc.TAG_LOGIN
    assert rc.build_get_callsigns() == rc.TAG_GET_CALLSIGNS
    assert rc.build_get_repeater(MODULE_B) == rc.TAG_GET_REPEATER + b"AE9S   B"


def test_enum_values_match_g4klx_declared_order():
    assert (Reconnect.NEVER, Reconnect.FIXED, Reconnect.MINS_180) == (0, 1, 11)
    assert (Protocol.UNKNOWN, Protocol.DEXTRA, Protocol.DPLUS, Protocol.DCS) == (0, 2, 3, 4)


# --------------------------------------------------------------------------------------
# Codec — parse
# --------------------------------------------------------------------------------------


def test_parse_random():
    msg = rc.parse(rc.TAG_RANDOM + struct.pack("<I", 0x11223344))
    assert msg.kind is RemoteKind.RANDOM and msg.random == 0x11223344


def test_parse_ack_and_nak():
    assert rc.parse(rc.TAG_ACK).kind is RemoteKind.ACK
    nak = rc.parse(rc.TAG_NAK + b"bad password\x00")
    assert nak.kind is RemoteKind.NAK and nak.text == "bad password"


def test_parse_repeater_with_a_link():
    msg = rc.parse(_rpt(MODULE_B, REF))
    assert msg.kind is RemoteKind.REPEATER
    assert msg.repeater == "AE9S   B" and msg.reflector == "REF001 C"
    assert msg.reconnect is Reconnect.FIXED
    assert len(msg.links) == 1
    link = msg.links[0]
    assert link.reflector == "REF001 C"
    assert link.protocol is Protocol.DPLUS and link.linked is True
    assert link.direction is Direction.OUTGOING and link.dongle is True


def test_parse_repeater_unlinked_has_no_link_records():
    msg = rc.parse(_rpt(MODULE_B, "", linked=False))
    assert msg.kind is RemoteKind.REPEATER and msg.reflector == "" and msg.links == ()


def test_parse_callsigns_lists_repeaters_and_starnets():
    pkt = rc.TAG_CALLSIGNS + b"R" + rc._field("AE9S   A") + b"R" + rc._field("AE9S   B") + b"S" + rc._field("AE9S   G")
    msg = rc.parse(pkt)
    assert msg.kind is RemoteKind.CALLSIGNS
    assert [(e.kind, e.callsign) for e in msg.callsigns] == [
        ("R", "AE9S   A"),
        ("R", "AE9S   B"),
        ("S", "AE9S   G"),
    ]


def test_parse_malformed_is_unknown():
    assert rc.parse(b"").kind is RemoteKind.UNKNOWN
    assert rc.parse(b"XY").kind is RemoteKind.UNKNOWN
    assert rc.parse(b"ZZZ garbage").kind is RemoteKind.UNKNOWN
    assert rc.parse(rc.TAG_RANDOM + b"\x01").kind is RemoteKind.UNKNOWN  # random truncated


def test_field_pads_and_truncates_and_read_strips():
    assert rc._field("AE9S") == b"AE9S    "
    assert rc._field("TOOLONGCALL") == b"TOOLONGC"  # truncated to 8
    assert rc._read_field(b"REF001 C") == "REF001 C"
    assert rc._read_field(b"AE9S    ") == "AE9S"


# --------------------------------------------------------------------------------------
# MockRemoteControlClient — models a tiny gateway
# --------------------------------------------------------------------------------------


def test_mock_link_status_unlink_round_trip():
    client = MockRemoteControlClient()
    assert client.status(MODULE_B).reflector == ""  # not linked yet
    client.link(MODULE_B, REF)
    linked = client.status(MODULE_B)
    assert linked.reflector == "REF001 C" and linked.links[0].linked is True
    client.unlink(MODULE_B)
    assert client.status(MODULE_B).reflector == ""
    assert [c[0] for c in client.sent] == ["status", "link", "status", "unlink", "status"]
    assert client.login_count == 1  # auth cached across commands


def test_mock_fail_auth_raises_and_does_not_link():
    client = MockRemoteControlClient(fail_auth=True)
    with pytest.raises(RemoteAuthError):
        client.link(MODULE_B, REF)
    assert client.linked == {} and client.authed is False


# --------------------------------------------------------------------------------------
# UdpRemoteControlClient — fake connected socket
# --------------------------------------------------------------------------------------


class FakeConnSocket:
    """A connected datagram socket: records ``send``s, serves preloaded inbound, else times out."""

    def __init__(self, inbound: list[bytes] | None = None) -> None:
        self.sent: list[bytes] = []
        self._inbound = list(inbound or [])
        self.closed = False

    def settimeout(self, _t: float) -> None:
        pass

    def send(self, data: bytes) -> int:
        self.sent.append(bytes(data))
        return len(data)

    def recv(self, _size: int) -> bytes:
        if self._inbound:
            return self._inbound.pop(0)
        raise TimeoutError

    def close(self) -> None:
        self.closed = True


def _udp(fake: FakeConnSocket, **kw) -> UdpRemoteControlClient:
    return UdpRemoteControlClient(password="password", _socket_factory=lambda h, p: fake, **kw)


def test_login_handshake_then_status_readback():
    fake = FakeConnSocket([rc.TAG_RANDOM + struct.pack("<I", 0x01020304), rc.TAG_ACK, _rpt(MODULE_B, REF)])
    client = _udp(fake)
    msg = client.status(MODULE_B)
    assert msg.kind is RemoteKind.REPEATER and msg.reflector == "REF001 C"
    # LIN, then the SHA over the exact random, then the GRP query — in order.
    assert fake.sent == [rc.build_login(), rc.build_hash("password", 0x01020304), rc.build_get_repeater(MODULE_B)]


def test_auth_is_cached_across_commands():
    fake = FakeConnSocket([rc.TAG_RANDOM + struct.pack("<I", 7), rc.TAG_ACK])
    client = _udp(fake)
    client.link(MODULE_B, REF)  # triggers login, then sends LNK
    client.unlink(MODULE_B)  # no second login
    tags = [p[:3] for p in fake.sent]
    assert tags == [rc.TAG_LOGIN, rc.TAG_HASH, rc.TAG_LINK, rc.TAG_UNLINK]


def test_login_nak_raises_auth_error():
    fake = FakeConnSocket([rc.TAG_RANDOM + struct.pack("<I", 7), rc.TAG_NAK + b"denied\x00"])
    client = _udp(fake)
    with pytest.raises(RemoteAuthError, match="denied"):
        client.status(MODULE_B)


def test_status_times_out_when_no_reply():
    fake = FakeConnSocket([rc.TAG_RANDOM + struct.pack("<I", 7), rc.TAG_ACK])  # auth ok, then silence
    client = _udp(fake, retries=1)
    with pytest.raises(RemoteTimeout):
        client.status(MODULE_B)
    # the GRP was resent (retries+1 = 2 attempts) after auth's LIN + SHA.
    assert [p[:3] for p in fake.sent] == [rc.TAG_LOGIN, rc.TAG_HASH, rc.TAG_GET_REPEATER, rc.TAG_GET_REPEATER]


def test_close_sends_logout_and_closes_socket():
    fake = FakeConnSocket([rc.TAG_RANDOM + struct.pack("<I", 7), rc.TAG_ACK])
    client = _udp(fake)
    client.link(MODULE_B, REF)
    client.close()
    assert fake.sent[-1] == rc.build_logout() and fake.closed is True
