"""``python -m radio_server`` — the ASGI entrypoint that binds the app to a port (ADR 0022, 0025).

This module is the thin bootstrap: it reads the config-file path from ``--config`` (the ONE pointer
that cannot itself live in the file), resolves the schema-driven `Settings` and the separate
`Secrets`, and hands the composed app to uvicorn on ``server.host``/``server.port``. All wiring lives
in ``build_app``, which fails loud when the API token secret is unset — so the server never binds
open.

    RADIO_API_TOKEN=secret python -m radio_server                    # mock server + web UI, defaults
    python -m radio_server --config /etc/radio-server/radio.toml     # explicit config file

The API token (and TOTP secret) are secrets: they come from ``radio-secrets.toml`` (chmod 600) or
the environment, never ``radio.toml``. Everything else — including the bind host/port — is a setting
in ``radio.toml``.
"""

from __future__ import annotations

import argparse
import os

import uvicorn

from .api import build_app
from .config import DEFAULT_CONFIG_PATH, DEFAULT_SECRETS_PATH, load_secrets, load_settings


def _tls_kwargs(settings) -> dict[str, str]:
    """Resolve optional HTTPS (ADR 0039). Both ``server.tls_cert`` and ``server.tls_key`` empty →
    plain HTTP (``{}``). Both set → ``ssl_certfile``/``ssl_keyfile`` for uvicorn. Anything in
    between — only one set, or a configured file that is missing/unreadable — fails loud here rather
    than silently downgrading to insecure HTTP (a phone needs HTTPS for mic + audio to work)."""
    cert = (settings.get("server.tls_cert") or "").strip()
    key = (settings.get("server.tls_key") or "").strip()
    if not cert and not key:
        return {}
    if bool(cert) != bool(key):
        missing = "server.tls_key" if cert else "server.tls_cert"
        raise RuntimeError(
            f"HTTPS is half-configured: {missing} is empty. Set BOTH server.tls_cert and "
            "server.tls_key to serve HTTPS, or clear both for plain HTTP (ADR 0039)."
        )
    for label, path in (("server.tls_cert", cert), ("server.tls_key", key)):
        if not os.access(path, os.R_OK):
            raise RuntimeError(f"{label}={path!r} is not a readable file (generate one with "
                               "scripts/gen-selfsigned-cert.sh).")
    return {"ssl_certfile": cert, "ssl_keyfile": key}


def main(argv: list[str] | None = None) -> None:
    """Resolve settings + secrets and serve the composed app on ``server.host``/``server.port``."""
    parser = argparse.ArgumentParser(prog="radio_server", description="Serve the radio-server API.")
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"path to the TOML config file (default: {DEFAULT_CONFIG_PATH}; "
        "a missing file falls back to built-in defaults)",
    )
    parser.add_argument(
        "--secrets",
        default=str(DEFAULT_SECRETS_PATH),
        help=f"path to the 0600 secrets file (default: {DEFAULT_SECRETS_PATH}; "
        "falls back to RADIO_TOTP_SECRET / RADIO_API_TOKEN in the environment)",
    )
    args = parser.parse_args(argv)
    settings = load_settings(args.config)
    secrets = load_secrets(args.secrets)
    # build_app() fails loud here if the API token secret is unset — the server never binds open. The
    # config/secrets paths are threaded through so the settings API (ADR 0026) persists to the same
    # files this process read.
    tls = _tls_kwargs(settings)  # fails loud on a half-configured / unreadable cert (ADR 0039)
    uvicorn.run(
        build_app(settings, secrets, config_path=args.config, secrets_path=args.secrets),
        host=settings.get("server.host"),
        port=settings.get("server.port"),
        **tls,
    )


if __name__ == "__main__":  # pragma: no cover - exercised by running the module, not pytest
    main()
