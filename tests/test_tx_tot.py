"""Tests for the transmitter time-out timer decorator (`TotRadio`, ADR 0090).

Expiry is driven through an injected fake timer so the hard-cap logic is proven deterministically
without real waits: `FakeTimerFactory.fire_latest()` invokes the armed watchdog callback exactly as
`threading.Timer` would after `tot` seconds. Keying is asserted on `SpyRadio.ptt_log` (the
`[True, False]` idiom) and `SpyRadio.tx_log`.
"""

from __future__ import annotations

from radio_server.backends import Capability, RadioStatus
from radio_server.config import Settings
from radio_server.tx.session import DEFAULT_TX_TOT, load_tx_tot
from radio_server.tx.tot import TotRadio

from .conftest import make_settings

FRAME = object()  # opaque payload; SpyRadio only records it


class SpyRadio:
    """Records PTT edges and TX frames; an `on_transmit` hook lets a test simulate a mid-call TOT."""

    def __init__(self, on_transmit=None):
        self.ptt_log: list[bool] = []
        self.tx_log: list[object] = []
        self.closed = False
        self._on_transmit = on_transmit

    def ptt(self, on: bool) -> None:
        self.ptt_log.append(on)

    def transmit(self, audio: object) -> None:
        if self._on_transmit is not None:
            self._on_transmit()
        self.tx_log.append(audio)

    def receive(self) -> object:
        return FRAME

    def status(self) -> RadioStatus:
        return RadioStatus(backend="spy")

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.PTT})

    def close(self) -> None:
        self.closed = True


class FakeTimer:
    def __init__(self, delay: float, fn):
        self.delay = delay
        self.fn = fn
        self.started = False
        self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True


class FakeTimerFactory:
    def __init__(self):
        self.timers: list[FakeTimer] = []

    def __call__(self, delay: float, fn) -> FakeTimer:
        timer = FakeTimer(delay, fn)
        self.timers.append(timer)
        return timer

    @property
    def live(self) -> FakeTimer | None:
        for timer in reversed(self.timers):
            if timer.started and not timer.cancelled:
                return timer
        return None

    def fire_latest(self) -> None:
        timer = self.live
        assert timer is not None, "no live timer to fire"
        timer.fn()


def _tot(radio, *, tot=180.0, factory=None):
    return TotRadio(radio, tot=tot, timer_factory=factory or FakeTimerFactory())


# --- streaming (ptt-held) key -----------------------------------------------------------------

def test_streaming_key_arms_one_watchdog_and_frames_do_not_reset_it():
    factory = FakeTimerFactory()
    radio = SpyRadio()
    tot = _tot(radio, factory=factory)
    tot.ptt(True)
    assert radio.ptt_log == [True]
    assert factory.live is not None and factory.live.delay == 180.0
    armed = factory.live
    for _ in range(3):
        tot.transmit(FRAME)
    assert radio.tx_log == [FRAME, FRAME, FRAME]
    # Same timer still live — a continuous stream is a HARD cap, not reset per frame.
    assert factory.live is armed


def test_streaming_timeout_force_drops_ptt_and_latches():
    factory = FakeTimerFactory()
    radio = SpyRadio()
    tot = _tot(radio, factory=factory)
    tot.ptt(True)
    tot.transmit(FRAME)
    factory.fire_latest()  # tot seconds elapse mid-over
    assert radio.ptt_log == [True, False]  # force-unkeyed
    # Latched: further audio from a (self-keying) source is dropped until an explicit re-key.
    tot.transmit(FRAME)
    tot.transmit(FRAME)
    assert radio.tx_log == [FRAME]  # only the pre-timeout frame went out


def test_explicit_unkey_clears_the_latch_and_rekey_rearms():
    factory = FakeTimerFactory()
    radio = SpyRadio()
    tot = _tot(radio, factory=factory)
    tot.ptt(True)
    factory.fire_latest()
    assert radio.ptt_log == [True, False]
    tot.ptt(False)  # explicit unkey clears the TOT-lock
    assert radio.ptt_log == [True, False, False]
    tot.ptt(True)  # a fresh key re-arms and works
    tot.transmit(FRAME)
    assert radio.tx_log == [FRAME]
    assert factory.live is not None


def test_streaming_close_before_timeout_disarms():
    factory = FakeTimerFactory()
    radio = SpyRadio()
    tot = _tot(radio, factory=factory)
    tot.ptt(True)
    tot.ptt(False)
    assert radio.ptt_log == [True, False]
    assert factory.live is None  # cancelled, so a stale fire can't unkey a later over


# --- one-shot (self-keying) transmit ----------------------------------------------------------

def test_one_shot_transmit_arms_then_disarms_on_return():
    factory = FakeTimerFactory()
    radio = SpyRadio()
    tot = _tot(radio, factory=factory)
    tot.transmit(FRAME)
    assert radio.tx_log == [FRAME]
    assert factory.live is None  # armed for the clip, disarmed when transmit() returned


def test_one_shot_overrun_force_drops_ptt_but_does_not_latch():
    factory = FakeTimerFactory()
    radio = SpyRadio(on_transmit=lambda: factory.fire_latest())  # TOT fires mid-clip
    tot = TotRadio(radio, tot=180.0, timer_factory=factory)
    tot.transmit(FRAME)
    assert radio.ptt_log == [False]  # forced unkey during the stuck clip
    assert tot._locked is False  # a one-shot overrun must NOT wedge future one-shots
    tot.transmit(FRAME)  # a later clip still goes out
    assert radio.tx_log == [FRAME, FRAME]


# --- disabled + delegation + close ------------------------------------------------------------

def test_tot_zero_disables_the_watchdog():
    factory = FakeTimerFactory()
    radio = SpyRadio()
    tot = TotRadio(radio, tot=0.0, timer_factory=factory)
    tot.ptt(True)
    tot.transmit(FRAME)
    tot.ptt(False)
    assert radio.ptt_log == [True, False]
    assert radio.tx_log == [FRAME]
    assert factory.timers == []  # nothing ever armed


def test_delegates_the_rest_of_the_radio_surface():
    radio = SpyRadio()
    tot = _tot(radio)
    assert tot.receive() is FRAME
    assert tot.status().backend == "spy"
    assert tot.capabilities() == frozenset({Capability.PTT})


def test_close_disarms_and_closes_the_wrapped_device():
    factory = FakeTimerFactory()
    radio = SpyRadio()
    tot = _tot(radio, factory=factory)
    tot.ptt(True)
    tot.close()
    assert factory.live is None
    assert radio.closed is True


# --- the forced-unkey hook + introspection (ADR 0117) -----------------------------------------

def test_on_timeout_hook_fires_after_a_forced_unkey():
    factory = FakeTimerFactory()
    radio = SpyRadio()
    fired: list[float] = []
    tot = TotRadio(radio, tot=180.0, on_timeout=lambda: fired.append(1), timer_factory=factory)
    tot.ptt(True)
    assert fired == []  # not until it actually fires
    factory.fire_latest()
    assert radio.ptt_log == [True, False]  # unkeyed first
    assert fired == [1]  # then the alarm hook


def test_set_on_timeout_wires_and_replaces_the_hook_post_construction():
    factory = FakeTimerFactory()
    radio = SpyRadio()
    tot = TotRadio(radio, tot=180.0, timer_factory=factory)  # built with no hook (the initial-radio case)
    seen: list[str] = []
    tot.set_on_timeout(lambda: seen.append("a"))
    tot.ptt(True)
    factory.fire_latest()
    assert seen == ["a"]
    # A replacement wins on the next fire; None silences it.
    tot.set_on_timeout(lambda: seen.append("b"))
    tot.ptt(True)
    factory.fire_latest()
    assert seen == ["a", "b"]
    tot.set_on_timeout(None)
    tot.ptt(True)
    factory.fire_latest()
    assert seen == ["a", "b"]  # unchanged — the hook was cleared


def test_tot_property_reports_the_cap():
    assert TotRadio(SpyRadio(), tot=120.0, timer_factory=FakeTimerFactory()).tot == 120.0
    assert TotRadio(SpyRadio(), tot=0.0, timer_factory=FakeTimerFactory()).tot == 0.0


# --- config -----------------------------------------------------------------------------------

def test_load_tx_tot_default_and_override():
    assert load_tx_tot(make_settings({})) == DEFAULT_TX_TOT
    assert load_tx_tot(make_settings({"tx.tot": 90.0})) == 90.0
    assert load_tx_tot(make_settings({"tx.tot": 0})) == 0  # 0 disables (nonneg coercer allows it)


def test_build_radio_wraps_the_backend_in_tot():
    from radio_server.api.holder import build_radio

    radio = build_radio(make_settings({"server.backend": "mock"}))
    assert isinstance(radio, TotRadio)


def test_build_radio_wires_on_tot_timeout_with_the_resolved_cap():
    # The swap-path alarm seam (ADR 0117): build_radio closes the resolved tot into TotRadio's no-arg
    # hook, so the alarm payload can name the cap that fired. (The forced unkey uses a real timer here,
    # so drive the wired hook directly rather than waiting on it.)
    from radio_server.api.holder import build_radio

    got: list[float] = []
    radio = build_radio(
        make_settings({"server.backend": "mock", "tx.tot": 90.0}),
        on_tot_timeout=got.append,
    )
    assert radio.tot == 90.0
    radio._on_timeout()  # the callback build_radio wrapped around on_tot_timeout(tot)
    assert got == [90.0]
