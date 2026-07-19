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


def test_dstar_bridge_starts_and_stops_over_the_lifespan():
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
    with TestClient(app):
        bridge = app.state.dstar_bridge
        assert bridge is not None and bridge.running
        assert bridge.status().registered  # the gateway endpoint registered on boot
        assert bridge.mode == "idle"
    # After the lifespan exits, the bridge is stopped.
    assert not app.state.dstar_bridge.running
