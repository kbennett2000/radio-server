"""A cadence pause hook that raises must not render as a hook that said "no" (ADR 0178).

Both cadences guard the shared AIOC wire with the same seven lines::

    if self._paused is not None:
        try:
            paused = bool(self._paused())
        except Exception:
            paused = False

The fail-open is right — ADR 0175's own reasoning is that a broken hook must not silence the meter
for ever. What is wrong is that it is **silent**. A hook that raises on every tick is byte-identical,
in every counter and every log line, to a hook that answers ``False``: ``skipped`` stays 0 and
``polls`` climbs in both. So the state that re-enables the contention ADR 0177 measured — the witness
saw RF in **0 of 81 carrier polls** with one control exchange across the DTR assert — is
indistinguishable from the state where the station simply never transmitted.

That is this cycle's whole subject: a check that answers the safe-looking default. `pause_errors`
names it. **A nonzero `pause_errors` says the pause hook is broken and the cadence is running
unguarded — it does NOT say any transmission was damaged.** Only an RF measurement at a witness says
that; ADR 0177's `WireStats` counters are the ones that speak to key-ups.

Every test here drives ``poll_once()`` by hand. No thread is ever started, so the tick count is exact
and no counter can be raced by a cadence nobody stopped — the discipline the existing suite already
follows (`tests/test_broadcast_fm_poll.py`, `tests/test_baofeng_rssi.py`). `FakeClock` cannot drive
ticks here: the loop is ``stop.wait(interval)`` in real time, and the injected clock is read only for
``_reading_at``/``age_s``.
"""

from __future__ import annotations

import logging
import threading

import pytest

from radio_server.activity.broadcast_fm_poll import NO_CADENCE, BroadcastFmPoller, cadence_stats
from radio_server.activity.rssi_poll import RssiPoller
from radio_server.backends.base import BroadcastFm

#: The two cadences, their answer type, and the logger their poll runs under. Parametrised because
#: the seven guarded lines are textually identical in both files — a fix applied to one and not the
#: other is exactly the shape ADR 0176 found in ADR 0175's work.
CADENCES = [
    pytest.param(RssiPoller, 161, "radio_server.activity.rssi_poll", id="rssi"),
    pytest.param(BroadcastFmPoller, True, "radio_server.activity.broadcast_fm_poll", id="fm"),
]

FULL = BroadcastFm(on=False, hz=104_300_000, blocks_tx=True, rescues=0)


class Probe:
    """Answers every call the same way, and counts. The count is what proves a poll reached the wire."""

    def __init__(self, answer) -> None:
        self.answer = answer
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, **kwargs):
        with self._lock:
            self.calls += 1
            return self.answer


def boom() -> bool:
    """A pause hook whose every call raises — a `cadence_paused` reading a torn-down transport, a
    predicate whose backend was swapped out from under it, a typo in a lambda."""
    raise RuntimeError("the hook is broken")


def drive(cls, answer, *, paused, ticks: int, clock=None):
    """`ticks` deterministic polls. Returns the poller and its probe.

    `clock` defaults to the real monotonic one. Pass a frozen clock wherever two runs are compared:
    `age_s` is ``clock() - _reading_at``, so on the real clock two otherwise identical `stats()`
    dicts differ by microseconds and an equality assertion passes without measuring anything.
    """
    probe = Probe(answer)
    kwargs = {"paused": paused, **({} if clock is None else {"clock": clock})}
    poller = (
        cls(probe, lambda: FULL, **kwargs)
        if cls is BroadcastFmPoller
        else cls(probe, **kwargs)
    )
    for _ in range(ticks):
        poller.poll_once()
    return poller, probe


# --- the counter ---------------------------------------------------------------------------


@pytest.mark.parametrize(("cls", "answer", "_logger"), CADENCES)
def test_a_pause_hook_that_raises_is_counted_and_not_merely_survived(cls, answer, _logger):
    """The fail-open is correct and stays; what changes is that it leaves a mark.

    `probe.calls` is asserted **first** and it is the load-bearing half: it proves the hook really
    did raise and the poll really did go to the wire anyway. Asserting the counter alone would pass
    against a poller that never polled at all — ADR 0165's vacuous "0 raw".
    """
    poller, probe = drive(cls, answer, paused=boom, ticks=5)
    assert probe.calls == 5, "the broken hook did not actually let the polls through"
    assert poller.stats()["pause_errors"] == 5


@pytest.mark.parametrize(("cls", "answer", "_logger"), CADENCES)
def test_a_pause_hook_that_answers_is_not_counted_as_a_broken_one(cls, answer, _logger):
    """The paired negative, without which the test above is satisfiable by a counter that is really
    "ticks". The two runs differ in exactly one thing: whether the hook raised."""
    poller, probe = drive(cls, answer, paused=lambda: False, ticks=5)
    assert probe.calls == 5
    assert poller.stats()["pause_errors"] == 0


@pytest.mark.parametrize(("cls", "answer", "_logger"), CADENCES)
def test_a_broken_hook_and_a_hook_that_says_no_stop_rendering_identically(cls, answer, _logger):
    """The finding, written as an assertion.

    On master these two `stats()` dicts compare **equal**, and that equality *is* the defect: a
    cadence running unguarded across every key-up and a cadence whose station is simply idle are the
    same reading. The first three asserts pin that the fields which were already equal stay equal —
    this cycle does not move `skipped` or `polls` — and the last one is the fix.
    """
    frozen = (lambda: 0.0)
    broken, _ = drive(cls, answer, paused=boom, ticks=5, clock=frozen)
    working, _ = drive(cls, answer, paused=lambda: False, ticks=5, clock=frozen)

    assert broken.stats()["age_s"] == working.stats()["age_s"], (
        "the clock must be frozen, or these two dicts differ by float noise and the "
        "inequality below passes without measuring anything"
    )
    assert broken.stats()["skipped"] == working.stats()["skipped"] == 0
    assert broken.stats()["polls"] == working.stats()["polls"] == 5
    assert broken.stats()["unknown"] == working.stats()["unknown"]
    assert broken.stats() != working.stats(), (
        "a broken pause hook and a hook that answers 'not transmitting' still render identically"
    )


@pytest.mark.parametrize(("cls", "answer", "_logger"), CADENCES)
def test_a_hook_that_says_yes_is_a_skip_and_never_an_error(cls, answer, _logger):
    """`skipped` and `pause_errors` are different facts and must not bleed into each other: one is
    the guard working, the other is the guard broken."""
    poller, probe = drive(cls, answer, paused=lambda: True, ticks=5)
    assert probe.calls == 0, "a paused cadence must not reach the wire at all (ADR 0176)"
    assert poller.stats()["skipped"] == 5
    assert poller.stats()["pause_errors"] == 0


@pytest.mark.parametrize(("cls", "answer", "_logger"), CADENCES)
def test_a_poller_with_no_pause_hook_reports_that_rather_than_a_clean_zero(cls, answer, _logger):
    """The tri-state, the house rule (`WireStats.key_ups`, `RadioStatus.wire`, `deafened_unknown`).

    ``pause_errors: 0`` beside a wired hook is a measurement. Beside no hook at all it is not — and
    "there is no hook here" is precisely the state ADR 0177 recorded as live on the `uvk5` backend,
    harmless only because that backend has no probe. A confident zero would hide it.
    """
    poller, probe = drive(cls, answer, paused=None, ticks=3)
    assert probe.calls == 3
    assert poller.stats()["pause_errors"] is None
    assert poller.stats()["skipped"] == 0


@pytest.mark.parametrize(("cls", "answer", "_logger"), CADENCES)
def test_a_wired_hook_that_has_never_been_asked_is_not_a_hook_that_answered(cls, answer, _logger):
    """Zero ticks against a wired hook: the counter is 0, and 0 is not `None`. The distinction is
    the whole point of the previous test, so it is pinned from the other side too."""
    poller, _ = drive(cls, answer, paused=lambda: False, ticks=0)
    assert poller.stats()["pause_errors"] == 0
    assert poller.stats()["pause_errors"] is not None


@pytest.mark.parametrize(("cls", "answer", "_logger"), CADENCES)
def test_the_counter_survives_concurrent_ticks(cls, answer, _logger):
    """An increment outside the lock loses counts under the thread the cadence actually runs on."""
    probe = Probe(answer)
    poller = (
        cls(probe, lambda: FULL, paused=boom)
        if cls is BroadcastFmPoller
        else cls(probe, paused=boom)
    )
    threads = [threading.Thread(target=lambda: [poller.poll_once() for _ in range(200)])
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert poller.stats()["pause_errors"] == 400


# --- the log line, which is what an operator can actually read today -------------------------


@pytest.mark.parametrize(("cls", "answer", "logger_name"), CADENCES)
def test_the_first_broken_tick_says_so_in_the_log_and_then_stops_shouting(
    cls, answer, logger_name, caplog
):
    """Counters in this repo reach no operator (ADR 0178 finding 2), so the log is the reader.

    Rate-limited to the first occurrence, following `link/bridge.py`'s deafened-log precedent: the
    RSSI cadence ticks at 2 Hz, so a line per tick is ~170 000 a day and an operator learns to scroll
    past it. The count is asserted **before** the log, so a poller that never ticked cannot pass.
    """
    with caplog.at_level(logging.WARNING, logger=logger_name):
        poller, probe = drive(cls, answer, paused=boom, ticks=50)

    assert probe.calls == 50
    assert poller.stats()["pause_errors"] == 50
    warnings = [r for r in caplog.records if r.levelname == "WARNING" and r.name == logger_name]
    assert len(warnings) == 1, f"expected exactly one line, got {len(warnings)}"
    assert "RuntimeError" in warnings[0].getMessage()


@pytest.mark.parametrize(("cls", "answer", "logger_name"), CADENCES)
def test_a_healthy_cadence_logs_nothing(cls, answer, logger_name, caplog):
    """The paired negative: without it, a WARNING emitted unconditionally would satisfy the test
    above."""
    with caplog.at_level(logging.WARNING, logger=logger_name):
        drive(cls, answer, paused=lambda: False, ticks=50)
    assert [r for r in caplog.records if r.name == logger_name] == []


# --- the merge the bridges depend on ---------------------------------------------------------


def test_the_no_cadence_sentinel_grew_the_same_key():
    """`cadence_stats` merges ``{**NO_CADENCE, **stats()}``, and both bridges read the result by
    key. A key present in one and absent from the other renders a bare-lambda `broadcast_fm` and a
    real poller differently for no reason — and `NO_CADENCE` is the "there is no mechanism here"
    sentinel, so its value must be the unwired one."""
    assert "pause_errors" in NO_CADENCE
    assert NO_CADENCE["pause_errors"] is None
    assert cadence_stats(lambda: FULL)["pause_errors"] is None
    assert set(cadence_stats(BroadcastFmPoller(Probe(True), lambda: FULL)).keys()) == set(NO_CADENCE)
