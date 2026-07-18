"""Backend selection in the composition root (`build_app`) — the kv4p wiring (ADR 0061/0063).

`build_app` reads `server.backend` and maps each backend's `[<backend>]` settings onto the concrete
constructor via `create_radio`. These tests exercise that mapping without any hardware by
monkeypatching `create_radio` to a stub that records the `(backend, kwargs)` it was called with and
returns a `MockRadio` — so we assert the *wiring* (which settings reach the backend, and the
fail-loud combinations) rather than constructing a real serial transport.
"""

from __future__ import annotations

import pytest

from radio_server.api import app as app_module
from radio_server.api.app import build_app
from radio_server.backends.mock import MockRadio

from .conftest import make_secrets, make_settings

SECRETS = make_secrets(api_token="wiring-token")


def _install_stub(monkeypatch):
    """Patch `create_radio` to record its call and hand back a harmless MockRadio."""
    calls: list[tuple[str, dict]] = []

    def stub(backend, **kwargs):
        calls.append((backend, kwargs))
        return MockRadio()

    monkeypatch.setattr(app_module, "create_radio", stub)
    return calls


def _settings(tmp_path, overrides):
    base = {"logging.path": str(tmp_path / "log.jsonl")}
    base.update(overrides)
    return make_settings(base)


def _build(tmp_path, settings):
    # A nonexistent config_path keeps the [[mumble.servers]] load empty and deterministic.
    build_app(settings, SECRETS, config_path=str(tmp_path / "absent.toml"))


def test_kv4p_backend_passes_every_setting_through(tmp_path, monkeypatch):
    calls = _install_stub(monkeypatch)
    _build(tmp_path, _settings(tmp_path, {
        "server.backend": "kv4p",
        "kv4p.serial_port": "/dev/ttyUSB3",
        "kv4p.module_type": "uhf",
        "kv4p.squelch": "5",
        "kv4p.tx_lead_seconds": "0.4",
        "kv4p.high_power": "false",
        "kv4p.tx_allowed": "false",
        "kv4p.frequency": "146520000",
        "kv4p.sample_rate_correction": "1.019",
    }))
    assert len(calls) == 1
    backend, kwargs = calls[0]
    assert backend == "kv4p"
    assert kwargs == {
        "serial_port": "/dev/ttyUSB3",
        "module_type": "uhf",
        "squelch": 5,
        "tx_lead_seconds": pytest.approx(0.4),
        "high_power": False,
        "tx_allowed": False,
        "frequency": 146_520_000,
        "sample_rate_correction": pytest.approx(1.019),
    }


def test_kv4p_unset_frequency_passes_none(tmp_path, monkeypatch):
    calls = _install_stub(monkeypatch)
    _build(tmp_path, _settings(tmp_path, {"server.backend": "kv4p"}))
    _, kwargs = calls[0]
    assert kwargs["frequency"] is None  # unset → keep the device's NVS frequency


def test_kv4p_cat_squelch_with_level_zero_fails_loud_naming_both(tmp_path, monkeypatch):
    _install_stub(monkeypatch)
    with pytest.raises(RuntimeError) as exc:
        _build(tmp_path, _settings(tmp_path, {
            "server.backend": "kv4p",
            "audio.squelch": "cat",
            "kv4p.squelch": "0",
        }))
    message = str(exc.value)
    assert "audio.squelch" in message and "kv4p.squelch" in message


def test_kv4p_cat_squelch_with_nonzero_level_builds(tmp_path, monkeypatch):
    calls = _install_stub(monkeypatch)
    _build(tmp_path, _settings(tmp_path, {
        "server.backend": "kv4p",
        "audio.squelch": "cat",
        "kv4p.squelch": "4",
    }))
    assert calls and calls[0][0] == "kv4p"  # cat is valid here (real busy line), so it builds


def test_baofeng_cat_squelch_still_fails_loud(tmp_path, monkeypatch):
    # Regression: the UV-5R has no busy line, so audio.squelch=cat must still be rejected for it.
    _install_stub(monkeypatch)
    with pytest.raises(RuntimeError, match="baofeng"):
        _build(tmp_path, _settings(tmp_path, {
            "server.backend": "baofeng",
            "audio.squelch": "cat",
        }))
