"""``python -m radio_server`` — the ASGI entrypoint that binds the app to a port (ADR 0022).

Until this cycle nothing served ``build_app()``: the composition root existed, but no process
bound it. This module is that missing seam — it reads the bind address from the environment and
hands the env-composed app to uvicorn. It stays deliberately thin: all wiring lives in
``build_app`` (which fails loud when ``RADIO_API_TOKEN`` is unset), so this file only decides
*where* to listen.

    RADIO_API_TOKEN=secret python -m radio_server        # mock server + web UI on 127.0.0.1:8000
    RADIO_HOST=0.0.0.0 RADIO_API_TOKEN=secret python -m radio_server   # reachable on the LAN
"""

from __future__ import annotations

import os

import uvicorn

from .api import build_app

#: Bind host. Marked default ``127.0.0.1`` (loopback only) — safe by default; set ``RADIO_HOST``
#: to ``0.0.0.0`` to expose the API to the LAN it is meant to serve.
RADIO_HOST_ENV_VAR = "RADIO_HOST"
DEFAULT_HOST = "127.0.0.1"

#: Bind port. Marked default ``8000`` (uvicorn's conventional port).
RADIO_PORT_ENV_VAR = "RADIO_PORT"
DEFAULT_PORT = 8000


def main(env: dict[str, str] | os._Environ = os.environ) -> None:
    """Compose the app from ``env`` and serve it with uvicorn on ``RADIO_HOST``/``RADIO_PORT``."""
    host = env.get(RADIO_HOST_ENV_VAR, DEFAULT_HOST)
    port = int(env.get(RADIO_PORT_ENV_VAR, str(DEFAULT_PORT)))
    # build_app() fails loud here if RADIO_API_TOKEN is unset — the server never binds open.
    uvicorn.run(build_app(env), host=host, port=port)


if __name__ == "__main__":  # pragma: no cover - exercised by running the module, not pytest
    main()
