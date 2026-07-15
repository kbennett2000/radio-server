"""The TX time limiter: a pure policy that bounds how long the transmitter may stay keyed (ADR 0045).

``tx.idle_timeout`` (ADR 0016) already handles one runaway mode — the inbound stream stops, so PTT
drops. It does **not** handle the other, worse one: **continuous audio**. A stuck VOX, a reflector
spraying noise, a bridge looped back on itself — none of that is silence, so ``idle_timeout`` never
fires and the radio keys indefinitely (a cooked finals stage, a self-jammed channel, and a Part 97
problem — the ID scheduler cannot acquire the radio while TX holds). Silence and stuck-on are different
failures; ``idle_timeout`` covers the first, this covers the second.

:class:`TxLimiter` is a **policy object, not a mechanism** — it answers questions, it keys nothing:

- Told when keying starts (:meth:`key_down`) and stops, either normally (:meth:`key_up`) or because the
  limit forced it (:meth:`force_unkey`).
- :meth:`expired` — has this key-down exceeded ``max_seconds``.
- :meth:`may_key` — ``False`` during the **cooloff**: after a forced unkey it refuses to re-key for
  ``cooloff_seconds``. Without that a stuck peer just re-keys instantly and you've built a square-wave
  generator instead of a limiter — the cooloff is the point.
- An optional :attr:`on_change` callback fired only on a real state transition, mirroring
  :class:`~radio_server.arbiter.RadioArbiter`'s ``on_change``.

**Pure leaf.** This module imports **nothing** from the rest of ``radio_server`` — and, because every
time-dependent method takes an explicit ``now: float`` (supplied by the caller from its injected
monotonic clock, the ``controller.step(now, …)`` shape), it imports no ``time`` either. So the whole
policy is exercisable from a fake clock (passed floats) with no radio and no I/O, and its consumers'
dependency arrows stay clean (``tx -> txlimit``, ``api -> txlimit``, no cycles). It models the *logical*
policy only; the real max-key duration and cooloff are bench facts (guardrail 1), passed in as config.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum


class TxLimitState(StrEnum):
    """Where a keyed transmission stands relative to the limiter's policy."""

    IDLE = "idle"  # not keyed, free to key
    KEYED = "keyed"  # keyed, running against max_seconds
    COOLOFF = "cooloff"  # forced off, refusing to re-key until the cooloff elapses


class TxLimiter:
    """Bounds a keyed transmission: expiry at ``max_seconds`` and a ``cooloff_seconds`` re-key refusal.

    A policy oracle. The caller reports keying edges (:meth:`key_down`/:meth:`key_up`/
    :meth:`force_unkey`) and consults :meth:`expired`/:meth:`may_key`; the limiter never touches a
    radio. State is **derived** from two timestamps (the arbiter's derived-mode style), so there is no
    stored state to fall out of sync: ``_keyed_since`` set ⇒ ``KEYED``; else a live ``_cooloff_until``
    ⇒ ``COOLOFF``; else ``IDLE``.

    ``on_change`` (optional, injected, keyword-only) fires **only when the derived state actually
    changes** on a reported edge — ``key_down`` → ``KEYED``, ``key_up`` → ``IDLE``, ``force_unkey`` →
    ``COOLOFF``. A no-op call (e.g. ``key_down`` while already keyed) fires nothing. The time-based
    ``COOLOFF`` → ``IDLE`` transition is **not** an ``on_change`` event: a pure leaf has no clock tick
    of its own, so cooloff expiry is derived and surfaced by :meth:`may_key` returning ``True`` again —
    mirroring how the arbiter only fires on latch flips, never on the passage of time. The callback is
    trusted non-raising (the wired publisher is non-raising by contract), so it is not guarded here.

    ``max_seconds`` and ``cooloff_seconds`` are trusted to be positive: config rejects ``<= 0`` and
    non-numeric at load, naming the key (the same split as ``create_link`` trusting resolved values).
    """

    def __init__(
        self,
        max_seconds: float,
        cooloff_seconds: float,
        *,
        on_change: Callable[[TxLimitState], None] | None = None,
    ) -> None:
        self._max = max_seconds
        self._cooloff = cooloff_seconds
        self._on_change = on_change
        self._keyed_since: float | None = None
        self._cooloff_until: float | None = None

    def _mode(self, now: float) -> TxLimitState:
        """Derive the current state at ``now`` — keyed > (live) cooloff > idle."""
        if self._keyed_since is not None:
            return TxLimitState.KEYED
        if self._cooloff_until is not None and now < self._cooloff_until:
            return TxLimitState.COOLOFF
        return TxLimitState.IDLE

    def _notify(self, before: TxLimitState, now: float) -> None:
        """Fire ``on_change`` iff the derived state changed from ``before``."""
        after = self._mode(now)
        if self._on_change is not None and after != before:
            self._on_change(after)

    # --- told when keying starts / stops ---------------------------------------------------------

    def key_down(self, now: float) -> None:
        """Begin a keyed period. Idempotent while already keyed (keeps the original start time).

        Clears any prior cooloff bookkeeping — keying now makes an earlier cooloff moot. Callers are
        expected to have consulted :meth:`may_key` first; the limiter advises, it does not enforce.
        """
        if self._keyed_since is not None:
            return
        before = self._mode(now)
        self._keyed_since = now
        self._cooloff_until = None
        self._notify(before, now)

    def key_up(self, now: float) -> None:
        """End a keyed period **normally** (the peer stopped) — no cooloff. No-op when not keyed."""
        if self._keyed_since is None:
            return
        before = self._mode(now)
        self._keyed_since = None
        self._notify(before, now)

    def force_unkey(self, now: float) -> None:
        """End a keyed period because the limit **forced** it — enter cooloff. No-op when not keyed.

        Sets the re-key refusal window (``now + cooloff_seconds``); :meth:`may_key` is ``False`` until
        it elapses. This is what stops a stuck peer from instantly re-keying into a square wave.
        """
        if self._keyed_since is None:
            return
        before = self._mode(now)
        self._keyed_since = None
        self._cooloff_until = now + self._cooloff
        self._notify(before, now)

    # --- answers questions (pure — no state change, no notify) -----------------------------------

    def expired(self, now: float) -> bool:
        """Whether the current key-down has been held for at least ``max_seconds``."""
        return self._keyed_since is not None and (now - self._keyed_since) >= self._max

    def may_key(self, now: float) -> bool:
        """Whether keying is permitted at ``now`` — ``False`` only during an active cooloff."""
        return self._mode(now) is not TxLimitState.COOLOFF

    def state(self, now: float) -> TxLimitState:
        """The derived state at ``now`` — for inspection and a future consumer's logging."""
        return self._mode(now)
