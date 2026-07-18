"""A fixed (static) over-RF login code — an opt-in alternative to rotating TOTP (ADR 0083).

Auth here is *gated* access, not *secure* access (guardrail 4): everything is in the clear over RF.
The rotating TOTP path (:mod:`.totp`) still gets the one property that matters — single-use — by
burning each consumed code so a replay inside its window fails.

A **fixed** code cannot have that property: the operator keys the *same* code on every login, so
burning it would lock them out after one use. So :class:`FixedCodeVerifier` deliberately does **not**
burn — it just constant-time-compares the keyed digits against the configured code. That makes a
fixed code strictly weaker than TOTP: anyone who overhears it can replay it indefinitely until it is
changed. It exists only as an explicit, warned, non-default convenience for operators who want a code
they never have to read off an authenticator; the security tradeoff is surfaced in the settings UI
and the docs, not hidden here.

It presents the exact surface :class:`~radio_server.auth.session.AuthGate` consumes — a
``verify_and_burn(code, now)`` method — so it drops in wherever a :class:`~.totp.TotpVerifier` would,
with no change to the gate or the session state machine. The fixed code is a credential and lives on
the secrets channel (``fixed_code`` / ``RADIO_FIXED_CODE``), never in ``radio.toml`` (ADR 0025).
"""

from __future__ import annotations

import hmac

from .totp import Clock


class FixedCodeVerifier:
    """Verifies keyed digits against a single fixed login code — no rotation, no burn (ADR 0083).

    Mirrors :class:`~radio_server.auth.totp.TotpVerifier`'s ``verify_and_burn`` signature so
    :class:`~radio_server.auth.session.AuthGate` uses it interchangeably. Unlike TOTP it accepts the
    same code every time (a fixed code is reused by design), so it enforces no single-use property —
    the documented, opt-in security downgrade. ``clock`` is accepted for interface parity with
    ``TotpVerifier`` (so both are constructed the same way) but is unused: there is no time window.
    """

    def __init__(self, code: str, *, clock: Clock | None = None) -> None:
        self._code = code

    def verify_and_burn(self, code: str, now: float | None = None) -> bool:
        """Return True iff ``code`` matches the configured fixed code (constant-time; never burns).

        Constant-time compare guards against leaking the code via timing, mirroring
        :meth:`TotpVerifier.verify_and_burn`. ``now`` is ignored — a fixed code has no time window —
        and nothing is consumed, so the same code authenticates every login (and, being static, can
        be replayed by anyone who overheard it: gated, not secure).
        """
        if not self._code or not code:
            return False
        return hmac.compare_digest(self._code, code)
