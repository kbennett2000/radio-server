"""The /link REST surface — config, composition, and the enable lifecycle (ADR 0042).

Driven through Starlette's ``TestClient`` over ``create_app`` with a ``MockLink`` injected through the
same DI seam the rest of the API suite uses — no server binds, no network. These tests prove:

- the bearer gate is enforced on every ``/link`` route;
- ``GET /link`` reports the ``LinkStatus``; enable/disable/connect/disconnect drive it over HTTP;
- ``GET /link/directory`` returns entries when supported and **501s by name** when it is not
  (guardrail 3);
- ``link = None`` (the ``link.backend = "none"`` deployment) makes every route **503**, not a crash;
- **the safety property (ADR 0041):** the app always boots with the link DISABLED — even with
  ``controller.autostart`` on and the controller actually autostarting, no startup path enables it;
- the lifecycle publishes ``link_*`` ledger records carrying only whitelisted fields.
"""

import json

import pytest
from fastapi.testclient import TestClient

from radio_server.api import create_app
from radio_server.backends import MockRadio
from radio_server.eventlog import EventLog, JsonlSink
from radio_server.link import MockLink, Station

from .conftest import make_settings
from .test_controller import SilentDecoder, build_ctrl

TOKEN = "test-lan-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _app(link, **kwargs):
    return create_app(MockRadio(supports_cat=False), api_token=TOKEN, link=link, **kwargs)


def _client(link, **kwargs) -> TestClient:
    return TestClient(_app(link, **kwargs))


# The full route surface, as (method, path, json-body-or-None) — used by the auth and 503 sweeps.
ROUTES = [
    ("get", "/link", None),
    ("post", "/link/enable", None),
    ("post", "/link/disable", None),
    ("post", "/link/connect", {"target": "M17-USA C"}),
    ("post", "/link/disconnect", None),
    ("get", "/link/directory", None),
]


# --- auth ----------------------------------------------------------------------------------------


def _call(client, method, path, body, **kwargs):
    # httpx's `.get()` rejects a `json=` kwarg, so only pass a body when there is one.
    if body is not None:
        kwargs["json"] = body
    return getattr(client, method)(path, **kwargs)


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_every_route_requires_the_bearer_token(method, path, body):
    resp = _call(_client(MockLink()), method, path, body)
    assert resp.status_code == 401


# --- GET /link: the status surface ---------------------------------------------------------------


def test_get_link_reports_status_shape():
    resp = _client(MockLink()).get("/link", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "backend": "mock",
        "enabled": False,
        "connected": False,
        "target": None,
        "stations": [],
        "talker": None,
    }


def test_get_link_reflects_stations_and_talker():
    link = MockLink(stations=[Station("AE9S"), Station("K1ABC")], talker=Station("AE9S"))
    body = _client(link).get("/link", headers=AUTH).json()
    assert body["stations"] == [{"callsign": "AE9S"}, {"callsign": "K1ABC"}]
    assert body["talker"] == {"callsign": "AE9S"}


# --- the enable lifecycle ------------------------------------------------------------------------


def test_enable_then_disable_over_http():
    link = MockLink()
    # Enable requires a real squelch (ADR 0044): audio.squelch="off" (the default) is refused because
    # a gate that never closes would feed the receiver's noise floor to peers forever.
    client = _client(link, settings=make_settings({"audio.squelch": "audio"}))
    assert client.get("/link", headers=AUTH).json()["enabled"] is False

    assert client.post("/link/enable", headers=AUTH).json()["enabled"] is True
    assert client.get("/link", headers=AUTH).json()["enabled"] is True

    assert client.post("/link/disable", headers=AUTH).json()["enabled"] is False
    assert client.get("/link", headers=AUTH).json()["enabled"] is False


def test_connect_then_disconnect_over_http():
    client = _client(MockLink())
    body = client.post("/link/connect", json={"target": "M17-USA C"}, headers=AUTH).json()
    assert body["connected"] is True and body["target"] == "M17-USA C"

    body = client.post("/link/disconnect", headers=AUTH).json()
    assert body["connected"] is False and body["target"] is None


# --- GET /link/directory: supported + 501-by-name ------------------------------------------------


def test_directory_returns_entries_when_supported():
    link = MockLink(directory_entries=(Station("AE9S"), Station("K1ABC")))
    resp = _client(link).get("/link/directory", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == [{"callsign": "AE9S"}, {"callsign": "K1ABC"}]


def test_directory_501s_by_name_when_unsupported():
    # M17-shaped: no central directory. The route must name the missing capability, never fake an
    # empty list (guardrail 3).
    resp = _client(MockLink(directory=False)).get("/link/directory", headers=AUTH)
    assert resp.status_code == 501
    assert "directory" in resp.json()["detail"]


# --- link.backend = "none" -> unavailable, not a crash -------------------------------------------


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_unwired_link_returns_503(method, path, body):
    # The `link.backend = "none"` deployment: app.state.link is None, so every route 503s with the
    # same fail-loud shape as an unwired controller — never a silent no-op.
    resp = _call(_client(None), method, path, body, headers=AUTH)
    assert resp.status_code == 503
    assert resp.json()["detail"] == "link not configured in this deployment"


# --- the safety property: the app always boots DISABLED ------------------------------------------


def test_link_is_disabled_before_any_request():
    app = _app(MockLink())
    assert app.state.link.status().enabled is False


def test_boot_never_enables_the_link_even_with_autostart(clock):
    # The load-bearing invariant (ADR 0041): there is no startup path from boot to enabled. Boot with
    # a link present AND controller.autostart on AND a controller that genuinely autostarts — the
    # controller comes up running, and the link is STILL disabled.
    radio = MockRadio()
    _, ctrl = build_ctrl(clock, [], radio=radio, decoder=SilentDecoder())
    app = create_app(
        radio,
        api_token=TOKEN,
        controller=ctrl,
        link=MockLink(),
        settings=make_settings({"controller.autostart": True}),
    )
    with TestClient(app) as client:  # the context manager runs the lifespan (startup autostart)
        # The controller really did autostart...
        assert client.get("/status", headers=AUTH).json()["controller"]["running"] is True
        # ...and the link is still disabled: nothing at boot touched it.
        assert app.state.link.status().enabled is False
        assert client.get("/link", headers=AUTH).json()["enabled"] is False


# --- the lifecycle reaches the ledger ------------------------------------------------------------


def test_lifecycle_writes_link_records_to_the_ledger(tmp_path):
    # End-to-end through create_app with a live EventLog: the routes publish to the hub, the drain
    # task writes records, and context exit flushes them (the test_event_log_wiring pattern).
    log_path = tmp_path / "link.jsonl"
    event_log = EventLog(JsonlSink(log_path))
    # A real squelch so POST /link/enable is accepted (ADR 0044).
    app = _app(MockLink(), event_log=event_log, settings=make_settings({"audio.squelch": "audio"}))

    with TestClient(app) as client:
        client.post("/link/enable", headers=AUTH)
        client.post("/link/connect", json={"target": "M17-USA C"}, headers=AUTH)
        client.post("/link/disconnect", headers=AUTH)
        client.post("/link/disable", headers=AUTH)
    # Context exit ran lifespan shutdown: every queued record is flushed.

    text = log_path.read_text(encoding="utf-8")
    records = [json.loads(line) for line in text.splitlines()]
    # Enabling the link now takes RX demand (ADR 0044: the feeder is a demand source), so the RxPump
    # spins up and the arbiter emits `arbiter_mode` records interleaved with these. Filter to the link
    # records — this test is about the link lifecycle, and their order/whitelist is what matters.
    link_types = [r["type"] for r in records if r["type"].startswith("link_")]
    assert link_types == ["link_enabled", "link_connected", "link_disconnected", "link_disabled"]

    connected = next(r for r in records if r["type"] == "link_connected")
    assert connected["target"] == "M17-USA C"
    # Whitelist discipline: the disable/enable/disconnect records carry no target.
    assert "target" not in next(r for r in records if r["type"] == "link_enabled")
