"""The Mumble link REST surface: `GET /link/status` + `POST /link` over N entries (ADR 0041/0042).

Driven over `create_app` with a `MockMumbleClient` factory (the DI seam `build_app` fills with the
real `PyMumbleClient` factory). Proves the token gate, the 503-when-unconfigured posture (mirroring
`/controller`), named connect + switch semantics, the autoconnect lifespan, the `link` WS event,
and the `build_app` composition boundary (entries from `radio.toml`, per-entry password secrets).
"""

from __future__ import annotations

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from radio_server.api import build_app, create_app
from radio_server.backends import MockRadio
from radio_server.link import MockMumbleClient, resolve_mumble_entries

from .conftest import make_secrets, make_settings

TOKEN = "test-lan-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

ENTRIES = resolve_mumble_entries(
    [
        {"name": "home", "host": "mumble.example", "dtmf": "13"},
        {"name": "club_net", "host": "mumble.example", "channel": "Club Net"},
    ]
)


def _factory(clients: dict[str, MockMumbleClient] | None = None):
    """A `ClientFactory` over `MockMumbleClient`, recording each built client by entry name."""

    def factory(entry):
        client = MockMumbleClient(host=entry.host, channel=entry.channel, peers=3)
        if clients is not None:
            clients[entry.name] = client
        return client

    return factory


def _client(radio: MockRadio, **kwargs) -> TestClient:
    return TestClient(create_app(radio, api_token=TOKEN, **kwargs))


def _linked_client(radio=None, entries=ENTRIES, clients=None, **kwargs) -> TestClient:
    return _client(
        radio if radio is not None else MockRadio(),
        mumble_entries=entries,
        mumble_client_factory=_factory(clients),
        **kwargs,
    )


# --- unconfigured posture --------------------------------------------------------------------


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
    client = _linked_client()
    assert client.get("/link/status").status_code == 401
    assert client.post("/link", json={"entry": "home", "on": True}).status_code == 401


# --- named connect / switch / disconnect -----------------------------------------------------


def test_link_status_lists_every_entry():
    body = _linked_client().get("/link/status", headers=AUTH).json()["link"]
    assert body["active"] is None
    assert [e["name"] for e in body["entries"]] == ["home", "club_net"]
    home = body["entries"][0]
    assert home["host"] == "mumble.example" and home["dtmf"] == "13"
    assert home["running"] is False and home["connected"] is False


def test_connect_disconnect_a_named_entry():
    client = _linked_client()
    on = client.post("/link", headers=AUTH, json={"entry": "club_net", "on": True}).json()["link"]
    assert on["active"] == "club_net"
    by_name = {e["name"]: e for e in on["entries"]}
    assert by_name["club_net"]["running"] and by_name["club_net"]["connected"]
    assert by_name["club_net"]["peers"] == 3
    assert not by_name["home"]["running"]

    off = client.post("/link", headers=AUTH, json={"on": False}).json()["link"]
    assert off["active"] is None
    assert all(not e["running"] for e in off["entries"])


def test_connect_switches_between_entries():
    clients: dict[str, MockMumbleClient] = {}
    client = _linked_client(clients=clients)
    client.post("/link", headers=AUTH, json={"entry": "home", "on": True})
    body = client.post("/link", headers=AUTH, json={"entry": "club_net", "on": True}).json()["link"]
    assert body["active"] == "club_net"
    assert not clients["home"].status().connected  # the switch fully dropped the old link


def test_unknown_entry_is_a_404():
    resp = _linked_client().post("/link", headers=AUTH, json={"entry": "nope", "on": True})
    assert resp.status_code == 404
    assert "nope" in resp.json()["detail"]


def test_bare_on_true_needs_entry_when_ambiguous_but_not_when_sole():
    # Two entries: the caller must name one.
    resp = _linked_client().post("/link", headers=AUTH, json={"on": True})
    assert resp.status_code == 422
    # One entry: the old {on: true} still works — it is unambiguous.
    sole = resolve_mumble_entries([{"name": "home", "host": "mumble.example"}])
    client = _linked_client(entries=sole)
    body = client.post("/link", headers=AUTH, json={"on": True}).json()["link"]
    assert body["active"] == "home"


def test_status_carries_the_entries_block():
    body = _linked_client().get("/status", headers=AUTH).json()
    assert body["link"]["active"] is None
    assert len(body["link"]["entries"]) == 2


def test_per_entry_tx_to_rf_is_surfaced():
    entries = resolve_mumble_entries(
        [{"name": "monitor", "host": "h", "tx_to_rf": False}]
    )
    body = _linked_client(entries=entries).get("/link/status", headers=AUTH).json()["link"]
    assert body["entries"][0]["tx_to_rf"] is False


def test_active_entry_carries_tx_counters():
    # The bridge observability block (ADR 0045, 0049): zeroed counters on the active entry, None on
    # inactive ones — so a silent field failure shows up as `dropped_*`/`dtmf_muted` climbing in
    # /link/status.
    client = _linked_client()
    body = client.post("/link", headers=AUTH, json={"entry": "home", "on": True}).json()["link"]
    by_name = {e["name"]: e for e in body["entries"]}
    assert by_name["home"]["tx"] == {
        "frames_in": 0,
        "dropped_rx_active": 0,
        "dropped_slot_busy": 0,
        "dropped_dtmf_yield": 0,
        "overs_keyed": 0,
        "dtmf_muted": 0,
        "op_yielded": 0,
    }
    assert by_name["club_net"]["tx"] is None


# --- Browser as a Mumble client (ADR 0050): /audio/mumble/rx + /audio/mumble/tx ----------------

CANONICAL_HEADER = {"rate": 48000, "width": 2, "channels": 1}


def test_mumble_rx_streams_channel_audio_to_the_browser():
    # With a link up, a peer's voice (mock `inject`) fans out to `/audio/mumble/rx`.
    clients: dict = {}
    client = _linked_client(clients=clients)
    with client:
        client.post("/link", headers=AUTH, json={"entry": "home", "on": True})
        with client.websocket_connect(f"/audio/mumble/rx?token={TOKEN}") as ws:
            assert ws.receive_json()["status"] == "ready"  # leading format header
            clients["home"].inject(b"\xaa\xbb")
            assert bytes(ws.receive_bytes()) == b"\xaa\xbb"


def test_mumble_tx_forwards_operator_audio_to_the_channel():
    # The browser mic reaches the live Mumble sender; nothing is keyed on RF.
    radio = MockRadio()
    clients: dict = {}
    client = _linked_client(radio=radio, clients=clients)
    with client:
        client.post("/link", headers=AUTH, json={"entry": "home", "on": True})
        with client.websocket_connect(f"/audio/mumble/tx?token={TOKEN}") as ws:
            ws.send_json(CANONICAL_HEADER)
            assert ws.receive_json()["status"] == "ready"
            ws.send_bytes(b"\x01\x02")
            ws.send_bytes(b"\x03\x04")
    assert clients["home"].sent_audio == [b"\x01\x02", b"\x03\x04"]
    assert radio.tx_log == []  # Mumble-only: the radio was never keyed


def test_mumble_tx_refuses_when_no_link_active():
    # Mumble configured but no link up: the endpoint tells the client and stops (nothing to send to).
    client = _linked_client()
    with client:
        with client.websocket_connect(f"/audio/mumble/tx?token={TOKEN}") as ws:
            ws.send_json(CANONICAL_HEADER)
            assert ws.receive_json()["status"] == "ready"
            ws.send_bytes(b"\x01\x02")
            assert ws.receive_json() == {"status": "no_link"}


def test_mumble_tx_second_talker_is_busy():
    clients: dict = {}
    client = _linked_client(clients=clients)
    with client:
        client.post("/link", headers=AUTH, json={"entry": "home", "on": True})
        with client.websocket_connect(f"/audio/mumble/tx?token={TOKEN}") as ws1:
            ws1.send_json(CANONICAL_HEADER)
            ws1.receive_json()
            with client.websocket_connect(f"/audio/mumble/tx?token={TOKEN}") as ws2:
                assert ws2.receive_json() == {"status": "busy"}  # one Mumble talker at a time


def test_mumble_audio_endpoints_closed_when_unconfigured():
    # No `[[mumble.servers]]`: both endpoints refuse pre-accept (1008), like a bad token.
    client = _client(MockRadio())
    for path in ("/audio/mumble/rx", "/audio/mumble/tx"):
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect(f"{path}?token={TOKEN}") as ws:
                ws.receive_json()
        assert excinfo.value.code == 1008


# --- DTMF mute wiring (ADR 0045): controller digits -> one shared gate -> every bridge --------


def test_dtmf_mute_gate_is_wired_into_controller_and_bridge(clock):
    from .test_controller import build_ctrl

    radio, ctrl = build_ctrl(clock, [])
    app = create_app(
        radio,
        api_token=TOKEN,
        controller=ctrl,
        mumble_entries=ENTRIES,
        mumble_client_factory=_factory(),
    )
    gate = app.state.dtmf_mute
    assert gate is not None
    assert ctrl.on_digit == gate.note_digit  # decoded digits arm the mute
    with TestClient(app) as client:
        client.post("/link", headers=AUTH, json={"entry": "home", "on": True})
        assert app.state.link_manager._bridge._dtmf_mute is gate  # the SAME gate, per connect


def test_dtmf_mute_can_be_configured_off(clock):
    from .test_controller import build_ctrl

    radio, ctrl = build_ctrl(clock, [])
    app = create_app(
        radio,
        api_token=TOKEN,
        controller=ctrl,
        mumble_entries=ENTRIES,
        mumble_client_factory=_factory(),
        mumble_dtmf_mute=False,
    )
    assert app.state.dtmf_mute is None
    assert ctrl.on_digit is None  # nothing listening — the raw zero-latency relay


def test_dtmf_mute_built_without_a_controller():
    # ADR 0049: the real-time tone detector (not multimon's decode) drives muting + the yield, so
    # the gate + detector are built even with no controller (e.g. a deployment with no TOTP secret).
    app = create_app(
        MockRadio(),
        api_token=TOKEN,
        controller=None,
        mumble_entries=ENTRIES,
        mumble_client_factory=_factory(),
    )
    assert app.state.dtmf_mute is not None
    with TestClient(app) as client:
        client.post("/link", headers=AUTH, json={"entry": "home", "on": True})
        assert app.state.link_manager._bridge._tone_detector is not None


# --- connect failures surface, never a bare 500 (the missing-mumble-extra case) ---------------


class _ExplodingClient(MockMumbleClient):
    """A client whose connect raises like `PyMumbleClient` on a box without the mumble extra."""

    def connect(self):
        raise RuntimeError(
            "pymumble is not installed - install the mumble extra: uv sync --extra mumble"
        )


def test_connect_failure_returns_actionable_503_not_500():
    client = _client(
        MockRadio(),
        mumble_entries=ENTRIES,
        mumble_client_factory=lambda entry: _ExplodingClient(host=entry.host),
    )
    resp = client.post("/link", headers=AUTH, json={"entry": "home", "on": True})
    assert resp.status_code == 503
    assert "mumble extra" in resp.json()["detail"]  # the install hint passes through verbatim
    # The failed connect left no half-active link behind.
    body = client.get("/link/status", headers=AUTH).json()["link"]
    assert body["active"] is None
    assert all(not e["running"] for e in body["entries"])


def test_dtmf_side_connect_failure_publishes_an_error_link_event(clock):
    # The DTMF/on_link path applies the connect as a background task; a failure must land in the
    # event stream (with the full block so the card refreshes), not just the server log. Drive
    # the REAL wiring: a controller with the link combos, its `on_link` bound by create_app, the
    # combo fired through the /services trigger seam (the same `_run_command` the RF path uses).
    from .test_controller import SilentDecoder, build_ctrl

    radio, ctrl = build_ctrl(clock, [], decoder=SilentDecoder(), mumble_entries=ENTRIES)
    app = create_app(
        radio,
        api_token=TOKEN,
        controller=ctrl,
        mumble_entries=ENTRIES,
        mumble_client_factory=lambda entry: _ExplodingClient(host=entry.host),
    )
    client = TestClient(app)
    with client.websocket_connect(f"/events?token={TOKEN}") as ws:
        ws.receive_json()  # the initial status snapshot
        client.post("/services/13", headers=AUTH)  # the "home" combo, via the trigger seam
        # First the command record (via: "dtmf"-equivalent trigger), then the error transition.
        seen = []
        for _ in range(6):
            event = ws.receive_json()
            seen.append(event)
            if event["type"] == "link" and event["data"].get("state") == "error":
                break
        else:
            raise AssertionError(f"no error link event; saw: {[e['type'] for e in seen]}")
        data = event["data"]
        assert data["entry"] == "home"
        assert "mumble extra" in data["detail"]
        assert data["active"] is None  # the full block rides along so the card refreshes
        assert [e["name"] for e in data["entries"]] == ["home", "club_net"]


# --- autoconnect lifespan + the link WS event -------------------------------------------------


def test_autoconnect_entry_connects_on_startup_and_stops_on_shutdown():
    entries = resolve_mumble_entries(
        [
            {"name": "home", "host": "h1", "autoconnect": True},
            {"name": "club_net", "host": "h2"},
        ]
    )
    clients: dict[str, MockMumbleClient] = {}
    app = create_app(
        MockRadio(),
        api_token=TOKEN,
        mumble_entries=entries,
        mumble_client_factory=_factory(clients),
    )
    with TestClient(app) as client:  # context manager runs the lifespan
        body = client.get("/link/status", headers=AUTH).json()["link"]
        assert body["active"] == "home"
    assert not clients["home"].status().connected  # shutdown dropped the link


def test_link_transitions_publish_ws_events():
    client = _linked_client()
    with client.websocket_connect(f"/events?token={TOKEN}") as ws:
        ws.receive_json()  # the initial status snapshot
        client.post("/link", headers=AUTH, json={"entry": "home", "on": True})
        event = ws.receive_json()
        assert event["type"] == "link"
        assert event["data"]["entry"] == "home" and event["data"]["state"] == "connected"
        # The event carries the full link block — WS status frames are RadioStatus-only, so this
        # is the push channel the web card folds into its state.
        assert event["data"]["active"] == "home"
        assert [e["name"] for e in event["data"]["entries"]] == ["home", "club_net"]


# --- build_app composition boundary (ADR 0042) ------------------------------------------------


def test_build_app_resolves_entries_and_per_entry_passwords(tmp_path):
    # The real composition root: entries come from [[mumble.servers]] in radio.toml, the client
    # factory builds a PyMumbleClient per entry, and each entry's password comes from its own
    # dynamic secret (mumble_password_<name>) — never radio.toml.
    from radio_server.link import PyMumbleClient

    cfg = tmp_path / "radio.toml"
    cfg.write_text(
        '[logging]\npath = "%s"\n'
        '[[mumble.servers]]\nname = "home"\nhost = "mumble.example"\nchannel = "Ham Net"\n'
        '[[mumble.servers]]\nname = "club_net"\nhost = "mumble.example"\n'
        % (tmp_path / "log.jsonl")
    )
    from radio_server.config import load_settings

    app = build_app(
        load_settings(cfg),
        make_secrets(api_token=TOKEN, mumble_password_home="hunter2"),
        config_path=cfg,
    )
    manager = app.state.link_manager
    assert manager is not None
    assert [e.name for e in manager.entries] == ["home", "club_net"]
    home_client = manager._client_factory(manager.entries[0])
    assert isinstance(home_client, PyMumbleClient)
    assert home_client._host == "mumble.example" and home_client._channel == "Ham Net"
    assert home_client._password == "hunter2"  # the per-entry secret landed
    club_client = manager._client_factory(manager.entries[1])
    assert club_client._password == ""  # no secret for this entry -> passwordless connect
    # No callsign configured (a bench/mock app) -> the bare default nick.
    assert home_client._username == "radio-server"


def test_build_app_nick_is_the_callsign_on_every_entry(tmp_path):
    # The nick is not per-entry config: with station.callsign set, every server sees the
    # station identify as the licensee — "<CALLSIGN> (radio-server)".
    cfg = tmp_path / "radio.toml"
    cfg.write_text(
        '[logging]\npath = "%s"\n'
        '[station]\ncallsign = "AE9S"\n'
        '[[mumble.servers]]\nname = "home"\nhost = "mumble.example"\n'
        '[[mumble.servers]]\nname = "club_net"\nhost = "mumble.example"\n'
        % (tmp_path / "log.jsonl")
    )
    from radio_server.config import load_settings

    app = build_app(load_settings(cfg), make_secrets(api_token=TOKEN), config_path=cfg)
    manager = app.state.link_manager
    for entry in manager.entries:
        assert manager._client_factory(entry)._username == "AE9S (radio-server)"


def test_build_app_fails_loud_on_a_bad_entry(tmp_path):
    cfg = tmp_path / "radio.toml"
    cfg.write_text(
        '[logging]\npath = "%s"\n'
        '[[mumble.servers]]\nname = "Bad Name"\nhost = "h"\n' % (tmp_path / "log.jsonl")
    )
    from radio_server.config import load_settings

    with pytest.raises(RuntimeError, match="name"):
        build_app(load_settings(cfg), make_secrets(api_token=TOKEN), config_path=cfg)


def test_build_app_without_entries_has_no_link_surface(tmp_path):
    settings = make_settings({"logging.path": str(tmp_path / "log.jsonl")})
    app = build_app(settings, make_secrets(api_token=TOKEN))
    assert app.state.link_manager is None
