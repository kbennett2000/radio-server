"""DTMF-gated TOTP authentication and session state machine.

Backend-agnostic: operates on digit strings, not audio. `AuthGate.on_dtmf` is the
single entry point a caller feeds decoded DTMF into.
"""

from .session import (
    AuthGate,
    Dispatch,
    Outcome,
    OutcomeKind,
    Session,
    SessionState,
)
from .totp import SECRET_ENV_VAR, Clock, TotpVerifier, load_totp_secret

__all__ = [
    "AuthGate",
    "Clock",
    "Dispatch",
    "Outcome",
    "OutcomeKind",
    "SECRET_ENV_VAR",
    "Session",
    "SessionState",
    "TotpVerifier",
    "load_totp_secret",
]
