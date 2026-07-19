"""The D-STAR link's composition-root wiring in create_app (ADR 0087).

Proves the OFF-by-default gate (no `dstar.callsign` → no bridge, no factory) and that, when a callsign
and injected fakes are supplied, the lifespan constructs and starts the bridge and stops it on
shutdown — all with a `MockGatewayClient` + a fake vocoder, no gateway and no DV Dongle.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from radio_server.api import create_app
from radio_server.audio import AudioFrame
from radio_server.backends import MockRadio
from radio_server.dstar import MockGatewayClient
from radio_server.vocoder.base import AMBE_BYTES_PER_FRAME, PCM_FORMAT

TOKEN = "test-lan-secret"


class _FakeVocoder:
    def encode(self, frame: AudioFrame) -> bytes:
        return bytes(AMBE_BYTES_PER_FRAME)

    def decode(self, ambe: bytes) -> AudioFrame:
        return AudioFrame(b"\x00\x00" * 160, PCM_FORMAT)

    def close(self) -> None:
        pass


def test_dstar_off_by_default():
    app = create_app(MockRadio(), api_token=TOKEN)
    with TestClient(app):
        assert app.state.dstar_bridge_factory is None
        assert app.state.dstar_bridge is None


def test_dstar_not_built_without_callsign_even_with_factories():
    # Factories present but no callsign → still off (the callsign is the gate).
    app = create_app(
        MockRadio(),
        api_token=TOKEN,
        dstar_gateway_factory=MockGatewayClient,
        dstar_vocoder_factory=_FakeVocoder,
    )
    with TestClient(app):
        assert app.state.dstar_bridge_factory is None


def test_dstar_bridge_built_but_idle_until_a_reflector_is_linked():
    # ADR 0089: the bridge is built at boot but does NOT hold the shared DV Dongle — it starts
    # (registers + opens the vocoder) only when a reflector is linked, and stops (releasing the dongle
    # for the other radio instance) on unlink.
    built = {}

    def gateway_factory():
        built["gateway"] = MockGatewayClient()
        return built["gateway"]

    app = create_app(
        MockRadio(),
        api_token=TOKEN,
        dstar_gateway_factory=gateway_factory,
        dstar_vocoder_factory=_FakeVocoder,
        dstar_callsign="AE9S",
        dstar_module="A",
    )
    assert app.state.dstar_bridge_factory is not None
    with TestClient(app) as client:
        bridge = app.state.dstar_bridge
        assert bridge is not None
        assert not bridge.running  # built, but not holding the dongle
        assert not bridge.status().registered
        client.post("/dstar/link", json={"reflector": "REF001 C"}, headers=_auth(client))
        assert bridge.running and bridge.status().registered  # acquired on link
        client.post("/dstar/unlink", headers=_auth(client))
        assert not bridge.running  # released on unlink
    assert not app.state.dstar_bridge.running


# --- Reflector control + browser audio endpoints (ADR 0088) --------------------------

FRAME = b"\x01\x00" * 960  # one canonical 20 ms frame (48 kHz / s16 / mono)


def _dstar_app(gateway_box: dict, **kwargs):
    def gateway_factory():
        gateway_box["gateway"] = MockGatewayClient()
        return gateway_box["gateway"]

    return create_app(
        MockRadio(),
        api_token=TOKEN,
        dstar_gateway_factory=gateway_factory,
        dstar_vocoder_factory=_FakeVocoder,
        dstar_callsign="AE9S",
        dstar_module="A",
        **kwargs,
    )


def _auth(client):
    return {"Authorization": f"Bearer {TOKEN}"}


def test_dstar_status_block_null_when_off():
    app = create_app(MockRadio(), api_token=TOKEN)
    with TestClient(app) as client:
        assert client.get("/status", headers=_auth(client)).json()["dstar"] is None
        assert client.get("/dstar/status", headers=_auth(client)).json()["dstar"] is None


def test_dstar_link_sends_urcall_and_tracks_state():
    box: dict = {}
    app = _dstar_app(box)
    with TestClient(app) as client:
        resp = client.post("/dstar/link", json={"reflector": "REF001 C"}, headers=_auth(client))
        assert resp.status_code == 200
        assert resp.json()["dstar"]["active"]["reflector"] == "REF001 C"
        # The link command reached the gateway as a header carrying the link URCALL.
        from radio_server.dstar import dsrp, header

        headers = [m for m in box["gateway"].sent if m.kind is dsrp.MessageKind.HEADER]
        assert headers and header.parse_header(headers[-1].radio_header).ur == "REF001CL"
        # And it shows up in the shared /status block.
        assert client.get("/status", headers=_auth(client)).json()["dstar"]["active"]["module"] == "C"


def test_dstar_unlink_clears_state():
    box: dict = {}
    app = _dstar_app(box)
    with TestClient(app) as client:
        client.post("/dstar/link", json={"reflector": "REF030 C"}, headers=_auth(client))
        resp = client.post("/dstar/unlink", headers=_auth(client))
        assert resp.status_code == 200
        assert resp.json()["dstar"]["active"] is None


def test_dstar_link_rejects_bad_reflector_422():
    box: dict = {}
    app = _dstar_app(box)
    with TestClient(app) as client:
        resp = client.post("/dstar/link", json={"reflector": "REF001"}, headers=_auth(client))
        assert resp.status_code == 422  # missing module letter


def test_dstar_link_503_when_unconfigured():
    app = create_app(MockRadio(), api_token=TOKEN)
    with TestClient(app) as client:
        resp = client.post("/dstar/link", json={"reflector": "REF001 C"}, headers=_auth(client))
        assert resp.status_code == 503


def test_dstar_rx_ws_sends_ready_then_reflector_audio():
    box: dict = {}
    app = _dstar_app(box)
    with TestClient(app) as client:
        with client.websocket_connect(f"/audio/dstar/rx?token={TOKEN}") as ws:
            ready = ws.receive_json()
            assert ready["status"] == "ready" and ready["format"]["rate"] == 48000
            # Publishing on the bridge's rx hub (as a decoded reflector frame would) reaches the browser.
            app.state.dstar_rx_hub.publish(FRAME)
            assert ws.receive_bytes() == FRAME


def test_dstar_tx_ws_encodes_browser_audio_to_the_reflector():
    box: dict = {}
    app = _dstar_app(box)
    from radio_server.dstar import dsrp

    with TestClient(app) as client:
        # The browser mic only feeds the reflector while a reflector is linked (ADR 0089) — link first.
        client.post("/dstar/link", json={"reflector": "REF001 C"}, headers=_auth(client))
        before = len(box["gateway"].sent)
        with client.websocket_connect(f"/audio/dstar/tx?token={TOKEN}") as ws:
            ws.send_json({"rate": 48000, "width": 2, "channels": 1})
            assert ws.receive_json()["status"] == "ready"
            ws.send_bytes(FRAME)
        # Closing the socket terminates the over: a voice header opened it and an end frame closed it.
        kinds = [m.kind for m in box["gateway"].sent[before:]]
        assert dsrp.MessageKind.HEADER in kinds
        assert box["gateway"].sent[-1].end


def test_dstar_link_busy_dongle_is_503():
    # When the shared DV Dongle is held by the other radio instance, start() raises VocoderUnavailable
    # and /dstar/link surfaces 503 "unavailable" (ADR 0089), not a 500.
    from radio_server.vocoder.base import VocoderUnavailable

    def busy_vocoder():
        raise VocoderUnavailable("in use by the other radio")

    box: dict = {}

    def gateway_factory():
        box["gateway"] = MockGatewayClient()
        return box["gateway"]

    app = create_app(
        MockRadio(),
        api_token=TOKEN,
        dstar_gateway_factory=gateway_factory,
        dstar_vocoder_factory=busy_vocoder,
        dstar_callsign="AE9S",
        dstar_module="A",
    )
    with TestClient(app) as client:
        resp = client.post("/dstar/link", json={"reflector": "REF001 C"}, headers=_auth(client))
        assert resp.status_code == 503


def test_dstar_status_carries_recent_activity():
    box: dict = {}
    app = _dstar_app(box)
    with TestClient(app) as client:
        client.post("/dstar/link", json={"reflector": "REF001 C"}, headers=_auth(client))
        # An inbound reflector over from K1ABC → an activity entry on the bridge → the /status ring.
        from radio_server.dstar import dsrp, header

        hdr = header.build_voice_header(callsign="K1ABC", module="A", ur="CQCQCQ")
        box["gateway"].inject(dsrp.build_header_packet(hdr, 0x0321))
        import time

        deadline = time.time() + 1.0
        activity = []
        while time.time() < deadline:
            activity = client.get("/dstar/status", headers=_auth(client)).json()["dstar"]["activity"]
            if activity:
                break
            time.sleep(0.02)
        assert any(a.get("mycall") == "K1ABC" and a.get("dir") == "rx" for a in activity)


def test_dstar_ws_rejects_bad_token():
    box: dict = {}
    app = _dstar_app(box)
    with TestClient(app) as client:
        import pytest
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/audio/dstar/rx?token=wrong") as ws:
                ws.receive_json()
