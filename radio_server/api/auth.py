"""The LAN-facing HTTP auth plane — a static shared-secret bearer token (ADR 0011).

This is a **separate** auth plane from the over-RF TOTP/DTMF one in ``radio_server.auth``.
The threat models differ: the RF plane fights code replay over a broadcast channel, so it
windows and single-use-burns each TOTP code; this plane guards a wired LAN API, so it is a
plain static secret compared in constant time — no window, no burn, no per-caller state
machine. It reuses none of ``TotpVerifier``/``AuthGate``/``Session`` by design.

The API is closed by default: a request without a valid token is rejected ``401``. The token is a
secret, loaded fail-loud by `radio_server.config.load_secrets` (from ``radio-secrets.toml`` or the
``RADIO_API_TOKEN`` env var), never from ``radio.toml`` — a secret must never be rendered or
round-tripped through the settings surface (ADR 0025). `build_app` requires it via
`Secrets.require`, so an unset token stops the server from binding open.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

#: Env var name for the LAN API bearer token (the secrets channel's documented env fallback). Never
#: hardcode a secret.
RADIO_API_TOKEN_ENV_VAR = "RADIO_API_TOKEN"


def token_matches(presented: str | None, expected: str) -> bool:
    """Constant-time compare a presented token against the configured one.

    `hmac.compare_digest` avoids leaking the token via timing. A missing/empty presented
    token never matches.
    """
    if not presented:
        return False
    return hmac.compare_digest(presented, expected)


def bearer_token(authorization: str | None) -> str | None:
    """Extract the token from an ``Authorization: Bearer <token>`` header, or ``None``."""
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def make_require_token(expected: str):
    """Build a FastAPI dependency that enforces the bearer token on a REST route.

    Closes over the app's configured token so handlers don't reach into the environment.
    Rejects a missing or non-matching token with ``401`` and a ``WWW-Authenticate: Bearer``
    challenge; returns ``None`` on success (the route needs no auth value, just the gate).
    """

    def require_token(authorization: str | None = Header(default=None)) -> None:
        if not token_matches(bearer_token(authorization), expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing or invalid API token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return require_token
