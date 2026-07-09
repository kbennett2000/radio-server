"""The half-duplex radio arbiter: who owns the radio right now (ADR 0017).

A half-duplex radio physically cannot receive and transmit at once — keying the transmitter
blinds the receiver. This module owns the single seam that coordinates the two: a small shared
:class:`RadioArbiter` that the TX session *claims* on key-up and *releases* on key-down, and that
the RX pump and scan engine *consult* to stand down while a transmission holds the radio.

The design is two independent latches with a **derived** mode:

- ``_transmitting`` — set by TX (``acquire_tx`` / ``release_tx``).
- ``_receiving`` — set by the RX pump around its active lifetime (``begin_receive`` /
  ``end_receive``).

``mode`` derives from them with **TX priority** (transmitting > receiving > idle). RX *wanting* the
radio and TX *holding* it are independent facts; the physical exclusion (they can't both happen) is
the derived mode — TX wins — enforced by the readers checking :attr:`transmitting` and pausing. So
when TX releases, the RX latch is still set and the mode returns to ``receiving`` on its own, with
no preempt/restore bookkeeping.

This package deliberately imports **nothing** from the rest of ``radio_server`` (only stdlib), so
every consumer's dependency arrow stays clean: ``tx -> arbiter``, ``rx -> arbiter``,
``scan -> arbiter``, ``api -> arbiter``, no cycles. It models the *logical* exclusion only; the
real PTT-tail / TX-to-RX turnaround timing is a bench fact (guardrail 1), not modeled here.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Callable


class RadioMode(StrEnum):
    """Who has the (single, half-duplex) radio right now."""

    IDLE = "idle"
    RECEIVING = "receiving"
    TRANSMITTING = "transmitting"


class ArbiterStateError(RuntimeError):
    """Raised on an incoherent transition — e.g. keying TX while already transmitting."""


class RadioArbiter:
    """Shared radio-ownership arbiter enforcing half-duplex, TX-priority exclusion.

    One instance per app, created at the composition root and injected into the TX session (the
    writer) and the RX pump + scan engine (the readers). The readers consult :attr:`transmitting`
    and stand down while it holds; TX claims it with :meth:`acquire_tx` and frees it with
    :meth:`release_tx`.

    ``on_change`` is an optional, injected callback fired **only when the derived** :attr:`mode`
    **actually changes** — the composition root wires it to publish an ``"arbiter"`` event so the
    ledger records mode transitions (ADR 0019). It stays leaf-pure: a plain ``Callable``, no
    ``radio_server`` import, and a publish fault can never reach here (the wired publisher is
    non-raising). A latch flip that leaves the derived mode unchanged (e.g. ``begin_receive`` while
    transmitting) fires nothing.
    """

    def __init__(
        self, *, on_change: Callable[[RadioMode], None] | None = None
    ) -> None:
        self._transmitting = False
        self._receiving = False
        self._on_change = on_change

    def _notify(self, before: RadioMode) -> None:
        """Fire ``on_change`` iff the derived mode changed from ``before``."""
        after = self.mode
        if self._on_change is not None and after != before:
            self._on_change(after)

    @property
    def mode(self) -> RadioMode:
        """The current owner, TX priority > RX > idle."""
        if self._transmitting:
            return RadioMode.TRANSMITTING
        if self._receiving:
            return RadioMode.RECEIVING
        return RadioMode.IDLE

    @property
    def transmitting(self) -> bool:
        """Whether TX holds the radio — what the RX pump and scan engine check to pause."""
        return self._transmitting

    def acquire_tx(self) -> None:
        """Claim the radio for TX (key-up). Raises if already transmitting (can't double-key).

        The coherence guard: one transmitter can only be keyed by one talker. In the app the
        ``TxSlot`` single-talker guard makes this belt-and-suspenders (the shared arbiter starts
        idle/receiving), but the check keeps the state machine honest.
        """
        if self._transmitting:
            raise ArbiterStateError("already transmitting — cannot key TX")
        before = self.mode
        self._transmitting = True
        self._notify(before)

    def release_tx(self) -> None:
        """Free the radio from TX (key-down). Idempotent — mirrors ``TxSession.close()``, which may
        be called on a stream that never keyed, so releasing when not transmitting is a no-op."""
        before = self.mode
        self._transmitting = False
        self._notify(before)

    def begin_receive(self) -> None:
        """Mark the RX pump as active (it wants the radio). Does not contend TX — the derived
        ``mode`` masks it while transmitting; delivery is what actually pauses."""
        before = self.mode
        self._receiving = True
        self._notify(before)

    def end_receive(self) -> None:
        """Mark the RX pump as no longer active (idempotent)."""
        before = self.mode
        self._receiving = False
        self._notify(before)
