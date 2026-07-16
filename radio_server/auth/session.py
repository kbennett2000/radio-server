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
        verifier: TotpVerifier | None,
        *,
        timeout: float = 300.0,
        clock: Clock | None = None,
        dispatch: Dispatch = _unwired_dispatch,
        enforce: bool = True,
    ) -> None:
        if clock is None:
            import time

            clock = time.time
        self._verifier = verifier
        self._timeout = timeout
        self._clock = clock
        self._dispatch = dispatch
        #: When False, TOTP auth is disabled (ADR 0048): every DTMF entry routes straight to
        #: dispatch with no code, no burn, and no rejection — the session is implicitly
        #: authenticated. `verifier` may be None in that mode (it is never consulted).
        self._enforce = enforce

    def expire_if_idle(self, session: Session, now: float | None = None) -> bool:
        """Drop an authenticated session that has been idle past the timeout.

        Returns ``True`` iff a session was actually closed by this call. This is the
        polling seam a real controller loop calls each tick (mirroring
        `DtmfFramer.tick`): the state machine only demotes lazily inside `on_dtmf`, so
        a session that goes idle with *no* further digits would otherwise never close
        (and its station ID would never sign off). Calling this makes the inactivity
        transition observable without feeding a key.
        """
        if now is None:
            now = self._clock()
        if session.authenticated and (now - session.last_activity) > self._timeout:
            session.state = SessionState.UNAUTHENTICATED
            return True
        return False

    def logout(self, session: Session) -> bool:
        """Demote a session to unauthenticated on demand — a deliberate close.

        The active-close analog of the idle demotion inside `expire_if_idle`: no timeout check, just
        an unconditional sign-off. Backs the ``99#`` force-logout command. Returns ``True`` iff a
        session was actually closed (it was authenticated), so the caller can skip announcing a
        logout for an already-closed session.
        """
        if not session.authenticated:
            return False
        session.state = SessionState.UNAUTHENTICATED
        return True

    def open(self, session: Session, now: float | None = None) -> bool:
        """Authenticate a session directly, without a TOTP code — the operator seam (ADR 0046).

        Backs the web UI's session-open: the caller already proved itself on the LAN token plane,
        so no code is verified and none is burned (an RF caller's code stays valid in its window).
        Always stamps ``last_activity`` (an open on an already-open session refreshes the
        inactivity clock). Returns ``True`` iff the session was newly opened.
        """
        if now is None:
            now = self._clock()
        session.last_activity = now
        if session.authenticated:
            return False
        session.state = SessionState.AUTHENTICATED
        return True

    def on_dtmf(
        self, digits: str, session: Session, now: float | None = None
    ) -> Outcome:
        if now is None:
            now = self._clock()

        # Inactivity check happens before we stamp this event: an authenticated
        # session idle longer than the timeout is dropped, and these digits are then
        # treated as a fresh authentication attempt rather than a command.
        self.expire_if_idle(session, now)

        session.last_activity = now

        # TOTP disabled (ADR 0048): no login step — implicitly authenticate and route every entry
        # straight to command dispatch. The idle expiry above still runs so the controller's
        # session lifecycle (station ID arm / sign-off) stays symmetric with the enforced path.
        if not self._enforce:
            session.state = SessionState.AUTHENTICATED
            result = self._dispatch(digits, session)
            return Outcome(OutcomeKind.COMMAND, detail=result)

        if not session.authenticated:
            if self._verifier.verify_and_burn(digits, now):
                session.state = SessionState.AUTHENTICATED
                return Outcome(OutcomeKind.ACCEPTED)
            return Outcome(OutcomeKind.REJECTED)

        result = self._dispatch(digits, session)
        return Outcome(OutcomeKind.COMMAND, detail=result)
