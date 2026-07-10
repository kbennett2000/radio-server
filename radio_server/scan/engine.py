"""The software scan engine (ADR 0012) — "scan channels remotely like in person."

This is a software scan *loop* over the CAT surface, distinct from the radio's built-in
``CatRadio.scan(on)`` hardware toggle. It steps a plan of frequencies, tunes each
(``set_frequency``), lets the reading settle, polls ``status().busy``, and acts on
activity — dwell, resume, hold, skip locked-out channels, re-check a priority channel.

Two drive surfaces share one set of pure helpers:

- :meth:`ScanEngine.tick` — the full clock-driven state machine (carrier / timed / hold
  resume modes). Every timing decision is made against an injected clock, so tests drive
  it with a fake clock and no real sleeps.
- :meth:`ScanEngine.sweep` — a synchronous single pass that stops-and-holds at the first
  active channel. Clear channels advance instantly (a sweep never dwells on time), so it
  needs no clock and runs with zero sleeps. This is what the HTTP ``POST /scan`` calls.

Layering: this module imports only :mod:`radio_server.backends` and emits progress through
an injected ``on_event`` callback (a :class:`ScanEvent`), never importing the API's
``EventHub``. The API adapts ``ScanEvent`` to an ``Event(type="scan", ...)``, so the
dependency arrow stays ``api → scan`` (and ``scan → nothing-above``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from ..arbiter import RadioArbiter
from ..backends import Capability, CatRadio, UnsupportedCapability

if TYPE_CHECKING:
    from ..config import Settings

#: A clock returns Unix-ish seconds as a float. Injectable so dwell/settle timing is exactly
#: testable with a fake clock (no real sleeps). Defined locally rather than imported from the
#: auth layer so this module stays lean (the dependency arrow stays scan → backends only).
Clock = Callable[[], float]


# --- resume modes --------------------------------------------------------------------------

class ResumeMode(StrEnum):
    """What the scan does when it finds an active channel."""

    #: Dwell while the channel stays busy; resume scanning when the carrier drops. The
    #: classic "listen until they stop talking" behavior — the marked default.
    CARRIER = "carrier"
    #: Dwell a fixed number of seconds, then move on even if still busy.
    TIMED = "timed"
    #: Stop the scan entirely on the first activity and hold until told to resume.
    HOLD = "hold"


# --- events --------------------------------------------------------------------------------

#: The progress phases the engine emits, in the order a client sees them for a held channel
#: (``scanning`` → ``active`` → ``dwelling``), plus ``resumed`` when a dwell ends.
SCAN_PHASES = ("scanning", "active", "dwelling", "resumed")


@dataclass(frozen=True)
class ScanEvent:
    """One scan-progress event handed to the injected ``on_event`` callback.

    ``phase`` is one of :data:`SCAN_PHASES`; ``frequency`` is the channel in Hz;
    ``channel`` is its index in the plan's active list (``None`` for the priority channel,
    which is not a plan index).
    """

    phase: str
    frequency: int | None = None
    channel: int | None = None


# --- scan plan -----------------------------------------------------------------------------

@dataclass(frozen=True)
class ScanPlan:
    """An ordered list of frequencies (Hz) to scan, with lockout and a priority channel.

    Channels are addressed by frequency (a range+step is naturally in Hz, and per-channel
    busy is keyed on frequency); channel-*number* plans are out of scope this cycle.
    """

    channels: tuple[int, ...]
    lockout: frozenset[int] = frozenset()
    priority: int | None = None

    @classmethod
    def from_frequencies(
        cls,
        freqs: Iterable[int],
        *,
        lockout: Iterable[int] = (),
        priority: int | None = None,
    ) -> ScanPlan:
        return cls(tuple(freqs), frozenset(lockout), priority)

    @classmethod
    def from_range(
        cls,
        start_hz: int,
        stop_hz: int,
        step_hz: int,
        *,
        lockout: Iterable[int] = (),
        priority: int | None = None,
    ) -> ScanPlan:
        """Build a plan from an inclusive ``[start_hz, stop_hz]`` range stepped by ``step_hz``."""
        if step_hz <= 0:
            raise ValueError("step_hz must be positive")
        if stop_hz < start_hz:
            raise ValueError("stop_hz must be >= start_hz")
        channels = tuple(range(start_hz, stop_hz + 1, step_hz))
        return cls(channels, frozenset(lockout), priority)

    def active_channels(self) -> list[int]:
        """The scanned frequencies in order, with locked-out channels removed."""
        return [c for c in self.channels if c not in self.lockout]


# --- config (guardrail 1: marked defaults, verify against hardware) ------------------------

#: Seconds to wait after tuning before the busy reading is trusted. VERIFY AGAINST HARDWARE
#: (guardrail 1) — real settle time is the radio's PLL lock + squelch response, an empirical
#: bring-up fact, not a confirmed value.
DEFAULT_SCAN_SETTLE = 0.05
#: Seconds between busy polls while a live pump ticks the engine. VERIFY AGAINST HARDWARE
#: (guardrail 1). Not consumed by the pure engine (which is tick-driven); it is the cadence
#: the future controller loop will call :meth:`ScanEngine.tick` at.
DEFAULT_SCAN_POLL = 0.5
#: Timed-mode dwell length (seconds). An operator preference, safe as config.
DEFAULT_SCAN_DWELL = 5.0
#: The marked-default resume mode when ``RADIO_SCAN_MODE`` is unset.
DEFAULT_SCAN_MODE = "carrier"

RADIO_SCAN_SETTLE_ENV_VAR = "RADIO_SCAN_SETTLE"
RADIO_SCAN_POLL_ENV_VAR = "RADIO_SCAN_POLL"
RADIO_SCAN_DWELL_ENV_VAR = "RADIO_SCAN_DWELL"
RADIO_SCAN_MODE_ENV_VAR = "RADIO_SCAN_MODE"


def load_scan_settle(settings: Settings) -> float:
    """Return the post-tune settle time in seconds (`scan.settle`)."""
    return settings.get("scan.settle")


def load_scan_poll(settings: Settings) -> float:
    """Return the busy-poll interval in seconds (`scan.poll`)."""
    return settings.get("scan.poll")


def load_scan_dwell(settings: Settings) -> float:
    """Return the timed-mode dwell in seconds (`scan.dwell`)."""
    return settings.get("scan.dwell")


def load_scan_mode(settings: Settings) -> ResumeMode:
    """Return the resume mode (`scan.mode`, default ``carrier``)."""
    return settings.get("scan.mode")


# --- the engine ----------------------------------------------------------------------------

class _State(StrEnum):
    """Internal engine state (distinct from the emitted :data:`SCAN_PHASES`)."""

    IDLE = "idle"          # not started
    LISTENING = "listening"  # tuned to a channel, settling then polling
    DWELLING = "dwelling"    # holding on an active channel (carrier/timed)
    HELD = "held"            # stopped on activity (hold mode) — terminal
    DONE = "done"            # nothing to scan (empty plan) — terminal


class ScanEngine:
    """Drive a scan over a :class:`CatRadio` per a :class:`ScanPlan`.

    Raises :class:`UnsupportedCapability` at construction on a backend that does not
    advertise :attr:`Capability.SCAN` — the same guard the API's capability gate relies on,
    so an audio-only radio can never be scanned.
    """

    def __init__(
        self,
        radio: CatRadio,
        plan: ScanPlan,
        *,
        on_event: Callable[[ScanEvent], None] | None = None,
        mode: ResumeMode = ResumeMode.CARRIER,
        dwell: float = DEFAULT_SCAN_DWELL,
        settle: float = DEFAULT_SCAN_SETTLE,
        clock: Clock | None = None,
        arbiter: RadioArbiter | None = None,
    ) -> None:
        if Capability.SCAN not in radio.capabilities():
            raise UnsupportedCapability(Capability.SCAN)
        if clock is None:
            import time

            clock = time.monotonic
        self._radio = radio
        self._plan = plan
        self._on_event = on_event
        self._mode = ResumeMode(mode)
        self._dwell = dwell
        self._settle = settle
        self._clock = clock
        # The shared half-duplex arbiter (ADR 0017): a TX key-up takes the radio, so `tick()`
        # pauses the scan in place while it holds. A private idle arbiter is the safe default —
        # `transmitting` is always False, so an un-injected engine never pauses.
        self._arbiter = arbiter if arbiter is not None else RadioArbiter()

        self._active = plan.active_channels()
        self._state = _State.IDLE
        self._i = 0
        self._current_freq: int | None = None
        self._current_channel: int | None = None
        self._tuned_at = 0.0
        self._dwell_deadline = 0.0

    @property
    def state(self) -> _State:
        return self._state

    @property
    def current_frequency(self) -> int | None:
        return self._current_freq

    # --- pure helpers (shared by tick and sweep) ------------------------------------------

    def _emit(self, phase: str, freq: int | None, channel: int | None) -> None:
        if self._on_event is not None:
            self._on_event(ScanEvent(phase=phase, frequency=freq, channel=channel))

    def _tune(self, freq: int) -> None:
        self._radio.set_frequency(freq)

    def _read_busy(self) -> bool:
        return self._radio.status().busy

    def _next_index(self) -> int:
        # Wrap: tick() is a continuous scanner. sweep() does its own single-pass iteration.
        return (self._i + 1) % len(self._active)

    def _begin_listen(self, freq: int, channel: int, now: float) -> None:
        self._tune(freq)
        self._current_freq = freq
        self._current_channel = channel
        self._tuned_at = now
        self._state = _State.LISTENING
        self._emit("scanning", freq, channel)

    def _begin_dwell(self, freq: int, channel: int, now: float) -> None:
        self._current_freq = freq
        self._current_channel = channel
        self._emit("active", freq, channel)
        if self._mode is ResumeMode.HOLD:
            self._state = _State.HELD
        else:
            if self._mode is ResumeMode.TIMED:
                self._dwell_deadline = now + self._dwell
            self._state = _State.DWELLING
        self._emit("dwelling", freq, channel)

    def _advance(self, now: float) -> None:
        """Move to the next channel to listen on, checking the priority channel first.

        Priority is an interstitial peek between steps: tune to it and poll once; if active,
        hold it (``scanning`` → ``active`` → ``dwelling``); otherwise fall through to the
        next sequential channel.
        """
        if self._plan.priority is not None:
            self._tune(self._plan.priority)
            if self._read_busy():
                self._emit("scanning", self._plan.priority, None)
                self._begin_dwell(self._plan.priority, None, now)
                return
        self._i = self._next_index()
        self._begin_listen(self._active[self._i], self._i, now)

    # --- clock-driven state machine -------------------------------------------------------

    def tick(self, now: float | None = None) -> _State:
        """Advance the scan by one step against the clock; returns the new state.

        The full resume-mode machine. Each channel settles for :attr:`_settle` before its
        busy poll is trusted, so a fresh channel typically takes two ticks (settle, then
        poll). A continuous scanner: after a clear channel or a resumed dwell it wraps.
        """
        if now is None:
            now = self._clock()

        if self._arbiter.transmitting:
            # Half-duplex (ADR 0017): a TX key-up takes the radio; pause the scan in place — do not
            # tune, poll, or advance while keyed. Every positional field (_state, _i, _current_freq,
            # _tuned_at, _dwell_deadline) lives on the instance and survives, so the next tick after
            # TX releases resumes exactly where it left off; no saved-position state is needed.
            return self._state

        if self._state is _State.IDLE:
            self._start(now)
            return self._state
        if self._state in (_State.HELD, _State.DONE):
            return self._state

        if self._state is _State.LISTENING:
            if now < self._tuned_at + self._settle:
                return self._state  # still settling; busy read not yet trusted
            if self._read_busy():
                self._begin_dwell(self._current_freq, self._current_channel, now)
            else:
                self._advance(now)
            return self._state

        if self._state is _State.DWELLING:
            if self._mode is ResumeMode.CARRIER:
                if not self._read_busy():
                    self._emit("resumed", self._current_freq, self._current_channel)
                    self._advance(now)
            elif self._mode is ResumeMode.TIMED:
                if now >= self._dwell_deadline:
                    self._emit("resumed", self._current_freq, self._current_channel)
                    self._advance(now)
            return self._state

        return self._state

    def _start(self, now: float) -> None:
        if not self._active:
            self._state = _State.DONE
            return
        self._i = 0
        self._begin_listen(self._active[0], 0, now)

    # --- synchronous single-pass sweep (what POST /scan calls) ----------------------------

    def sweep(self) -> int | None:
        """Tune each channel once, stopping-and-holding at the first active one.

        Clear channels advance instantly (no dwell, no clock), so this runs with zero
        sleeps. Emits ``scanning`` for each channel listened to, and ``active`` →
        ``dwelling`` on the one it holds. The priority channel is peeked between steps and
        held (``scanning`` → ``active`` → ``dwelling``) if active. Returns the held
        frequency, or ``None`` if the whole plan was clear.
        """
        if not self._active:
            self._state = _State.DONE
            return None
        for idx, freq in enumerate(self._active):
            if idx > 0 and self._plan.priority is not None:
                self._tune(self._plan.priority)
                if self._read_busy():
                    self._emit("scanning", self._plan.priority, None)
                    self._current_freq = self._plan.priority
                    self._current_channel = None
                    self._emit("active", self._plan.priority, None)
                    self._state = _State.HELD
                    self._emit("dwelling", self._plan.priority, None)
                    return self._plan.priority
            self._i = idx
            self._tune(freq)
            self._current_freq = freq
            self._current_channel = idx
            self._emit("scanning", freq, idx)
            if self._read_busy():
                self._emit("active", freq, idx)
                self._state = _State.HELD
                self._emit("dwelling", freq, idx)
                return freq
        self._state = _State.DONE
        return None


# --- composition root ----------------------------------------------------------------------

def build_scan_engine(
    settings: Settings,
    *,
    radio: CatRadio,
    plan: ScanPlan,
    on_event: Callable[[ScanEvent], None] | None = None,
    clock: Clock | None = None,
    arbiter: RadioArbiter | None = None,
) -> ScanEngine:
    """Construct a :class:`ScanEngine` with mode/dwell/settle from ``settings``.

    Mirrors `build_id_encoder`: the schema-resolved values supply the timing and resume mode, while
    ``radio``, ``plan``, ``on_event``, and the half-duplex ``arbiter`` (ADR 0017) are injected.
    """
    return ScanEngine(
        radio,
        plan,
        on_event=on_event,
        mode=load_scan_mode(settings),
        dwell=load_scan_dwell(settings),
        settle=load_scan_settle(settings),
        clock=clock,
        arbiter=arbiter,
    )
