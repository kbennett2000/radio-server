"""The acceptance banner tells the truth about a stage that never ran (ADR 0165).

ADR 0161 finding 8, opened four cycles ago and restated in 0162, 0163 and 0164: a run where every
attempted stage passes but `split-minus` SKIPs prints `RESULT: FAIL`. A safety net whose own summary
line is wrong is a net people learn to read past — which is exactly how a real failure gets waved
through, and this arc has leaned on `acceptance.py` to catch what pytest structurally cannot.

The fix is the remedy 0161 itself proposed: give a skip its own exit code. `PASS`/0, `FAIL`/1,
`INCOMPLETE`/3 — 2 was already taken by "could not start" (no `RADIO_API_TOKEN`, unknown stage
name). Non-zero for both failure and incompleteness, so every existing `rc != 0` caller keeps
today's safety; the audit that permits the change is in the ADR (no CI, no cron, no timer, and every
`from acceptance import ...` in `scripts/bench/` imports helpers rather than calling `main`).

`overall_verdict` is a pure function of the stage list so the banner can be proven without a radio,
a station or a token — the coverage the runner has never had.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ACCEPTANCE = Path(__file__).resolve().parent.parent / "scripts" / "bench" / "acceptance.py"


@pytest.fixture(scope="module")
def acceptance():
    spec = importlib.util.spec_from_file_location("bench_acceptance", _ACCEPTANCE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_acceptance"] = module
    spec.loader.exec_module(module)
    return module


def _stage(acceptance, name, *, ok=True, skipped=""):
    return acceptance.Stage(name=name, ok=ok, skipped=skipped)


def test_every_stage_passing_is_a_pass(acceptance):
    stages = [_stage(acceptance, "rx"), _stage(acceptance, "tx")]
    assert acceptance.overall_verdict(stages) == ("PASS", 0)


def test_a_failed_stage_is_a_failure(acceptance):
    stages = [_stage(acceptance, "rx"), _stage(acceptance, "tx", ok=False)]
    assert acceptance.overall_verdict(stages) == ("FAIL", 1)


def test_an_unattempted_stage_is_incomplete_not_a_failure(acceptance):
    """The live case, four cycles running: 9 of 9 attempted stages pass and `split-minus` cannot be
    attempted because the deployed `radio.toml` has no `Bench Split Minus` preset."""
    stages = [_stage(acceptance, "rx"), _stage(acceptance, "split-minus", skipped="no preset")]
    assert acceptance.overall_verdict(stages) == ("INCOMPLETE", 3)


def test_incomplete_is_still_non_zero(acceptance):
    """The safety property the change must not break: a skipped stage is not a pass, and anything
    testing `rc != 0` keeps behaving exactly as it does today."""
    _, code = acceptance.overall_verdict([_stage(acceptance, "s", skipped="why")])
    assert code != 0


def test_a_real_failure_outranks_an_unattempted_stage(acceptance):
    # A run that both failed something and skipped something is a failure. Reporting it as merely
    # incomplete would hide the failure behind the softer word.
    stages = [_stage(acceptance, "tx", ok=False), _stage(acceptance, "split-minus", skipped="no preset")]
    assert acceptance.overall_verdict(stages) == ("FAIL", 1)


def test_a_stage_that_failed_a_check_then_skipped_displays_as_failed(acceptance):
    """The per-stage line and the overall tally must not use different rules.

    Today the display reads `SKIP if skipped else (PASS if ok else FAIL)`, so a stage that failed a
    check and *then* skipped would print SKIP while still counting as a failure. No current stage
    can reach that state — every `skip()` call site returns immediately — which is precisely why it
    would go unnoticed if one ever did.
    """
    stage = _stage(acceptance, "rx", ok=False, skipped="lost the witness half way")
    assert acceptance.stage_verdict(stage) == "FAIL"


def test_the_ordinary_stage_verdicts_are_unchanged(acceptance):
    assert acceptance.stage_verdict(_stage(acceptance, "rx")) == "PASS"
    assert acceptance.stage_verdict(_stage(acceptance, "rx", ok=False)) == "FAIL"
    assert acceptance.stage_verdict(_stage(acceptance, "rx", skipped="no witness")) == "SKIP"


def test_could_not_start_keeps_exit_code_two(acceptance, capsys, monkeypatch):
    """2 already means "the run never began" — an unset token or an unknown stage name. The third
    state has to be a different number or the two become indistinguishable."""
    monkeypatch.setattr(acceptance, "TOKEN", "")
    assert acceptance.main([]) == 2
    monkeypatch.setattr(acceptance, "TOKEN", "set")
    assert acceptance.main(["--only", "not-a-stage"]) == 2
