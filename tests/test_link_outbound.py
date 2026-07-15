"""Outbound link audio: the `LinkFeeder`, the squelch precondition, and stream boundaries (ADR 0044).

The talking-tier mirror of `test_link_audio.py`: where that suite fans the network to the browser,
this one fans the *radio* to the network. Software-first, no hardware — `MockRadio` serves scripted RX
and `MockLink` records every outbound event (frames + `stream()` boundaries) to `tx_log`, so the whole
path is deterministic. Three instruments:

- **Feeder unit tests** drive `LinkFeeder`/`AudioHub` directly via `asyncio.run(...)`, pushing frames
  and gate edges by hand — the boundary/bracketing logic lives here.
- **An end-to-end pump→feeder test** runs a real `RxPump` over scripted signal/silence frames, with the
  feeder wired to the pump's `on_activity` exactly as `create_app` wires it, and asserts the received
  transmission arrives at `MockLink.tx_log` bracketed by one `StreamEdge.START`/`END` pair.
- **WebSocket/REST integration** drives `POST /link/enable` through `TestClient`: the squelch-off
  refusal (400 by name) and the feeder counting as RX demand.

The load-bearing proofs: frames reach `link.transmit` only bracketed by one stream open/close; the
boundaries fire once per span; a disabled feeder transmits nothing; a TX-keyed pause (a gate-close)
ends the stream and a returning frame re-opens it; disabling mid-stream sends the EOT; enabling with
`audio.squelch = "off"` is refused by name; and the feeder is RX demand (the pump runs with no browser).
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from radio_server.api import create_app
from radio_server.audio import AudioFrame
from radio_server.backends import MockRadio
from radio_server.link import MockLink, StreamEdge
from radio_server.rx import AudioHub, LinkFeeder, RxPump

from .conftest import make_settings

TOKEN = "test-lan-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _app(link, **kwargs):
    return create_app(MockRadio(supports_cat=False), api_token=TOKEN, link=link, **kwargs)


async def _noop() -> None:
    """A no-op RX-demand hook — the unit feeder tests don't exercise the pump."""


async def _settle(pred, turns: int = 100) -> None:
    """Pump the event loop until `pred()` holds (the feeder consumes its queue asynchronously)."""
    for _ in range(turns):
        if pred():
            return
        await asyncio.sleep(0)


# --- the feeder: bracketing, boundaries, the enable/pause semantics (asyncio.run) -----------------


def test_frames_reach_transmit_bracketed_by_one_stream_open_close():
    async def scenario() -> list:
        link, hub = MockLink(), AudioHub()
        feeder = LinkFeeder(link, hub, acquire=_noop, release=_noop)
        await feeder.start()
        feeder.note_activity(True)  # gate opens → LSF
        hub.publish(b"\x01\x02")
        hub.publish(b"\x03\x04")
        feeder.note_activity(False)  # gate closes → EOT
        await _settle(lambda: len(link.tx_log) >= 4)
        await feeder.stop()
        return link.tx_log

    assert asyncio.run(scenario()) == [
        StreamEdge.START,
        AudioFrame(b"\x01\x02"),
        AudioFrame(b"\x03\x04"),
        StreamEdge.END,
    ]


def test_boundaries_fire_once_per_span_across_two_spans():
    # A run of frames inside one open edge yields exactly one START; two spans → two clean brackets.
    async def scenario() -> list:
        link, hub = MockLink(), AudioHub()
        feeder = LinkFeeder(link, hub, acquire=_noop, release=_noop)
        await feeder.start()
        feeder.note_activity(True)
        hub.publish(b"aa")
        hub.publish(b"bb")  # second frame, same span → no extra START
        feeder.note_activity(False)
        feeder.note_activity(True)  # a new span
        hub.publish(b"cc")
        feeder.note_activity(False)
        await _settle(lambda: len(link.tx_log) >= 7)
        await feeder.stop()
        return link.tx_log

    assert asyncio.run(scenario()) == [
        StreamEdge.START,
        AudioFrame(b"aa"),
        AudioFrame(b"bb"),
        StreamEdge.END,
        StreamEdge.START,
        AudioFrame(b"cc"),
        StreamEdge.END,
    ]


def test_a_feeder_that_never_started_transmits_nothing():
    # Disabled = never started: note_activity is a no-op (no queue) and it isn't subscribed, so a hub
    # publish reaches no one. Nothing is transmitted.
    async def scenario() -> list:
        link, hub = MockLink(), AudioHub()
        feeder = LinkFeeder(link, hub, acquire=_noop, release=_noop)
        feeder.note_activity(True)  # no-op — not started
        hub.publish(b"\x01\x02")  # feeder isn't a subscriber
        await asyncio.sleep(0)
        return link.tx_log

    assert asyncio.run(scenario()) == []


def test_tx_keyed_pause_ends_the_stream_and_a_returning_frame_reopens_it():
    # The inherited half-duplex behavior (ADR 0017): a local key-up looks like a gate-close, so the
    # feed ends (EOT); when RX resumes, the returning frame lazily re-opens a fresh stream (LSF) — the
    # feed resumes "on its own" with no explicit re-open edge needed.
    async def scenario() -> list:
        link, hub = MockLink(), AudioHub()
        feeder = LinkFeeder(link, hub, acquire=_noop, release=_noop)
        await feeder.start()
        feeder.note_activity(True)
        hub.publish(b"\x11\x11")
        feeder.note_activity(False)  # TX key-up → the pump stands down → gate-close → EOT
        hub.publish(b"\x22\x22")  # RX resumes: a frame with no open edge → lazy LSF
        feeder.note_activity(False)  # gate closes again → EOT
        await _settle(lambda: len(link.tx_log) >= 6)
        await feeder.stop()
        return link.tx_log

    assert asyncio.run(scenario()) == [
        StreamEdge.START,
        AudioFrame(b"\x11\x11"),
        StreamEdge.END,
        StreamEdge.START,
        AudioFrame(b"\x22\x22"),
        StreamEdge.END,
    ]


def test_disabling_mid_stream_sends_a_final_eot():
    # A stream left open at stop() (disable mid-transmission) is closed cleanly with an EOT.
    async def scenario() -> list:
        link, hub = MockLink(), AudioHub()
        feeder = LinkFeeder(link, hub, acquire=_noop, release=_noop)
        await feeder.start()
        feeder.note_activity(True)
        hub.publish(b"\x01\x02")
        await _settle(lambda: len(link.tx_log) >= 2)  # START, frame — no close edge yet
        await feeder.stop()  # stop() sends the EOT
        return link.tx_log

    assert asyncio.run(scenario()) == [
        StreamEdge.START,
        AudioFrame(b"\x01\x02"),
        StreamEdge.END,
    ]


def test_start_stop_is_idempotent_and_leaves_no_task():
    async def scenario() -> list[asyncio.Task]:
        feeder = LinkFeeder(MockLink(), AudioHub(), acquire=_noop, release=_noop)
        await feeder.start()
        await feeder.start()  # idempotent — no second task
        assert feeder.running is True
        await feeder.stop()
        await feeder.stop()  # idempotent
        assert feeder.running is False
        return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    assert asyncio.run(scenario()) == []


# --- end-to-end: a real RxPump drives the feeder over scripted signal/silence ---------------------


class _ScriptedRadio(MockRadio):
    """A MockRadio that signals when its scripted RX drains, so a pump loop terminates (the
    `test_rx_audio.py` pattern)."""

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


def _loud(frame: AudioFrame) -> bool:
    """The `test_rx_audio.py` stand-in squelch: 0xff... = signal, everything else = silence."""
    return frame.samples[:1] == b"\xff"


def test_received_transmission_reaches_the_link_bracketed_by_one_stream():
    # The acceptance path: "speak into" the mock's RX (signal frames), then silence. The RxPump's gate
    # opens on signal and closes on the silence frame; the feeder — wired to the pump's on_activity
    # exactly as create_app wires it — delivers those frames to link.transmit bracketed by one
    # START/END. The silence frame itself is gated out (never transmitted).
    async def scenario() -> list:
        loud, quiet = AudioFrame(b"\xff\xff"), AudioFrame(b"\x00\x00")
        radio = _ScriptedRadio([loud, loud, quiet])
        hub, link = AudioHub(), MockLink()
        feeder = LinkFeeder(link, hub, acquire=_noop, release=_noop)
        await feeder.start()  # subscribes to the same hub the pump feeds
        pump = RxPump(radio, hub, poll=0, gate=_loud, on_activity=feeder.note_activity)
        pump.start()
        await radio.drained.wait()
        await _settle(lambda: link.tx_log[-1:] == [StreamEdge.END])
        await pump.stop()
        await feeder.stop()
        return link.tx_log

    assert asyncio.run(scenario()) == [
        StreamEdge.START,
        AudioFrame(b"\xff\xff"),
        AudioFrame(b"\xff\xff"),
        StreamEdge.END,
    ]


# --- integration: the squelch precondition and RX demand (TestClient) -----------------------------


def test_enable_is_refused_by_name_when_squelch_is_off():
    # The load-bearing safety rule (ADR 0044): squelch "off" has no gate edge, so the feed would never
    # end — refuse to enable, fail loud by name. The default settings have audio.squelch = "off".
    app = _app(MockLink())
    with TestClient(app) as client:
        resp = client.post("/link/enable", headers=AUTH)
        assert resp.status_code == 400
        detail = resp.json()["detail"].lower()
        assert "audio.squelch" in detail and "off" in detail
        # The link stays disabled and no feed started.
        assert client.get("/link", headers=AUTH).json()["enabled"] is False
        assert app.state.rx_pump.running is False


def test_enable_succeeds_with_a_real_squelch():
    app = _app(MockLink(), settings=make_settings({"audio.squelch": "audio"}))
    with TestClient(app) as client:
        assert client.post("/link/enable", headers=AUTH).json()["enabled"] is True


def test_the_feeder_counts_as_rx_demand():
    # Enabling the link runs the shared RxPump even with NO /audio/rx listener, and disabling releases
    # that demand — the feeder is a demand source, exactly like a browser (ADR 0044).
    app = _app(MockLink(), settings=make_settings({"audio.squelch": "audio"}))
    with TestClient(app) as client:
        assert app.state.rx_pump.running is False  # no listener, no controller → idle
        client.post("/link/enable", headers=AUTH)
        assert app.state.rx_pump.running is True and app.state.rx_demand == 1
        client.post("/link/disable", headers=AUTH)
        assert app.state.rx_pump.running is False and app.state.rx_demand == 0


def test_none_backend_enable_still_503s_and_builds_no_feeder():
    # link.backend = "none" → no link, no feeder; /link/enable 503s before any squelch check.
    app = create_app(MockRadio(supports_cat=False), api_token=TOKEN)  # link defaults to None
    with TestClient(app) as client:
        assert client.post("/link/enable", headers=AUTH).status_code == 503
    assert app.state.link_feeder is None
