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
    client = _client(fake, module="A")
    client._sock = fake  # bypass start()/thread; exercise the send path directly
    client.register()
    client.send_header(HEADER, 0x0203)
    client.send_data(dsrp.build_dv_frame(bytes(9), dsrp.DATA_SYNC), 0x0203, 0, end=True)
    kinds = [dsrp.parse(p).kind for p in fake.sent]
    assert kinds == [dsrp.MessageKind.REGISTER, dsrp.MessageKind.HEADER, dsrp.MessageKind.DATA]
    assert dsrp.parse(fake.sent[1]).session_id == 0x0203
    assert dsrp.parse(fake.sent[2]).end


def test_tick_drives_register_and_poll_cadence():
    fake = FakeSocket()
    now = {"t": 100.0}
    client = _client(fake, register_interval=30.0, poll_interval=60.0, _clock=lambda: now["t"])
    client._sock = fake
    client._last_register = 100.0
    client._last_poll = 100.0
    client._tick(120.0)  # 20 s elapsed: nothing due
    assert fake.sent == []
    client._tick(131.0)  # 31 s: a register is due
    assert [dsrp.parse(p).kind for p in fake.sent] == [dsrp.MessageKind.REGISTER]
    client._tick(161.0)  # 61 s since last poll (100): a poll is due (register already reset to 131)
    assert dsrp.parse(fake.sent[-1]).kind is dsrp.MessageKind.POLL


def test_handle_packet_dispatches_to_sinks():
    fake = FakeSocket()
    client = _client(fake)
    headers: list[dsrp.DsrpMessage] = []
    datas: list[dsrp.DsrpMessage] = []
    client.on_header = headers.append
    client.on_data = datas.append
    client._handle_packet(dsrp.build_header_packet(HEADER, 0x33))
    client._handle_packet(dsrp.build_data_packet(dsrp.build_dv_frame(bytes(9)), 0x33, 0))
    client._handle_packet(dsrp.build_register("X"))  # non-stream packet: ignored by the sinks
    assert len(headers) == 1 and len(datas) == 1


def test_start_registers_and_reader_dispatches_then_close():
    fake = FakeSocket()
    client = _client(fake, register_interval=1e6, poll_interval=1e6)  # no cadence noise
    seen = threading.Event()
    client.on_header = lambda msg: seen.set()
    client.start()
    try:
        assert client.status().running
        assert dsrp.parse(fake.sent[0]).kind is dsrp.MessageKind.REGISTER  # initial register at start
        fake.inject(dsrp.build_header_packet(HEADER, 0x44))
        assert seen.wait(timeout=1.0)  # the reader thread delivered the inbound header
    finally:
        client.close()
    assert fake.closed and not client.status().running


def test_send_before_open_is_a_noop():
    client = UdpGatewayClient(_socket_factory=lambda port: FakeSocket())
    # No socket yet (start() not called): sends are silently dropped, not raised.
    client.register()
    client.send_header(HEADER, 1)
    assert client.status().running is False
