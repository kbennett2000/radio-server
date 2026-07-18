"""DTMF-gated TOTP authentication and session state machine.

Backend-agnostic: operates on digit strings, not audio. `AuthGate.on_dtmf` is the
single entry point a caller feeds decoded DTMF into.
"""

from .fixed import FixedCodeVerifier
from .session import (
    AuthGate,
    Dispatch,
    Outcome,
    OutcomeKind,
    Session,
    SessionState,
)
from .totp import SECRET_ENV_VAR, Clock, TotpVerifier

__all__ = [
    "AuthGate",
    "Clock",
    "Dispatch",
    "FixedCodeVerifier",
    "Outcome",
    "OutcomeKind",
    "SECRET_ENV_VAR",
    "Session",
    "SessionState",
    "TotpVerifier",
]
