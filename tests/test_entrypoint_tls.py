"""The entrypoint's optional-HTTPS resolution (ADR 0039): ``_tls_kwargs``.

Both TLS paths empty → plain HTTP (no ssl kwargs). Both set and readable → uvicorn ssl kwargs.
Anything in between — only one set, or a configured file that isn't readable — fails loud rather
than silently downgrading to insecure HTTP (a phone needs HTTPS for mic + audio).
"""

from __future__ import annotations

import pytest

from radio_server.__main__ import _tls_kwargs
from radio_server.config import resolve_settings


def test_no_tls_paths_yields_plain_http():
    assert _tls_kwargs(resolve_settings({})) == {}


def test_both_paths_set_and_readable_yields_ssl_kwargs(tmp_path):
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("x")
    key.write_text("y")
    kwargs = _tls_kwargs(
        resolve_settings({"server.tls_cert": str(cert), "server.tls_key": str(key)})
    )
    assert kwargs == {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}


@pytest.mark.parametrize("present, missing", [("server.tls_cert", "server.tls_key"),
                                              ("server.tls_key", "server.tls_cert")])
def test_half_configured_tls_fails_loud(tmp_path, present, missing):
    f = tmp_path / "f.pem"
    f.write_text("x")
    with pytest.raises(RuntimeError, match=missing.replace(".", r"\.")):
        _tls_kwargs(resolve_settings({present: str(f)}))


def test_unreadable_cert_fails_loud(tmp_path):
    key = tmp_path / "key.pem"
    key.write_text("y")
    with pytest.raises(RuntimeError, match="server.tls_cert"):
        _tls_kwargs(
            resolve_settings(
                {"server.tls_cert": str(tmp_path / "nope.pem"), "server.tls_key": str(key)}
            )
        )
