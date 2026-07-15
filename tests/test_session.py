"""Session state machine: routing, single-use, timeout, dispatch handoff."""

import pytest

from radio_server.auth import (
    AuthGate,
    OutcomeKind,
    Session,
    SessionState,
)

from .conftest import INTERVAL


@pytest.fixture
def gate(verifier, clock):
    # Short timeout so the fake clock can cross it cheaply.
    return AuthGate(verifier, timeout=120.0, clock=clock)


def test_fresh_valid_code_authenticates(gate, clock, code_for):
    session = Session()
    outcome = gate.on_dtmf(code_for(clock.now), session)
    assert outcome.kind is OutcomeKind.ACCEPTED
    assert session.state is SessionState.AUTHENTICATED


def test_replayed_code_is_rejected(gate, clock, code_for):
    first = Session()
    code = code_for(clock.now)
    assert gate.on_dtmf(code, first).kind is OutcomeKind.ACCEPTED

    # A different caller replays the overheard code inside its window: refused.
    replayer = Session()
    outcome = gate.on_dtmf(code, replayer)
    assert outcome.kind is OutcomeKind.REJECTED
    assert replayer.state is SessionState.UNAUTHENTICATED


def test_rejected_code_leaves_session_unauthenticated(gate, clock):
    session = Session()
    outcome = gate.on_dtmf("000000", session)
    assert outcome.kind is OutcomeKind.REJECTED
    assert session.state is SessionState.UNAUTHENTICATED


def test_expired_step_code_is_rejected(gate, clock, code_for):
    session = Session()
    stale = code_for(clock.now)
    clock.advance(2 * INTERVAL)  # outside the ±1 window
    outcome = gate.on_dtmf(stale, session)
    assert outcome.kind is OutcomeKind.REJECTED
    assert session.state is SessionState.UNAUTHENTICATED


def test_previous_and_next_step_codes_authenticate(gate, clock, code_for):
    prev = Session()
    assert gate.on_dtmf(code_for(clock.now - INTERVAL), prev).kind is OutcomeKind.ACCEPTED

    nxt = Session()
    assert gate.on_dtmf(code_for(clock.now + INTERVAL), nxt).kind is OutcomeKind.ACCEPTED


def test_inactivity_timeout_drops_session(gate, clock, code_for):
    session = Session()
    gate.on_dtmf(code_for(clock.now), session)
    assert session.state is SessionState.AUTHENTICATED

    # Idle past the timeout, then send digits: the session is dropped and these
    # digits are treated as a fresh (failing) auth attempt, not a command.
    clock.advance(121.0)
    outcome = gate.on_dtmf("999999", session)
    assert session.state is SessionState.UNAUTHENTICATED
    assert outcome.kind is OutcomeKind.REJECTED


def test_activity_within_timeout_keeps_session(gate, clock, code_for):
    session = Session()
    gate.on_dtmf(code_for(clock.now), session)

    # Two sub-timeout gaps that sum past the timeout must NOT expire the session,
    # because each event refreshes last_activity.
    clock.advance(100.0)
    assert gate.on_dtmf("1", session).kind is OutcomeKind.COMMAND
    clock.advance(100.0)
    assert gate.on_dtmf("2", session).kind is OutcomeKind.COMMAND
    assert session.state is SessionState.AUTHENTICATED


def test_authenticated_digits_route_to_dispatch(verifier, clock, code_for):
    calls = []

    def spy(digits, session):
        calls.append(digits)
        return {"ran": digits}

    gate = AuthGate(verifier, timeout=120.0, clock=clock, dispatch=spy)
    session = Session()
    gate.on_dtmf(code_for(clock.now), session)  # authenticate first
    assert calls == []  # auth code must NOT reach dispatch

    outcome = gate.on_dtmf("42", session)
    assert outcome.kind is OutcomeKind.COMMAND
    assert outcome.detail == {"ran": "42"}
    assert calls == ["42"]


def test_default_dispatch_is_stubbed_not_wired(gate, clock, code_for):
    session = Session()
    gate.on_dtmf(code_for(clock.now), session)
    outcome = gate.on_dtmf("5", session)
    assert outcome.kind is OutcomeKind.COMMAND
    assert "not wired" in str(outcome.detail)


# --- expire_if_idle: the polling seam the controller loop calls each tick -----------------

def test_expire_if_idle_closes_an_idle_authenticated_session(gate, clock, code_for):
    session = Session()
    gate.on_dtmf(code_for(clock.now), session)
    assert session.authenticated

    clock.advance(121.0)  # idle past the 120s timeout
    assert gate.expire_if_idle(session, clock.now) is True
    assert session.state is SessionState.UNAUTHENTICATED
    # Idempotent: a closed session is not "closed" again.
    assert gate.expire_if_idle(session, clock.now) is False


def test_expire_if_idle_leaves_a_fresh_session_alone(gate, clock, code_for):
    session = Session()
    gate.on_dtmf(code_for(clock.now), session)

    clock.advance(60.0)  # still within the timeout
    assert gate.expire_if_idle(session, clock.now) is False
    assert session.authenticated
    # An unauthenticated session is never "closed" either.
    assert gate.expire_if_idle(Session(), clock.now) is False


# --- logout: the deliberate-close seam (99# force logout) ---------------------------------

def test_logout_demotes_an_authenticated_session(gate, clock, code_for):
    session = Session()
    gate.on_dtmf(code_for(clock.now), session)
    assert session.authenticated

    assert gate.logout(session) is True  # closed a live session
    assert session.state is SessionState.UNAUTHENTICATED


def test_logout_of_an_unauthenticated_session_is_a_noop(gate):
    # Nothing to close (e.g. a web logout with no active RF session) — reports False, no error.
    assert gate.logout(Session()) is False
