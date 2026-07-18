"""The fixed over-RF login code (ADR 0083): a static code with NO single-use burn.

The whole point that distinguishes it from TOTP is that the SAME code authenticates every time —
so, unlike `TotpVerifier`, a fixed code is deliberately replayable. These tests pin that contract
(the documented security downgrade) plus the constant-time-compare surface `AuthGate` consumes, and
the config/build wiring that selects it over TOTP.
"""

from __future__ import annotations

import pytest

from radio_server.auth import FixedCodeVerifier
from radio_server.backends import MockRadio
from radio_server.controller.engine import build_controller, load_fixed_code_enabled
from radio_server.services import StubTts

from .conftest import make_settings
from .test_dtmf import FakeDtmfDecoder


# --- the verifier ------------------------------------------------------------


def test_matching_code_authenticates():
    assert FixedCodeVerifier("135790").verify_and_burn("135790") is True


def test_wrong_code_is_rejected():
    assert FixedCodeVerifier("135790").verify_and_burn("000000") is False


def test_the_same_code_authenticates_every_time_no_burn():
    # The defining difference from TOTP: a fixed code is reused on every login, so it is NOT burned.
    # (This is the documented security downgrade — a fixed code is replayable by anyone who hears it.)
    v = FixedCodeVerifier("135790")
    assert v.verify_and_burn("135790") is True
    assert v.verify_and_burn("135790") is True
    assert v.verify_and_burn("135790") is True


def test_empty_inputs_never_match():
    assert FixedCodeVerifier("").verify_and_burn("135790") is False
    assert FixedCodeVerifier("135790").verify_and_burn("") is False


def test_now_argument_is_ignored_no_time_window():
    v = FixedCodeVerifier("135790")
    assert v.verify_and_burn("135790", now=0.0) is True
    assert v.verify_and_burn("135790", now=10_000_000.0) is True


# --- config + build wiring ---------------------------------------------------


def _settings(**overrides):
    base = {"station.callsign": "N0CALL", "controller.login_announcement": "",
            "controller.timeout_announcement": "", "controller.logout_announcement": ""}
    base.update(overrides)
    return make_settings(base)


def _build(settings, *, totp_secret=None, fixed_code=None):
    # Wire through the real build with test doubles (no piper/multimon), like test_controller.build_ctrl.
    return build_controller(
        settings, radio=MockRadio(), totp_secret=totp_secret, fixed_code=fixed_code,
        decoder=FakeDtmfDecoder([]), tts=StubTts(),
    )


def test_load_fixed_code_enabled_defaults_off():
    assert load_fixed_code_enabled(_settings()) is False
    assert load_fixed_code_enabled(_settings(**{"auth.fixed_code": True})) is True


def test_build_controller_uses_fixed_verifier_when_enabled():
    # auth.fixed_code on + a code present -> the gate authenticates that code and reports "fixed".
    controller = _build(_settings(**{"auth.fixed_code": True}), fixed_code="246813")
    assert controller.auth_method == "fixed"
    assert controller._gate._verifier is not None
    assert controller._gate._verifier.verify_and_burn("246813") is True
    assert controller._gate._verifier.verify_and_burn("000000") is False


def test_build_controller_defaults_to_totp_verifier():
    # Fixed mode off -> the rotating TOTP verifier is used (existing behavior, unchanged).
    controller = _build(_settings(), totp_secret="JBSWY3DPEHPK3PXP", fixed_code="246813")
    assert controller.auth_method == "totp"
    from radio_server.auth import TotpVerifier

    assert isinstance(controller._gate._verifier, TotpVerifier)


def test_build_controller_fixed_mode_without_a_code_has_no_verifier():
    # Fixed mode on but no code set -> no verifier (auth is effectively unconfigured, like a missing
    # TOTP secret); build_app's gate withholds the controller in that state.
    controller = _build(_settings(**{"auth.fixed_code": True}), fixed_code=None)
    assert controller.auth_method == "fixed"
    assert controller._gate._verifier is None
