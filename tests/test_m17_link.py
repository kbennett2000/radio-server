"""M17Link (ADR 0052): the Link-protocol binding over M17Client + Codec2 + the wire codec.

The mapping is the whole cycle, so it is tested against a localhost :class:`FakeReflector` (the
``test_m17_client`` idiom — ``asyncio.run`` + bounded ``asyncio.wait_for``, no real network) with a
deterministic :class:`FakeCodec2` so the edge/talker/buffering/partial logic runs **without**
``libcodec2``. Two ``@_CODEC2_SKIP`` tests prove the mapping composes with the *real* Codec2
(cycle-54 build-check shape). Nothing here touches a real reflector.
"""

from __future__ import annotations

import asyncio
from ctypes.util import find_library

import pytest

from radio_server.audio.format import CANONICAL_FORMAT, AudioFormat, AudioFrame
from radio_server.link.base import (
    LinkCapability,
    Station,
    StreamEdge,
    UnsupportedLinkCapability,
)
from radio_server.link.m17.packet import (
    ControlPacket,
    build_ackn,
    build_disc,
    build_stream,
    parse_control,
    parse_stream,
)
from radio_server.link.m17_link import M17Link

_CODEC2_SKIP = pytest.mark.skipif(
    find_library("codec2") is None,
    reason="libcodec2 not installed; real Codec2 encode/decode is a build check",
)

_META = bytes(14)
_TALKER = "W1AW"
_STATION = "KE0ABC"
_MODULE = "A"
#: One 20 ms canonical block (960 samples * 2 bytes); two of these = one 40 ms M17 frame.
_BLOCK_20MS = AudioFrame(b"\x01\x00" * 960, CANONICAL_FORMAT)


# --- a localhost stand-in for mrefd ---------------------------------------------------------------


class _FakeProto(asyncio.DatagramProtocol):
    def __init__(self, fake: FakeReflector) -> None:
        self._fake = fake

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        self._fake._on_datagram(data, addr)


class FakeReflector:
    """A UDP endpoint on 127.0.0.1 that ACKNs a link request, records what the client sends, and can
    push datagrams back to it (inbound stream frames)."""

    def __init__(self) -> None:
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
        ctrl = parse_control(data)
        if ctrl is not None and ctrl.kind in ("CONN", "LSTN"):
            self.send(build_ackn())  # link established; keepalive would commence

    def stream_frames(self) -> list:
        return [f for f in (parse_stream(d) for d in self.received) if f is not None]

    async def wait_for_control(self, predicate, timeout: float = 2.0) -> ControlPacket:
        async def _wait() -> ControlPacket:
            while True:
                for raw in self.received:
                    parsed = parse_control(raw)
                    if parsed is not None and predicate(parsed):
                        return parsed
                self._got.clear()
                await self._got.wait()

        return await asyncio.wait_for(_wait(), timeout)

    async def wait_for_stream_frames(self, count: int, timeout: float = 2.0) -> list:
        async def _wait() -> list:
            while True:
                frames = self.stream_frames()
                if len(frames) >= count:
                    return frames
                self._got.clear()
                if len(self.stream_frames()) >= count:
                    return self.stream_frames()
                await self._got.wait()

        return await asyncio.wait_for(_wait(), timeout)


class FakeCodec2:
    """A deterministic stand-in for the Codec2 seam so the mapping is testable without libcodec2.

    Honors the geometry the mapping relies on: a 40 ms canonical frame (3840 bytes) encodes to a
    16-byte payload (2 Codec2 frames), and a 16-byte payload decodes back to a 40 ms canonical frame.
    """

    bytes_per_frame = 8
    samples_per_frame = 160

    def encode(self, frame: AudioFrame) -> bytes:
        return b"\xa5" * (16 * (len(frame.samples) // 3840))

    def decode(self, packets: bytes) -> AudioFrame:
        return AudioFrame(b"\x22\x00" * 1920, CANONICAL_FORMAT)  # 1920 samples = 40 ms @ 48k


def _link(fake: FakeReflector, *, codec=None, **kwargs) -> M17Link:
    params = dict(
        reflector_host="127.0.0.1",
        reflector_port=fake.addr[1],
        module=_MODULE,
        callsign=_STATION,
        bind_host="127.0.0.1",
        codec=codec if codec is not None else FakeCodec2(),
    )
    params.update(kwargs)
    return M17Link(**params)


async def _await_connected(link: M17Link, timeout: float = 2.0) -> None:
    async def _w() -> None:
        while not link.status().connected:
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_w(), timeout)


async def _drain(link: M17Link, count: int, timeout: float = 2.0) -> list:
    out: list = []

    async def _w() -> None:
        while len(out) < count:
            ev = link.receive()
            if ev is None:
                await asyncio.sleep(0.005)
            else:
                out.append(ev)

    await asyncio.wait_for(_w(), timeout)
    return out


# --- capabilities ---------------------------------------------------------------------------------


def test_capabilities_advertise_listen_only_not_directory():
    link = M17Link(reflector_host="h", reflector_port=17000, module="A", callsign=_STATION,
                   codec=FakeCodec2())
    caps = link.capabilities()
    assert LinkCapability.LISTEN_ONLY in caps
    assert LinkCapability.DIRECTORY not in caps
    for cap in (LinkCapability.CONNECT, LinkCapability.TRANSMIT, LinkCapability.RECEIVE):
        assert cap in caps


def test_directory_raises_unsupported_by_name():
    link = M17Link(reflector_host="h", reflector_port=17000, module="A", callsign=_STATION,
                   codec=FakeCodec2())
    with pytest.raises(UnsupportedLinkCapability) as excinfo:
        link.directory()
    assert excinfo.value.capability is LinkCapability.DIRECTORY


def test_born_disabled_and_disconnected():
    link = M17Link(reflector_host="h", reflector_port=17000, module="A", callsign=_STATION,
                   codec=FakeCodec2())
    status = link.status()
    assert status.enabled is False and status.connected is False
    assert status.backend == "m17"


# --- LSTN vs CONN ---------------------------------------------------------------------------------


def test_connect_sends_conn_by_default():
    async def scenario():
        fake = await FakeReflector().start()
        link = _link(fake)
        try:
            link.connect("ref")
            conn = await fake.wait_for_control(lambda p: p.kind in ("CONN", "LSTN"))
            assert (conn.kind, conn.callsign, conn.module) == ("CONN", _STATION, _MODULE)
        finally:
            link.disconnect()
            fake.close()

    asyncio.run(scenario())


def test_set_listen_only_sends_lstn():
    async def scenario():
        fake = await FakeReflector().start()
        link = _link(fake)
        link.set_listen_only(True)
        assert link.listen_only is True
        try:
            link.connect("ref")
            lstn = await fake.wait_for_control(lambda p: p.kind in ("CONN", "LSTN"))
            assert lstn.kind == "LSTN"
        finally:
            link.disconnect()
            fake.close()

    asyncio.run(scenario())


# --- inbound mapping + talker ---------------------------------------------------------------------


def test_inbound_stream_maps_to_edges_and_frames():
    async def scenario():
        fake = await FakeReflector().start()
        link = _link(fake)
        try:
            link.connect("ref")
            await _await_connected(link)
            sid = 0x1234
            fake.send(build_stream(sid, _MODULE, _TALKER, 0x0005, _META, 0, bytes(16), last=False))
            fake.send(build_stream(sid, _MODULE, _TALKER, 0x0005, _META, 1, bytes(16), last=True))
            events = await _drain(link, 4)
            assert events[0] is StreamEdge.START
            assert isinstance(events[1], AudioFrame) and events[1].format == CANONICAL_FORMAT
            assert isinstance(events[2], AudioFrame)
            assert events[3] is StreamEdge.END
        finally:
            link.disconnect()
            fake.close()

    asyncio.run(scenario())


def test_talker_surfaces_from_lsf_then_clears_on_end():
    async def scenario():
        fake = await FakeReflector().start()
        link = _link(fake)
        try:
            link.connect("ref")
            await _await_connected(link)
            sid = 0xABCD
            fake.send(build_stream(sid, _MODULE, _TALKER, 0x0005, _META, 0, bytes(16), last=True))
            # First event is START; the talker is populated from the LSF source at that point.
            assert (await _drain(link, 1))[0] is StreamEdge.START
            assert link.status().talker == Station(_TALKER)
            # Drain the AudioFrame then END; talker clears when END is handed out.
            rest = await _drain(link, 2)
            assert rest[-1] is StreamEdge.END
            assert link.status().talker is None
        finally:
            link.disconnect()
            fake.close()

    asyncio.run(scenario())


# --- outbound mapping -----------------------------------------------------------------------------


def test_outbound_stream_emits_frames_with_final_eot():
    async def scenario():
        fake = await FakeReflector().start()
        link = _link(fake)
        try:
            link.connect("ref")
            await _await_connected(link)
            link.stream(True)
            for _ in range(4):  # 4 * 20 ms = two 40 ms M17 frames
                link.transmit(_BLOCK_20MS)
            link.stream(False)
            frames = await fake.wait_for_stream_frames(2)
            assert len(frames) == 2
            assert [f.frame_number for f in frames] == [0, 1]
            assert frames[0].last is False and frames[1].last is True  # EOT on the final frame
            assert all(f.src == _STATION for f in frames)  # this station is the talker upstream
        finally:
            link.disconnect()
            fake.close()

    asyncio.run(scenario())


def test_partial_frame_at_end_fails_loud():
    async def scenario():
        fake = await FakeReflector().start()
        link = _link(fake)
        try:
            link.connect("ref")
            await _await_connected(link)
            link.stream(True)
            link.transmit(_BLOCK_20MS)  # 20 ms only — half of a 40 ms M17 frame
            with pytest.raises(ValueError):
                link.stream(False)  # partial buffer at END -> fail loud, no pad, no half frame
        finally:
            link.disconnect()
            fake.close()

    asyncio.run(scenario())


def test_transmit_rejects_wrong_format_fail_loud():
    from radio_server.audio.format import AudioFormatMismatch

    link = M17Link(reflector_host="h", reflector_port=17000, module="A", callsign=_STATION,
                   codec=FakeCodec2())
    link.stream(True)
    wrong = AudioFrame(b"\x00\x00" * 160, AudioFormat(8000, 2, 1))
    with pytest.raises(AudioFormatMismatch):
        link.transmit(wrong)


# --- loss is state, never a synthesized END -------------------------------------------------------


def test_connection_loss_surfaces_as_state_not_stream_edge():
    async def scenario():
        fake = await FakeReflector().start()
        link = _link(fake, keepalive_timeout=0.05)  # ACKNs then goes silent
        try:
            link.connect("ref")
            await _await_connected(link)

            async def _lost() -> None:
                while link.status().connected:
                    await asyncio.sleep(0.005)

            await asyncio.wait_for(_lost(), timeout=2.0)
            # Loss shows only as connection state — receive() never synthesizes an edge.
            assert link.status().connected is False
            assert link.receive() is None
        finally:
            link.disconnect()
            fake.close()

    asyncio.run(scenario())


def test_reflector_disc_disconnects_without_end_edge():
    async def scenario():
        fake = await FakeReflector().start()
        link = _link(fake)
        try:
            link.connect("ref")
            await _await_connected(link)
            fake.send(build_disc(_TALKER))

            async def _down() -> None:
                while link.status().connected:
                    await asyncio.sleep(0.005)

            await asyncio.wait_for(_down(), timeout=2.0)
            assert link.receive() is None  # no synthesized END on a reflector-initiated DISC
        finally:
            link.disconnect()
            fake.close()

    asyncio.run(scenario())


# --- the mapping composes with the REAL Codec2 (skip-gated build check) ---------------------------


@_CODEC2_SKIP
def test_outbound_roundtrips_through_real_codec2():
    from radio_server.audio.codec2 import Codec2

    async def scenario():
        fake = await FakeReflector().start()
        codec = Codec2()
        link = _link(fake, codec=codec)
        try:
            link.connect("ref")
            await _await_connected(link)
            link.stream(True)
            for _ in range(2):  # one 40 ms M17 frame
                link.transmit(_BLOCK_20MS)
            link.stream(False)
            frames = await fake.wait_for_stream_frames(1)
            assert frames[-1].last is True
            assert len(frames[-1].payload) == 16  # real Codec2 40 ms -> 16-byte payload
        finally:
            link.disconnect()
            fake.close()
            codec.close()

    asyncio.run(scenario())


@_CODEC2_SKIP
def test_inbound_decodes_real_codec2_payload_to_canonical():
    from radio_server.audio.codec2 import Codec2

    async def scenario():
        fake = await FakeReflector().start()
        codec = Codec2()
        # A real 16-byte payload: encode 40 ms of canonical audio down through the codec.
        payload = codec.encode(AudioFrame(b"\x03\x00" * 1920, CANONICAL_FORMAT))
        assert len(payload) == 16
        link = _link(fake, codec=codec)
        try:
            link.connect("ref")
            await _await_connected(link)
            fake.send(build_stream(0x77, _MODULE, _TALKER, 0x0005, _META, 0, payload, last=True))
            events = await _drain(link, 3)
            assert events[0] is StreamEdge.START
            assert isinstance(events[1], AudioFrame)
            assert events[1].format == CANONICAL_FORMAT
            assert len(events[1].samples) == 1920 * 2  # decoded to one 40 ms canonical frame
            assert events[2] is StreamEdge.END
        finally:
            link.disconnect()
            fake.close()
            codec.close()

    asyncio.run(scenario())
