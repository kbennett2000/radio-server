"""The TOTP enroll CLI (`python -m radio_server.enroll`): mint a secret, print URI (+ QR), 0600 file.

Hardware-free and network-free: `enroll` writes to a `tmp_path` secrets file and is driven with an
isolated `env={}` (so an ambient `RADIO_TOTP_SECRET` can't leak in). The QR path is guarded by
`importorskip` on the optional `qrcode` package.
"""

from __future__ import annotations

import io
import stat

import pytest

from radio_server.config.secrets import load_secrets
from radio_server.enroll import enroll, main

_BASE32 = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")


def _run(tmp_path, *, account="AE9S", **kwargs):
    out = io.StringIO()
    path = tmp_path / "radio-secrets.toml"
    code = enroll(path, account, out=out, env={}, **kwargs)
    return code, out.getvalue(), path


def test_enroll_mints_and_persists_a_base32_secret_0600(tmp_path):
    code, text, path = _run(tmp_path)
    assert code == 0
    secret = load_secrets(path, env={}).totp_secret
    assert secret and set(secret) <= _BASE32  # a base32 TOTP secret
    # The minted secret and an otpauth URI (with the account label) are shown to the operator.
    assert secret in text
    assert "otpauth://totp/" in text
    assert "AE9S" in text
    # The secrets file is written 0600.
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_enroll_refuses_existing_secret_without_force(tmp_path):
    code1, _, path = _run(tmp_path)
    assert code1 == 0
    first = load_secrets(path, env={}).totp_secret
    # A second run without --force refuses (non-zero) and leaves the secret unchanged.
    code2, text2, _ = _run(tmp_path)
    assert code2 == 1
    assert "--force" in text2
    assert load_secrets(path, env={}).totp_secret == first


def test_enroll_force_replaces_the_secret(tmp_path):
    _run(tmp_path)
    first = load_secrets(tmp_path / "radio-secrets.toml", env={}).totp_secret
    code, _, path = _run(tmp_path, force=True)
    assert code == 0
    assert load_secrets(path, env={}).totp_secret != first


def test_enroll_falls_back_to_uri_when_qrcode_absent(tmp_path, monkeypatch):
    import radio_server.enroll as enroll_mod

    # Simulate the optional dep being absent → the URI/secret fallback path with an install hint.
    monkeypatch.setattr(enroll_mod, "_render_qr", lambda uri: None)
    code, text, _ = _run(tmp_path)
    assert code == 0
    assert "otpauth://" in text
    assert "qrcode" in text.lower()


def test_enroll_renders_a_qr_when_qrcode_is_available(tmp_path):
    pytest.importorskip("qrcode")
    code, text, _ = _run(tmp_path)
    assert code == 0
    assert "Scan this QR" in text  # the QR banner (the art itself follows)


def test_main_wires_args_and_writes_the_named_file(tmp_path, monkeypatch):
    monkeypatch.delenv("RADIO_TOTP_SECRET", raising=False)  # isolate from the ambient environment
    path = tmp_path / "custom-secrets.toml"
    rc = main(["--secrets", str(path), "--account", "W1AW"])
    assert rc == 0
    assert load_secrets(path, env={}).totp_secret
