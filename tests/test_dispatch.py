"""Command dispatch: registry routing and the full auth → dispatch → tx_log path.

Everything runs on a FakeClock (from conftest) and a fresh MockRadio built inline, per
the existing test convention. The time service is bound to UTC so the spoken string is
deterministic regardless of the host timezone. Since cycle 4 the dispatcher transmits
through a `StationId`, so the first over of a session carries the station ID; that is
asserted here and exercised in depth in test_station_id.py.
"""

from zoneinfo import ZoneInfo

import pytest

from radio_server.auth import AuthGate, OutcomeKind, Session, SessionState
from radio_server.backends import MockRadio
from radio_server.services import (
    Dispatcher,
    ServiceContext,
    ServiceRegistry,
    StationId,
    StubId,
    StubTts,
    format_spoken_time,
    register,
)

TZ = ZoneInfo("UTC")
CALLSIGN = "AE9S"
ID = b"<id:AE9S>"


def build_dispatcher(radio, clock):
    """A dispatcher whose transmit seam is a fresh StationId around `radio`."""
    registry = ServiceRegistry()
    register(registry, TZ)
    ctx = ServiceContext(clock=clock, tts=StubTts())
    station = StationId(radio, StubId(), CALLSIGN, clock=clock)
    return Dispatcher(station, ctx, registry)


def build_gate(radio, verifier, clock, *, timeout=120.0):
    """Wire an AuthGate whose command hook is the real dispatcher (time service on '1').

    The gate, the service, and the station ID all share the one injected `clock`, so the
    announced time and the ID timing are driven by the same source as the session timeout.
    """
    dispatcher = build_dispatcher(radio, clock)
    return AuthGate(verifier, timeout=timeout, clock=clock, dispatch=dispatcher)


def expected_time_audio(now):
    return StubTts().render(format_spoken_time(now, TZ))


# --- dispatcher unit (no auth) ----------------------------------------------


def test_dispatcher_transmits_registered_service(clock):
    radio = MockRadio()
    dispatcher = build_dispatcher(radio, clock)

    result = dispatcher("1", Session(state=SessionState.AUTHENTICATED))

    assert result.service == "time"
    assert result.transmitted is True
    # First over of the session: the station ID is prepended into the same transmission.
    assert radio.tx_log == [ID + expected_time_audio(clock.now)]


def test_dispatcher_unknown_digit_does_not_transmit(clock):
    radio = MockRadio()
    dispatcher = build_dispatcher(radio, clock)

    result = dispatcher("7", Session(state=SessionState.AUTHENTICATED))

    assert result.service is None
    assert result.transmitted is False
    assert radio.tx_log == []  # nothing sent, so nothing to ID


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
    assert radio.tx_log == [ID + expected_time_audio(clock.now)]


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

    # Authenticate with the code for the current step, then ask for the time. The first
    # announcement carries the station ID in the same over.
    assert gate.on_dtmf(code_for(clock.now), session).kind is OutcomeKind.ACCEPTED
    gate.on_dtmf("1", session)
    assert radio.tx_log == [ID + b"<audio:The time is 13:46 UTC>"]

    # Advancing the shared clock (still within the timeout and the ID interval) changes the
    # announced time and does NOT repeat the ID — proving both the service and the station
    # ID read the same clock the session does.
    clock.advance(60.0)
    gate.on_dtmf("1", session)
    assert radio.tx_log[-1] == b"<audio:The time is 13:47 UTC>"
    assert radio.tx_log[-1] == expected_time_audio(clock.now)
