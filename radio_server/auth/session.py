"""Session state machine + the single DTMF entry point.

Two states. An unauthenticated session routes incoming digits to TOTP verification;
an authenticated one routes them to command dispatch. Inactivity closes the session
back to unauthenticated. Everything is time-driven through an injected clock so the
whole machine is unit-tested with a fake clock and no real sleeps.

Command dispatch itself is cycle 3 — the hook is injectable and stubbed here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from collections.abc import Callable

from .totp import Clock, TotpVerifier


class SessionState(StrEnum):
    UNAUTHENTICATED = "unauthenticated"
    AUTHENTICATED = "authenticated"


class OutcomeKind(StrEnum):
    ACCEPTED = "accepted"  # a valid code just authenticated the session
    REJECTED = "rejected"  # digits failed TOTP verification
    COMMAND = "command"  # authenticated digits routed to command dispatch


@dataclass(frozen=True)
class Outcome:
    """Result of a single `on_dtmf` call.

    The caller (a later audio-feedback cycle) maps this to CW/voice; nothing here
    produces audio. `detail` carries the dispatch hook's return value for COMMAND.
    """

    kind: OutcomeKind
    detail: object = None


@dataclass
class Session:
    """Per-caller auth state. Mutable — `AuthGate` advances it in place."""

    state: SessionState = SessionState.UNAUTHENTICATED
    last_activity: float = 0.0

    @property
    def authenticated(self) -> bool:
        return self.state is SessionState.AUTHENTICATED


# Cycle 3 replaces this. Kept as a loud-but-harmless default so an unconfigured gate
# doesn't silently pretend to run commands.
def _unwired_dispatch(digits: str, session: Session) -> object:
    return f"dispatch not wired (cycle 3): {digits}"


Dispatch = Callable[[str, "Session"], object]


class AuthGate:
    """Routes DTMF digit strings by session state; owns the timeout clock.

    `on_dtmf` is the single entry point. The gate holds no per-caller state itself —
    the `Session` does — so one gate serves many sessions.
    """

    def __init__(
        self,
        verifier: TotpVerifier,
        *,
        timeout: float = 300.0,
        clock: Clock | None = None,
        dispatch: Dispatch = _unwired_dispatch,
    ) -> None:
        if clock is None:
            import time

            clock = time.time
        self._verifier = verifier
        self._timeout = timeout
        self._clock = clock
        self._dispatch = dispatch

    def on_dtmf(
        self, digits: str, session: Session, now: float | None = None
    ) -> Outcome:
        if now is None:
            now = self._clock()

        # Inactivity check happens before we stamp this event: an authenticated
        # session idle longer than the timeout is dropped, and these digits are then
        # treated as a fresh authentication attempt rather than a command.
        if session.authenticated and (now - session.last_activity) > self._timeout:
            session.state = SessionState.UNAUTHENTICATED

        session.last_activity = now

        if not session.authenticated:
            if self._verifier.verify_and_burn(digits, now):
                session.state = SessionState.AUTHENTICATED
                return Outcome(OutcomeKind.ACCEPTED)
            return Outcome(OutcomeKind.REJECTED)

        result = self._dispatch(digits, session)
        return Outcome(OutcomeKind.COMMAND, detail=result)
