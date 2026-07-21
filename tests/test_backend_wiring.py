"""Backend selection in the composition root (`build_app`) — the kv4p wiring (ADR 0061/0063).

`build_app` reads `server.backend` and maps each backend's `[<backend>]` settings onto the concrete
constructor via `create_radio` — the switch lives in `build_radio` (api/holder.py) since ADR 0073.
These tests exercise that mapping without any hardware by monkeypatching `create_radio` (where
`build_radio` looks it up) to a stub that records the `(backend, kwargs)` it was called with and
returns a `MockRadio` — so we assert the *wiring* (which settings reach the backend, and the
fail-loud combinations) rather than constructing a real serial transport.
"""

from __future__ import annotations

import pytest

from radio_server.api import holder as holder_module
from radio_server.api.app import build_app
from radio_server.backends.mock import MockRadio
from radio_server.tx.tot import TotRadio

from .conftest import make_secrets, make_settings

SECRETS = make_secrets(api_token="wiring-token")


def _install_stub(monkeypatch):
    """Patch `create_radio` to record its call and hand back a harmless MockRadio."""
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
        "kv4p.tx_gain": "0.5",
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
        "tx_gain": pytest.approx(0.5),
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


# --- UV-K5 (Quansheng Dock) wiring (ADR 0114) --------------------------------


def test_uvk5_backend_passes_every_setting_through(tmp_path, monkeypatch):
    calls = _install_stub(monkeypatch)
    _build(tmp_path, _settings(tmp_path, {
        "server.backend": "uvk5",
        "uvk5.serial_port": "/dev/serial/by-id/usb-AIOC",
        "uvk5.frequency": "442000000",
        "uvk5.tone": "100.0",
        "uvk5.mode": "NFM",
        "uvk5.tx_allowed": "false",
        "uvk5.input_device": "AIOC-in",
        "uvk5.output_device": "AIOC-out",
        "uvk5.blocksize": "480",
        "uvk5.tx_lead_seconds": "0.3",
        "uvk5.squelch_threshold": "35",
    }))
    assert len(calls) == 1
    backend, kwargs = calls[0]
    assert backend == "uvk5"
    assert kwargs == {
        "serial_port": "/dev/serial/by-id/usb-AIOC",
        "frequency": 442_000_000,
        "tone": pytest.approx(100.0),
        "mode": "NFM",
        "tx_allowed": False,
        "input_device": "AIOC-in",
        "output_device": "AIOC-out",
        "blocksize": 480,
        "tx_lead_seconds": pytest.approx(0.3),
        "squelch_threshold": 35,
    }


def test_uvk5_missing_required_serial_port_fails_loud(tmp_path, monkeypatch):
    # serial_port is REQUIRED (no guessed default): building uvk5 without it fails loud on read.
    _install_stub(monkeypatch)
    with pytest.raises(Exception):
        _build(tmp_path, _settings(tmp_path, {
            "server.backend": "uvk5",
            "uvk5.frequency": "442000000",
        }))


def test_uvk5_cat_squelch_with_zero_threshold_fails_loud_naming_both(tmp_path, monkeypatch):
    _install_stub(monkeypatch)
    with pytest.raises(RuntimeError) as exc:
        _build(tmp_path, _settings(tmp_path, {
            "server.backend": "uvk5",
            "uvk5.serial_port": "/dev/ttyACM0",
            "uvk5.frequency": "442000000",
            "audio.squelch": "cat",
            "uvk5.squelch_threshold": "0",
        }))
    message = str(exc.value)
    assert "audio.squelch" in message and "uvk5.squelch_threshold" in message


def test_uvk5_cat_squelch_with_nonzero_threshold_builds(tmp_path, monkeypatch):
    calls = _install_stub(monkeypatch)
    _build(tmp_path, _settings(tmp_path, {
        "server.backend": "uvk5",
        "uvk5.serial_port": "/dev/ttyACM0",
        "uvk5.frequency": "442000000",
        "audio.squelch": "cat",
        "uvk5.squelch_threshold": "40",
    }))
    assert calls and calls[0][0] == "uvk5"  # cat is valid — the UV-K5 has a real RSSI busy line


# --- the mandatory UV-K5 transmitter time-out is wired at the composition root (ADR 0117) -----


def _built_app(tmp_path, overrides):
    """Build the real app over the stubbed backend and hand back the wrapped active radio's TOT."""
    return build_app(
        _settings(tmp_path, overrides), SECRETS, config_path=str(tmp_path / "absent.toml")
    )


def test_uvk5_tot_is_mandatory_and_ignores_the_global_disable(tmp_path, monkeypatch):
    _install_stub(monkeypatch)
    app = _built_app(tmp_path, {
        "server.backend": "uvk5",
        "uvk5.serial_port": "/dev/ttyACM0",
        "uvk5.frequency": "442000000",
        "uvk5.tot": "120",
        "tx.tot": "0",  # global cap disabled...
    })
    assert isinstance(app.state.radio, TotRadio)
    assert app.state.radio.tot == 120.0  # ...but the UV-K6 keeps its own mandatory cap


def test_non_uvk5_backend_uses_the_global_tx_tot(tmp_path, monkeypatch):
    _install_stub(monkeypatch)
    app = _built_app(tmp_path, {"server.backend": "mock", "tx.tot": "90"})
    assert isinstance(app.state.radio, TotRadio)
    assert app.state.radio.tot == 90.0
