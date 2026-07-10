"""The LAN-facing HTTP/WebSocket API over an injected ``Radio`` (ADR 0011).

REST + WebSocket surface with the capability split at the HTTP boundary (guardrail 3) and a
shared-secret bearer-token auth plane kept separate from the over-RF TOTP/DTMF plane. No
server binds here; `create_app` is the DI seam for tests, `build_app` the composition root.
"""

from .app import RADIO_BACKEND_ENV_VAR, build_app, create_app
from .auth import (
    RADIO_API_TOKEN_ENV_VAR,
    bearer_token,
    make_require_token,
    token_matches,
)
from .events import EVENT_TYPES, Event, EventHub, status_event

__all__ = [
    "EVENT_TYPES",
    "Event",
    "EventHub",
    "RADIO_API_TOKEN_ENV_VAR",
    "RADIO_BACKEND_ENV_VAR",
    "bearer_token",
    "build_app",
    "create_app",
    "make_require_token",
    "status_event",
    "token_matches",
]
