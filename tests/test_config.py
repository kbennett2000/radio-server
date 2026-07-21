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
    assert s.get("dtmf.decode_mode") == "auto"
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
    assert s.get("server.backend") == "mock"
    assert s.get("server.host") == "127.0.0.1"
    assert s.get("server.port") == 8000
    assert s.get("server.web_dir").endswith("web/dist")
    assert s.get("server.mock_cat") is True
    assert s.get("server.tls_cert") == ""  # HTTPS off by default (ADR 0039)
    assert s.get("server.tls_key") == ""


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


def test_tls_paths_override_default(tmp_path):
    cfg = tmp_path / "radio.toml"
    cfg.write_text('[server]\ntls_cert = "/etc/radio/cert.pem"\ntls_key = "/etc/radio/key.pem"\n')
    s = load_settings(cfg)
    assert s.get("server.tls_cert") == "/etc/radio/cert.pem"
    assert s.get("server.tls_key") == "/etc/radio/key.pem"


# --- invalid value fails loud, naming the key ----------------------------------------------

@pytest.mark.parametrize(
    "overrides, needle",
    [
        ({"station.id_interval": 700}, "station.id_interval"),  # over the Part-97 ceiling
        ({"station.cw_wpm": -1}, "station.cw_wpm"),
        ({"audio.squelch": "loud"}, "audio.squelch"),
        ({"dtmf.decode_mode": "turbo"}, "dtmf.decode_mode"),
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
    # The required station callsign is skipped when unset (never written as an empty string). Scope to
    # the [station] table so the optional, blank-by-default dstar.callsign (ADR 0087) is not a false hit.
    station = cfg.read_text().split("[station]", 1)[1].split("\n[", 1)[0]
    assert "callsign" not in station


def test_save_settings_creates_a_fresh_file_when_absent(tmp_path):
    cfg = tmp_path / "new.toml"
    save_settings(resolve_settings({"station.callsign": "K2ABC"}), cfg)
    assert load_settings(cfg).get("station.callsign") == "K2ABC"


# --- secrets: loaded from a separate source, never in the schema or the file ----------------

def test_secrets_are_not_in_the_settings_schema():
    keys = {s.key for s in SETTINGS}
    # The secret KEYS themselves must never be in the schema (they live on the secrets channel).
    # `auth.totp_enabled` is a non-secret toggle (ADR 0048) that legitimately contains "totp".
    assert not any(k.endswith("totp_secret") or k.endswith("api_token") for k in keys)
    assert "totp_secret" in KNOWN_SECRETS and "api_token" in KNOWN_SECRETS


def test_secrets_never_written_into_radio_toml(tmp_path):
    cfg = tmp_path / "radio.toml"
    save_settings(resolve_settings({"station.callsign": "W1AW"}), cfg)
    text = cfg.read_text().lower()
    # The secret identifiers must never be serialized. The non-secret `totp_enabled` toggle and the
    # "[auth] over-RF TOTP/DTMF plane" banner may mention "totp"; the secret KEYS must not appear.
    assert "totp_secret" not in text and "api_token" not in text


def test_secrets_load_from_env_fallback():
    sec = load_secrets(
        "/nonexistent-secrets.toml",
        env={"RADIO_TOTP_SECRET": "ABC123", "RADIO_API_TOKEN": "tok"},
    )
    assert sec.totp_secret == "ABC123"
    assert sec.require("api_token") == "tok"


def test_fixed_code_is_a_secret_channel_value_not_a_schema_key():
    # The fixed login code (ADR 0083) is a credential: it lives on the secrets channel, NEVER in the
    # settings schema (only the non-secret `auth.fixed_code` toggle is a schema key).
    assert "fixed_code" in KNOWN_SECRETS
    keys = {s.key for s in SETTINGS}
    assert not any(k.endswith("fixed_code") and k != "auth.fixed_code" for k in keys)


def test_fixed_code_loads_from_env_and_file(tmp_path):
    # Env fallback, then file precedence — same channel as the other secrets.
    assert load_secrets("/nonexistent.toml", env={"RADIO_FIXED_CODE": "135790"}).fixed_code == "135790"
    sp = tmp_path / "radio-secrets.toml"
    save_secret(sp, "fixed_code", "246813")
    assert load_secrets(sp, env={"RADIO_FIXED_CODE": "000000"}).fixed_code == "246813"  # file wins


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


# --- the [[mumble.servers]] channel (ADR 0042) ----------------------------------------------

def test_load_mumble_servers_absent_returns_none(tmp_path):
    from radio_server.config import load_mumble_servers

    assert load_mumble_servers(None) is None
    assert load_mumble_servers(tmp_path / "missing.toml") is None
    cfg = tmp_path / "radio.toml"
    cfg.write_text("[mumble]\ntx_hang = 1.5\n")
    assert load_mumble_servers(cfg) is None


def test_load_mumble_servers_returns_raw_tables(tmp_path):
    from radio_server.config import load_mumble_servers

    cfg = tmp_path / "radio.toml"
    cfg.write_text(
        '[[mumble.servers]]\nname = "home"\nhost = "h1"\ndtmf = "13"\n'
        '[[mumble.servers]]\nname = "away"\nhost = "h2"\n'
    )
    servers = load_mumble_servers(cfg)
    assert servers == [
        {"name": "home", "host": "h1", "dtmf": "13"},
        {"name": "away", "host": "h2"},
    ]


def test_servers_list_is_skipped_by_schema_resolution(tmp_path):
    # The entry list is a separate channel (like [services]) — the schema loader must not see it.
    cfg = tmp_path / "radio.toml"
    cfg.write_text('[mumble]\ntx_hang = 1.5\n[[mumble.servers]]\nname = "home"\nhost = "h1"\n')
    settings = load_settings(cfg)
    assert settings.get("mumble.tx_hang") == 1.5


def test_legacy_flat_mumble_block_fails_loud_with_migration_message(tmp_path):
    cfg = tmp_path / "radio.toml"
    cfg.write_text('[mumble]\nenabled = true\nhost = "old.example.net"\n')
    with pytest.raises(RuntimeError, match=r"\[\[mumble\.servers\]\]"):
        load_settings(cfg)


def test_save_mumble_servers_round_trips_preserving_comments(tmp_path):
    from radio_server.config import load_mumble_servers, save_mumble_servers

    cfg = tmp_path / "radio.toml"
    cfg.write_text("# operator note\n[station]\nid_interval = 120\n")
    save_mumble_servers([{"name": "home", "host": "h1", "dtmf": "13"}], cfg)
    assert "# operator note" in cfg.read_text()
    assert load_mumble_servers(cfg) == [{"name": "home", "host": "h1", "dtmf": "13"}]
    # Whole-list replace: saving a different list drops the old entries.
    save_mumble_servers([{"name": "away", "host": "h2"}], cfg)
    assert load_mumble_servers(cfg) == [{"name": "away", "host": "h2"}]
    # Empty list removes the array entirely.
    save_mumble_servers([], cfg)
    assert load_mumble_servers(cfg) is None


def test_save_mumble_servers_keeps_schema_settings_intact(tmp_path):
    from radio_server.config import save_mumble_servers

    cfg = tmp_path / "radio.toml"
    save_settings(resolve_settings({"mumble.tx_hang": 1.5}), cfg)
    save_mumble_servers([{"name": "home", "host": "h1"}], cfg)
    assert load_settings(cfg).get("mumble.tx_hang") == 1.5


def test_link_announcement_template_validates_at_load():
    good = resolve_settings({"mumble.link_announcement": "On {name} now."})
    assert good.get("mumble.link_announcement") == "On {name} now."
    # Blank = silent (the announcement convention), never an error.
    assert resolve_settings({"mumble.link_announcement": ""}).get("mumble.link_announcement") == ""
    # A typo'd placeholder fails loud at load, not at controller build.
    with pytest.raises(RuntimeError, match="mumble.link_announcement"):
        resolve_settings({"mumble.link_announcement": "On {nmae} now."})


def test_disconnect_dtmf_setting_validates_charset():
    assert resolve_settings({"mumble.disconnect_dtmf": "0A"}).get("mumble.disconnect_dtmf") == "0A"
    with pytest.raises(RuntimeError, match="mumble.disconnect_dtmf"):
        resolve_settings({"mumble.disconnect_dtmf": "73#"})


# --- dynamic per-entry Mumble password secrets (ADR 0042) ------------------------------------

def test_mumble_password_loads_from_file_and_env(tmp_path):
    sp = tmp_path / "radio-secrets.toml"
    save_secret(sp, "mumble_password_home", "hunter2")
    sec = load_secrets(sp, env={"RADIO_MUMBLE_PASSWORD_CLUB_NET": "clubpw"})
    assert sec.get("mumble_password_home") == "hunter2"
    assert sec.get("mumble_password_club_net") == "clubpw"  # env suffix lowercased to the slug
    assert sec.get("mumble_password_other") is None


def test_save_secret_preserves_dynamic_mumble_passwords(tmp_path):
    sp = tmp_path / "radio-secrets.toml"
    save_secret(sp, "mumble_password_home", "hunter2")
    save_secret(sp, "api_token", "tok")  # a later fixed-secret write must not drop the dynamic key
    sec = load_secrets(sp, env={})
    assert sec.api_token == "tok"
    assert sec.get("mumble_password_home") == "hunter2"
    assert stat.S_IMODE(sp.stat().st_mode) == 0o600


def test_save_secret_still_rejects_truly_unknown_names(tmp_path):
    with pytest.raises(ValueError, match="unknown secret"):
        save_secret(tmp_path / "s.toml", "mumble_password_", "x")  # empty entry suffix
    with pytest.raises(ValueError, match="unknown secret"):
        save_secret(tmp_path / "s.toml", "nope", "x")


# --- the [plugins.*] local-plugin config channel (ADR 0051) ---------------------------------

def test_extra_returns_the_plugins_channel_value_or_default():
    s = resolve_settings({}, extra={"weather.base_url": "http://w/api"})
    assert s.extra("weather.base_url") == "http://w/api"
    # Deliberately default-forgiving — plugins own their own coercion/failure story.
    assert s.extra("weather.timeout") is None
    assert s.extra("weather.timeout", 3.0) == 3.0


def test_extras_returns_the_whole_channel_as_a_copy():
    # The whole-channel accessor (ADR 0078) — what a settings patch round-trips through
    # resolve_settings(extra=...). Returns a copy so `Settings` stays immutable.
    channel = {"weather.base_url": "http://w/api", "quote.base_url": "http://q/api"}
    s = resolve_settings({}, extra=channel)
    assert s.extras() == channel
    got = s.extras()
    got["weather.base_url"] = "mutated"
    assert s.extra("weather.base_url") == "http://w/api"  # the stored channel is untouched


def test_patch_idiom_preserves_the_extra_channel(tmp_path):
    # The regression that guards ADR 0078: the POST /radio/select / PATCH /settings patch idiom
    # rebuilds `base` from SCHEMA KEYS ONLY, so it MUST carry the extra channel through explicitly
    # or every local plugin's config is dropped. Reproduce the handler's expression directly.
    current = resolve_settings(
        {"server.backend": "baofeng"}, extra={"weather.base_url": "http://w/api"}
    )
    base = {spec.key: current.get(spec.key) for spec in SETTINGS if current.is_set(spec.key)}
    patched = resolve_settings({**base, "server.backend": "kv4p"}, extra=current.extras())
    assert patched.get("server.backend") == "kv4p"  # the patch applied...
    assert patched.extra("weather.base_url") == "http://w/api"  # ...and the extra channel survived


def test_load_settings_flattens_the_plugins_table_into_extra(tmp_path):
    # [plugins.<group>] flattens with the prefix DROPPED (a migrated plugin keeps its old key
    # spelling); deeper nesting keeps its dots; a scalar directly under [plugins] keeps its bare key.
    cfg = tmp_path / "radio.toml"
    cfg.write_text(
        '[time]\ntz = "UTC"\n'
        "[plugins]\n"
        'motd = "hi"\n'
        "[plugins.weather]\n"
        'base_url = "http://w/api"\n'
        "timeout = 5.0\n"
        "[plugins.weather.alerts]\n"
        "enabled = true\n"
    )
    s = load_settings(cfg)
    assert s.get("time.tz") == "UTC"  # schema keys still resolve normally
    assert s.extra("weather.base_url") == "http://w/api"
    assert s.extra("weather.timeout") == 5.0
    assert s.extra("weather.alerts.enabled") is True
    assert s.extra("motd") == "hi"


def test_top_level_weather_fails_loud_but_plugins_weather_passes(tmp_path):
    # The six per-service specs (weather/quote/battery/bible) left the schema (ADR 0051): a
    # leftover top-level [weather] is an unknown setting — fail loud, not a silent no-op —
    # while the same keys under [plugins.weather] ride the unvalidated plugins channel.
    stale = tmp_path / "stale.toml"
    stale.write_text('[weather]\nbase_url = "http://w/api"\n')
    with pytest.raises(RuntimeError, match=r"weather\.base_url"):
        load_settings(stale)
    migrated = tmp_path / "migrated.toml"
    migrated.write_text('[plugins.weather]\nbase_url = "http://w/api"\n')
    assert load_settings(migrated).extra("weather.base_url") == "http://w/api"


def test_flat_plugin_table_points_at_the_plugins_channel():
    # ADR 0059: a key under a non-schema table (weather.* — no `weather` group) is almost always
    # local-plugin settings left out of [plugins.*]. Name the table and its home, not a bare
    # "unknown setting(s)". This was the #1 migration that took stations down at restart.
    with pytest.raises(RuntimeError, match=r"\[plugins\.weather\]") as exc:
        resolve_settings({"weather.base_url": "http://w/api"})
    msg = str(exc.value)
    assert "weather.base_url" in msg  # names the offending key
    assert "only the TOML nesting moves" in msg  # the code-unchanged reassurance


def test_real_typo_under_a_schema_table_keeps_the_generic_message():
    # A typo whose namespace *is* a schema group (server.prot) is not a plugin migration — it keeps
    # the generic unknown-setting error, never the [plugins.*] hint.
    with pytest.raises(RuntimeError, match="not in the config schema") as exc:
        resolve_settings({"server.prot": 8000})
    msg = str(exc.value)
    assert "server.prot" in msg
    assert "plugins" not in msg


def test_save_settings_preserves_a_hand_written_plugins_table(tmp_path):
    # Like [services]: the settings-write API only rewrites schema keys via tomlkit; the
    # operator's [plugins.*] tables must survive a save untouched.
    cfg = tmp_path / "radio.toml"
    cfg.write_text('[plugins.weather]\nbase_url = "http://w/api"\n')
    save_settings(resolve_settings({"station.callsign": "AE9S"}), cfg)
    text = cfg.read_text()
    assert "[plugins.weather]" in text and "callsign" in text  # both channels coexist
    assert load_settings(cfg).extra("weather.base_url") == "http://w/api"  # value untouched


# --- radio.toml.example stays consistent with the registry ---------------------------------

def test_render_example_covers_every_setting():
    text = render_example()
    for spec in SETTINGS:
        assert spec.leaf in text, f"{spec.key} missing from radio.toml.example"
    # Required settings appear commented, with the marker; optional ones as real values.
    assert "REQUIRED" in text
    for group in {s.group for s in SETTINGS}:
        assert f"[{group}]" in text


def test_render_example_ships_the_demo_server_and_plugins_note():
    # ADR 0052: an ACTIVE (uncommented) demo entry so 10# works out of the box — its password is
    # a public gate code, not a secret. ADR 0051: the [plugins] channel is documented as comments
    # (nothing ships by default).
    text = render_example()
    assert "[[mumble.servers]]" in text
    assert 'name = "Radio Server Demo"' in text
    assert 'dtmf = "10"' in text
    assert "password = " in text
    assert "[plugins" in text
    assert "local_services" in text


def test_shipped_example_file_matches_the_generator():
    shipped = Path(__file__).resolve().parent.parent / "radio.toml.example"
    assert shipped.read_text() == render_example(), (
        "radio.toml.example is stale; regenerate with "
        "`python -c 'from radio_server.config import render_example; "
        "open(\"radio.toml.example\",\"w\").write(render_example())'`"
    )


# --- the [kv4p] backend section (ADR 0061/0063 wiring) --------------------------------------

def test_kv4p_settings_resolve_and_coerce():
    s = resolve_settings({
        "kv4p.serial_port": "/dev/ttyUSB1",
        "kv4p.squelch": "2",
        "kv4p.tx_lead_seconds": "0.3",
        "kv4p.high_power": "false",
        "kv4p.tx_allowed": "false",
        "kv4p.frequency": "146520000",
    })
    assert s.get("kv4p.serial_port") == "/dev/ttyUSB1"
    assert s.get("kv4p.squelch") == 2  # coerced to int
    assert s.get("kv4p.tx_lead_seconds") == pytest.approx(0.3)
    assert s.get("kv4p.high_power") is False
    assert s.get("kv4p.tx_allowed") is False
    assert s.get("kv4p.frequency") == 146_520_000  # coerced to int


def test_kv4p_defaults_are_a_ttyusb_and_a_nonzero_squelch():
    s = resolve_settings({})
    # CP210x/CH340 enumerate as ttyUSB, not the AIOC's ttyACM; a non-zero squelch so cat works.
    assert s.get("kv4p.serial_port") == "/dev/ttyUSB0"
    assert s.get("kv4p.squelch") != 0
    assert s.get("kv4p.high_power") is True
    assert s.get("kv4p.tx_allowed") is True


def test_kv4p_frequency_is_optional_none_when_unset():
    assert resolve_settings({}).get("kv4p.frequency") is None


def test_kv4p_sample_rate_correction_defaults_to_the_firmware_offset():
    # Shipped firmware runs the RX ADC ~2% fast (rxAudio.h: AUDIO_SAMPLE_RATE * 1.02), ADR 0070.
    assert resolve_settings({}).get("kv4p.sample_rate_correction") == pytest.approx(1.02)


def test_kv4p_sample_rate_correction_coerces_and_rejects_non_positive():
    assert resolve_settings(
        {"kv4p.sample_rate_correction": "1.019"}
    ).get("kv4p.sample_rate_correction") == pytest.approx(1.019)
    with pytest.raises(RuntimeError, match="kv4p.sample_rate_correction"):
        resolve_settings({"kv4p.sample_rate_correction": "0"})
    assert resolve_settings({"kv4p.frequency": "146520000"}).get("kv4p.frequency") == 146_520_000


def test_kv4p_tx_gain_defaults_to_unity_no_op():
    # Default 1.0: no attenuation, no behaviour change for anyone who doesn't set it (ADR 0080).
    assert resolve_settings({}).get("kv4p.tx_gain") == pytest.approx(1.0)


def test_kv4p_tx_gain_coerces_and_rejects_non_positive():
    assert resolve_settings({"kv4p.tx_gain": "0.5"}).get("kv4p.tx_gain") == pytest.approx(0.5)
    # >1.0 is a valid setting (the audio clamps rather than the value being rejected).
    assert resolve_settings({"kv4p.tx_gain": "1.5"}).get("kv4p.tx_gain") == pytest.approx(1.5)
    with pytest.raises(RuntimeError, match="kv4p.tx_gain"):
        resolve_settings({"kv4p.tx_gain": "0"})
    with pytest.raises(RuntimeError, match="kv4p.tx_gain"):
        resolve_settings({"kv4p.tx_gain": "-1"})


def test_kv4p_frequency_rejects_a_non_integer():
    with pytest.raises(RuntimeError, match="kv4p.frequency"):
        resolve_settings({"kv4p.frequency": "not-a-number"})


def test_kv4p_settings_round_trip_through_save(tmp_path):
    cfg = tmp_path / "radio.toml"
    s = resolve_settings({
        "kv4p.serial_port": "/dev/ttyUSB2",
        "kv4p.squelch": "3",
        "kv4p.high_power": "false",
        "kv4p.frequency": "445000000",
    })
    save_settings(s, cfg)
    reloaded = load_settings(cfg)
    assert reloaded.get("kv4p.serial_port") == "/dev/ttyUSB2"
    assert reloaded.get("kv4p.squelch") == 3
    assert reloaded.get("kv4p.high_power") is False
    assert reloaded.get("kv4p.frequency") == 445_000_000


def test_kv4p_unset_frequency_is_not_written(tmp_path):
    # An optional None must not be persisted as `frequency = None` (unwritable TOML) — it is simply
    # omitted, and reloads back to None.
    cfg = tmp_path / "radio.toml"
    save_settings(resolve_settings({}), cfg)
    assert "frequency" not in cfg.read_text()
    assert load_settings(cfg).get("kv4p.frequency") is None


# --- the [uvk5] backend section (ADR 0110-0114 wiring) ---------------------------------------

def test_uvk5_settings_resolve_and_coerce():
    s = resolve_settings({
        "uvk5.serial_port": "/dev/serial/by-id/usb-AIOC",
        "uvk5.frequency": "442000000",
        "uvk5.tone": "100.0",
        "uvk5.mode": "NFM",
        "uvk5.tx_allowed": "false",
        "uvk5.blocksize": "480",
        "uvk5.tx_lead_seconds": "0.3",
        "uvk5.squelch_threshold": "35",
    })
    assert s.get("uvk5.serial_port") == "/dev/serial/by-id/usb-AIOC"
    assert s.get("uvk5.frequency") == 442_000_000  # coerced to int
    assert s.get("uvk5.tone") == pytest.approx(100.0)  # coerced to float
    assert s.get("uvk5.mode") == "NFM"
    assert s.get("uvk5.tx_allowed") is False
    assert s.get("uvk5.blocksize") == 480
    assert s.get("uvk5.tx_lead_seconds") == pytest.approx(0.3)
    assert s.get("uvk5.squelch_threshold") == 35


def test_uvk5_serial_port_is_required_no_guessed_default():
    # No safe default (the AIOC is an ambiguous ttyACM* on a multi-adapter bench) → REQUIRED.
    s = resolve_settings({})
    assert s.is_set("uvk5.serial_port") is False
    with pytest.raises(RuntimeError, match="uvk5.serial_port"):
        s.get("uvk5.serial_port")


def test_uvk5_frequency_is_required_fail_loud_when_unset():
    # XVFO has no radio-side value to preserve, so unset is an error, not an invented default.
    s = resolve_settings({})
    assert s.is_set("uvk5.frequency") is False
    with pytest.raises(RuntimeError, match="uvk5.frequency"):
        s.get("uvk5.frequency")


def test_uvk5_frequency_rejects_a_non_integer():
    with pytest.raises(RuntimeError, match="uvk5.frequency"):
        resolve_settings({"uvk5.frequency": "not-a-number"})


def test_uvk5_tone_is_optional_none_when_unset_and_rejects_non_number():
    assert resolve_settings({}).get("uvk5.tone") is None
    assert resolve_settings({"uvk5.tone": "88.5"}).get("uvk5.tone") == pytest.approx(88.5)
    with pytest.raises(RuntimeError, match="uvk5.tone"):
        resolve_settings({"uvk5.tone": "not-a-tone"})


def test_uvk5_defaults_are_the_aioc_card_and_a_wide_fm_tx_on():
    s = resolve_settings({})
    assert s.get("uvk5.mode") == "FM"
    assert s.get("uvk5.tx_allowed") is True
    assert s.get("uvk5.blocksize") == 960
    assert s.get("uvk5.squelch_threshold") == 40
    assert s.get("uvk5.tx_lead_seconds") == pytest.approx(0.5)
    assert "All-In-One-Cable" in s.get("uvk5.input_device")
    assert "All-In-One-Cable" in s.get("uvk5.output_device")


def test_uvk5_settings_round_trip_through_save(tmp_path):
    cfg = tmp_path / "radio.toml"
    s = resolve_settings({
        "uvk5.serial_port": "/dev/serial/by-id/usb-AIOC",
        "uvk5.frequency": "445000000",
        "uvk5.tone": "127.3",
        "uvk5.mode": "NFM",
        "uvk5.tx_allowed": "false",
    })
    save_settings(s, cfg)
    reloaded = load_settings(cfg)
    assert reloaded.get("uvk5.serial_port") == "/dev/serial/by-id/usb-AIOC"
    assert reloaded.get("uvk5.frequency") == 445_000_000
    assert reloaded.get("uvk5.tone") == pytest.approx(127.3)
    assert reloaded.get("uvk5.mode") == "NFM"
    assert reloaded.get("uvk5.tx_allowed") is False


def test_uvk5_unset_tone_is_not_written(tmp_path):
    # Optional None must not persist as `tone = None` (unwritable TOML) — omitted, reloads to None.
    cfg = tmp_path / "radio.toml"
    save_settings(resolve_settings({"uvk5.serial_port": "/dev/ttyACM0", "uvk5.frequency": "1"}), cfg)
    assert "\ntone = " not in cfg.read_text()  # no uvk5 tone leaf line (cw_tone_hz is unrelated)
    assert load_settings(cfg).get("uvk5.tone") is None


def test_unconfigured_uvk5_block_is_not_fabricated_on_save(tmp_path):
    # ADR 0114: saving a config that never configured uvk5 must not write a phantom [uvk5] block
    # from its defaults (which would make an unconfigured, unbuildable uvk5 look "configured" and
    # crash backend enumeration on its unset REQUIRED serial_port). A backend with an unset REQUIRED
    # key is skipped wholesale on save.
    cfg = tmp_path / "radio.toml"
    save_settings(resolve_settings({"server.backend": "baofeng"}), cfg)
    text = cfg.read_text()
    # No uvk5 VALUE lines are written (an empty banner header from a fresh doc is harmless — it
    # carries no keys), so uvk5 does not read back as a configured, unbuildable backend.
    assert "serial_port =" not in text.split("[uvk5]")[-1].split("[")[0]
    assert "uvk5" not in load_settings(cfg).configured_backend_names()


def test_configured_uvk5_block_round_trips_when_required_keys_are_set(tmp_path):
    # The complement: once the REQUIRED keys are set, the whole [uvk5] block persists and reloads as
    # a configured, buildable backend.
    cfg = tmp_path / "radio.toml"
    save_settings(
        resolve_settings({"uvk5.serial_port": "/dev/ttyACM9", "uvk5.frequency": "146520000"}), cfg
    )
    reloaded = load_settings(cfg)
    assert "uvk5" in reloaded.configured_backend_names()
    assert reloaded.get("uvk5.serial_port") == "/dev/ttyACM9"


def test_advanced_keys_are_all_real_settings():
    # The "known keys" tuple (_ADVANCED_KEYS) must not drift from the schema: every entry names a
    # real setting, so a typo or a removed key is caught rather than silently ignored.
    from radio_server.config.spec import _ADVANCED_KEYS

    real = {s.key for s in SETTINGS}
    assert _ADVANCED_KEYS <= real, f"stale advanced keys: {sorted(_ADVANCED_KEYS - real)}"
