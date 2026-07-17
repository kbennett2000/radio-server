"""The docs<->install.sh contract: the copy-pasteable install command must actually run (ADR 0058).

The headline `curl … | sh` regressed unnoticed through four cycles because nothing tied the shell the
README pipes to (`sh`) to the shell install.sh needs. On Debian/Ubuntu `sh` is dash, and the old
`#!/usr/bin/env bash` + `set -euo pipefail` died on dash's missing `pipefail`. These tests lock the
contract: change the README's pipe target or install.sh's shebang without the other, or reintroduce
`pipefail`, and the suite fails.

`sh -n`/`bash -n` would not have caught it — `set -o pipefail` is a runtime error, not a syntax one —
so the core test *executes* install.sh far enough to prove it starts.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent  # tests/ is one level below the repo root
_INSTALL_SH = _ROOT / "scripts" / "install.sh"
_README = _ROOT / "README.md"
_GETTING_STARTED = _ROOT / "docs" / "getting-started.md"


def _curl_install_pipe_target(text: str) -> str:
    """The shell after the final `|` on the `curl … scripts/install.sh | <shell>` line in `text`."""
    for line in text.splitlines():
        if "curl" in line and "install.sh" in line and "|" in line:
            after = line.rsplit("|", 1)[1].strip()
            assert after, f"no shell after the pipe in: {line!r}"
            return after.split()[0]
    raise AssertionError("no `curl … scripts/install.sh | <shell>` line found")


def _shebang_interpreter(script: Path) -> str:
    """The interpreter name from a script's shebang (`#!/bin/sh` -> 'sh', `env bash` -> 'bash')."""
    first = script.read_text().splitlines()[0]
    assert first.startswith("#!"), f"no shebang: {first!r}"
    parts = first[2:].split()
    name = parts[0].rsplit("/", 1)[-1]
    if name == "env" and len(parts) > 1:
        name = parts[1]
    return name


def test_readme_pipe_target_agrees_with_install_sh_shebang():
    # The acceptance line: fail if README's pipe target and install.sh's shebang disagree.
    target = _curl_install_pipe_target(_README.read_text())
    shebang = _shebang_interpreter(_INSTALL_SH)
    assert target == shebang, (
        f"README pipes install.sh to `{target}` but its shebang is `#!…{shebang}` — "
        f"they must match, or `curl … | {target}` runs the script under the wrong shell"
    )


def test_getting_started_pipe_target_matches_readme():
    # The two docs carry the identical one-liner; keep them from drifting apart.
    assert _curl_install_pipe_target(_GETTING_STARTED.read_text()) == _curl_install_pipe_target(
        _README.read_text()
    )


def test_install_sh_starts_under_the_readme_pipe_target():
    # Execute far enough to prove it *starts* (past the shebang + `set` line) under the exact shell the
    # README pipes to. `--help` exits 0 before any clone/download/sync, so this is safe and side-effect
    # free. This is what `sh -n` can't do: catch a runtime `set` failure like dash rejecting pipefail.
    target = _curl_install_pipe_target(_README.read_text())
    exe = shutil.which(target)
    assert exe, f"README pipes install.sh to `{target}`, but that shell isn't available to run it"
    proc = subprocess.run(
        [exe, str(_INSTALL_SH), "--help"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"`{target} scripts/install.sh --help` failed (rc={proc.returncode}) — it didn't get past the "
        f"shebang/`set` line under `{target}`.\nstderr:\n{proc.stderr}"
    )
    assert proc.stdout.strip(), "install.sh --help printed nothing — it may not have started"


def test_install_sh_is_posix_no_pipefail():
    # Deterministic guard for the exact runtime bomb, independent of the test box's dash version (dash
    # >=0.5.12 tolerates pipefail, so the execution test above can't catch a reintroduction on a modern
    # box). install.sh must stay POSIX: `#!/bin/sh`, and no `pipefail` in any executable line.
    lines = _INSTALL_SH.read_text().splitlines()
    assert lines[0] == "#!/bin/sh", f"install.sh must be POSIX sh, got shebang {lines[0]!r}"
    for i, line in enumerate(lines, start=1):
        if line.lstrip().startswith("#"):
            continue  # comments may mention pipefail (they explain why it's gone)
        assert "pipefail" not in line, f"install.sh:{i} reintroduces non-POSIX pipefail: {line!r}"
