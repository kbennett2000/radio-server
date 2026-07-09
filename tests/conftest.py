"""Shared test fixtures: a fake clock and a TOTP secret/verifier wired to it.

No real time anywhere in the auth tests — `FakeClock` is advanced explicitly so the
window, single-use burn, and session timeout are all driven deterministically.
"""

import pyotp
import pytest

from radio_server.auth import TotpVerifier

# A fixed, well-known base32 secret. Test-only — never a real enrollment secret.
TEST_SECRET = "JBSWY3DPEHPK3PXP"
INTERVAL = 30


class FakeClock:
    """Callable clock whose value only changes when the test moves it."""

    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def verifier(clock: FakeClock) -> TotpVerifier:
    return TotpVerifier(TEST_SECRET, interval=INTERVAL, clock=clock)


@pytest.fixture
def code_for():
    """Return a helper that generates the valid code for a given unix time."""
    totp = pyotp.TOTP(TEST_SECRET, interval=INTERVAL)

    def _code_for(when: float) -> str:
        return totp.at(int(when))

    return _code_for
