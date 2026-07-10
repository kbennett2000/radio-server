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
    # Every registry setting is present with the render metadata the UI needs.
    assert len(by_key) == 31
    squelch = by_key["audio.squelch"]
    assert squelch["type"] == "enum"
    assert squelch["choices"] == ["off", "audio", "cat"]
    assert squelch["value"] == "off" and squelch["default"] == "off"
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
    assert body["secrets"] == {"api_token": {"set": True}, "totp_secret": {"set": True}}
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


def test_totp_enroll_never_returns_the_existing_secret(tmp_path):
    client, _, _ = _client(tmp_path, totp_secret=TEST_SECRET)
    resp = client.post("/settings/secrets/totp/enroll", headers=AUTH)
    assert TEST_SECRET not in json.dumps(resp.json())


# --- token gating on every endpoint -------------------------------------------------------


def test_all_settings_endpoints_require_the_token(tmp_path):
    client, _, _ = _client(tmp_path)
    assert client.get("/settings").status_code == 401
    assert client.patch("/settings", json={"values": {"station.cw_wpm": 25}}).status_code == 401
    assert client.post("/settings/secrets/api-token/rotate").status_code == 401
    assert client.post("/settings/secrets/totp/enroll").status_code == 401
