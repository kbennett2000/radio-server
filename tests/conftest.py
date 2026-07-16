"""Shared test fixtures: a fake clock, a TOTP secret/verifier, and config helpers.

No real time anywhere in the auth tests — `FakeClock` is advanced explicitly so the
window, single-use burn, and session timeout are all driven deterministically.

`make_settings` / `make_secrets` are the config-construction helpers the migration off env-var
dicts (ADR 0025) uses: tests pass typed values by dotted key (``{"station.cw_wpm": 25}``) rather
than ``RADIO_*`` strings, and secrets are constructed separately from settings.
"""

from collections.abc import Mapping
from typing import Any

import pyotp
import pytest

from radio_server.auth import TotpVerifier
from radio_server.config import Secrets, Settings, resolve_settings

# A fixed, well-known base32 secret. Test-only — never a real enrollment secret.
TEST_SECRET = "JBSWY3DPEHPK3PXP"
INTERVAL = 30


def make_settings(overrides: Mapping[str, Any] | None = None) -> Settings:
    """Resolve a `Settings` from dotted-key overrides through the real pipeline.

    Same validation as loading a ``radio.toml``: a present-but-invalid value fails loud, a
    required-unset value fails loud only on access. Keys are dotted (``"station.cw_wpm"``) — leaf
    names collide across groups, so leaf-only keys are not accepted.
    """
    return resolve_settings(dict(overrides or {}))


def make_secrets(
    *,
    totp_secret: str | None = None,
    api_token: str | None = None,
    mumble_password: str | None = None,
) -> Secrets:
    """Construct a `Secrets` for the auth/controller/link paths, bypassing the file/env loader."""
    return Secrets(
        {
            "totp_secret": totp_secret,
            "api_token": api_token,
            "mumble_password": mumble_password,
        }
    )


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
