"""StreamingId — the radio-free station-ID scheduler for streaming TX (ADR 0041, Part 97).

Drives the due/interval logic with a fake clock (no real sleeps): the ID renders at the key-up edge
of a fresh transmission, re-renders across the <=10-minute boundary, and the closing sign-off is
due-gated so a rapid exchange is not ID'd after every short over.
"""

from __future__ import annotations

from radio_server.services import StreamingId
from radio_server.services.station_id import StubId

CALL = "AE9S"
ID_BYTES = b"<id:AE9S>"


def _id(clock, interval: float = 600.0) -> StreamingId:
    return StreamingId(StubId(), CALL, interval=interval, clock=clock)


def test_key_up_ids_the_first_over(clock):
    sid = _id(clock)
    frame = sid.key_up_id(clock())
    assert frame is not None and frame.samples == ID_BYTES


def test_key_up_does_not_repeat_within_interval(clock):
    sid = _id(clock)
    assert sid.key_up_id(clock()) is not None  # first over IDs, stamps last_id=now
    clock.advance(30)  # a re-key well inside the 600 s interval
    assert sid.key_up_id(clock()) is None  # not due -> no repeat ID


def test_periodic_ids_only_across_the_interval_boundary(clock):
    sid = _id(clock)
    sid.key_up_id(clock())  # stamp
    clock.advance(120)
    assert sid.periodic_id(clock()) is None  # mid-over, not yet due
    clock.advance(600)  # now past the 10-minute ceiling since the last ID
    frame = sid.periodic_id(clock())
    assert frame is not None and frame.samples == ID_BYTES


def test_sign_off_is_due_gated(clock):
    sid = _id(clock)
    sid.key_up_id(clock())  # ID at key-up, stamps last_id=now
    # A short over: key-down arrives well within the interval -> no closing ID (the key-up ID
    # already identifies the station within the 10-minute window).
    clock.advance(5)
    assert sid.sign_off_id(clock()) is None


def test_sign_off_ids_when_overdue(clock):
    sid = _id(clock)
    sid.key_up_id(clock())
    clock.advance(700)  # past the interval since the last ID
    frame = sid.sign_off_id(clock())
    assert frame is not None and frame.samples == ID_BYTES


def test_sign_off_silent_when_never_transmitted(clock):
    sid = _id(clock)
    # No key_up/periodic ever called: the stream never transmitted, so there is nothing to sign off.
    assert sid.sign_off_id(clock()) is None
