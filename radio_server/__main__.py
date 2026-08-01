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
import logging
import os

import uvicorn

from . import logsafe
from .api import build_app
from .config import DEFAULT_CONFIG_PATH, DEFAULT_SECRETS_PATH, load_secrets, load_settings

#: Seconds uvicorn waits for in-flight connections/tasks before cancelling them on SIGTERM
#: (ADR 0127). uvicorn's default is ``None`` = wait forever, and a browser holding ``/audio/rx``
#: or ``/events`` open never closes on its own — so every ``systemctl stop`` sat in "Waiting for
#: background tasks to complete", blew ``TimeoutStopSec``, and was SIGKILLed (measured: 20.0 s,
#: 7 times in 14 h). With a bound, uvicorn cancels the stragglers and STILL runs the lifespan
#: teardown (``force_exit`` is only set by a second SIGINT), so the radio is still unkeyed, the
#: decoder reaped and the event log flushed on the way out. Keep this well under the unit's
#: ``TimeoutStopSec`` so the bounded lifespan teardown (ADR 0104) fits in the remaining budget.
GRACEFUL_SHUTDOWN_SECONDS = 5.0

#: WebSocket keepalive, pinned rather than inherited (ADR 0171). **The reap of a dead RX listener
#: depends on these two values**, so leaving them to uvicorn's defaults meant a load-bearing
#: behaviour rested on a number this project had never chosen: a version bump could change or drop
#: it and nothing here would notice, and the symptom would be listeners quietly staying counted
#: again — precisely the defect ADR 0171 exists to fix. (They ARE uvicorn's current defaults;
#: measured identical with and without the kwargs, so pinning changes nothing today. That is the
#: point — it is a dependency made visible, not a tuning change.)
#:
#: What they buy, measured against real uvicorn on the production ``websockets`` path: a peer that
#: RSTs is reaped after ~20 s, because the keepalive *write* fails; a peer that goes silent with its
#: socket still open takes ~40 s, the full interval-plus-timeout. Report that spread as a WINDOW
#: rather than a single figure — it falls out of both values and cannot be moved by changing one.
#: Halving them roughly halves the window at the cost of doubling ping traffic on every socket,
#: including ``/audio/tx`` mid-transmission; no measurement has yet asked for that.
WS_PING_INTERVAL_SECONDS = 20.0
WS_PING_TIMEOUT_SECONDS = 20.0


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
    # FIRST statement, before anything in this process can log (ADR 0165). The WebSocket sockets
    # authenticate with `?token=` in the query string — a browser cannot set a header on a
    # `WebSocket` — and uvicorn logs the full path on every accept, so the LAN token was landing in
    # journald once per connect. Redaction is installed at the record factory, upstream of every
    # handler, because uvicorn's loggers do not propagate to root and would miss a filter there.
    logsafe.install()
    # Root logging at INFO (ADR 0107): uvicorn configures only its OWN loggers, so without this
    # every radio_server module log below WARNING — the per-over lines (ADR 0106), the decode
    # throughput probe, dongle-recovery notices — was silently dropped instead of reaching journald.
    # It sits up here rather than just above `uvicorn.run` because everything logged during startup
    # was being dropped for the same reason, `register_secret_values`' report included — found by
    # running the real entrypoint and noticing the line was missing (ADR 0165).
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(name)s: %(message)s")
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
    # The backstop layer, armed once the values are known: it catches a secret logged in a shape no
    # `name=value` pattern can span. Names are reported, never values — and so are the ones skipped
    # for being too short to redact safely, since that is a real gap an operator should be able to
    # see rather than assume away.
    logsafe.register_secret_values(secrets)
    # build_app() fails loud here if the API token secret is unset — the server never binds open. The
    # config/secrets paths are threaded through so the settings API (ADR 0026) persists to the same
    # files this process read.
    tls = _tls_kwargs(settings)  # fails loud on a half-configured / unreadable cert (ADR 0039)
    uvicorn.run(
        build_app(settings, secrets, config_path=args.config, secrets_path=args.secrets),
        host=settings.get("server.host"),
        port=settings.get("server.port"),
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
        ws_ping_interval=WS_PING_INTERVAL_SECONDS,
        ws_ping_timeout=WS_PING_TIMEOUT_SECONDS,
        **tls,
    )


if __name__ == "__main__":  # pragma: no cover - exercised by running the module, not pytest
    main()
