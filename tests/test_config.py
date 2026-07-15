"""The config system: schema resolution, fail-loud validation, round-trip save, and secrets (ADR 0025).

The load-bearing invariant is **behavior-preserving**: with no config file present, every default
equals today's default (proven per group below), so the rest of the suite still passes. The rest
covers the reversal's new surface — file overrides, fail-loud-at-load, the lazy-required rule, the
tomlkit round-trip, and the secrets split (never in the schema/file, 0600-enforced).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

import pytest

from radio_server.activity import SquelchMode
from radio_server.config import (
    SETTINGS,
    KNOWN_SECRETS,
    Secrets,
    load_secrets,
    load_settings,
    render_example,
    resolve_settings,
    rotate,
    save_secret,
    save_settings,
)
from radio_server.recording import RecordMode
from radio_server.scan import ResumeMode


# --- no config file → identical defaults to today (behavior-preserving) ---------------------

def test_no_config_file_yields_todays_defaults():
    s = load_settings("/nonexistent/radio.toml")
    # One assertion per group — the full set of defaults the pre-config-file code used.
    assert s.get("station.id_interval") == 600.0
    assert s.get("station.id_mode") == "cw"
    assert s.get("station.cw_wpm") == 20.0
    assert s.get("station.cw_tone_hz") == 600.0
    assert s.get("audio.squelch") is SquelchMode.OFF
    assert s.get("audio.vad_on_rms") == 500.0
    assert s.get("audio.vad_off_rms") == 300.0
    assert s.get("audio.vad_hang") == 0.5
    assert s.get("dtmf.multimon_bin") == "multimon-ng"
    assert s.get("dtmf.timeout") == 3.0
    assert s.get("recording.enabled") is False
    assert s.get("recording.path") == "recordings"
    assert s.get("recording.mode") is RecordMode.GATED
    assert s.get("recording.max_seconds") == 3600.0
    assert s.get("recording.tx") is False
    assert s.get("time.tz") == "UTC"
    assert s.get("tx.idle_timeout") == 2.0
    assert s.get("scan.settle") == 0.05
    assert s.get("scan.poll") == 0.5
    assert s.get("scan.dwell") == 5.0
    assert s.get("scan.mode") is ResumeMode.CARRIER
    assert s.get("controller.poll") == 0.5
    assert s.get("controller.session_timeout") == 300.0
    assert s.get("logging.path") == "radio-server.jsonl"
    assert s.get("activity.window") == 604800.0
    assert s.get("activity.min_duration") == 1.0
    assert s.get("server.backend") == "mock"
    assert s.get("server.host") == "127.0.0.1"
    assert s.get("server.port") == 8000
    assert s.get("server.web_dir").endswith("web/dist")
    assert s.get("server.mock_cat") is True


def test_missing_file_and_empty_mapping_agree():
    assert load_settings("/nope")._values == resolve_settings({})._values


# --- a file value overrides the default ----------------------------------------------------

def test_toml_value_overrides_default(tmp_path):
    cfg = tmp_path / "radio.toml"
    cfg.write_text("[station]\nid_interval = 300\ncw_wpm = 25\n\n[audio]\nsquelch = \"audio\"\n")
    s = load_settings(cfg)
    assert s.get("station.id_interval") == 300.0
    assert s.get("station.cw_wpm") == 25.0
    assert s.get("audio.squelch") is SquelchMode.AUDIO
    # Untouched keys keep their defaults.
    assert s.get("scan.dwell") == 5.0


# --- invalid value fails loud, naming the key ----------------------------------------------

@pytest.mark.parametrize(
    "overrides, needle",
    [
        ({"station.id_interval": 700}, "station.id_interval"),  # over the Part-97 ceiling
        ({"station.cw_wpm": -1}, "station.cw_wpm"),
        ({"audio.squelch": "loud"}, "audio.squelch"),
        ({"recording.enabled": "maybe"}, "recording.enabled"),
        ({"recording.mode": "sometimes"}, "recording.mode"),
        ({"server.port": "notanint"}, "server.port"),
    ],
)
def test_invalid_value_fails_loud_naming_key(overrides, needle):
    with pytest.raises(RuntimeError, match=needle.replace(".", r"\.")):
        resolve_settings(overrides)


def test_bad_timezone_raises_zoneinfo_not_found():
    # tz keeps its native exception type (asserted by test_time_service too), not RuntimeError.
    with pytest.raises(ZoneInfoNotFoundError):
        resolve_settings({"time.tz": "Nowhere/Nowhere"})


def test_unknown_key_fails_loud(tmp_path):
    with pytest.raises(RuntimeError, match="station.bogus"):
        resolve_settings({"station.bogus": 1})


# --- a missing required setting fails loud ON ACCESS, not at load ---------------------------

def test_missing_required_callsign_fails_loud_only_on_access():
    s = resolve_settings({})  # no callsign — resolves fine (default mock app must still start)
    with pytest.raises(RuntimeError, match="station.callsign"):
        s.get("station.callsign")


def test_present_but_empty_required_fails_loud_at_load():
    with pytest.raises(RuntimeError, match="station.callsign"):
        resolve_settings({"station.callsign": ""})


def test_required_becomes_readable_when_set():
    assert resolve_settings({"station.callsign": "W1AW"}).get("station.callsign") == "W1AW"
    assert resolve_settings({"tts.voice": "/v.onnx"}).get("tts.voice") == "/v.onnx"


# --- bool coercion traps (strict vs permissive) --------------------------------------------

def test_permissive_mock_cat_never_fails_and_defaults_on():
    assert resolve_settings({"server.mock_cat": "garbage"}).get("server.mock_cat") is True
    assert resolve_settings({"server.mock_cat": "off"}).get("server.mock_cat") is False
    assert resolve_settings({}).get("server.mock_cat") is True


def test_strict_record_bool_rejects_garbage():
    with pytest.raises(RuntimeError):
        resolve_settings({"recording.enabled": "garbage"})


# --- save_settings: round-trip + comment preservation --------------------------------------

def test_save_settings_round_trips_and_preserves_comments(tmp_path):
    cfg = tmp_path / "radio.toml"
    cfg.write_text(
        "# a hand-added banner comment\n"
        "[station]\n"
        "callsign = \"W1AW\"  # inline note\n"
        "id_interval = 120\n"
    )
    s = load_settings(cfg)
    save_settings(s, cfg)
    text = cfg.read_text()
    assert "# a hand-added banner comment" in text
    assert "# inline note" in text
    reloaded = load_settings(cfg)
    assert reloaded.get("station.callsign") == "W1AW"
    assert reloaded.get("station.id_interval") == 120.0


def test_save_settings_skips_required_unset_never_emits_empty_callsign(tmp_path):
    cfg = tmp_path / "radio.toml"
    save_settings(resolve_settings({}), cfg)  # no callsign set
    assert "callsign" not in cfg.read_text()


def test_save_settings_creates_a_fresh_file_when_absent(tmp_path):
    cfg = tmp_path / "new.toml"
    save_settings(resolve_settings({"station.callsign": "K2ABC"}), cfg)
    assert load_settings(cfg).get("station.callsign") == "K2ABC"


# --- secrets: loaded from a separate source, never in the schema or the file ----------------

def test_secrets_are_not_in_the_settings_schema():
    keys = {s.key for s in SETTINGS}
    assert not any("totp" in k or "token" in k for k in keys)
    assert "totp_secret" in KNOWN_SECRETS and "api_token" in KNOWN_SECRETS


def test_secrets_never_written_into_radio_toml(tmp_path):
    cfg = tmp_path / "radio.toml"
    save_settings(resolve_settings({"station.callsign": "W1AW"}), cfg)
    text = cfg.read_text().lower()
    assert "totp" not in text and "token" not in text and "secret" not in text


def test_secrets_load_from_env_fallback():
    sec = load_secrets(
        "/nonexistent-secrets.toml",
        env={"RADIO_TOTP_SECRET": "ABC123", "RADIO_API_TOKEN": "tok"},
    )
    assert sec.totp_secret == "ABC123"
    assert sec.require("api_token") == "tok"


def test_missing_required_secret_fails_loud():
    sec = load_secrets("/nonexistent-secrets.toml", env={})
    assert sec.get("api_token") is None
    with pytest.raises(RuntimeError, match="RADIO_API_TOKEN"):
        sec.require("api_token")


def test_secrets_file_takes_precedence_and_loads(tmp_path):
    sp = tmp_path / "radio-secrets.toml"
    save_secret(sp, "api_token", "from-file")
    sec = load_secrets(sp, env={"RADIO_API_TOKEN": "from-env"})
    assert sec.api_token == "from-file"


# --- secrets file permissions: 0600 enforced ----------------------------------------------

def test_save_secret_writes_0600(tmp_path):
    sp = tmp_path / "radio-secrets.toml"
    save_secret(sp, "api_token", "tok123")
    assert stat.S_IMODE(sp.stat().st_mode) == 0o600


def test_group_or_world_readable_secrets_file_fails_loud(tmp_path):
    sp = tmp_path / "radio-secrets.toml"
    save_secret(sp, "api_token", "tok123")
    os.chmod(sp, 0o644)  # world-readable — a secrets file a neighbour can read
    with pytest.raises(RuntimeError, match="chmod 600"):
        load_secrets(sp, env={})


def test_save_secret_preserves_the_other_secret(tmp_path):
    sp = tmp_path / "radio-secrets.toml"
    save_secret(sp, "api_token", "tok")
    save_secret(sp, "totp_secret", "SEEED")
    sec = load_secrets(sp, env={})
    assert sec.api_token == "tok"
    assert sec.totp_secret == "SEEED"


def test_rotate_generates_and_persists_a_new_secret(tmp_path):
    sp = tmp_path / "radio-secrets.toml"
    save_secret(sp, "api_token", "old")
    new = rotate(sp, "api_token")
    assert new and new != "old"
    assert load_secrets(sp, env={}).api_token == new
    assert stat.S_IMODE(sp.stat().st_mode) == 0o600


def test_rotate_totp_secret_is_base32_usable(tmp_path):
    # A rotated TOTP secret must be a base32 string pyotp accepts.
    import pyotp

    sp = tmp_path / "radio-secrets.toml"
    secret = rotate(sp, "totp_secret")
    assert pyotp.TOTP(secret).now()  # would raise on a non-base32 secret


# --- radio.toml.example stays consistent with the registry ---------------------------------

def test_render_example_covers_every_setting():
    text = render_example()
    for spec in SETTINGS:
        assert spec.leaf in text, f"{spec.key} missing from radio.toml.example"
    # Required settings appear commented, with the marker; optional ones as real values.
    assert "REQUIRED" in text
    for group in {s.group for s in SETTINGS}:
        assert f"[{group}]" in text


def test_shipped_example_file_matches_the_generator():
    shipped = Path(__file__).resolve().parent.parent / "radio.toml.example"
    assert shipped.read_text() == render_example(), (
        "radio.toml.example is stale; regenerate with "
        "`python -c 'from radio_server.config import render_example; "
        "open(\"radio.toml.example\",\"w\").write(render_example())'`"
    )
