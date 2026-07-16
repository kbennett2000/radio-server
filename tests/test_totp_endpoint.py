"""`GET /auth/totp` — the current over-the-air login code for the web UI's code card.

Posture proofs matter more than the arithmetic here: the response carries ONLY the code + timing
(never the secret — the ADR 0025 line), it is token-gated like everything else, it 503s when no
TOTP secret is enrolled (the hide-when-unconfigured signal), and reading it never burns anything —
keying the displayed code over RF still passes `verify_and_burn` exactly once.
"""

from __future__ import annotations

import json

import pyotp
from fastapi.testclient import TestClient

from radio_server.api import create_app
from radio_server.auth import TotpVerifier
from radio_server.backends import MockRadio

from .conftest import TEST_SECRET, make_secrets

TOKEN = "test-lan-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _client(*, totp_secret=None) -> TestClient:
    return TestClient(
        create_app(
            MockRadio(),
            api_token=TOKEN,
            secrets=make_secrets(api_token=TOKEN, totp_secret=totp_secret),
        )
    )


# --- the verifier accessors (clock-injected, exact) ------------------------------------------


def test_current_code_matches_an_enrolled_authenticator(clock):
    verifier = TotpVerifier(TEST_SECRET, clock=clock)
    assert verifier.current_code() == pyotp.TOTP(TEST_SECRET).at(int(clock.now))
    # Stable within a step, rolls on the boundary.
    clock.advance(verifier.interval)
    assert verifier.current_code() == pyotp.TOTP(TEST_SECRET).at(int(clock.now))


def test_seconds_remaining_counts_down_to_the_step_boundary():
    step_start = 1_000_020.0  # divisible by 30 — the start of a step
    verifier = TotpVerifier(TEST_SECRET, clock=lambda: step_start + 20)
    assert verifier.interval == 30
    assert verifier.seconds_remaining() == 10  # 20 s into the step
    assert verifier.seconds_remaining(step_start + 29) == 1
    assert verifier.seconds_remaining(step_start) == 30  # a fresh step


def test_reading_the_code_does_not_burn_it(clock):
    verifier = TotpVerifier(TEST_SECRET, clock=clock)
    code = verifier.current_code()
    assert verifier.verify_and_burn(code) is True  # first keyed use still accepted
    assert verifier.verify_and_burn(code) is False  # single-use burn intact


# --- the endpoint -----------------------------------------------------------------------------


def test_endpoint_returns_the_live_code_and_timing():
    body = _client(totp_secret=TEST_SECRET).get("/auth/totp", headers=AUTH).json()
    assert body["enforced"] is True
    assert body["code"] == pyotp.TOTP(TEST_SECRET).now()
    assert 1 <= body["seconds_remaining"] <= 30
    assert body["interval"] == 30


def test_endpoint_never_exposes_the_secret():
    resp = _client(totp_secret=TEST_SECRET).get("/auth/totp", headers=AUTH)
    assert TEST_SECRET not in json.dumps(resp.json())
    assert set(resp.json()) == {"enforced", "code", "seconds_remaining", "interval"}


def test_endpoint_503_when_totp_not_configured():
    resp = _client().get("/auth/totp", headers=AUTH)
    assert resp.status_code == 503
    assert "TOTP" in resp.json()["detail"]


def test_endpoint_requires_the_token():
    assert _client(totp_secret=TEST_SECRET).get("/auth/totp").status_code == 401
