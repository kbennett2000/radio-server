"""Station ID scheduler: config, the inclusion rule, forced ID, and sign-off.

Everything runs on a FakeClock (from conftest) and a fresh MockRadio built inline. The ID
audio is the deterministic StubId, so tx_log is asserted with exact bytes.
"""

import pytest

from radio_server.backends import MockRadio
from radio_server.services import (
    DEFAULT_ID_INTERVAL,
    MAX_ID_INTERVAL,
    RADIO_CALLSIGN_ENV_VAR,
    RADIO_ID_INTERVAL_ENV_VAR,
    IdEncoder,
    StationId,
    StubId,
    load_callsign,
    load_id_interval,
)

CALLSIGN = "AE9S"
ID = b"<id:AE9S>"
INTERVAL = 600.0  # seconds; the default and the legal maximum


def build(clock, *, interval=INTERVAL):
    radio = MockRadio()
    station = StationId(radio, StubId(), CALLSIGN, interval=interval, clock=clock)
    return radio, station


# --- callsign config (fail loud, no default) --------------------------------


def test_load_callsign_returns_configured_value():
    assert load_callsign({RADIO_CALLSIGN_ENV_VAR: "AE9S"}) == "AE9S"


def test_load_callsign_missing_raises():
    with pytest.raises(RuntimeError):
        load_callsign({})


def test_load_callsign_empty_raises():
    with pytest.raises(RuntimeError):
        load_callsign({RADIO_CALLSIGN_ENV_VAR: ""})


# --- interval config (default 600, enforce <= 600) --------------------------


def test_load_id_interval_defaults_to_600():
    assert load_id_interval({}) == DEFAULT_ID_INTERVAL == 600.0


def test_load_id_interval_reads_a_legal_value():
    assert load_id_interval({RADIO_ID_INTERVAL_ENV_VAR: "300"}) == 300.0


def test_load_id_interval_rejects_over_max():
    # 700 > 600 is illegal — reject, do not clamp.
    with pytest.raises(RuntimeError):
        load_id_interval({RADIO_ID_INTERVAL_ENV_VAR: "700"})


def test_load_id_interval_accepts_exactly_max():
    assert load_id_interval({RADIO_ID_INTERVAL_ENV_VAR: str(int(MAX_ID_INTERVAL))}) == 600.0


def test_load_id_interval_rejects_non_numeric():
    with pytest.raises(RuntimeError):
        load_id_interval({RADIO_ID_INTERVAL_ENV_VAR: "soon"})


def test_load_id_interval_rejects_non_positive():
    with pytest.raises(RuntimeError):
        load_id_interval({RADIO_ID_INTERVAL_ENV_VAR: "0"})


# --- encoder stub ------------------------------------------------------------


def test_stub_id_embeds_the_callsign():
    assert StubId().encode("AE9S") == b"<id:AE9S>"


def test_stub_id_is_deterministic():
    assert StubId().encode("W1AW") == StubId().encode("W1AW")


def test_stub_satisfies_the_encoder_protocol():
    assert isinstance(StubId(), IdEncoder)


# --- inclusion rule on content transmissions --------------------------------


def test_first_transmission_carries_id(clock):
    radio, station = build(clock)
    station.transmit(b"CONTENT")
    assert radio.tx_log == [ID + b"CONTENT"]


def test_second_transmission_within_interval_does_not_repeat_id(clock):
    radio, station = build(clock)
    station.transmit(b"one")
    clock.advance(INTERVAL - 1)  # still inside the window
    station.transmit(b"two")
    assert radio.tx_log[-1] == b"two"  # content only, no ID prefix
    assert radio.tx_log == [ID + b"one", b"two"]


def test_transmission_after_interval_carries_id_again(clock):
    radio, station = build(clock)
    station.transmit(b"one")
    clock.advance(INTERVAL)  # exactly at the boundary re-IDs
    station.transmit(b"two")
    assert radio.tx_log[-1] == ID + b"two"


# --- forced periodic ID ------------------------------------------------------


def test_check_forces_id_when_overdue(clock):
    radio, station = build(clock)
    station.transmit(b"one")  # last_id = base
    clock.advance(INTERVAL)
    assert station.check() is True
    assert radio.tx_log[-1] == ID  # ID-only transmission


def test_check_is_noop_within_interval(clock):
    radio, station = build(clock)
    station.transmit(b"one")
    clock.advance(INTERVAL - 1)
    assert station.check() is False
    assert radio.tx_log == [ID + b"one"]  # nothing appended


def test_check_is_noop_without_prior_transmission(clock):
    radio, station = build(clock)
    clock.advance(INTERVAL * 10)  # time has passed but the station never keyed up
    assert station.check() is False
    assert radio.tx_log == []


def test_check_does_not_repeat_within_a_new_interval(clock):
    radio, station = build(clock)
    station.transmit(b"one")
    clock.advance(INTERVAL)
    assert station.check() is True  # forced ID, resets the timer
    clock.advance(INTERVAL - 1)
    assert station.check() is False  # not yet due again
    assert radio.tx_log == [ID + b"one", ID]


# --- sign-off ----------------------------------------------------------------


def test_sign_off_after_activity_emits_id(clock):
    radio, station = build(clock)
    station.transmit(b"one")
    assert station.sign_off() is True
    assert radio.tx_log[-1] == ID  # closing ID-only transmission


def test_sign_off_without_activity_emits_nothing(clock):
    radio, station = build(clock)
    assert station.sign_off() is False
    assert radio.tx_log == []


# --- session reset (begin_session / post-sign-off) --------------------------


def test_sign_off_rearms_first_over_id(clock):
    radio, station = build(clock)
    station.transmit(b"one")  # ID + content
    station.sign_off()  # closing ID, resets session state
    station.transmit(b"two")  # a new session's first over must re-ID
    assert radio.tx_log[-1] == ID + b"two"


def test_begin_session_rearms_first_over_id(clock):
    radio, station = build(clock)
    station.transmit(b"one")
    clock.advance(1)  # well inside the interval
    station.begin_session()  # e.g. after an inactivity timeout, no sign-off sent
    station.transmit(b"two")  # still the first over of the new session -> ID
    assert radio.tx_log[-1] == ID + b"two"
