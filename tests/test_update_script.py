"""`update-radio-server.sh` — the ref handling, against a throwaway origin (ADR 0169).

**This script failed on every run against the box it was written for, and nothing caught it.** It
ran `git pull`, while `docs/server-notes.md` deploys the station with `git switch --detach <ref>` so
a bench measurement is attributable to an exact commit. On a detached HEAD `git pull` prints *"You
are not currently on a branch"* and exits 1. Two documented paths, contradicting each other, for
however many cycles nobody ran the one-command update.

Nothing here can run `uv sync`, `npm run build` or `systemctl restart`, so those are stubbed and the
test says so rather than implying more than it measured. What it locks is the part that was wrong:
which ref the box ends up on, and which moves are refused.

The stubs matter in one specific way — an earlier version of this exercised the missing-`uv` branch
with `UV=` alone, which left `command -v uv` free to find the developer's own `uv` and prove nothing.
`PATH` and `HOME` are both scrubbed for that case.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "update-radio-server.sh"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def box(tmp_path: Path):
    """A fake deployment: a bare origin, a checkout with the real script, and stubbed tooling.

    `origin/master` sits two commits ahead of the checkout's starting point, and `origin/bench`
    carries a commit master does not have — the two shapes the script has to tell apart.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("npm", "uvstub"):
        stub = bin_dir / name
        stub.write_text(f'#!/bin/sh\necho "[stub {name} $*]"\n')
        stub.chmod(0o755)

    origin, work, other = tmp_path / "origin.git", tmp_path / "work", tmp_path / "other"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git(work, "config", "user.email", "t@example.invalid")
    _git(work, "config", "user.name", "test")

    (work / "web").mkdir()
    (work / "web" / "package.json").write_text("{}\n")
    restart = work / "restart-radio-server.sh"
    restart.write_text('#!/bin/sh\necho "[stub restart]"\n')
    restart.chmod(0o755)
    shutil.copy(_SCRIPT, work / _SCRIPT.name)
    (work / _SCRIPT.name).chmod(0o755)
    (work / "f").write_text("one\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "one")
    _git(work, "branch", "-M", "master")
    _git(work, "push", "-q", "-u", "origin", "master")
    base = _git(work, "rev-parse", "--short", "HEAD")

    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    _git(other, "config", "user.email", "t@example.invalid")
    _git(other, "config", "user.name", "test")
    for n in ("two", "three"):
        (other / n).write_text(f"{n}\n")
        _git(other, "add", "-A")
        _git(other, "commit", "-qm", n)
    _git(other, "push", "-q", "origin", "master")
    _git(other, "checkout", "-q", "-b", "bench", base)
    (other / "fb").write_text("bench\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-qm", "bench")
    _git(other, "push", "-q", "-u", "origin", "bench")
    _git(work, "fetch", "-q", "origin")

    def run(*args: str, scrub_uv: bool = False) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        if scrub_uv:
            env.pop("UV", None)
            env["HOME"] = str(tmp_path / "nohome")
            env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
        else:
            env["UV"] = str(bin_dir / "uvstub")
            env["PATH"] = f"{bin_dir}:{env['PATH']}"
        return subprocess.run(
            [str(work / _SCRIPT.name), *args], cwd=work, capture_output=True, text=True, env=env
        )

    box_ns = type("Box", (), {})()
    box_ns.work, box_ns.base, box_ns.run = work, base, run
    box_ns.head = lambda: _git(work, "rev-parse", "--short", "HEAD")
    box_ns.branch = lambda: _git(work, "status", "-sb").splitlines()[0]
    return box_ns


def test_a_detached_head_fast_forwards(box):
    """THE REPORTED FAILURE. `git pull` cannot run here at all; this is the state the box lives in."""
    _git(box.work, "switch", "-q", "--detach", box.base)
    r = box.run()
    assert r.returncode == 0, r.stderr
    assert box.head() == _git(box.work, "rev-parse", "--short", "origin/master")
    assert "(no branch)" in box.branch()  # still detached: the deployment style is preserved


def test_a_branch_fast_forwards_in_place_and_stays_a_branch(box):
    """Switching a branch checkout to detached behind the operator's back would be its own surprise."""
    _git(box.work, "switch", "-q", "master")
    _git(box.work, "reset", "-q", "--hard", box.base)
    r = box.run()
    assert r.returncode == 0, r.stderr
    assert "## master" in box.branch()
    assert box.head() == _git(box.work, "rev-parse", "--short", "origin/master")


def test_a_non_fast_forward_is_refused_without_an_explicit_target(box):
    """A bench deployment carries commits master does not have.

    ADR 0164 ran the station on `adr-0164-the-on-path` for a full cycle. A routine "just update the
    server" that silently yanked it back to master would destroy the thing being measured.
    """
    _git(box.work, "switch", "-q", "--detach", "origin/bench")
    before = box.head()
    r = box.run()
    assert r.returncode == 1
    assert "refusing to move this checkout" in r.stderr
    assert box.head() == before  # and it really did not move


def test_naming_the_target_is_how_the_operator_says_they_mean_it(box):
    _git(box.work, "switch", "-q", "--detach", "origin/master")
    r = box.run("origin/bench")
    assert r.returncode == 0, r.stderr
    assert box.head() == _git(box.work, "rev-parse", "--short", "origin/bench")


def test_a_bench_branch_stops_needing_the_argument_once_it_merges(box):
    """The "put the box back on the mainline once the PR merges" step, no longer a manual ritual.

    Merging makes the deployed commit an ancestor of master, so the plain no-argument update starts
    working again on its own.
    """
    _git(box.work, "switch", "-q", "--detach", "origin/bench")
    assert box.run().returncode == 1
    merger = box.work.parent / "other"
    _git(merger, "checkout", "-q", "master")
    _git(merger, "merge", "-q", "--no-ff", "-m", "merge bench", "origin/bench")
    _git(merger, "push", "-q", "origin", "master")
    r = box.run()
    assert r.returncode == 0, r.stderr
    assert box.head() == _git(box.work, "rev-parse", "--short", "origin/master")


def test_an_unknown_ref_is_named_rather_than_guessed(box):
    r = box.run("origin/no-such-branch")
    assert r.returncode == 1
    assert "is not a commit this checkout knows about" in r.stderr


def test_already_at_the_target_still_syncs_and_rebuilds(box):
    """An update run reconciles dependencies and the bundle even when no commit moved."""
    _git(box.work, "switch", "-q", "--detach", "origin/master")
    r = box.run()
    assert r.returncode == 0, r.stderr
    assert "already at" in r.stdout
    assert "[stub restart]" in r.stdout


def test_a_missing_uv_is_explained_rather_than_a_bare_command_not_found(box):
    """`uv` lives in `~/.local/bin`, which only a login shell puts on PATH.

    So a bare `uv` works when a human runs this and fails under cron, a wrapper, or
    `ssh host ./update-radio-server.sh` — which is how this was found.
    """
    _git(box.work, "switch", "-q", "--detach", box.base)
    r = box.run(scrub_uv=True)
    assert r.returncode == 1
    assert "cannot find 'uv'" in r.stderr


def test_the_extras_are_still_named_on_the_sync(box):
    """`uv sync` is EXACT: run bare it uninstalls every extra. The Mumble link lost pymumble this way.

    A source check, because the stub cannot prove what the real `uv` would have removed.
    """
    text = _SCRIPT.read_text()
    assert "--extra hardware" in text
    assert "--extra tts" in text
    assert "--extra mumble" in text
