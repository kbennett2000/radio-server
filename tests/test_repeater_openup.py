"""The repeater open-up verdict (ADR 0139).

This script is the first thing in the bench that keys a **real repeater's input**, and its output is
a claim about a machine nobody can inspect. Two ways that claim goes wrong, and both have already
happened to this bench in other forms:

1. **"No signal" reported when the truth is "no measurement."** A dead poller, a busy channel, an
   over that never keyed — each produces 0.00 s of tail, which is byte-identical to a repeater that
   ignored us. ADR 0136 measured the wrong window, ADR 0137 took `max()` of two disagreeing
   controls, ADR 0138 read a wedged radio as a failed AIOC. Same error class three times.
2. **Our own carrier counted as the repeater's.** The witness sits inches from the transmitter and
   can be desensed by it 5 MHz away, so a carrier that lives and dies with our over is the expected
   artefact, not evidence. If the settle window slipped, every run would report OPENED — including
   runs against a repeater that is switched off.

Neither failure is loud. Both fabricate exactly the answer the run was hoping for, which is why the
verdict is pure functions over a sample timeline and pinned here rather than judged live.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bench" / "repeater_openup.py"


@pytest.fixture(scope="module")
def rou():
    spec = importlib.util.spec_from_file_location("bench_repeater_openup", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_repeater_openup"] = module
    spec.loader.exec_module(module)
    return module


def timeline(rou, *segments: tuple[float, float, bool], step: float = 0.05):
    """Samples over ``(from, to, busy)`` segments, polled every ``step`` seconds.

    A trailing sample is appended past the end of the last segment, because a sample only ever
    describes the span up to the *next* one — without it the last segment measures as zero-width.
    """
    out = []
    for start, end, busy in segments:
        t = start
        while t < end - 1e-9:
            out.append(rou.Sample(t, busy))
            t += step
    if out:
        out.append(rou.Sample(segments[-1][1], segments[-1][2]))
    return out


def over(rou, index=1, *, keyed=True, clear=True, covered=None, tail=0.0):
    return rou.Over(
        index=index,
        keyed=keyed,
        clear_before=clear,
        covered=rou.TAIL_WINDOW if covered is None else covered,
        tail=tail,
    )


# --- integrating the busy timeline -------------------------------------------------------------


def test_busy_seconds_measures_only_the_window_asked_for(rou):
    samples = timeline(rou, (0.0, 10.0, True))
    assert rou.busy_seconds(samples, 2.0, 5.0) == pytest.approx(3.0, abs=0.06)
    assert rou.busy_seconds(samples, 0.0, 10.0) == pytest.approx(10.0, abs=0.06)


def test_busy_seconds_ignores_quiet_spans(rou):
    samples = timeline(rou, (0.0, 2.0, True), (2.0, 6.0, False), (6.0, 7.0, True))
    assert rou.busy_seconds(samples, 0.0, 7.0) == pytest.approx(3.0, abs=0.06)


def test_coverage_and_busy_are_different_questions(rou):
    """A quiet window and an unwatched window both report 0.0 s busy; only coverage tells them apart."""
    quiet = timeline(rou, (0.0, 4.0, False))
    unwatched: list = []
    assert rou.busy_seconds(quiet, 0.0, 4.0) == 0.0
    assert rou.busy_seconds(unwatched, 0.0, 4.0) == 0.0
    assert rou.covered_seconds(quiet, 0.0, 4.0) == pytest.approx(4.0, abs=0.06)
    assert rou.covered_seconds(unwatched, 0.0, 4.0) == 0.0


def test_a_lone_sample_measures_nothing_rather_than_everything(rou):
    """One sample says nothing about how long its state lasted. Bias down, never up."""
    assert rou.busy_seconds([rou.Sample(0.0, True)], 0.0, 4.0) == 0.0
    assert rou.covered_seconds([rou.Sample(0.0, True)], 0.0, 4.0) == 0.0


# --- the property the whole design rests on ----------------------------------------------------


def test_a_carrier_that_dies_with_our_own_over_is_not_a_tail(rou):
    """Desense: the witness hears our transmitter and drops the instant we unkey.

    This is the artefact the post-unkey window exists to reject. If it ever counted, a repeater that
    was switched off would still read OPENED.
    """
    t0 = 10.0
    samples = timeline(rou, (5.0, t0, True), (t0, t0 + 6.0, False))
    tail = rou.busy_seconds(samples, t0 + rou.TAIL_SETTLE, t0 + rou.TAIL_WINDOW)
    assert tail < rou.MIN_TAIL_SECONDS
    assert rou.run_verdict([over(rou, i, tail=tail) for i in (1, 2, 3)])[0] == rou.NO_RESPONSE


def test_a_carrier_that_outlives_our_over_is_a_tail(rou):
    """The repeater holding its transmitter up after ours stopped — the thing being measured."""
    t0 = 10.0
    samples = timeline(rou, (5.0, t0 + 2.5, True), (t0 + 2.5, t0 + 8.0, False))
    tail = rou.busy_seconds(samples, t0 + rou.TAIL_SETTLE, t0 + rou.TAIL_WINDOW)
    assert tail == pytest.approx(2.5 - rou.TAIL_SETTLE, abs=0.06)
    assert tail >= rou.MIN_TAIL_SECONDS
    assert rou.run_verdict([over(rou, i, tail=tail) for i in (1, 2, 3)])[0] == rou.OPENED


def test_the_settle_band_is_excluded_from_the_tail(rou):
    """A carrier that only just outlives our PTT is the line release, not a repeater."""
    t0 = 10.0
    samples = timeline(rou, (5.0, t0 + rou.TAIL_SETTLE - 0.05, True), (t0 + 0.2, t0 + 6.0, False))
    assert rou.busy_seconds(samples, t0 + rou.TAIL_SETTLE, t0 + rou.TAIL_WINDOW) == 0.0


# --- turning overs into a verdict --------------------------------------------------------------


def test_two_of_three_tails_opens_it(rou):
    overs = [over(rou, 1, tail=1.2), over(rou, 2, tail=0.0), over(rou, 3, tail=0.9)]
    verdict, reason = rou.run_verdict(overs)
    assert verdict == rou.OPENED
    assert "2 of 3" in reason


def test_one_tail_of_three_is_not_a_result_either_way(rou):
    """Another station keying the machine while we listened looks exactly like this."""
    overs = [over(rou, 1, tail=1.4), over(rou, 2, tail=0.0), over(rou, 3, tail=0.0)]
    verdict, reason = rou.run_verdict(overs)
    assert verdict == rou.INCONCLUSIVE
    assert "another station" in reason


def test_a_busy_channel_voids_the_over_instead_of_failing_the_repeater(rou):
    """Traffic before the over means its tail window cannot be attributed. Not a NO RESPONSE."""
    overs = [over(rou, i, clear=False, tail=0.0) for i in (1, 2, 3)]
    verdict, reason = rou.run_verdict(overs)
    assert verdict == rou.INCONCLUSIVE
    assert "traffic on the output" in reason


def test_losing_the_witness_voids_the_over_instead_of_failing_the_repeater(rou):
    """0.00 s of tail from a poller that was not polling is not evidence about the repeater."""
    overs = [over(rou, i, covered=0.1, tail=0.0) for i in (1, 2, 3)]
    verdict, reason = rou.run_verdict(overs)
    assert verdict == rou.INCONCLUSIVE
    assert "coverage" in reason


def test_never_keying_is_inconclusive_not_a_dead_repeater(rou):
    overs = [over(rou, i, keyed=False) for i in (1, 2, 3)]
    verdict, reason = rou.run_verdict(overs)
    assert verdict == rou.INCONCLUSIVE
    assert "never keyed" in reason


def test_no_response_requires_enough_clean_overs_to_say_it(rou):
    """One clean silent over is not enough to declare a machine dead."""
    overs = [over(rou, 1, tail=0.0), over(rou, 2, keyed=False), over(rou, 3, clear=False)]
    assert rou.run_verdict(overs)[0] == rou.INCONCLUSIVE
    overs = [over(rou, 1, tail=0.0), over(rou, 2, tail=0.0), over(rou, 3, keyed=False)]
    assert rou.run_verdict(overs)[0] == rou.NO_RESPONSE


def test_no_response_is_reachable_when_everything_worked_and_nothing_answered(rou):
    verdict, reason = rou.run_verdict([over(rou, i, tail=0.0) for i in (1, 2, 3)])
    assert verdict == rou.NO_RESPONSE
    assert "3 clean overs" in reason
