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

import uvicorn

from .api import build_app
from .config import DEFAULT_SECRETS_PATH, load_secrets, load_settings

#: Default config file location: ``radio.toml`` in the working directory (self-hosting-friendly,
#: consistent with the other CWD-relative defaults like the log path).
DEFAULT_CONFIG_PATH = "radio.toml"


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
    # build_app() fails loud here if the API token secret is unset — the server never binds open.
    uvicorn.run(
        build_app(settings, secrets),
        host=settings.get("server.host"),
        port=settings.get("server.port"),
    )


if __name__ == "__main__":  # pragma: no cover - exercised by running the module, not pytest
    main()
