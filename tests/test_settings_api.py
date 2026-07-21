"""The settings REST API + secret rotation (ADR 0026), driven through Starlette's TestClient.

Everything is schema-driven over the cycle-25 config: GET serializes the `SettingSpec` registry
with current values (and never a secret), PATCH validates atomically and round-trips ``radio.toml``,
and the two write-only endpoints rotate secrets. All endpoints are token-gated like the rest of the
API.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from radio_server.api import create_app
from radio_server.backends import MockRadio
from radio_server.config import load_secrets, load_settings

from .conftest import TEST_SECRET, make_secrets, make_settings

TOKEN = "Zx9-distinct-lan-secret-Qw7"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _client(tmp_path, *, settings=None, totp_secret=None):
    """A TestClient over a settings-API-enabled app pointed at temp config/secrets files."""
    cfg = tmp_path / "radio.toml"
    sec = tmp_path / "radio-secrets.toml"
    app = create_app(
        MockRadio(),
        api_token=TOKEN,
        settings=settings if settings is not None else make_settings({}),
        config_path=cfg,
        secrets=make_secrets(api_token=TOKEN, totp_secret=totp_secret),
        secrets_path=sec,
    )
    return TestClient(app), cfg, sec


# --- GET /settings ------------------------------------------------------------------------


def test_get_returns_schema_with_values_and_descriptions(tmp_path):
    client, _, _ = _client(tmp_path)
    body = client.get("/settings", headers=AUTH).json()
    assert body["apply"] == "restart"
    by_key = {s["key"]: s for s in body["settings"]}
    # Every registry setting is present with the render metadata the UI needs. (The slimmed
    # service catalog dropped the weather/quote/battery/bible network-service specs — ADR 0051.)
    # 75 + dvap.host/dvap.port (ADR 0095) + dstar.max_over_seconds (ADR 0097)
    # + dstar.dead_air_seconds (ADR 0106) + the 10-key [uvk5] backend block (ADR 0110-0114)
    # + uvk5.tot (the mandatory UV-K5 transmitter time-out, ADR 0117).
    assert len(by_key) == 90
    squelch = by_key["audio.squelch"]
    assert squelch["type"] == "enum"
    assert squelch["choices"] == ["off", "audio", "cat"]
    assert squelch["value"] == "off" and squelch["default"] == "off"
    # The AIOC/Baofeng backend keys (ADR 0029) render too — the PTT line is an rts/dtr enum.
    ptt_line = by_key["baofeng.ptt_line"]
    assert ptt_line["type"] == "enum"
    assert ptt_line["choices"] == ["rts", "dtr"]
    assert ptt_line["default"] == "dtr"  # confirmed on the bench (cycle 29)
    # The kv4p backend keys (ADR 0061/0063) render too, including the optional frequency (null
    # default — untyped, so it renders as a plain string field the operator fills with Hz).
    assert by_key["kv4p.squelch"]["type"] == "integer"
    assert by_key["kv4p.frequency"]["default"] is None
    assert squelch["description"]  # a real, non-empty description
    port = by_key["server.port"]
    assert port["type"] == "integer" and port["value"] == 8000
    assert by_key["station.cw_wpm"]["type"] == "number"
    assert by_key["server.mock_cat"]["type"] == "boolean" and by_key["server.mock_cat"]["value"] is True
    assert by_key["station.id_mode"]["type"] == "enum" and by_key["station.id_mode"]["choices"] == ["cw", "voice"]


def test_get_required_unset_reports_null_value_not_an_error(tmp_path):
    client, _, _ = _client(tmp_path)
    by_key = {s["key"]: s for s in client.get("/settings", headers=AUTH).json()["settings"]}
    callsign = by_key["station.callsign"]
    assert callsign["required"] is True
    assert callsign["value"] is None and callsign["default"] is None


def test_get_reports_secret_presence_only_never_a_value(tmp_path):
    client, _, _ = _client(tmp_path, totp_secret=TEST_SECRET)
    body = client.get("/settings", headers=AUTH).json()
    assert body["secrets"] == {
        "api_token": {"set": True},
        "totp_secret": {"set": True},
        "fixed_code": {"set": False},
    }
    # No secret value appears anywhere in the payload.
    dumped = json.dumps(body)
    assert TOKEN not in dumped and TEST_SECRET not in dumped


def test_get_totp_absent_when_no_secret(tmp_path):
    client, _, _ = _client(tmp_path)  # no totp secret
    body = client.get("/settings", headers=AUTH).json()
    assert body["secrets"]["totp_secret"] == {"set": False}


# --- PATCH /settings ----------------------------------------------------------------------


def test_patch_valid_persists_and_reports_restart(tmp_path):
    client, cfg, _ = _client(tmp_path)
    resp = client.patch(
        "/settings", headers=AUTH, json={"values": {"station.id_interval": 300, "audio.squelch": "audio"}}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["apply"] == "restart"
    assert sorted(body["restart_required"]) == ["audio.squelch", "station.id_interval"]
    # Persisted to the temp radio.toml — re-read confirms.
    reread = load_settings(cfg)
    assert reread.get("station.id_interval") == 300.0
    assert reread.get("audio.squelch").value == "audio"


def test_patch_preserves_the_extra_plugins_channel(tmp_path):
    # ADR 0078: PATCH rebuilds settings from schema keys only, so it MUST carry the [plugins.*] extra
    # channel (ADR 0051) through — otherwise a save strips every local plugin's config off the live
    # app.state.settings until a restart. Assert the channel survives an unrelated schema patch.
    cfg = tmp_path / "radio.toml"
    cfg.write_text('[plugins.weather]\nbase_url = "http://w/api"\n')
    client, _, _ = _client(tmp_path, settings=load_settings(cfg))
    assert client.app.state.settings.extra("weather.base_url") == "http://w/api"
    resp = client.patch("/settings", headers=AUTH, json={"values": {"station.cw_wpm": 25}})
    assert resp.status_code == 200
    assert client.app.state.settings.extra("weather.base_url") == "http://w/api"


def test_patch_preserves_hand_added_comment(tmp_path):
    cfg = tmp_path / "radio.toml"
    cfg.write_text("# operator note\n[station]\nid_interval = 120\n")
    client, _, _ = _client(tmp_path, settings=load_settings(cfg))
    client.patch("/settings", headers=AUTH, json={"values": {"station.cw_wpm": 25}})
    text = cfg.read_text()
    assert "# operator note" in text
    assert load_settings(cfg).get("station.cw_wpm") == 25.0


def test_patch_invalid_rejects_whole_patch_atomically_naming_key(tmp_path):
    client, cfg, _ = _client(tmp_path)
    # Seed the file so we can prove it is untouched by a rejected patch.
    client.patch("/settings", headers=AUTH, json={"values": {"station.cw_wpm": 22}})
    before = cfg.read_bytes()
    resp = client.patch(
        "/settings",
        headers=AUTH,
        # First value is valid, second is over the Part-97 ceiling — the WHOLE patch must reject.
        json={"values": {"station.cw_tone_hz": 700, "station.id_interval": 700}},
    )
    assert resp.status_code == 400
    assert "station.id_interval" in resp.json()["detail"]
    assert cfg.read_bytes() == before  # nothing written
    assert load_settings(cfg).get("station.cw_tone_hz") == 600.0  # the valid one did NOT land


def test_patch_rejects_unknown_key(tmp_path):
    client, _, _ = _client(tmp_path)
    resp = client.patch("/settings", headers=AUTH, json={"values": {"station.nope": 1}})
    assert resp.status_code == 400
    assert "station.nope" in resp.json()["detail"]


def test_patch_rejects_secret_key_with_rotation_hint(tmp_path):
    client, _, _ = _client(tmp_path)
    for key in ("api_token", "totp_secret"):
        resp = client.patch("/settings", headers=AUTH, json={"values": {key: "x"}})
        assert resp.status_code == 400
        assert "rotation" in resp.json()["detail"].lower()


# --- secret rotation (write-only) ---------------------------------------------------------


def test_api_token_rotate_generates_persists_and_returns_once(tmp_path):
    client, _, sec = _client(tmp_path)
    resp = client.post("/settings/secrets/api-token/rotate", headers=AUTH)
    assert resp.status_code == 200
    new = resp.json()["api_token"]
    assert new and new != TOKEN
    assert resp.json()["restart_required"] is True
    # Persisted 0600 — re-reading the secrets file confirms.
    assert load_secrets(sec, env={}).api_token == new


def test_api_token_rotate_honors_a_provided_token(tmp_path):
    client, _, sec = _client(tmp_path)
    resp = client.post(
        "/settings/secrets/api-token/rotate", headers=AUTH, json={"token": "operator-chosen-token"}
    )
    assert resp.json()["api_token"] == "operator-chosen-token"
    assert load_secrets(sec, env={}).api_token == "operator-chosen-token"


def test_totp_enroll_returns_fresh_uri_and_persists_new_secret(tmp_path):
    client, _, sec = _client(tmp_path, totp_secret=TEST_SECRET)
    resp = client.post("/settings/secrets/totp/enroll", headers=AUTH, json={"account": "AE9S"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["provisioning_uri"].startswith("otpauth://")
    assert "AE9S" in body["provisioning_uri"]
    # A fresh secret was generated (not the pre-existing one) and persisted.
    assert body["secret"] != TEST_SECRET
    assert load_secrets(sec, env={}).totp_secret == body["secret"]


# --- fixed login code (ADR 0083) --------------------------------------------------------------


def test_fixed_code_set_persists_write_only(tmp_path):
    client, _, sec = _client(tmp_path)
    resp = client.post("/settings/secrets/fixed-code", headers=AUTH, json={"code": "135790"})
    assert resp.status_code == 200
    assert resp.json() == {"set": True, "restart_required": True}
    # Persisted to the 0600 secrets channel; never echoed back in the response.
    assert load_secrets(sec, env={}).fixed_code == "135790"
    assert "135790" not in json.dumps(resp.json())


def test_fixed_code_rejects_a_non_six_digit_code(tmp_path):
    client, _, sec = _client(tmp_path)
    for bad in ("12345", "1234567", "12ab56", ""):
        resp = client.post("/settings/secrets/fixed-code", headers=AUTH, json={"code": bad})
        assert resp.status_code == 400
        assert "6 digits" in resp.json()["detail"]
    assert load_secrets(sec, env={}).fixed_code is None  # nothing was written


def test_fixed_code_presence_reflected_in_get_settings(tmp_path):
    # Presence reads the RUNNING Secrets (restart-to-apply, like the other secrets): unset by default,
    # set when a fixed code is present — and never the value itself.
    client, _, _ = _client(tmp_path)
    assert client.get("/settings", headers=AUTH).json()["secrets"]["fixed_code"] == {"set": False}

    app = create_app(
        MockRadio(),
        api_token=TOKEN,
        settings=make_settings({}),
        config_path=tmp_path / "radio.toml",
        secrets=make_secrets(api_token=TOKEN, fixed_code="135790"),
        secrets_path=tmp_path / "radio-secrets.toml",
    )
    body = TestClient(app).get("/settings", headers=AUTH).json()
    assert body["secrets"]["fixed_code"] == {"set": True}
    assert "135790" not in json.dumps(body)  # presence only, never the code


def test_fixed_code_requires_the_token(tmp_path):
    client, _, _ = _client(tmp_path)
    assert client.post("/settings/secrets/fixed-code", json={"code": "135790"}).status_code == 401


def test_totp_enroll_never_returns_the_existing_secret(tmp_path):
    client, _, _ = _client(tmp_path, totp_secret=TEST_SECRET)
    resp = client.post("/settings/secrets/totp/enroll", headers=AUTH)
    assert TEST_SECRET not in json.dumps(resp.json())


# --- the [[mumble.servers]] editor endpoints (ADR 0042) ------------------------------------


def test_mumble_servers_get_empty_when_none_configured(tmp_path):
    client, _, _ = _client(tmp_path)
    body = client.get("/settings/mumble-servers", headers=AUTH).json()
    assert body == {"servers": [], "apply": "restart"}


def test_mumble_servers_put_persists_and_get_round_trips(tmp_path):
    client, cfg, _ = _client(tmp_path)
    servers = [
        {"name": "home", "host": "mumble.example", "dtmf": "13"},
        {"name": "club_net", "host": "mumble.example", "channel": "Club Net", "tx_to_rf": False},
    ]
    resp = client.put("/settings/mumble-servers", headers=AUTH, json={"servers": servers})
    assert resp.status_code == 200
    assert resp.json()["restart_required"] is True
    # GET reflects the persisted file, fully populated (defaults resolved) + the derived slug
    # (the UI's stable key, ADR 0052) + password presence.
    body = client.get("/settings/mumble-servers", headers=AUTH).json()
    by_name = {s["name"]: s for s in body["servers"]}
    assert by_name["home"]["dtmf"] == "13" and by_name["home"]["port"] == 64738
    assert by_name["home"]["slug"] == "home"
    assert by_name["club_net"]["tx_to_rf"] is False
    assert by_name["home"]["password_set"] is False
    assert by_name["home"]["password"] == ""  # the plaintext field, empty here
    # The file itself round-trips through the raw loader with defaults omitted (a lean file);
    # the derived slug is never persisted.
    from radio_server.config import load_mumble_servers

    assert load_mumble_servers(cfg) == [
        {"name": "home", "host": "mumble.example", "dtmf": "13"},
        {"name": "club_net", "host": "mumble.example", "channel": "Club Net", "tx_to_rf": False},
    ]


def test_mumble_servers_editor_round_trip_preserves_the_plaintext_password(tmp_path):
    # ADR 0052: the plaintext `password` field (a public gate code) is part of the editor's
    # round-trip. A PUT of exactly what GET returned — slug, password_set and all — must persist
    # unchanged; a regression here silently erases passwords on every editor save.
    client, cfg, _ = _client(tmp_path)
    servers = [
        {"name": "Radio Server Demo", "host": "demo.example", "password": "gate-code"},
    ]
    client.put("/settings/mumble-servers", headers=AUTH, json={"servers": servers})
    body = client.get("/settings/mumble-servers", headers=AUTH).json()
    (row,) = body["servers"]
    assert row["name"] == "Radio Server Demo" and row["slug"] == "radio_server_demo"
    assert row["password"] == "gate-code"  # the editor sees (and must re-send) the plaintext
    # PUT the GET payload back minus the read-only presence flag (`password_set`, which the
    # editor strips); the incoming `slug` is tolerated and recomputed, so it may ride along.
    echoed = [{k: v for k, v in s.items() if k != "password_set"} for s in body["servers"]]
    resp = client.put("/settings/mumble-servers", headers=AUTH, json={"servers": echoed})
    assert resp.status_code == 200
    body = client.get("/settings/mumble-servers", headers=AUTH).json()
    assert body["servers"][0]["password"] == "gate-code"  # survived the round trip
    # Persisted TOML: the password field is written, the derived slug never is.
    from radio_server.config import load_mumble_servers

    (table,) = load_mumble_servers(cfg)
    assert table["password"] == "gate-code"
    assert "slug" not in table


def test_mumble_servers_put_rejects_bad_lists_atomically(tmp_path):
    client, cfg, _ = _client(tmp_path)
    client.put(
        "/settings/mumble-servers",
        headers=AUTH,
        json={"servers": [{"name": "home", "host": "h"}]},
    )
    before = cfg.read_bytes()
    for bad, needle in [
        # Uniqueness is on slugs (ADR 0052): an exact duplicate and a free-text alias of an
        # existing slug are the same collision.
        ([{"name": "home", "host": "h"}, {"name": "home", "host": "h2"}], "both shorten to"),
        ([{"name": "Club Net", "host": "h"}, {"name": "club_net", "host": "h2"}], "collides with"),
        ([{"name": "x", "host": "h", "dtmf": "02"}], "already bound"),  # collides with time on 02
        ([{"name": "x", "host": "h", "dtmf": "98"}], "disconnect"),  # the default disconnect combo
        ([{"name": "!!!", "host": "h"}], "letter or digit"),  # a name with no derivable slug
    ]:
        resp = client.put("/settings/mumble-servers", headers=AUTH, json={"servers": bad})
        assert resp.status_code == 400, bad
        assert needle in resp.json()["detail"]
        assert cfg.read_bytes() == before  # nothing written on rejection


def test_mumble_password_is_write_only_and_presence_reported(tmp_path):
    from radio_server.config import load_secrets

    client, _, sec = _client(tmp_path)
    client.put(
        "/settings/mumble-servers",
        headers=AUTH,
        json={"servers": [{"name": "home", "host": "h"}]},
    )
    resp = client.post(
        "/settings/mumble-servers/home/password", headers=AUTH, json={"password": "hunter2"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"set": True, "restart_required": True}
    # Landed on the 0600 secrets channel under the dynamic name; never echoed back.
    assert load_secrets(sec, env={}).get("mumble_password_home") == "hunter2"
    assert "hunter2" not in resp.text
    body = client.get("/settings/mumble-servers", headers=AUTH).json()
    assert body["servers"][0]["password_set"] is True
    assert "hunter2" not in json.dumps(body)


def test_mumble_password_path_accepts_display_name_or_slug(tmp_path):
    # ADR 0052: the path param may be the free-text display name or the derived slug — slugifying
    # either lands on the same dynamic secret (mumble_password_<slug>).
    from radio_server.config import load_secrets

    client, _, sec = _client(tmp_path)
    client.put(
        "/settings/mumble-servers",
        headers=AUTH,
        json={"servers": [{"name": "Radio Server Demo", "host": "demo.example"}]},
    )
    resp = client.post(
        "/settings/mumble-servers/Radio Server Demo/password",
        headers=AUTH,
        json={"password": "by-name"},
    )
    assert resp.status_code == 200
    assert load_secrets(sec, env={}).get("mumble_password_radio_server_demo") == "by-name"
    resp = client.post(
        "/settings/mumble-servers/radio_server_demo/password",
        headers=AUTH,
        json={"password": "by-slug"},
    )
    assert resp.status_code == 200
    assert load_secrets(sec, env={}).get("mumble_password_radio_server_demo") == "by-slug"
    # The presence flag reads the secrets channel back by slug.
    body = client.get("/settings/mumble-servers", headers=AUTH).json()
    assert body["servers"][0]["password_set"] is True


def test_mumble_password_unknown_entry_is_404_and_empty_is_400(tmp_path):
    client, _, _ = _client(tmp_path)
    resp = client.post(
        "/settings/mumble-servers/nope/password", headers=AUTH, json={"password": "x"}
    )
    assert resp.status_code == 404
    client.put(
        "/settings/mumble-servers",
        headers=AUTH,
        json={"servers": [{"name": "home", "host": "h"}]},
    )
    resp = client.post(
        "/settings/mumble-servers/home/password", headers=AUTH, json={"password": ""}
    )
    assert resp.status_code == 400


# --- token gating on every endpoint -------------------------------------------------------


def test_all_settings_endpoints_require_the_token(tmp_path):
    client, _, _ = _client(tmp_path)
    assert client.get("/settings").status_code == 401
    assert client.patch("/settings", json={"values": {"station.cw_wpm": 25}}).status_code == 401
    assert client.post("/settings/secrets/api-token/rotate").status_code == 401
    assert client.post("/settings/secrets/totp/enroll").status_code == 401
    assert client.get("/settings/mumble-servers").status_code == 401
    assert client.put("/settings/mumble-servers", json={"servers": []}).status_code == 401
    assert (
        client.post("/settings/mumble-servers/home/password", json={"password": "x"}).status_code
        == 401
    )
