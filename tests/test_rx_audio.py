"""RX audio streaming: the bounded fan-out hub, the pump, and the `/audio/rx` WS (ADR 0014).

Software-first, no hardware: `MockRadio` serves a scripted RX sequence instantly, so the whole
transport is deterministic. Two instruments, matching the rest of the suite:

- **Unit tests** drive `RxPump`/`AudioHub` directly. Async ones use `asyncio.run(...)` with a
  self-terminating scripted radio (the `test_controller.py` `CountingRadio` pattern) — there is
  no pytest-asyncio. The drop-policy test needs no loop at all: `AudioHub.publish` is synchronous.
- **WebSocket tests** drive the real endpoint through Starlette's `TestClient` with the token in
  the `?token=` query string. The endpoint sends a JSON format header first (ADR 0023, mirroring
  `/audio/tx`), so a test reads it with `receive_json()` before the binary `receive_bytes()`
  frames. They use `with TestClient(app) as client:` so the lifespan shutdown handler runs in the
  pump's loop.

The load-bearing proofs: a token'd client receives the scripted frames in order as raw canonical
PCM; a bad/missing token is rejected; a reject-all gate suppresses everything; the pump skips
empty frames; a slow listener drops its oldest frames without stalling the pump or a healthy
listener; and the demand-driven pump starts/stops cleanly with no leaked task.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from radio_server.api import create_app
from radio_server.audio import CANONICAL_FORMAT, AudioFrame
from radio_server.backends import MockRadio
from radio_server.rx import AudioHub, RxPump, pass_through_gate

TOKEN = "test-lan-secret"


# --- MockRadio scripted RX -----------------------------------------------------------------

def test_mock_radio_serves_scripted_rx_then_falls_back_to_canned():
    canned = AudioFrame(b"\x00\x00")
    radio = MockRadio(canned_rx=canned, rx_frames=[AudioFrame(b"\x01\x02")])
    radio.script_rx(AudioFrame(b"\x03\x04"))
    assert radio.receive().samples == b"\x01\x02"  # scripted, FIFO
    assert radio.receive().samples == b"\x03\x04"  # scripted, FIFO
    assert radio.receive() is canned               # falls back once drained


# --- the pump: gate seam, empty-skip (asyncio.run, self-terminating) -----------------------

class _ScriptedRadio(MockRadio):
    """A MockRadio that signals when its scripted RX sequence is exhausted, so a pump loop
    over it terminates deterministically (the `CountingRadio` pattern, event-driven)."""

    def __init__(self, frames: list[AudioFrame]) -> None:
        super().__init__(rx_frames=frames)
        self._remaining = len(frames)
        self.drained = asyncio.Event()

    def receive(self) -> AudioFrame:
        frame = super().receive()
        if self._remaining > 0:
            self._remaining -= 1
            if self._remaining == 0:
                self.drained.set()
        return frame


async def _pump_out(frames: list[AudioFrame], **pump_kwargs) -> list[bytes]:
    """Run a pump over `frames` until the radio drains, then return what reached the hub."""
    radio = _ScriptedRadio(frames)
    hub = AudioHub()
    queue = hub.subscribe()
    pump = RxPump(radio, hub, poll=0, **pump_kwargs)
    pump.start()
    await radio.drained.wait()
    await asyncio.sleep(0)  # let the pump publish the final frame before we stop it
    await pump.stop()
    out: list[bytes] = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


def test_pass_through_gate_relays_every_frame():
    frames = [AudioFrame(b"\x01\x02"), AudioFrame(b"\x03\x04"), AudioFrame(b"\x05\x06")]
    out = asyncio.run(_pump_out(frames, gate=pass_through_gate))
    assert out == [f.samples for f in frames]


def test_reject_all_gate_suppresses_frames():
    frames = [AudioFrame(b"\x01\x02"), AudioFrame(b"\x03\x04")]
    out = asyncio.run(_pump_out(frames, gate=lambda frame: False))
    assert out == []


def test_pump_skips_empty_frames():
    # An empty (0-byte) frame carries no audio: the transport skip drops it, independent of the
    # gate. The scripted radio still counts it, so the loop terminates.
    frames = [AudioFrame(b"\x01\x02"), AudioFrame(b""), AudioFrame(b"\x05\x06")]
    out = asyncio.run(_pump_out(frames))
    assert out == [b"\x01\x02", b"\x05\x06"]


# --- the hub: bounded, drop-oldest backpressure (synchronous, no loop) ---------------------

def test_slow_listener_drops_oldest_without_affecting_healthy():
    hub = AudioHub(maxsize=4)
    slow = hub.subscribe()      # never drained -> a slow/stuck listener
    healthy = hub.subscribe()   # drained every publish -> a healthy listener
    frames = [bytes([i, i]) for i in range(1, 11)]  # 10 frames, > maxsize

    healthy_got: list[bytes] = []
    for frame in frames:
        hub.publish(frame)                    # never raises, never blocks
        healthy_got.append(healthy.get_nowait())

    # The healthy listener saw every frame in order — one slow listener didn't affect it.
    assert healthy_got == frames
    # The slow listener's queue capped at maxsize and kept the NEWEST frames (drop-oldest).
    assert slow.qsize() == 4
    kept = [slow.get_nowait() for _ in range(4)]
    assert kept == frames[-4:]


# --- the pump: clean start/stop, no leaked task; demand-driven ref-counting ----------------

def test_pump_start_stop_leaves_no_task():
    async def scenario() -> list[asyncio.Task]:
        pump = RxPump(MockRadio(), AudioHub(), poll=0)  # empty radio produces nothing
        pump.start()
        pump.start()  # idempotent — no second task
        await asyncio.sleep(0)
        assert pump.running is True
        await pump.stop()
        assert pump.running is False
        return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    assert asyncio.run(scenario()) == []


def test_demand_driven_lifecycle_is_ref_counted():
    # Exercises exactly the coordination the `/audio/rx` handler encodes: start the pump on the
    # first subscriber, keep it running while any remain, stop it on the last — no leaked task.
    async def scenario() -> list[asyncio.Task]:
        hub = AudioHub()
        pump = RxPump(MockRadio(), hub, poll=0)

        first = hub.subscribe()
        if hub.subscriber_count == 1:
            pump.start()
        assert pump.running is True

        second = hub.subscribe()  # already running; no restart
        hub.unsubscribe(second)
        assert hub.subscriber_count == 1 and pump.running is True

        hub.unsubscribe(first)
        if hub.subscriber_count == 0:
            await pump.stop()
        assert pump.running is False
        return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    assert asyncio.run(scenario()) == []


# --- the WebSocket: /audio/rx binary transport + auth --------------------------------------

def test_audio_rx_streams_scripted_frames_in_order():
    frames = [AudioFrame(b"\x01\x02"), AudioFrame(b"\x03\x04"), AudioFrame(b"\x05\x06")]
    radio = MockRadio(rx_frames=frames)
    app = create_app(radio, api_token=TOKEN)
    with TestClient(app) as client:
        with client.websocket_connect(f"/audio/rx?token={TOKEN}") as ws:
            ws.receive_json()  # leading format header (ADR 0023), then the PCM frames
            got = [ws.receive_bytes() for _ in range(len(frames))]
    assert got == [f.samples for f in frames]
    # Teardown (last disconnect + lifespan shutdown) leaves the pump stopped — no leaked task.
    assert app.state.rx_pump.running is False


def test_audio_rx_sends_format_header():
    # The stream opens with a JSON format declaration, symmetric with `/audio/tx`'s ready ack.
    radio = MockRadio(rx_frames=[AudioFrame(b"\x01\x02")])
    with TestClient(create_app(radio, api_token=TOKEN)) as client:
        with client.websocket_connect(f"/audio/rx?token={TOKEN}") as ws:
            header = ws.receive_json()
    assert header == {
        "status": "ready",
        "format": {"rate": 48000, "width": 2, "channels": 1},
    }


def test_audio_rx_sends_binary_canonical_pcm():
    frame = AudioFrame(b"\x10\x20\x30\x40")  # 4 bytes == 2 sample-frames of 16-bit mono
    radio = MockRadio(rx_frames=[frame])
    with TestClient(create_app(radio, api_token=TOKEN)) as client:
        with client.websocket_connect(f"/audio/rx?token={TOKEN}") as ws:
            ws.receive_json()  # skip the format header
            data = ws.receive_bytes()  # binary, not JSON
    assert isinstance(data, (bytes, bytearray))
    assert bytes(data) == frame.samples
    assert len(data) % CANONICAL_FORMAT.frame_bytes == 0  # whole 16-bit mono samples


def test_audio_rx_rejects_bad_token():
    with TestClient(create_app(MockRadio(), api_token=TOKEN)) as client:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect("/audio/rx?token=nope") as ws:
                ws.receive_bytes()
    assert excinfo.value.code == 1008  # policy violation, rejected before accept


def test_audio_rx_rejects_missing_token():
    with TestClient(create_app(MockRadio(), api_token=TOKEN)) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/audio/rx") as ws:
                ws.receive_bytes()
