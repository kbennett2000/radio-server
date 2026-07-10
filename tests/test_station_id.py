"""Station ID scheduler: config, the inclusion rule, forced ID, and sign-off.

Everything runs on a FakeClock (from conftest) and a fresh MockRadio built inline. The ID
audio is the deterministic StubId, so tx_log is asserted with exact bytes.
"""

import pytest

from radio_server.audio import AudioFrame
from radio_server.backends import MockRadio
from radio_server.services import (
    DEFAULT_ID_INTERVAL,
    MAX_ID_INTERVAL,
    IdEncoder,
    StationId,
    StubId,
    load_callsign,
    load_id_interval,
)

from .conftest import make_settings

CALLSIGN = "AE9S"
ID = StubId().encode(CALLSIGN)  # AudioFrame(b"<id:AE9S>")
INTERVAL = 600.0  # seconds; the default and the legal maximum


def frame(payload: bytes) -> AudioFrame:
    """A canonical-format frame wrapping symbolic content, for terse tx_log assertions."""
    return AudioFrame(payload)


def build(clock, *, interval=INTERVAL):
    radio = MockRadio()
    station = StationId(radio, StubId(), CALLSIGN, interval=interval, clock=clock)
    return radio, station


# --- callsign config (fail loud, no default) --------------------------------


def test_load_callsign_returns_configured_value():
    assert load_callsign(make_settings({"station.callsign": "AE9S"})) == "AE9S"


def test_load_callsign_missing_raises():
    # Required-unset fails loud on access (lazily), preserving the point-of-use behavior.
    with pytest.raises(RuntimeError):
        load_callsign(make_settings({}))


def test_load_callsign_empty_raises():
    # Present-but-empty fails loud at resolution (naming the key).
    with pytest.raises(RuntimeError):
        make_settings({"station.callsign": ""})


# --- callsign / mode exposed for the station_id ledger record (ADR 0019) ----


def test_callsign_and_mode_properties_report_constructed_values():
    station = StationId(MockRadio(), StubId(), CALLSIGN, mode="voice")
    assert station.callsign == CALLSIGN
    assert station.mode == "voice"


def test_mode_defaults_to_cw():
    # The default matches voice_id.DEFAULT_ID_MODE; build_controller overrides via load_id_mode.
    assert StationId(MockRadio(), StubId(), CALLSIGN).mode == "cw"


# --- interval config (default 600, enforce <= 600) --------------------------


def test_load_id_interval_defaults_to_600():
    assert load_id_interval(make_settings({})) == DEFAULT_ID_INTERVAL == 600.0


def test_load_id_interval_reads_a_legal_value():
    assert load_id_interval(make_settings({"station.id_interval": 300})) == 300.0


def test_load_id_interval_rejects_over_max():
    # 700 > 600 is illegal — reject, do not clamp.
    with pytest.raises(RuntimeError):
        make_settings({"station.id_interval": 700})


def test_load_id_interval_accepts_exactly_max():
    assert load_id_interval(make_settings({"station.id_interval": int(MAX_ID_INTERVAL)})) == 600.0


def test_load_id_interval_rejects_non_numeric():
    with pytest.raises(RuntimeError):
        make_settings({"station.id_interval": "soon"})


def test_load_id_interval_rejects_non_positive():
    with pytest.raises(RuntimeError):
        make_settings({"station.id_interval": 0})


# --- encoder stub ------------------------------------------------------------


def test_stub_id_embeds_the_callsign():
    assert StubId().encode("AE9S") == AudioFrame(b"<id:AE9S>")


def test_stub_id_is_deterministic():
    assert StubId().encode("W1AW") == StubId().encode("W1AW")


def test_stub_satisfies_the_encoder_protocol():
    assert isinstance(StubId(), IdEncoder)


# --- inclusion rule on content transmissions --------------------------------


def test_first_transmission_carries_id(clock):
    radio, station = build(clock)
    station.transmit(frame(b"CONTENT"))
    assert radio.tx_log == [ID + frame(b"CONTENT")]


def test_second_transmission_within_interval_does_not_repeat_id(clock):
    radio, station = build(clock)
    station.transmit(frame(b"one"))
    clock.advance(INTERVAL - 1)  # still inside the window
    station.transmit(frame(b"two"))
    assert radio.tx_log[-1] == frame(b"two")  # content only, no ID prefix
    assert radio.tx_log == [ID + frame(b"one"), frame(b"two")]


def test_transmission_after_interval_carries_id_again(clock):
    radio, station = build(clock)
    station.transmit(frame(b"one"))
    clock.advance(INTERVAL)  # exactly at the boundary re-IDs
    station.transmit(frame(b"two"))
    assert radio.tx_log[-1] == ID + frame(b"two")


# --- forced periodic ID ------------------------------------------------------


def test_check_forces_id_when_overdue(clock):
    radio, station = build(clock)
    station.transmit(frame(b"one"))  # last_id = base
    clock.advance(INTERVAL)
    assert station.check() is True
    assert radio.tx_log[-1] == ID  # ID-only transmission


def test_check_is_noop_within_interval(clock):
    radio, station = build(clock)
    station.transmit(frame(b"one"))
    clock.advance(INTERVAL - 1)
    assert station.check() is False
    assert radio.tx_log == [ID + frame(b"one")]  # nothing appended


def test_check_is_noop_without_prior_transmission(clock):
    radio, station = build(clock)
    clock.advance(INTERVAL * 10)  # time has passed but the station never keyed up
    assert station.check() is False
    assert radio.tx_log == []


def test_check_does_not_repeat_within_a_new_interval(clock):
    radio, station = build(clock)
    station.transmit(frame(b"one"))
    clock.advance(INTERVAL)
    assert station.check() is True  # forced ID, resets the timer
    clock.advance(INTERVAL - 1)
    assert station.check() is False  # not yet due again
    assert radio.tx_log == [ID + frame(b"one"), ID]


# --- sign-off ----------------------------------------------------------------


def test_sign_off_after_activity_emits_id(clock):
    radio, station = build(clock)
    station.transmit(frame(b"one"))
    assert station.sign_off() is True
    assert radio.tx_log[-1] == ID  # closing ID-only transmission


def test_sign_off_without_activity_emits_nothing(clock):
    radio, station = build(clock)
    assert station.sign_off() is False
    assert radio.tx_log == []


# --- session reset (begin_session / post-sign-off) --------------------------


def test_sign_off_rearms_first_over_id(clock):
    radio, station = build(clock)
    station.transmit(frame(b"one"))  # ID + content
    station.sign_off()  # closing ID, resets session state
    station.transmit(frame(b"two"))  # a new session's first over must re-ID
    assert radio.tx_log[-1] == ID + frame(b"two")


def test_begin_session_rearms_first_over_id(clock):
    radio, station = build(clock)
    station.transmit(frame(b"one"))
    clock.advance(1)  # well inside the interval
    station.begin_session()  # e.g. after an inactivity timeout, no sign-off sent
    station.transmit(frame(b"two"))  # still the first over of the new session -> ID
    assert radio.tx_log[-1] == ID + frame(b"two")
