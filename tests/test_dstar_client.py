"""The gateway client seam (ADR 0087): MockGatewayClient + UdpGatewayClient over a fake socket.

No real network: the UDP client is driven with an injected ``_socket_factory`` and ``_clock`` so the
send path, register/poll cadence, and inbound dispatch are exactly testable (the mock-first
discipline; the `test_pymumble_client.py` fake-transport pattern).
"""

from __future__ import annotations

import queue
import socket
import threading
import time

from radio_server.dstar import dsrp, header
from radio_server.dstar.client import MockGatewayClient, UdpGatewayClient

HEADER = header.build_voice_header(callsign="AE9S", module="A", ur="E")


class FakeSocket:
    """A minimal datagram socket: records sends, serves injected inbound, else times out."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self._inbound: queue.Queue[bytes] = queue.Queue()
        self.closed = False

    def inject(self, data: bytes) -> None:
        self._inbound.put(data)

    # -- socket surface the client uses --
    def sendto(self, data: bytes, addr) -> int:
        self.sent.append(bytes(data))
        return len(data)

    def recvfrom(self, size: int):
        try:
            data = self._inbound.get(timeout=0.02)
        except queue.Empty as exc:
            raise socket.timeout from exc
        return data, ("127.0.0.1", 20010)

    def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------------------
# MockGatewayClient
# --------------------------------------------------------------------------------------


def test_mock_records_stream_and_registers_on_start():
    client = MockGatewayClient()
    client.start()
    assert client.status().running and client.status().registered
    client.send_header(HEADER, 0x0101)
    client.send_data(dsrp.build_dv_frame(bytes(9), dsrp.DATA_SYNC), 0x0101, 0)
    assert [m.kind for m in client.sent] == [dsrp.MessageKind.HEADER, dsrp.MessageKind.DATA]
    assert client.sent[0].radio_header == HEADER


def test_mock_inject_drives_the_sinks():
    client = MockGatewayClient()
    got: list[dsrp.DsrpMessage] = []
    client.on_header = got.append
    client.on_data = got.append
    client.inject(dsrp.build_header_packet(HEADER, 0x22))
    client.inject(dsrp.build_data_packet(dsrp.build_dv_frame(bytes(9)), 0x22, 0, end=True))
    assert [m.kind for m in got] == [dsrp.MessageKind.HEADER, dsrp.MessageKind.DATA]
    assert got[1].end


# --------------------------------------------------------------------------------------
# UdpGatewayClient (fake socket)
# --------------------------------------------------------------------------------------


def _client(fake, **kw):
    return UdpGatewayClient(_socket_factory=lambda port: fake, **kw)


def test_register_and_send_emit_correct_bytes():
    fake = FakeSocket()
    client = _client(fake, module="A", register_name="AE9S   A")
    client._sock = fake  # bypass start()/thread; exercise the send path directly
    client.register()  # registration IS the poll now
    client.send_header(HEADER, 0x0203)
    client.send_data(dsrp.build_dv_frame(bytes(9), dsrp.DATA_SYNC), 0x0203, 0, end=True)
    kinds = [dsrp.parse(p).kind for p in fake.sent]
    assert kinds == [dsrp.MessageKind.POLL, dsrp.MessageKind.HEADER, dsrp.MessageKind.DATA]
    assert dsrp.parse(fake.sent[0]).text == "AE9S   A"  # the poll carries the full callsign
    assert dsrp.parse(fake.sent[1]).session_id == 0x0203
    assert dsrp.parse(fake.sent[2]).end


def test_keepalive_is_poll_not_register_direction():
    # Regression for ADR 0101: every repeater -> gateway keep-alive must be a 0x0A POLL. A 0x0B
    # (gateway -> repeater NETWORK_REGISTER) sent the wrong way gets "Unknown packet from the Repeater".
    fake = FakeSocket()
    client = _client(fake, register_name="AE9S   A")
    client._sock = fake
    client.register()
    client.poll()
    assert fake.sent, "expected keep-alive packets"
    assert all(p[4] == dsrp.TYPE_POLL for p in fake.sent)
    assert not any(p[4] == dsrp.TYPE_REGISTER for p in fake.sent)


def test_poll_uses_full_callsign_not_module():
    # Regression for ADR 0101: the poll must carry the 8-char callsign the gateway registers on, not
    # the bare module letter.
    fake = FakeSocket()
    client = _client(fake, module="A", register_name="AE9S   A")
    client._sock = fake
    client.poll()
    text = dsrp.parse(fake.sent[0]).text
    assert text == "AE9S   A"
    assert text != "A"


def test_tick_drives_poll_cadence():
    fake = FakeSocket()
    now = {"t": 100.0}
    client = _client(fake, register_name="AE9S   A", poll_interval=10.0, _clock=lambda: now["t"])
    client._sock = fake
    client._last_poll = 100.0
    client._tick(105.0)  # 5 s elapsed: nothing due
    assert fake.sent == []
    client._tick(111.0)  # 11 s: a poll is due
    assert [dsrp.parse(p).kind for p in fake.sent] == [dsrp.MessageKind.POLL]
    client._tick(122.0)  # another interval elapsed: another poll
    assert [dsrp.parse(p).kind for p in fake.sent] == [dsrp.MessageKind.POLL, dsrp.MessageKind.POLL]


def test_handle_packet_dispatches_to_sinks():
    fake = FakeSocket()
    client = _client(fake, register_name="AE9S   A")
    headers: list[dsrp.DsrpMessage] = []
    datas: list[dsrp.DsrpMessage] = []
    client.on_header = headers.append
    client.on_data = datas.append
    client._handle_packet(dsrp.build_header_packet(HEADER, 0x33))
    client._handle_packet(dsrp.build_data_packet(dsrp.build_dv_frame(bytes(9)), 0x33, 0))
    # a raw inbound register (0x0B, gateway -> repeater): non-stream, ignored by the header/data sinks
    client._handle_packet(dsrp.MAGIC + bytes([dsrp.TYPE_REGISTER]) + b"AE9S   A\x00")
    assert len(headers) == 1 and len(datas) == 1


def test_inbound_marks_registered_but_start_does_not():
    # Regression for ADR 0101: "registered" means the gateway answered us, not "we sent a poll".
    fake = FakeSocket()
    client = _client(fake, register_name="AE9S   A", poll_interval=1e6)
    client.start()
    try:
        assert client.status().registered is False  # sending our poll is not proof of acceptance
        client._handle_packet(dsrp.MAGIC + bytes([dsrp.TYPE_REGISTER]) + b"AE9S   A\x00")
        assert client.status().registered is True  # the gateway's reply is
    finally:
        client.close()
    assert client.status().registered is False  # close() clears it


def test_start_polls_and_reader_dispatches_then_close():
    fake = FakeSocket()
    client = _client(fake, register_name="AE9S   A", poll_interval=1e6)  # no cadence noise
    seen = threading.Event()
    client.on_header = lambda msg: seen.set()
    client.start()
    try:
        assert client.status().running
        assert dsrp.parse(fake.sent[0]).kind is dsrp.MessageKind.POLL  # initial poll registers us
        fake.inject(dsrp.build_header_packet(HEADER, 0x44))
        assert seen.wait(timeout=1.0)  # the reader thread delivered the inbound header
    finally:
        client.close()
    assert fake.closed and not client.status().running


def test_send_before_open_is_a_noop():
    client = UdpGatewayClient(_socket_factory=lambda port: FakeSocket(), register_name="AE9S   A")
    # No socket yet (start() not called): sends are silently dropped, not raised.
    client.register()
    client.send_header(HEADER, 1)
    assert client.status().running is False
