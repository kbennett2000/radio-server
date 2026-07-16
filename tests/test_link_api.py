"""The Mumble link REST surface: `GET /link/status` + `POST /link` (ADR 0041).

Driven over `create_app` with an injected `MockMumbleClient` (the DI seam `build_app` uses for the
real client). Proves the token gate, the 503-when-unconfigured posture (mirroring `/controller`),
the connect/disconnect toggle, and that `/status` carries the link block.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from radio_server.api import build_app, create_app
from radio_server.backends import MockRadio
from radio_server.link import MockMumbleClient

from .conftest import make_secrets, make_settings

TOKEN = "test-lan-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _client(radio: MockRadio, **kwargs) -> TestClient:
    return TestClient(create_app(radio, api_token=TOKEN, **kwargs))


def test_link_status_null_when_unconfigured():
    body = _client(MockRadio()).get("/link/status", headers=AUTH).json()
    assert body == {"link": None}


def test_status_carries_a_null_link_block_when_unconfigured():
    body = _client(MockRadio()).get("/status", headers=AUTH).json()
    assert body["link"] is None


def test_post_link_503_when_unconfigured():
    resp = _client(MockRadio()).post("/link", headers=AUTH, json={"on": True})
    assert resp.status_code == 503


def test_link_requires_token():
    client = _client(MockRadio(), mumble_client=MockMumbleClient())
    assert client.get("/link/status").status_code == 401
    assert client.post("/link", json={"on": True}).status_code == 401


def test_link_connect_and_disconnect():
    client = _client(MockRadio(), mumble_client=MockMumbleClient(host="mumble.example", peers=3))
    # Configured but not autostarted: present and disconnected.
    before = client.get("/link/status", headers=AUTH).json()["link"]
    assert before["running"] is False and before["connected"] is False
    assert before["tx_to_rf"] is True

    on = client.post("/link", headers=AUTH, json={"on": True}).json()["link"]
    assert on["running"] is True and on["connected"] is True
    assert on["host"] == "mumble.example" and on["peers"] == 3

    off = client.post("/link", headers=AUTH, json={"on": False}).json()["link"]
    assert off["running"] is False and off["connected"] is False


def test_status_reflects_link_block_when_configured():
    client = _client(MockRadio(), mumble_client=MockMumbleClient(), mumble_tx_to_rf=False)
    body = client.get("/status", headers=AUTH).json()
    assert body["link"] is not None
    assert body["link"]["tx_to_rf"] is False  # receive-only wiring surfaced in status


# --- build_app composition boundary (ADR 0041 Cycle C) -------------------------------------

def test_build_app_fails_loud_when_link_enabled_without_host(tmp_path):
    settings = make_settings(
        {"mumble.enabled": True, "logging.path": str(tmp_path / "log.jsonl")}
    )
    with pytest.raises(RuntimeError, match="mumble.host"):
        build_app(settings, make_secrets(api_token=TOKEN))


def test_build_app_link_client_not_implemented_yet(tmp_path):
    # The bridge + wiring are complete; the real pymumble client is a later bring-up cycle, so
    # enabling the link with a host fails loud (the SignaLinkV71 stub posture) rather than silently
    # doing nothing.
    settings = make_settings(
        {
            "mumble.enabled": True,
            "mumble.host": "mumble.example",
            "logging.path": str(tmp_path / "log.jsonl"),
        }
    )
    with pytest.raises(NotImplementedError):
        build_app(settings, make_secrets(api_token=TOKEN))
