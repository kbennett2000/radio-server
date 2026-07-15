"""Inbound link audio: the `LinkPump`, the second `AudioHub`, and the `/audio/link` WS (ADR 0043).

The network-audio mirror of `test_rx_audio.py`. Software-first, no hardware: `MockLink` serves a
scripted inbound sequence (or a continuous `canned_rx`) instantly, so the transport is deterministic.
Two instruments, matching the RX suite:

- **Unit tests** drive `LinkPump`/`AudioHub` directly via `asyncio.run(...)` (there is no
  pytest-asyncio). The load-bearing proof lives here: the pump is gated on `status().enabled` — no
  frames flow while the link is disabled, and disabling mid-stream stops them.
- **WebSocket tests** drive the real `/audio/link` endpoint through Starlette's `TestClient` with the
  token in the `?token=` query string, reading the JSON format header (ADR 0023) before the binary
  frames, inside `with TestClient(app) as client:` so the lifespan shutdown handler runs.

The proofs: an enabled link's scripted frames reach a token'd client in order as raw canonical PCM; a
disabled link (and an idle `None`-returning one) publishes nothing; disabling mid-stream stops the
flow; a bad/missing token is rejected 1008; and the `link.backend = "none"` deployment connects
without a crash and yields nothing (no pump).
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from radio_server.api import create_app
from radio_server.audio import CANONICAL_FORMAT, AudioFrame
from radio_server.backends import MockRadio
from radio_server.link import MockLink, StreamEdge
from radio_server.rx import AudioHub, LinkPump

from .conftest import make_settings

TOKEN = "test-lan-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _app(link, **kwargs):
    return create_app(MockRadio(supports_cat=False), api_token=TOKEN, link=link, **kwargs)


# --- the pump: the enable gate (asyncio.run, self-terminating) ------------------------------------


class _ScriptedLink(MockLink):
    """A MockLink that signals when its scripted RX sequence is exhausted, so a pump loop over it
    terminates deterministically (the `_ScriptedRadio` pattern). Born disabled like any Link."""

    def __init__(self, frames: list[AudioFrame | StreamEdge | None]) -> None:
        super().__init__(rx_frames=frames)
        self._remaining = len(frames)
        self.drained = asyncio.Event()

    def receive(self) -> AudioFrame | StreamEdge | None:
        frame = super().receive()
        if self._remaining > 0:
            self._remaining -= 1
            if self._remaining == 0:
                self.drained.set()
        return frame


async def _pump_link_out(frames: list[AudioFrame | StreamEdge | None]) -> list[bytes]:
    """Run an ENABLED `LinkPump` over `frames` until the link drains; return what reached the hub."""
    link = _ScriptedLink(frames)
    link.enable(True)
    hub = AudioHub()
    queue = hub.subscribe()
    pump = LinkPump(link, hub, poll=0)
    pump.start()
    await link.drained.wait()
    await asyncio.sleep(0)  # let the pump publish the final frame before we stop it
    await pump.stop()
    out: list[bytes] = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


def test_enabled_pump_publishes_scripted_frames_in_order():
    frames = [AudioFrame(b"\x01\x02"), AudioFrame(b"\x03\x04"), AudioFrame(b"\x05\x06")]
    out = asyncio.run(_pump_link_out(frames))
    assert out == [f.samples for f in frames]


def test_disabled_pump_publishes_nothing_and_leaves_frames_queued():
    # The enable gate (ADR 0041/0042): a disabled link never drains — the pump reads nothing and
    # publishes nothing, so the scripted frames survive until it is deliberately enabled.
    async def scenario() -> tuple[list[bytes], list[bytes]]:
        link = MockLink(rx_frames=[AudioFrame(b"\x01\x02"), AudioFrame(b"\x03\x04")])  # born disabled
        hub = AudioHub()
        queue = hub.subscribe()
        pump = LinkPump(link, hub, poll=0)
        pump.start()
        for _ in range(5):
            await asyncio.sleep(0)  # several loop turns, all gated off
        await pump.stop()
        published = []
        while not queue.empty():
            published.append(queue.get_nowait())
        # The frames are untouched: enabling and reading yields both, in order.
        link.enable(True)
        remaining = [link.receive().samples, link.receive().samples]
        return published, remaining

    published, remaining = asyncio.run(scenario())
    assert published == []
    assert remaining == [b"\x01\x02", b"\x03\x04"]


def test_disabling_mid_stream_stops_the_flow():
    # Enable a continuously-receiving link → frames flow; disable → they stop on the next polls.
    async def scenario() -> bool:
        link = MockLink(canned_rx=AudioFrame(b"\xaa\xbb"))  # a never-idle network while enabled
        hub = AudioHub()
        queue = hub.subscribe()
        pump = LinkPump(link, hub, poll=0)
        link.enable(True)
        pump.start()
        while queue.qsize() < 3:  # frames are flowing
            await asyncio.sleep(0)
        link.enable(False)
        await asyncio.sleep(0)  # let any in-flight publish land, then drain the backlog
        while not queue.empty():
            queue.get_nowait()
        for _ in range(5):  # give the (now gated) loop several more turns
            await asyncio.sleep(0)
        stalled_empty = queue.empty()
        await pump.stop()
        return stalled_empty

    assert asyncio.run(scenario()) is True


def test_pump_publishes_only_frames_skipping_edges_and_gaps():
    # receive() now yields AudioFrame | StreamEdge | None (ADR 0047). The listening tier needs no stream
    # boundaries, so the pump publishes frame audio only — it drops StreamEdge edges and None gaps and
    # never raises on an edge (a StreamEdge has no `.samples`). Only the transmit path (a later cycle)
    # acts on the boundaries.
    f1, f2, f3 = AudioFrame(b"\x01\x02"), AudioFrame(b"\x03\x04"), AudioFrame(b"\x05\x06")
    scripted = [StreamEdge.START, f1, None, f2, StreamEdge.END, f3]
    out = asyncio.run(_pump_link_out(scripted))
    assert out == [f1.samples, f2.samples, f3.samples]


def test_idle_link_returning_none_publishes_nothing():
    # `Link.receive()` returns None on an idle network (unlike `Radio.receive()`); the pump handles
    # None before touching `.samples` — nothing published, no crash.
    async def scenario() -> bool:
        link = MockLink()  # empty queue, canned_rx defaults to None
        link.enable(True)
        hub = AudioHub()
        queue = hub.subscribe()
        pump = LinkPump(link, hub, poll=0)
        pump.start()
        for _ in range(5):
            await asyncio.sleep(0)
        await pump.stop()
        return queue.empty()

    assert asyncio.run(scenario()) is True


def test_link_pump_start_stop_is_idempotent_and_leaves_no_task():
    async def scenario() -> list[asyncio.Task]:
        pump = LinkPump(MockLink(), AudioHub(), poll=0)
        pump.start()
        pump.start()  # idempotent — no second task
        await asyncio.sleep(0)
        assert pump.running is True
        await pump.stop()
        assert pump.running is False
        return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    assert asyncio.run(scenario()) == []


# --- the WebSocket: /audio/link binary transport + the enable gate + auth -------------------------


def test_audio_link_streams_frames_over_the_socket_when_enabled():
    # Since ADR 0048 the single reader is the `LinkTxBridge`, started by `POST /link/enable`; it tees each
    # inbound frame to `link_hub`, which `/audio/link` fans to the browser. A continuous canned frame
    # avoids a drain race between enable-start and the browser subscribe. Enable requires a real squelch.
    link = MockLink(canned_rx=AudioFrame(b"\xaa\xbb"))
    app = _app(link, settings=make_settings({"audio.squelch": "audio"}))
    with TestClient(app) as client:
        client.post("/link/enable", headers=AUTH)
        with client.websocket_connect(f"/audio/link?token={TOKEN}") as ws:
            ws.receive_json()  # leading format header (ADR 0023), then the PCM frames
            got = [ws.receive_bytes() for _ in range(3)]
    assert got == [b"\xaa\xbb", b"\xaa\xbb", b"\xaa\xbb"]
    # Teardown (last disconnect + lifespan shutdown) leaves the bridge stopped — no leaked task.
    assert app.state.link_bridge.running is False


def test_post_enable_then_frames_arrive_over_the_socket():
    # The acceptance path: the app boots disabled, `POST /link/enable` opens the gate, and frames
    # then flow over `/audio/link`. Enable now requires a real squelch (ADR 0044) — the outbound
    # feeder refuses to run under a gate that never closes — so configure audio.squelch="audio".
    link = MockLink(canned_rx=AudioFrame(b"\xaa\xbb"))  # continuous once enabled
    app = _app(link, settings=make_settings({"audio.squelch": "audio"}))
    with TestClient(app) as client:
        assert client.get("/link", headers=AUTH).json()["enabled"] is False
        client.post("/link/enable", headers=AUTH)
        with client.websocket_connect(f"/audio/link?token={TOKEN}") as ws:
            ws.receive_json()
            data = ws.receive_bytes()
    assert data == b"\xaa\xbb"


def test_audio_link_sends_format_header():
    link = MockLink(rx_frames=[AudioFrame(b"\x01\x02")])
    link.enable(True)
    with TestClient(_app(link)) as client:
        with client.websocket_connect(f"/audio/link?token={TOKEN}") as ws:
            header = ws.receive_json()
    assert header == {"status": "ready", "format": {"rate": 48000, "width": 2, "channels": 1}}


def test_audio_link_sends_binary_canonical_pcm():
    frame = AudioFrame(b"\x10\x20\x30\x40")  # 4 bytes == 2 sample-frames of 16-bit mono
    link = MockLink(canned_rx=frame)  # continuous, so no enable-start / subscribe race
    app = _app(link, settings=make_settings({"audio.squelch": "audio"}))
    with TestClient(app) as client:
        client.post("/link/enable", headers=AUTH)
        with client.websocket_connect(f"/audio/link?token={TOKEN}") as ws:
            ws.receive_json()  # skip the format header
            data = ws.receive_bytes()  # binary, not JSON
    assert isinstance(data, (bytes, bytearray))
    assert bytes(data) == frame.samples
    assert len(data) % CANONICAL_FORMAT.frame_bytes == 0  # whole 16-bit mono samples


def test_audio_link_rejects_bad_token():
    with TestClient(_app(MockLink())) as client:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect("/audio/link?token=nope") as ws:
                ws.receive_bytes()
    assert excinfo.value.code == 1008  # policy violation, rejected before accept


def test_audio_link_rejects_missing_token():
    with TestClient(_app(MockLink())) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/audio/link") as ws:
                ws.receive_bytes()


def test_audio_link_none_backend_connects_but_yields_nothing():
    # `link.backend = "none"` (link=None): no pump is built, so the socket connects and sends its
    # header, then yields nothing — the WS analogue of the REST 503, and no crash.
    app = create_app(MockRadio(supports_cat=False), api_token=TOKEN)  # link defaults to None
    with TestClient(app) as client:
        with client.websocket_connect(f"/audio/link?token={TOKEN}") as ws:
            header = ws.receive_json()
    assert header["status"] == "ready"
    assert app.state.link is None
    assert app.state.link_bridge is None


# --- inbound-link TX: contention + the /link/disable hard unkey (ADR 0048) ------------------------


def test_browser_talk_refused_while_link_holds_the_slot():
    # THE LOCAL OPERATOR OWNS THE STATION (ADR 0048), from the other side: while the link holds the
    # shared TxSlot, a browser Talk (`/audio/tx`) is refused for the duration — the existing single-talker
    # refusal, now reached via the link. Drive the bridge's keying core directly so the link
    # deterministically holds the slot (no poll-loop timing race).
    app = _app(MockLink())
    app.state.link_bridge.on_start(0.0)  # the link keys — acquires the shared tx_slot
    assert app.state.tx_slot.occupied is True
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws:
                assert ws.receive_json() == {"status": "busy"}  # refused by name, never a silent no-op
                ws.receive_bytes()  # the next read raises the 1013 close
    assert excinfo.value.code == 1013  # try again later — the link holds the transmitter


def test_link_disable_is_a_hard_unkey_mid_stream():
    # `POST /link/disable` is the panic button (ADR 0048): it drops PTT NOW, mid-frame, and releases the
    # slot — not "stop feeding" — so it works while a stranger is keying the rig. Key the link (bridge
    # holds the slot, radio transmitting), then disable and assert PTT dropped and the slot freed.
    link = MockLink()
    link.enable(True)
    app = _app(link)
    bridge = app.state.link_bridge
    bridge.on_start(0.0)
    bridge.on_frame(b"\x01\x02", 0.0)  # keyed, mid-stream
    assert bridge.keyed is True
    assert app.state.tx_slot.occupied is True
    with TestClient(app) as client:
        client.post("/link/disable", headers=AUTH)
    # The hard unkey: PTT dropped (bridge no longer keyed) and the slot freed for the local operator.
    # (MockRadio.transmit() returns to receive immediately, so its `transmitting` flag can't stand in
    # for the keyed state after a frame — `bridge.keyed` is the load-bearing signal.)
    assert bridge.keyed is False
    assert app.state.tx_slot.occupied is False
