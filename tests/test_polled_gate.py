"""PolledGate — decouple an expensive signal gate's read from the RX audio reader (ADR 0125).

`CatBusyGate.__call__` is a ~100 ms serial round-trip; the RX pump calls its gate once per 20 ms
frame on the single capture reader, so an inline CAT read stalls that reader past the 60 ms ALSA
ring (bench: 14 % pump duty, zero bytes to the browser, shredded DTMF). `PolledGate` runs the inner
gate on a background thread and caches the verdict, so `__call__` returns a cached bool with zero
serial in the audio path (bench: ~100 % duty).

The load-bearing proofs, kept off wall-clock timing where possible: `__call__` never drives the
inner gate; `_poll_once` caches the verdict and feeds the inner gate the latest frame; the background
thread runs while started and is joined on stop (idempotent, restartable); a raising inner gate reads
as not-busy and never kills the poller; and `detects_signal` mirrors the inner gate.
"""

from __future__ import annotations

import logging
import threading

from radio_server.activity import PolledGate
from radio_server.audio import AudioFrame


def frame(rms: int = 0) -> AudioFrame:
    """A tiny non-empty canonical frame; content is irrelevant to these tests."""
    return AudioFrame(bytes([rms & 0xFF, 0x00]))


class SpyGate:
    """An inner-gate double: records every call, can flip its verdict, raise, and signal N polls."""

    def __init__(self, value: bool = False, *, detects_signal: bool = True, raises: bool = False):
        self.value = value
        self.detects_signal = detects_signal
        self._raises = raises
        self.calls = 0
        self.frames: list[AudioFrame] = []
        self.signal_at = 1
        self.polled = threading.Event()  # set once `calls` reaches `signal_at`

    def __call__(self, f: AudioFrame) -> bool:
        self.calls += 1
        self.frames.append(f)
        if self.calls >= self.signal_at:
            self.polled.set()
        if self._raises:
            raise RuntimeError("inner gate boom")
        return self.value


def _live_pollers() -> int:
    return sum(1 for t in threading.enumerate() if t.name == "rx-cat-gate" and t.is_alive())


# --- __call__ is cheap: zero inner calls in the audio path --------------------------------------

def test_call_returns_the_cached_verdict_without_driving_the_inner_gate():
    spy = SpyGate(value=True)
    gate = PolledGate(spy)
    assert gate(frame()) is False  # the default seed before any poll
    assert spy.calls == 0  # __call__ must NEVER run the inner gate — that is the whole point
    gate._poll_once()
    assert spy.calls == 1
    assert gate(frame()) is True  # now reflects the polled verdict
    for _ in range(10):
        gate(frame())
    assert spy.calls == 1  # the 10 __call__s did not touch the inner gate


def test_poll_once_tracks_the_inner_verdict_both_ways():
    spy = SpyGate(value=True)
    gate = PolledGate(spy)
    gate._poll_once()
    assert gate(frame()) is True
    spy.value = False
    gate._poll_once()
    assert gate(frame()) is False


def test_poll_once_feeds_the_inner_gate_the_latest_frame():
    spy = SpyGate()
    gate = PolledGate(spy)
    f = frame(123)
    gate(f)  # store the latest frame
    gate._poll_once()
    assert spy.frames[-1] is f  # the poller evaluated the inner gate against that frame


def test_poll_once_before_any_frame_uses_an_empty_seed():
    spy = SpyGate()
    gate = PolledGate(spy)
    gate._poll_once()  # no __call__ has happened yet
    assert spy.frames[-1].samples == b""  # a safe empty canonical frame, not None


def test_poll_once_swallows_inner_exceptions_and_reads_not_busy():
    gate = PolledGate(SpyGate(value=True, raises=True))
    gate._poll_once()  # must not raise
    assert gate(frame()) is False  # a failed read gates closed, matching status()-over-serial


# --- background lifecycle: runs while started, joins on stop, restartable ------------------------

def test_start_spins_a_poller_and_stop_joins_it():
    spy = SpyGate(value=True)
    gate = PolledGate(spy, interval=0.001)
    before = _live_pollers()
    gate.start()
    try:
        assert spy.polled.wait(2.0)  # the background thread evaluated the inner gate
        assert gate(frame()) is True  # and its verdict reached the cache
        assert _live_pollers() == before + 1
    finally:
        gate.stop()
    assert _live_pollers() == before  # stop() joined the thread


def test_start_is_idempotent_no_second_poller():
    spy = SpyGate(value=True)
    gate = PolledGate(spy, interval=0.001)
    before = _live_pollers()
    gate.start()
    gate.start()  # second start while alive must not spawn another thread
    try:
        assert spy.polled.wait(2.0)
        assert _live_pollers() == before + 1
    finally:
        gate.stop()


def test_restartable_after_stop():
    spy = SpyGate(value=True)
    gate = PolledGate(spy, interval=0.001)
    gate.start()
    assert spy.polled.wait(2.0)
    gate.stop()
    # A fresh cycle: the same gate must poll again (the demand-driven pump start/stops it repeatedly).
    spy.polled.clear()
    spy.calls = 0
    gate.start()
    try:
        assert spy.polled.wait(2.0)
    finally:
        gate.stop()


def test_stop_is_idempotent_and_safe_when_never_started():
    gate = PolledGate(SpyGate())
    gate.stop()  # never started — no error, no thread
    gate.start()
    gate.stop()
    gate.stop()  # double stop — no error


def test_background_thread_survives_inner_exceptions():
    spy = SpyGate(value=True, raises=True)
    spy.signal_at = 3
    gate = PolledGate(spy, interval=0.001)
    gate.start()
    try:
        # The poller keeps polling despite every inner call raising — it did not die on the first one.
        assert spy.polled.wait(2.0)
        assert spy.calls >= 3
        assert gate(frame()) is False
    finally:
        gate.stop()


# --- signal-awareness and inner exposure --------------------------------------------------------

def test_detects_signal_mirrors_the_inner_gate():
    assert PolledGate(SpyGate(detects_signal=True)).detects_signal is True
    assert PolledGate(SpyGate(detects_signal=False)).detects_signal is False
    # A bare callable with no attribute is treated as signal-aware — the pump's own default.
    assert PolledGate(lambda f: False).detects_signal is True


def test_inner_is_exposed_for_the_live_switch_rebuild():
    inner = SpyGate()
    assert PolledGate(inner).inner is inner


# --- the silent failures become readable (ADR 0178) ---------------------------------------------
#
# Both failures below are real and both were invisible. `_poll_once` catching everything to
# `_busy = False` is correct — a serial timeout must not kill the poller thread — but a gate whose
# every tick raises then reports "channel clear" for ever, and every received frame is dropped at
# `RxPump`'s `if self._gate(frame):`: no browser audio, no recording, no Mumble or D-STAR relay, no
# RX-activity edge, no scan dwell. It renders exactly like a quiet band.
#
# Log lines only, deliberately: counters here would reach no endpoint (ADR 0178 finding 2), and this
# is an RX path where a behaviour change is the wrong shape for an audit cycle. **They cover the
# event, not the standing state.** `PolledGate` still has no staleness expiry — a gate whose thread
# died holds `_busy = False` indefinitely and nothing ages it out, so nobody reading the station an
# hour later sees anything wrong. `RssiPoller.reading()` already solved exactly that by returning
# `None` past `STALE_AFTER` intervals; that is the successor's shape, and it is carried named.

GATE_LOGGER = "radio_server.activity.gate"


def test_a_raising_inner_gate_says_so_once_instead_of_silently_reading_not_busy(caplog):
    """Proof-of-event first: the verdict really did collapse to False, forty times."""
    spy = SpyGate(value=True, raises=True)
    gate = PolledGate(spy)
    with caplog.at_level(logging.WARNING, logger=GATE_LOGGER):
        for _ in range(40):
            gate._poll_once()

    assert spy.calls == 40
    assert gate(frame()) is False, "the fail-closed verdict is unchanged; only its silence is"
    warnings = [r for r in caplog.records if r.levelname == "WARNING" and r.name == GATE_LOGGER]
    assert len(warnings) == 1, (
        f"expected one line for forty failed polls, got {len(warnings)} — the CAT gate ticks at "
        "5 Hz, so a line per tick is 432 000 a day and an operator learns to scroll past it"
    )
    assert "RuntimeError" in warnings[0].getMessage()


def test_a_gate_that_starts_working_again_says_that_too(caplog):
    """Rate-limited on the transition, not on the first-ever failure: a gate that fails, recovers
    and fails again is three facts, and reporting only the first makes the second failure silent."""
    spy = SpyGate(value=True, raises=True)
    gate = PolledGate(spy)
    with caplog.at_level(logging.INFO, logger=GATE_LOGGER):
        gate._poll_once()
        spy._raises = False
        gate._poll_once()
        assert gate(frame()) is True  # proof it really recovered
        spy._raises = True
        gate._poll_once()

    levels = [r.levelname for r in caplog.records if r.name == GATE_LOGGER]
    assert levels == ["WARNING", "INFO", "WARNING"], levels


def test_a_healthy_gate_logs_nothing(caplog):
    """The paired negative — without it, an unconditional log satisfies both tests above."""
    gate = PolledGate(SpyGate(value=True))
    with caplog.at_level(logging.DEBUG, logger=GATE_LOGGER):
        for _ in range(40):
            gate._poll_once()
    assert [r for r in caplog.records if r.name == GATE_LOGGER] == []


def test_a_gate_whose_poller_cannot_be_started_is_not_swallowed(caplog):
    """`RxPump._start_gate` caught every exception and passed. A gate whose thread never spawned is
    indistinguishable from a permanently quiet channel, and the pump is the only thing that would
    ever have known."""
    from radio_server.rx.pump import RxPump

    class Unstartable(SpyGate):
        def start(self):
            raise RuntimeError("no thread for you")

    pump = object.__new__(RxPump)
    pump._gate = Unstartable()
    with caplog.at_level(logging.WARNING, logger="radio_server.rx.pump"):
        pump._start_gate()  # must not raise: the pump still runs, ungated

    records = [r for r in caplog.records if r.name == "radio_server.rx.pump"]
    assert len(records) == 1
    assert "no thread for you" in caplog.text
