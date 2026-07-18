"""radio.toml describes more than one backend — presence, load-time validation, enumeration (ADR 0074).

`server.backend` is the *initial* selection; a backend is "configured" if its `[<backend>]` block is
present (plus the active one). Every configured backend is validated at load, so a broken *inactive*
switch target fails loud at startup — not when someone selects it live. These tests cover the presence
model, the stricter validation, the single-block back-compat case, and the enumeration surface the
swap cycle's select endpoint + UI will consume. No hardware: the failure cases raise before any radio
is built, and the success cases monkeypatch `create_radio` (as `test_backend_wiring` does).
"""

from __future__ import annotations

import pytest

from radio_server.api import holder as holder_module
from radio_server.api.app import build_app
from radio_server.api.backend_config import (
    BackendChoice,
    backend_kwargs,
    configured_backends,
    validate_backend_config,
)
from radio_server.backends.mock import MockRadio

from .conftest import make_secrets, make_settings

SECRETS = make_secrets(api_token="multi-backend-token")

# A 2 m (VHF) frequency — out of band for a UHF module, the canonical broken-switch-target value.
VHF_FREQ_HZ = 146_520_000


def _install_stub(monkeypatch):
    """Patch `create_radio` so the active backend builds a harmless MockRadio (no hardware)."""
    calls: list[tuple[str, dict]] = []

    def stub(backend, **kwargs):
        calls.append((backend, kwargs))
        return MockRadio()

    monkeypatch.setattr(holder_module, "create_radio", stub)
    return calls


def _settings(tmp_path, overrides):
    base = {"logging.path": str(tmp_path / "log.jsonl")}
    base.update(overrides)
    return make_settings(base)


def _build(tmp_path, settings):
    build_app(settings, SECRETS, config_path=str(tmp_path / "absent.toml"))


# --- presence: which backends a config declares -------------------------------------------------

def test_bare_config_configures_only_the_active_backend():
    settings = make_settings({})  # server.backend defaults to mock, no blocks
    assert settings.configured_backend_names() == frozenset({"mock"})


def test_a_present_block_is_configured_alongside_the_active_backend():
    settings = make_settings({"server.backend": "kv4p", "baofeng.serial_port": "/dev/ttyACM0"})
    assert settings.configured_backend_names() == frozenset({"kv4p", "baofeng"})


def test_single_block_config_configures_only_that_backend():
    # The back-compat shape: only [baofeng] named, nothing about kv4p.
    settings = make_settings({"server.backend": "baofeng", "baofeng.ptt_line": "rts"})
    assert settings.configured_backend_names() == frozenset({"baofeng"})


# --- load-time validation: an invalid inactive block fails loud ---------------------------------

def test_inactive_baofeng_block_with_cat_squelch_fails_at_load(tmp_path, monkeypatch):
    # kv4p is active and valid; a present [baofeng] block + audio.squelch=cat is invalid (no busy
    # line). Before this cycle the stray block was ignored; now it fails loud at startup.
    _install_stub(monkeypatch)
    with pytest.raises(RuntimeError, match="baofeng"):
        _build(tmp_path, _settings(tmp_path, {
            "server.backend": "kv4p",
            "kv4p.squelch": "4",  # keep the active kv4p valid under cat
            "audio.squelch": "cat",
            "baofeng.serial_port": "/dev/ttyACM0",  # makes baofeng a configured switch target
        }))


def test_inactive_kv4p_block_with_out_of_band_frequency_fails_at_load(tmp_path, monkeypatch):
    # baofeng is active and valid; a present [kv4p] block tunes a VHF frequency on a UHF module —
    # a switch target that can't tune, caught at load rather than at select time.
    _install_stub(monkeypatch)
    with pytest.raises(RuntimeError, match="kv4p.frequency"):
        _build(tmp_path, _settings(tmp_path, {
            "server.backend": "baofeng",
            "kv4p.module_type": "uhf",
            "kv4p.frequency": str(VHF_FREQ_HZ),
        }))


def test_both_blocks_present_and_valid_builds_and_configures_both(tmp_path, monkeypatch):
    calls = _install_stub(monkeypatch)
    settings = _settings(tmp_path, {
        "server.backend": "kv4p",
        "kv4p.module_type": "uhf",
        "kv4p.frequency": "446000000",  # in band for UHF
        "baofeng.serial_port": "/dev/ttyACM0",
    })
    _build(tmp_path, settings)
    assert settings.configured_backend_names() == frozenset({"kv4p", "baofeng"})
    assert calls and calls[0][0] == "kv4p"  # the active backend is the one built


# --- back-compat: a single-backend config boots exactly as before -------------------------------

def test_single_backend_config_still_boots(tmp_path, monkeypatch):
    # Only [baofeng] named; no [kv4p] block, so kv4p is never validated — a config that names one
    # backend is unaffected by the multi-backend validation.
    calls = _install_stub(monkeypatch)
    settings = _settings(tmp_path, {
        "server.backend": "baofeng",
        "baofeng.serial_port": "/dev/ttyACM0",
    })
    _build(tmp_path, settings)
    assert settings.configured_backend_names() == frozenset({"baofeng"})
    assert calls and calls[0][0] == "baofeng"


# --- validate_backend_config: the active/construction split -------------------------------------

def test_active_backend_skips_the_construction_frequency_check():
    # With include_construction_checks=False (the active-backend path used by build_radio), an
    # out-of-band frequency is NOT rejected here — construction validates it (HELLO-aware). This is
    # what keeps the active backend's behaviour byte-identical.
    settings = make_settings({
        "server.backend": "kv4p", "kv4p.module_type": "uhf", "kv4p.frequency": str(VHF_FREQ_HZ),
    })
    validate_backend_config(settings, "kv4p", include_construction_checks=False)  # no raise


def test_inactive_backend_frequency_check_rejects_out_of_band():
    settings = make_settings({
        "server.backend": "kv4p", "kv4p.module_type": "uhf", "kv4p.frequency": str(VHF_FREQ_HZ),
    })
    with pytest.raises(RuntimeError, match="out of band"):
        validate_backend_config(settings, "kv4p", include_construction_checks=True)


# --- enumeration: the surface the select endpoint + UI dropdown consume -------------------------

def test_configured_backends_lists_active_first_with_resolved_settings():
    settings = make_settings({
        "server.backend": "kv4p",
        "kv4p.serial_port": "/dev/ttyUSB9",
        "baofeng.serial_port": "/dev/ttyACM0",
    })
    choices = configured_backends(settings)
    assert [c.name for c in choices] == ["kv4p", "baofeng"]  # active first
    assert isinstance(choices[0], BackendChoice)
    assert choices[0].active is True and choices[1].active is False
    # Each choice carries that backend's resolved build kwargs (== backend_kwargs).
    assert choices[0].settings == backend_kwargs(settings, "kv4p")
    assert choices[0].settings["serial_port"] == "/dev/ttyUSB9"
    assert choices[1].settings["serial_port"] == "/dev/ttyACM0"


def test_configured_backends_bare_config_is_just_the_active_mock():
    choices = configured_backends(make_settings({}))
    assert len(choices) == 1
    assert choices[0].name == "mock" and choices[0].active is True


# --- doctor validates the selected backend against the real radio.toml --------------------------

def test_doctor_reports_a_broken_block_for_the_selected_backend(monkeypatch):
    # doctor validates the backend it is about to test, reading the real config (ADR 0069/0074).
    from radio_server import doctor

    monkeypatch.setattr(doctor, "_doctor_settings", lambda: make_settings({
        "server.backend": "kv4p", "kv4p.module_type": "uhf", "kv4p.frequency": str(VHF_FREQ_HZ),
    }))
    problem = doctor._validate_doctor_backend_config("kv4p")
    assert problem is not None and "out of band" in problem


def test_doctor_passes_a_valid_selected_backend(monkeypatch):
    from radio_server import doctor

    monkeypatch.setattr(doctor, "_doctor_settings", lambda: make_settings({
        "server.backend": "kv4p", "kv4p.module_type": "uhf", "kv4p.frequency": "446000000",
    }))
    assert doctor._validate_doctor_backend_config("kv4p") is None
