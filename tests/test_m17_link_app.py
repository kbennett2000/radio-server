"""App-level composition of the M17 Link backend (ADR 0052), through ``build_app``.

Proves the config → ``create_link("m17")`` → ``M17Link`` path the composition root wires: an
``link.backend = "m17"`` deployment boots a real M17 link that is **disabled** (ADR 0041), advertises
no directory (``GET /link/directory`` → **501 by name**), and — composed straight from settings —
completes the CONN→ACKN handshake against a localhost fake reflector. Because a real ``M17Link``
constructs the real ``Codec2`` seam at build time, these are skip-gated on ``libcodec2`` exactly like
the cycle-54 build checks; nothing here touches a real reflector.
"""

from __future__ import annotations

import asyncio
from ctypes.util import find_library

import pytest
from fastapi.testclient import TestClient

from radio_server.api import build_app

from .conftest import make_secrets, make_settings
from .test_m17_link import FakeReflector, _await_connected

_CODEC2_SKIP = pytest.mark.skipif(
    find_library("codec2") is None,
    reason="libcodec2 not installed; build_app('m17') constructs the real Codec2 seam",
)

TOKEN = "test-lan-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SECRETS = make_secrets(api_token=TOKEN)


def _m17_settings(tmp_path, **overrides):
    base = {
        "logging.path": str(tmp_path / "log.jsonl"),
        "server.backend": "mock",
        "link.backend": "m17",
        "link.reflector_host": "127.0.0.1",
        "link.bind_host": "127.0.0.1",
        "station.callsign": "KE0ABC",  # REUSED as the M17 source — no second callsign
    }
    base.update(overrides)
    return make_settings(base)


@_CODEC2_SKIP
def test_build_app_boots_m17_link_disabled(tmp_path):
    app = build_app(_m17_settings(tmp_path), SECRETS)
    link = app.state.link
    assert link is not None
    assert link.backend_name == "m17"
    # The load-bearing safety property: composed straight from config, it comes up DISABLED.
    assert link.status().enabled is False
    assert link.status().connected is False


@_CODEC2_SKIP
def test_build_app_m17_directory_501s_by_name(tmp_path):
    app = build_app(_m17_settings(tmp_path), SECRETS)
    with TestClient(app) as client:
        status = client.get("/link", headers=AUTH).json()
        assert status["backend"] == "m17" and status["enabled"] is False
        resp = client.get("/link/directory", headers=AUTH)
        assert resp.status_code == 501
        assert "directory" in resp.json()["detail"]


@_CODEC2_SKIP
def test_composed_m17_link_completes_handshake_against_fake_reflector(tmp_path):
    async def scenario():
        fake = await FakeReflector().start()
        settings = _m17_settings(tmp_path, **{"link.reflector_port": fake.addr[1]})
        app = build_app(settings, SECRETS)
        link = app.state.link
        assert link.backend_name == "m17"
        assert link.status().enabled is False  # boots disabled even after compose
        try:
            link.connect("ref")
            await _await_connected(link)
            assert link.status().connected is True
        finally:
            link.disconnect()
            fake.close()

    asyncio.run(scenario())
