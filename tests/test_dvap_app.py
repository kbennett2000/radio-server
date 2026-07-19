"""The DVAP control wiring in create_app + the /dvap/* routes (ADR 0095, PR 2).

Proves the OFF-by-default gate (no ``[[dvap.modules]]`` → no manager, ``dvap`` null everywhere) and,
with modules + an injected ``MockRemoteControlClient`` (which models a tiny gateway), the full
link/status/unlink flow, confirmed-state readback, error mapping, and graceful degrade when the gateway
is unreachable — all with no socket and no gateway.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from radio_server.api import create_app
from radio_server.backends import MockRadio
from radio_server.dstar import DvapModule, MockRemoteControlClient

TOKEN = "test-lan-secret"
MODULES = [DvapModule("B", "DVAP 70cm #1", 441_600_000), DvapModule("C", "DVAP 70cm #2", 441_000_000)]


def _auth() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


def _app(client=None):
    client = client if client is not None else MockRemoteControlClient()
    app = create_app(
        MockRadio(),
        api_token=TOKEN,
        dvap_client_factory=lambda: client,
        dvap_modules=MODULES,
        dvap_station_callsign="AE9S",
        dvap_remote_host="127.0.0.1",
        dvap_remote_port=10022,
    )
    return app, client


def test_dvap_off_by_default():
    app = create_app(MockRadio(), api_token=TOKEN)
    with TestClient(app) as client:
        assert app.state.dvap_manager is None
        assert client.get("/status", headers=_auth()).json()["dvap"] is None
        assert client.get("/dvap/status", headers=_auth()).json()["dvap"] is None
        # Link on an unconfigured deployment is a clean 503.
        r = client.post("/dvap/link", json={"module": "B", "reflector": "REF001 C"}, headers=_auth())
        assert r.status_code == 503


def test_dvap_status_lists_configured_modules():
    app, _ = _app()
    with TestClient(app) as client:
        block = client.get("/dvap/status", headers=_auth()).json()["dvap"]
        assert block["configured"] is True
        assert block["remote"] == {"host": "127.0.0.1", "port": 10022}
        assert [(m["module"], m["frequency_hz"]) for m in block["modules"]] == [
            ("B", 441_600_000),
            ("C", 441_000_000),
        ]
        assert all(m["reachable"] and not m["linked"] for m in block["modules"])


def test_dvap_link_then_unlink_round_trip():
    app, remote = _app()
    with TestClient(app) as client:
        r = client.post("/dvap/link", json={"module": "B", "reflector": "REF001 C"}, headers=_auth())
        assert r.status_code == 200
        b = r.json()["dvap"]["modules"][0]
        assert b["module"] == "B" and b["linked"] and b["reflector"] == "REF001 C"
        # the gateway saw the module-B callsign field.
        assert remote.linked == {"AE9S   B": "REF001 C"}
        # /status carries the same confirmed block (from cache, no extra gateway read).
        assert client.get("/status", headers=_auth()).json()["dvap"]["modules"][0]["linked"] is True

        r = client.post("/dvap/unlink", json={"module": "B"}, headers=_auth())
        assert r.status_code == 200
        assert r.json()["dvap"]["modules"][0]["linked"] is False
        assert remote.linked == {}


def test_dvap_link_unknown_module_is_404():
    app, _ = _app()
    with TestClient(app) as client:
        r = client.post("/dvap/link", json={"module": "Z", "reflector": "REF001 C"}, headers=_auth())
        assert r.status_code == 404


def test_dvap_link_bad_reflector_is_422():
    app, _ = _app()
    with TestClient(app) as client:
        r = client.post("/dvap/link", json={"module": "B", "reflector": "REF"}, headers=_auth())
        assert r.status_code == 422


def test_dvap_link_when_gateway_rejects_auth_is_503():
    app, _ = _app(MockRemoteControlClient(fail_auth=True))
    with TestClient(app) as client:
        r = client.post("/dvap/link", json={"module": "B", "reflector": "REF001 C"}, headers=_auth())
        assert r.status_code == 503


def test_dvap_status_degrades_when_gateway_unreachable():
    # An unreachable gateway must not fail the status call — modules report reachable=false.
    app, _ = _app(MockRemoteControlClient(fail_auth=True))
    with TestClient(app) as client:
        block = client.get("/dvap/status", headers=_auth()).json()["dvap"]
        assert all(m["reachable"] is False for m in block["modules"])
