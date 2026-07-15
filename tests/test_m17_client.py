"""The mrefd UDP client (ADR 0051): lifecycle + the source-validation guardrail.

No pytest-asyncio in this repo — every test drives an inner ``async def scenario()`` with
``asyncio.run(...)`` and bounds every wait with ``asyncio.wait_for`` (the ``test_link_tx`` /
``test_scan_runner`` idiom). A :class:`FakeReflector` stands in for mrefd on ``127.0.0.1`` and is
driven by an injected handler; nothing here touches a real reflector or the network beyond
loopback, and the only "sleeps" are a tiny injected ``keepalive_timeout``.
"""

from __future__ import annotations

import asyncio
import socket

from radio_server.link.m17.client import M17Client, M17ClientState
from radio_server.link.m17.packet import (
    ControlPacket,
    build_ackn,
    build_disc,
    build_nack,
    build_ping,
    build_stream,
    parse_control,
)

_META = bytes(14)
_PAYLOAD = bytes(16)
_REFLECTOR_CS = "W1AW"  # the fake's callsign in PING/DISC; the client never validates it


# --- a localhost stand-in for mrefd ---------------------------------------------------------------


class _FakeProto(asyncio.DatagramProtocol):
    def __init__(self, fake: FakeReflector) -> None:
        self._fake = fake

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        self._fake._on_datagram(data, addr)


class FakeReflector:
    """A UDP endpoint on 127.0.0.1 that records what a client sends and can script replies.

    ``handler(fake, data, addr)`` runs on each received datagram (e.g. ACKN a CONN); pass ``None``
    for a reflector that stays silent.
    """

    def __init__(self, handler=None) -> None:
        self._handler = handler
        self.transport: asyncio.DatagramTransport | None = None
        self.received: list[bytes] = []
        self.client_addr: tuple | None = None
        self._got = asyncio.Event()

    async def start(self) -> FakeReflector:
        loop = asyncio.get_running_loop()
        self.transport, _ = await loop.create_datagram_endpoint(
            lambda: _FakeProto(self), local_addr=("127.0.0.1", 0)
        )
        return self

    @property
    def addr(self) -> tuple:
        assert self.transport is not None
        return self.transport.get_extra_info("sockname")

    def send(self, data: bytes) -> None:
        assert self.transport is not None and self.client_addr is not None
        self.transport.sendto(data, self.client_addr)

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()

    def _on_datagram(self, data: bytes, addr: tuple) -> None:
        self.client_addr = addr
        self.received.append(data)
        self._got.set()
        if self._handler is not None:
            self._handler(self, data, addr)

    def _find(self, predicate) -> ControlPacket | None:
        for raw in self.received:
            parsed = parse_control(raw)
            if parsed is not None and predicate(parsed):
                return parsed
        return None

    async def wait_for_control(self, predicate, timeout: float = 2.0) -> ControlPacket:
        """Await a received control packet matching ``predicate`` (race-free double-check)."""

        async def _wait() -> ControlPacket:
            while True:
                found = self._find(predicate)
                if found is not None:
                    return found
                self._got.clear()
                found = self._find(predicate)
                if found is not None:
                    return found
                await self._got.wait()

        return await asyncio.wait_for(_wait(), timeout)


def _ackn_on_conn(fake: FakeReflector, data: bytes, addr: tuple) -> None:
    ctrl = parse_control(data)
    if ctrl is not None and ctrl.kind in ("CONN", "LSTN"):
        fake.send(build_ackn())


def _nack_on_conn(fake: FakeReflector, data: bytes, addr: tuple) -> None:
    ctrl = parse_control(data)
    if ctrl is not None and ctrl.kind in ("CONN", "LSTN"):
        fake.send(build_nack())


def _client(fake: FakeReflector, **kwargs) -> M17Client:
    params = dict(
        reflector_host="127.0.0.1",
        reflector_port=fake.addr[1],
        module="A",
        callsign="KE0ABC",
        bind_host="127.0.0.1",
    )
    params.update(kwargs)
    return M17Client(**params)


# --- handshake ------------------------------------------------------------------------------------


def test_conn_handshake_ackn_connects():
    async def scenario():
        fake = await FakeReflector(_ackn_on_conn).start()
        client = _client(fake)
        try:
            assert await client.connect() is True
            assert client.state is M17ClientState.CONNECTED
            sent = parse_control(fake.received[0])
            assert sent is not None
            assert (sent.kind, sent.callsign, sent.module) == ("CONN", "KE0ABC", "A")
        finally:
            await client.close()
            fake.close()

    asyncio.run(scenario())


def test_listen_only_sends_lstn_not_conn():
    async def scenario():
        fake = await FakeReflector(_ackn_on_conn).start()
        client = _client(fake, listen_only=True)
        try:
            assert await client.connect() is True
            sent = parse_control(fake.received[0])
            assert sent is not None and sent.kind == "LSTN"
        finally:
            await client.close()
            fake.close()

    asyncio.run(scenario())


def test_nack_refuses_connection():
    async def scenario():
        fake = await FakeReflector(_nack_on_conn).start()
        client = _client(fake)
        try:
            assert await client.connect() is False
            assert client.state is M17ClientState.DISCONNECTED
        finally:
            await client.close()
            fake.close()

    asyncio.run(scenario())


def test_connect_timeout_when_reflector_silent():
    async def scenario():
        fake = await FakeReflector(handler=None).start()  # never replies
        client = _client(fake, connect_timeout=0.1)
        try:
            assert await client.connect() is False
            assert client.state is not M17ClientState.CONNECTED
        finally:
            await client.close()
            fake.close()

    asyncio.run(scenario())


# --- keepalive ------------------------------------------------------------------------------------


def test_ping_elicits_pong():
    async def scenario():
        fake = await FakeReflector(_ackn_on_conn).start()
        client = _client(fake)
        try:
            assert await client.connect() is True
            fake.send(build_ping(_REFLECTOR_CS))  # reflector pings; client must PONG
            pong = await fake.wait_for_control(lambda p: p.kind == "PONG")
            assert pong.callsign == "KE0ABC"
        finally:
            await client.close()
            fake.close()

    asyncio.run(scenario())


def test_connection_loss_surfaces_as_state_not_stream_edge():
    async def scenario():
        fake = await FakeReflector(_ackn_on_conn).start()  # ACKNs, then stays silent
        client = _client(fake, keepalive_timeout=0.05)
        try:
            assert await client.connect() is True
            await client.wait_for_state(M17ClientState.LOST, timeout=2.0)
            assert client.state is M17ClientState.LOST
            assert client.frames.empty()  # loss is state, never a synthesized frame/edge
        finally:
            await client.close()
            fake.close()

    asyncio.run(scenario())


# --- inbound frames + THE source-validation guardrail ---------------------------------------------


def test_valid_stream_frame_from_reflector_is_enqueued():
    async def scenario():
        fake = await FakeReflector(_ackn_on_conn).start()
        client = _client(fake)
        try:
            assert await client.connect() is True
            fake.send(build_stream(0xABCD, "M17", _REFLECTOR_CS, 5, _META, 1, _PAYLOAD))
            frame = await asyncio.wait_for(client.frames.get(), timeout=2.0)
            assert frame.src == _REFLECTOR_CS
            assert frame.payload == _PAYLOAD
            assert client.dropped_source == 0
        finally:
            await client.close()
            fake.close()

    asyncio.run(scenario())


def test_wrong_source_datagram_is_dropped_before_parsing():
    """The point of the cycle: a valid stream frame from a NON-reflector source never reaches the
    queue — it is dropped by source validation before the parser ever sees it."""

    async def scenario():
        fake = await FakeReflector(_ackn_on_conn).start()
        client = _client(fake)
        loop = asyncio.get_running_loop()
        # A second endpoint on a different port impersonating the reflector's traffic.
        spoof, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0)
        )
        try:
            assert await client.connect() is True
            frame = build_stream(0x1234, "M17", _REFLECTOR_CS, 5, _META, 1, _PAYLOAD)
            spoof.sendto(frame, client.local_addr)

            async def _dropped() -> None:
                while client.dropped_source == 0:
                    await asyncio.sleep(0.005)

            await asyncio.wait_for(_dropped(), timeout=2.0)
            assert client.dropped_source == 1
            assert client.frames.empty()  # the spoofed frame never reached the parser
        finally:
            spoof.close()
            await client.close()
            fake.close()

    asyncio.run(scenario())


# --- teardown -------------------------------------------------------------------------------------


def test_close_sends_disc_and_reaches_closed_idempotently():
    async def scenario():
        fake = await FakeReflector(_ackn_on_conn).start()
        client = _client(fake)
        try:
            assert await client.connect() is True
            await client.close()
            assert client.state is M17ClientState.CLOSED
            disc = await fake.wait_for_control(lambda p: p.kind == "DISC")
            assert disc.callsign == "KE0ABC"
            await client.close()  # idempotent
            assert client.state is M17ClientState.CLOSED
        finally:
            fake.close()

    asyncio.run(scenario())


def test_reflector_initiated_disc_disconnects_client():
    async def scenario():
        fake = await FakeReflector(_ackn_on_conn).start()
        client = _client(fake)
        try:
            assert await client.connect() is True
            fake.send(build_disc(_REFLECTOR_CS))
            await client.wait_for_state(M17ClientState.DISCONNECTED, timeout=2.0)
            assert client.state is M17ClientState.DISCONNECTED
        finally:
            await client.close()
            fake.close()

    asyncio.run(scenario())
