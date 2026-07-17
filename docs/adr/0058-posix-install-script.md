# 0058 — install.sh is POSIX sh, so `curl … | sh` actually runs

Status: Accepted

## Context

The README's headline command is `curl -LsSf …/scripts/install.sh | sh` (README:78, and the same in
getting-started.md:17). But `scripts/install.sh` was `#!/usr/bin/env bash` with `set -euo pipefail` at
the top. When the script's *bytes* are piped into `sh`, the shebang is irrelevant — the user's `sh`
interprets it. On Debian/Ubuntu `sh` is **dash**, and `-o pipefail` is not POSIX; `set` is a special
builtin, so the failure is fatal and immediate — the headline command dies before doing anything, with
a cryptic `set: Illegal option -o pipefail`.

Reproduction, stated honestly: I could **not** reproduce it on the dev box — its dash is
`0.5.12-12ubuntu3` (Ubuntu 24.04), and dash **added `pipefail` in 0.5.12** (2023); busybox `sh` accepts
it too. The bug bites dash **≤ 0.5.11** — Ubuntu 22.04 LTS and Debian 11, both still widely deployed
(22.04 is supported to 2027). So it is real for a large installed base but version-dependent — which is
why the regression test (below) cannot rely on executing under the local box's `sh`.

Constraints found in the tree (a full bashism audit of the 233-line script):

- The script is **~99% POSIX already**. The only non-POSIX constructs are: `set -o pipefail` (line 21);
  the `curl "$REPO_TARBALL" | tar -xz` pipeline (line 93), where `pipefail` is what made a failed
  `curl` abort before the `mv`; and `local` on lines 56/68. There are **no** arrays, `[[ … ]]`, `==`,
  `source`, here-strings, or process substitution.
- `local` is supported by every `/bin/sh` the installer targets — dash, busybox ash, and macOS's
  `/bin/sh` (bash) — and POSIX Issue 8 (2024) standardised it. It is not a portability problem in
  practice.
- The other pipe, `curl …/uv/install.sh | sh` (line 107), does not need `pipefail`: line 114's
  `have uv || die` already turns a masked download failure into a clear error.
- `--help` (lines 35-37) exits 0 before any clone/download/sync — a safe way to execute the shebang +
  `set` line without side effects.

## Decision

**Make `install.sh` strict-POSIX so `| sh` is correct, rather than changing the docs to `| bash`.**

- Shebang `#!/usr/bin/env bash` → `#!/bin/sh`; `set -euo pipefail` → `set -eu`.
- Replace the one `pipefail`-dependent pipeline (line 93) with an explicit temp-file download that
  checks `curl`'s exit status, then `tar -xzf` — **more** robust than the old masked pipe, not less:
  a failed/partial download now aborts with a clear message instead of feeding `tar`.
- Leave `local` (universally supported; see Context) and line 107 (guarded by `have uv || die`).
- README:78 and getting-started.md:17 keep `| sh` — now correct — so nothing there changes.

**Why POSIX over `| bash`.** Fingers type `| sh` from muscle memory and from every other project's
install line; the README cannot police that, and the failure mode is cryptic and instant. uv's own
installer — which this script pipes to — is POSIX `sh` for exactly this reason. The cost here is a
handful of lines (the audit found no arrays or `[[ ]]` to unwind), so the durable fix is cheap.

### Alternative considered — change the docs to `curl … | bash`

The one-line fix (README/getting-started `| sh` → `| bash`), keeping the bash shebang. Rejected: it
leaves a loaded gun — the next person to copy `| sh` from memory hits the same wall — and it treats a
script that needs *no* bash features (only `pipefail` and `local`, both now handled) as bash-only.

### Alternative considered — drop `pipefail` but keep the `curl | tar` pipe

Rejected: without `pipefail`, a failed `curl` whose `tar` still returns 0 would be masked and the
script would `mv` a nonexistent dir. The temp-file form removes the ambiguity outright.

## Consequences

- **The headline `curl … | sh` runs on dash** (Ubuntu/Debian), macOS `/bin/sh`, and busybox — the
  install path 0053/0057 promised now actually starts everywhere.
- **A docs↔script contract test** (`tests/test_docs_install_command.py`) locks it: it parses the pipe
  target out of README, asserts it agrees with install.sh's shebang, executes `sh scripts/install.sh
  --help` to prove it starts, and statically forbids `pipefail` (the local dash 0.5.12 tolerates
  `pipefail`, so execution alone can't guard the reintroduction). Change one side without the other and
  the suite fails — this regressed unnoticed through four cycles because nothing tested it.
- **The download path is more robust** (explicit `curl` status check) at the cost of a temp file.
- No behaviour change for bash users; `local`'s scoping is unchanged (dash honours it).
