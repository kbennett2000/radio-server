"""TOTP verification: windowed accept, single-use burn, secret loading, enrollment."""

import pyotp
import pytest

from radio_server.auth import SECRET_ENV_VAR, TotpVerifier, load_totp_secret

from .conftest import INTERVAL, TEST_SECRET


# --- windowed verification ---------------------------------------------------


def test_fresh_valid_code_accepts(verifier, clock, code_for):
    assert verifier.verify_and_burn(code_for(clock.now)) is True


def test_replayed_code_is_rejected(verifier, clock, code_for):
    code = code_for(clock.now)
    assert verifier.verify_and_burn(code) is True
    # Same code, same window, immediately after — must not authenticate twice.
    assert verifier.verify_and_burn(code) is False


def test_replay_rejected_across_the_window(verifier, clock, code_for):
    # Burn a code, then advance within its window; the replay is still refused.
    code = code_for(clock.now)
    assert verifier.verify_and_burn(code) is True
    clock.advance(INTERVAL)  # now_step + 1: code still inside the ±1 window
    assert verifier.verify_and_burn(code) is False


def test_expired_step_code_is_rejected(verifier, clock, code_for):
    old_code = code_for(clock.now)
    clock.advance(2 * INTERVAL)  # two steps later: outside the ±1 window
    assert verifier.verify_and_burn(old_code) is False


def test_previous_step_within_tolerance_accepts(verifier, clock, code_for):
    code = code_for(clock.now - INTERVAL)  # generated one step ago
    assert verifier.verify_and_burn(code) is True


def test_next_step_within_tolerance_accepts(verifier, clock, code_for):
    code = code_for(clock.now + INTERVAL)  # one step in the future (clock skew)
    assert verifier.verify_and_burn(code) is True


def test_wrong_code_is_rejected(verifier):
    assert verifier.verify_and_burn("000000") is False


def test_explicit_now_overrides_clock(verifier, clock, code_for):
    future = clock.now + 10 * INTERVAL
    code = code_for(future)
    # Fails at the clock's time...
    assert verifier.verify_and_burn(code) is False
    # ...but validates when `now` is passed explicitly.
    assert verifier.verify_and_burn(code, now=future) is True


def test_burn_set_stays_bounded(verifier, clock, code_for):
    # Consume a code each step for many steps; pruning keeps the set from growing.
    for _ in range(50):
        verifier.verify_and_burn(code_for(clock.now))
        clock.advance(INTERVAL)
    assert len(verifier._consumed) <= 3


# --- secret loading ----------------------------------------------------------


def test_load_secret_from_env():
    assert load_totp_secret({SECRET_ENV_VAR: "ABC123"}) == "ABC123"


def test_missing_secret_raises():
    with pytest.raises(RuntimeError, match=SECRET_ENV_VAR):
        load_totp_secret({})


def test_empty_secret_raises():
    with pytest.raises(RuntimeError):
        load_totp_secret({SECRET_ENV_VAR: ""})


# --- enrollment --------------------------------------------------------------


def test_provisioning_uri_is_enrollable():
    verifier = TotpVerifier(TEST_SECRET)
    uri = verifier.provisioning_uri("KB0XYZ", issuer="radio-server")
    assert uri.startswith("otpauth://totp/")
    assert f"secret={TEST_SECRET}" in uri
    assert "issuer=radio-server" in uri
    # The URI round-trips to a working TOTP for the same secret.
    parsed = pyotp.parse_uri(uri)
    assert parsed.at(0) == pyotp.TOTP(TEST_SECRET).at(0)
