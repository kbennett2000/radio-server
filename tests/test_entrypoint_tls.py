"""What the entrypoint hands uvicorn: TLS resolution (ADR 0039) and the ws keepalive (ADR 0171).

Both TLS paths empty → plain HTTP (no ssl kwargs). Both set and readable → uvicorn ssl kwargs.
Anything in between — only one set, or a configured file that isn't readable — fails loud rather
than silently downgrading to insecure HTTP (a phone needs HTTPS for mic + audio).

And the WebSocket ping kwargs are asserted on the real ``uvicorn.run`` call, because a reap that
depends on them is only as durable as the line that passes them.
"""

from __future__ import annotations

import pytest

import radio_server.__main__ as entry
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


# --- the WebSocket keepalive the RX reap depends on (ADR 0171) ---------------------------------


def _run_main(monkeypatch, tmp_path) -> dict:
    """Run the real ``main()`` with only uvicorn and app-building stubbed, and capture the kwargs."""
    captured: dict = {}
    monkeypatch.setattr(entry, "build_app", lambda *a, **k: object())
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kwargs: captured.update(kwargs))
    # Left alone, these mutate process-global logging state for the rest of the session.
    monkeypatch.setattr(entry.logsafe, "install", lambda: False)
    monkeypatch.setattr(entry.logsafe, "register_secret_values", lambda secrets: None)
    entry.main(
        ["--config", str(tmp_path / "absent.toml"), "--secrets", str(tmp_path / "absent.toml")]
    )
    return captured


def test_main_passes_the_websocket_ping_kwargs(monkeypatch, tmp_path):
    # The presence half of the pin. ADR 0171's reap of an RST'd listener happens because uvicorn
    # posts an ASGI disconnect when its keepalive fails — so a refactor that tidies these kwargs away
    # would silently restore the leak, with every other test still green and no symptom until a
    # listener quietly stayed counted again. This is the test that fails loudly instead.
    captured = _run_main(monkeypatch, tmp_path)
    assert captured["ws_ping_interval"] == entry.WS_PING_INTERVAL_SECONDS
    assert captured["ws_ping_timeout"] == entry.WS_PING_TIMEOUT_SECONDS


def test_the_websocket_ping_values_are_the_measured_ones(monkeypatch, tmp_path):
    # The value half. Asserting the literals rather than the constants is deliberate: these are
    # measured numbers, not preferences. Against real uvicorn (ADR 0171) they put the reap window at
    # ~20 s for a peer that RSTs — the keepalive *write* fails — and ~40 s for one that goes silent
    # with its socket still open, which is interval + timeout. Change either and the window moves,
    # so changing either should have to come past a failing test and into an ADR.
    captured = _run_main(monkeypatch, tmp_path)
    assert (captured["ws_ping_interval"], captured["ws_ping_timeout"]) == (20.0, 20.0)
