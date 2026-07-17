"""The ``[services]`` digit-binding config channel (ADR 0034).

`[services]` is a separate channel from the `SettingSpec` schema — arbitrary DTMF-digit keys don't fit
one-spec-per-key — so it must be peeled off before schema resolution (never tripping the unknown-key
check) and survive the settings-write round-trip. These tests pin that behavior.
"""

from __future__ import annotations

from radio_server.config import (
    load_service_bindings,
    load_settings,
    render_example,
    resolve_settings,
    save_settings,
)


def _write(tmp_path, text):
    path = tmp_path / "radio.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_no_path_or_no_table_yields_none(tmp_path):
    assert load_service_bindings(None) is None
    assert load_service_bindings(tmp_path / "absent.toml") is None
    assert load_service_bindings(_write(tmp_path, '[time]\ntz = "UTC"\n')) is None


def test_services_table_is_read_as_a_digit_map(tmp_path):
    # Values are raw here — id validation is resolve_bindings' job, so a local-plugin id passes.
    path = _write(tmp_path, '[services]\n"02" = "time"\n"8" = "my-local-plugin"\n')
    assert load_service_bindings(path) == {"02": "time", "8": "my-local-plugin"}


def test_load_settings_ignores_the_services_table(tmp_path):
    # A [services] table must NOT trip resolve_settings' unknown-key rejection.
    path = _write(tmp_path, '[time]\ntz = "UTC"\n\n[services]\n"02" = "time"\n"8" = "my-local-plugin"\n')
    settings = load_settings(path)  # would raise "unknown setting(s)" if not peeled off
    assert settings.get("time.tz") == "UTC"


def test_save_settings_preserves_a_hand_written_services_table(tmp_path):
    # The settings-write API only rewrites schema keys via tomlkit; it must leave [services] intact.
    path = _write(tmp_path, '[services]\n"02" = "time"\n"8" = "my-local-plugin"\n')
    settings = resolve_settings({"station.callsign": "AE9S"})
    save_settings(settings, path)
    text = path.read_text(encoding="utf-8")
    assert "[services]" in text and "callsign" in text  # both channels coexist
    assert load_service_bindings(path) == {"02": "time", "8": "my-local-plugin"}  # bindings untouched


def test_example_file_documents_the_services_table():
    # The shipped two-digit keypad (ADR 0051/0052): 01# ID, 02# time, 99# logout.
    example = render_example()
    assert "[services]" in example
    assert '01 = "station-id"' in example
    assert '02 = "time"' in example
    assert '99 = "logout"' in example
