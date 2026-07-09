"""TOTP verification with enforced single-use (guardrail 4).

Auth here is *gated* access, not *secure* access: every transmission is in the clear
over RF, so a valid code can be overheard and replayed within its validity window.
The property that actually matters is therefore single-use — once a code is consumed
it is burned so it cannot be replayed inside its window. `valid_window=1` (±1 time
step, ~±30 s) absorbs over-the-air latency without widening the replay surface, since
every accepted code is burned.

No audio or hardware here: `verify_and_burn` takes a digit string directly so the
auth path is unit-tested in isolation with an injected clock.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Callable

import pyotp

# Unix seconds. Injected so tests drive time with a fake clock (no real sleeps) and so
# TOTP math and session inactivity share one source of truth.
Clock = Callable[[], float]

#: Environment variable holding the base32 TOTP shared secret. Never hardcode a secret.
SECRET_ENV_VAR = "RADIO_TOTP_SECRET"


def load_totp_secret(env: dict[str, str] | os._Environ = os.environ) -> str:
    """Return the base32 TOTP secret from the environment.

    Raises `RuntimeError` (not a silent default) when unset — a missing secret means
    auth is unconfigured, which must fail loudly rather than accept nothing or
    everything. Enroll by generating a secret (`pyotp.random_base32()`), exporting it
    as ``RADIO_TOTP_SECRET``, and scanning `TotpVerifier.provisioning_uri()`.
    """
    secret = env.get(SECRET_ENV_VAR)
    if not secret:
        raise RuntimeError(
            f"{SECRET_ENV_VAR} is not set; generate one with pyotp.random_base32() "
            "and export it before starting the server"
        )
    return secret


class TotpVerifier:
    """Verifies TOTP codes over a ±1-step window and burns each consumed code.

    `verify_and_burn` is the whole contract: it returns True at most once per
    (code, time-step). A second presentation of the same code — a replay — returns
    False even while the code is still within its window.
    """

    def __init__(
        self,
        secret: str,
        *,
        interval: int = 30,
        digits: int = 6,
        clock: Clock | None = None,
    ) -> None:
        # `import time` lazily via default so a caller can inject any Clock; keep the
        # attribute a plain callable either way.
        if clock is None:
            import time

            clock = time.time
        self._totp = pyotp.TOTP(secret, interval=interval, digits=digits)
        self._interval = interval
        self._clock = clock
        # Burned codes, keyed by (code, time_step). Pruned on every call so it stays
        # bounded to at most the handful of steps still inside the window.
        self._consumed: set[tuple[str, int]] = set()

    def _step_at(self, now: float) -> int:
        return int(now) // self._interval

    def verify_and_burn(self, code: str, now: float | None = None) -> bool:
        """Return True iff `code` is valid for the current window and not yet used.

        Windowed like pyotp's ``valid_window=1``, but implemented by hand so we learn
        *which* step matched — that step is what we burn. A successful call burns the
        (code, step) pair; any later call with the same code (same step) returns False.
        """
        if now is None:
            now = self._clock()
        now_step = self._step_at(now)
        self._prune(now_step)

        for offset in (-1, 0, 1):
            step = now_step + offset
            expected = self._totp.at(step * self._interval)
            # Constant-time compare; guards the accepted branch, not the reject branch,
            # but keeps parity with pyotp's own comparison.
            if hmac.compare_digest(expected, code):
                key = (code, step)
                if key in self._consumed:
                    return False  # replay within the window — burned already
                self._consumed.add(key)
                return True
        return False

    def _prune(self, now_step: int) -> None:
        # A code for step s only verifies while now_step is in {s-1, s, s+1}. Once
        # now_step > s + 1 the burn can never be tested again, so drop it.
        self._consumed = {
            (code, step) for (code, step) in self._consumed if step >= now_step - 1
        }

    def provisioning_uri(self, account: str, issuer: str = "radio-server") -> str:
        """Return the ``otpauth://`` enrollment URI (the payload a QR encodes).

        Rendering it as an image/terminal QR is a thin wrapper deliberately left out
        of this cycle to avoid an image dependency; authenticator apps accept the URI
        directly.
        """
        return self._totp.provisioning_uri(name=account, issuer_name=issuer)
