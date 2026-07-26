"""Run an RF check N times and report the fraction, because one reading is not a measurement.

This exists because of a specific, repeated, expensive mistake.

ADRs 0136-0139 each built a careful instrument and then believed a single sample from it. On
2026-07-26 that finally cost something: the station was found to key only about half the time --
0/4 from cold, 4/4 when warm -- which means every RF number in ADRs 0138 and 0139 was a coin flip
nobody knew they were tossing. ``1926.8 / 1970.5``. ``1.21 s / RMS 3816.5``. All real, all single-
or two-shot, all taken while a 50% failure mode was live and invisible.

The error class is now two-sided and both sides look identical on the page:

* an instrument that cannot tell "no signal" from "no measurement" (ADR 0136's silent no-op), and
* a *true* reading that happened to fall in a good burst.

Repetition is the only defence against the second one, and it is cheap. So: every RF claim from
here is **N-of-M with N stated**, and anything short of unanimous is a finding, not a pass.

Usage -- the probe returns (ok, value) for one attempt::

    from trials import run_trials, require_unanimous

    def probe() -> tuple[bool, float]:
        carrier = key_and_measure(watch, 445_800_000)
        return carrier >= 0.30, carrier

    ts = run_trials("key-up on 445.800", probe, n=10)
    print(ts.report())
    return require_unanimous(ts)

The pure parts (:class:`TrialSet` and its arithmetic) carry the verdict logic and are unit-tested
in ``tests/test_trials.py`` against synthetic results, so the reporting cannot drift from the rule
without a test failing.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass

#: Default repetitions. Ten is enough to make a 50% fault essentially impossible to miss
#: (a coin lands the same way ten times about once in a thousand runs) while still costing
#: under two minutes of bench time at a typical 6 s spacing.
DEFAULT_N = 10

#: Default spacing between attempts. Long enough that consecutive keyings are not one long
#: transmission, short enough that the radio stays "warm" -- which matters, because the cold-key
#: fault means spacing is itself a variable. State it rather than leaving it implicit.
DEFAULT_GAP_S = 6.0


@dataclass(frozen=True)
class Trial:
    """One attempt: whether it passed, the number behind it, and anything worth printing."""

    index: int
    ok: bool
    value: float
    detail: str = ""


@dataclass(frozen=True)
class TrialSet:
    """A named run of attempts, and the arithmetic that turns them into a verdict."""

    name: str
    trials: tuple[Trial, ...]
    gap_seconds: float = DEFAULT_GAP_S

    @property
    def total(self) -> int:
        return len(self.trials)

    @property
    def passed(self) -> int:
        return sum(1 for t in self.trials if t.ok)

    @property
    def unanimous(self) -> bool:
        """All attempts passed, and there was at least one. Empty is never a pass.

        An empty run means the probe never executed -- which is exactly the "no measurement"
        that must not be allowed to read as "no problem".
        """
        return self.total > 0 and self.passed == self.total

    @property
    def values(self) -> tuple[float, ...]:
        return tuple(t.value for t in self.trials if t.ok)

    def spread(self) -> tuple[float, float, float] | None:
        """(min, mean, max) over the attempts that passed, or None if none did.

        Reported alongside the fraction because a unanimous pass with a wildly varying value is
        a different animal from a unanimous pass with a tight one -- the first says "it works but
        something is moving", which is worth seeing before it becomes the next week of debugging.
        """
        vals = self.values
        if not vals:
            return None
        return min(vals), statistics.fmean(vals), max(vals)

    def report(self) -> str:
        lines = [f"  {self.name}: {self.passed}/{self.total} "
                 f"(spacing {self.gap_seconds:.1f}s)"]
        sp = self.spread()
        if sp is not None:
            lo, mean, hi = sp
            lines.append(f"    when it passed: min {lo:.2f} mean {mean:.2f} max {hi:.2f}")
        if not self.unanimous:
            missed = [t.index for t in self.trials if not t.ok]
            lines.append(f"    FAILED on attempt(s): {missed}")
        return "\n".join(lines)


def run_trials(
    name: str,
    probe: Callable[[], tuple[bool, float]],
    *,
    n: int = DEFAULT_N,
    gap: float = DEFAULT_GAP_S,
    verbose: bool = True,
) -> TrialSet:
    """Run ``probe`` ``n`` times with ``gap`` seconds between attempts.

    Deliberately runs the full count even after a failure. Stopping early would save bench time
    and lose the thing worth knowing -- *how often* it fails, and whether failures cluster (which
    is what exposed the cold-key fault: silences bunched at the start of every run, never in the
    middle of a warm one).
    """
    results: list[Trial] = []
    for i in range(1, n + 1):
        ok, value = probe()
        results.append(Trial(index=i, ok=ok, value=value))
        if verbose:
            print(f"    attempt {i:2d}/{n}: {value:7.2f}  {'OK' if ok else 'FAIL'}", flush=True)
        if i < n:
            time.sleep(gap)
    return TrialSet(name=name, trials=tuple(results), gap_seconds=gap)


def require_unanimous(*sets: TrialSet) -> int:
    """Exit code for a bench script: 0 only if every set passed every attempt.

    Returns 1 rather than 2 for a real, measured failure -- 2 stays reserved for INCONCLUSIVE
    (the instrument could not answer), so a script's exit code keeps saying which of the two
    happened. Collapsing them is how "no measurement" gets read as "it failed", and vice versa.
    """
    if not sets:
        return 2
    return 0 if all(s.unanimous for s in sets) else 1
