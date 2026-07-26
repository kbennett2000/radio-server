"""The N-of-M verdict (ADR 0140).

This harness exists because ADRs 0136-0139 each believed a single sample, and on 2026-07-26 the
station turned out to key only about half the time — 0/4 cold, 4/4 warm. So every RF number in the
two most recent ADRs was a coin toss nobody knew about.

That makes the arithmetic here load-bearing in a specific way: it is the thing standing between
"I saw it work once" and "it works". The two ways it could quietly stop doing that job:

1. **An empty run reading as a pass.** No attempts means the probe never ran — the "no measurement"
   that must never be reported as "no problem". It is the same fault ADR 0136 shipped as a silent
   no-op, and it would turn a broken harness into a green light on every script that uses it.
2. **A partial pass reading as a pass.** 9/10 is the exact shape of the cold-key fault; if
   `unanimous` ever rounded that up, the harness would certify the very bug it was built to catch.

Both are pinned below against synthetic results, so neither can drift without a test failing.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bench" / "trials.py"


@pytest.fixture(scope="module")
def tr():
    spec = importlib.util.spec_from_file_location("bench_trials", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_trials"] = module
    spec.loader.exec_module(module)
    return module


def make(tr, *oks: bool, name: str = "probe"):
    trials = tuple(
        tr.Trial(index=i, ok=ok, value=1.2 if ok else 0.0) for i, ok in enumerate(oks, start=1)
    )
    return tr.TrialSet(name=name, trials=trials)


def test_all_pass_is_the_only_pass(tr):
    assert make(tr, True, True, True).unanimous is True


def test_one_failure_in_ten_is_not_a_pass(tr):
    """9/10 is the shape of the cold-key fault. If this ever rounds up, the harness certifies
    the bug it was built to catch."""
    ts = make(tr, False, *([True] * 9))
    assert ts.passed == 9
    assert ts.total == 10
    assert ts.unanimous is False


def test_an_empty_run_is_never_a_pass(tr):
    """No attempts means the probe never executed. "No measurement" must not read as "no
    problem" — that is ADR 0136's silent no-op, reincarnated inside the thing meant to prevent it."""
    ts = tr.TrialSet(name="nothing ran", trials=())
    assert ts.total == 0
    assert ts.unanimous is False
    assert tr.require_unanimous(ts) != 0


def test_require_unanimous_with_no_sets_is_inconclusive_not_pass(tr):
    """Called with nothing to judge, the answer is 2 (INCONCLUSIVE), never 0."""
    assert tr.require_unanimous() == 2


def test_failure_and_inconclusive_keep_different_exit_codes(tr):
    """A measured failure is 1; an unanswerable one is 2. Collapsing them is how "we could not
    tell" gets recorded as "it does not work"."""
    assert tr.require_unanimous(make(tr, True, False)) == 1
    assert tr.require_unanimous() == 2
    assert tr.require_unanimous(make(tr, True, True)) == 0


def test_every_set_must_pass_not_just_one(tr):
    good = make(tr, True, True, name="good")
    bad = make(tr, True, False, name="bad")
    assert tr.require_unanimous(good, bad) == 1
    assert tr.require_unanimous(good, good) == 0


def test_spread_describes_only_the_attempts_that_passed(tr):
    """A failed attempt contributes 0.0, and averaging that in would drag the reported figure
    toward zero and make a healthy run look weak."""
    trials = (
        tr.Trial(index=1, ok=True, value=1.20),
        tr.Trial(index=2, ok=False, value=0.0),
        tr.Trial(index=3, ok=True, value=1.30),
    )
    ts = tr.TrialSet(name="mixed", trials=trials)
    lo, mean, hi = ts.spread()
    assert (lo, hi) == (1.20, 1.30)
    assert mean == pytest.approx(1.25)


def test_spread_is_none_when_nothing_passed(tr):
    assert make(tr, False, False).spread() is None


def test_report_names_the_failed_attempts(tr):
    """Which attempts failed is the signal that found the cold-key fault: failures clustered at
    the start of every run and never in the middle of a warm one. A bare fraction hides that."""
    text = make(tr, False, True, True, False).report()
    assert "2/4" in text
    assert "[1, 4]" in text


def test_report_of_a_clean_run_does_not_cry_failure(tr):
    text = make(tr, True, True).report()
    assert "2/2" in text
    assert "FAILED" not in text


def test_run_trials_runs_every_attempt_even_after_a_failure(tr):
    """Stopping early would save bench time and lose the frequency and clustering of failures,
    which is the whole diagnostic value."""
    calls: list[int] = []

    def probe():
        calls.append(1)
        return len(calls) != 2, float(len(calls))

    ts = tr.run_trials("counting", probe, n=5, gap=0.0, verbose=False)
    assert len(calls) == 5
    assert ts.total == 5
    assert ts.passed == 4
    assert [t.index for t in ts.trials if not t.ok] == [2]
