"""Command dispatch: registry routing and the full auth → dispatch → tx_log path.

Everything runs on a FakeClock (from conftest) and a fresh MockRadio built inline, per
the existing test convention. The time service is bound to UTC so the spoken string is
deterministic regardless of the host timezone.
"""

from zoneinfo import ZoneInfo

import pytest

from radio_server.auth import AuthGate, OutcomeKind, Session, SessionState
from radio_server.backends import MockRadio
from radio_server.services import (
    Dispatcher,
    ServiceContext,
    ServiceRegistry,
    StubTts,
    format_spoken_time,
    register,
)

from .conftest import INTERVAL

TZ = ZoneInfo("UTC")


def build_gate(radio, verifier, clock, *, timeout=120.0):
    """Wire an AuthGate whose command hook is the real dispatcher (time service on '1').

    The gate and the service share the one injected `clock`, so the announced time is
    driven by the same source as the session timeout.
    """
    registry = ServiceRegistry()
    register(registry, TZ)
    ctx = ServiceContext(clock=clock, tts=StubTts())
    dispatcher = Dispatcher(radio, ctx, registry)
    return AuthGate(verifier, timeout=timeout, clock=clock, dispatch=dispatcher)


def expected_time_audio(now):
    return StubTts().render(format_spoken_time(now, TZ))


# --- dispatcher unit (no auth) ----------------------------------------------


def test_dispatcher_transmits_registered_service(clock):
    radio = MockRadio()
    registry = ServiceRegistry()
    register(registry, TZ)
    dispatcher = Dispatcher(radio, ServiceContext(clock=clock, tts=StubTts()), registry)

    result = dispatcher("1", Session(state=SessionState.AUTHENTICATED))

    assert result.service == "time"
    assert result.transmitted is True
    assert radio.tx_log == [expected_time_audio(clock.now)]


def test_dispatcher_unknown_digit_does_not_transmit(clock):
    radio = MockRadio()
    registry = ServiceRegistry()
    register(registry, TZ)
    dispatcher = Dispatcher(radio, ServiceContext(clock=clock, tts=StubTts()), registry)

    result = dispatcher("7", Session(state=SessionState.AUTHENTICATED))

    assert result.service is None
    assert result.transmitted is False
    assert radio.tx_log == []


# --- through the auth gate ---------------------------------------------------


def test_authenticated_one_announces_the_time(verifier, clock, code_for):
    radio = MockRadio()
    gate = build_gate(radio, verifier, clock)
    session = Session()
    gate.on_dtmf(code_for(clock.now), session)  # authenticate
    assert radio.tx_log == []  # the auth code itself never transmits

    outcome = gate.on_dtmf("1", session)
    assert outcome.kind is OutcomeKind.COMMAND
    assert outcome.detail.service == "time"
    assert outcome.detail.transmitted is True
    assert radio.tx_log == [expected_time_audio(clock.now)]


def test_authenticated_unknown_digit_is_graceful_no_transmit(verifier, clock, code_for):
    radio = MockRadio()
    gate = build_gate(radio, verifier, clock)
    session = Session()
    gate.on_dtmf(code_for(clock.now), session)

    outcome = gate.on_dtmf("7", session)
    assert outcome.kind is OutcomeKind.COMMAND
    assert outcome.detail.transmitted is False
    assert radio.tx_log == []


def test_unauthenticated_one_routes_to_totp_not_dispatch(verifier, clock):
    radio = MockRadio()
    gate = build_gate(radio, verifier, clock)
    session = Session()

    outcome = gate.on_dtmf("1", session)  # "1" is not a valid TOTP code
    assert outcome.kind is OutcomeKind.REJECTED
    assert session.state is SessionState.UNAUTHENTICATED
    assert radio.tx_log == []  # dispatcher never reached


def test_full_path_enroll_authenticate_announce(verifier, clock, code_for):
    radio = MockRadio()
    gate = build_gate(radio, verifier, clock)
    session = Session()

    # Authenticate with the code for the current step, then ask for the time.
    assert gate.on_dtmf(code_for(clock.now), session).kind is OutcomeKind.ACCEPTED
    gate.on_dtmf("1", session)
    assert radio.tx_log == [b"<audio:The time is 13:46 UTC>"]

    # Advancing the shared clock (still within the timeout) changes the announced time,
    # proving the service reads the same clock the session does.
    clock.advance(60.0)
    gate.on_dtmf("1", session)
    assert radio.tx_log[-1] == b"<audio:The time is 13:47 UTC>"
    assert radio.tx_log[-1] == expected_time_audio(clock.now)
