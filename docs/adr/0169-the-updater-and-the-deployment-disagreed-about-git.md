# ADR 0169 — The updater and the deployment disagreed about git

**Status:** Accepted · 2026-08-01 · fixes `update-radio-server.sh`, which has never worked on the box
it was written for; reconciles it with the deploy procedure in
[server-notes.md](../server-notes.md)

## Context

Updating the station failed:

```
==> Running the repo's update-radio-server.sh (pull → sync extras → build → restart)
You are not currently on a branch.
Please specify which branch you want to merge with.
 ✗ update-radio-server.sh failed.
```

`update-radio-server.sh` began with `git pull`. **The deployment is on a detached HEAD, on purpose,
because this project's own documentation puts it there.** `docs/server-notes.md` deploys with
`git switch --detach origin/master` — and that is the right call: a bench measurement has to be
attributable to an exact commit, every ADR in the 0156→0168 arc records *"the station runs `<sha>`"*,
and a branch that fast-forwards underneath you between two measurements makes that unanswerable.

So the repo contained two documented paths that could not both be right, and the one that runs
lost. `git pull` on a detached HEAD does not fall back to anything; it prints the message above and
exits 1. **The script has never worked on the box it was written for**, and nothing failed when it
didn't, because nothing runs it in CI and a human hitting it just pulls by hand.

### What the box actually looked like

`HEAD` detached at `a6a4cd4` (the #222 merge), **0 ahead / 9 behind** `origin/master`, tree clean.
The local `master` branch was at `ba134b0` — **168 commits behind** — so the reflexive
`git checkout master && git pull` would have "worked" and deployed a build from many cycles ago. The
reflog shows how it got there: a run of `git checkout origin/<branch>` bench deploys, then a
`git reset --hard origin/HEAD` against a stale symbolic ref. That last one is the same failure
`server-notes.md` already records under ADR 0162, from a different direction.

## Decision

### 1. The update is a fast-forward to a target ref, not a pull

`git pull` means "merge my upstream into my branch", which is undefined here. "Fast-forward this
checkout to `<ref>`" means the same thing detached and on a branch, so that is what the script now
says:

```sh
TARGET="${1:-origin/master}"
git fetch origin --prune
# ...
if [ -n "$BRANCH" ]; then git merge --ff-only "$TARGET"; else git switch --detach "$TARGET"; fi
```

**Whichever mode the checkout is in is preserved.** Detached stays detached — that is the
deployment style, and silently converting the box to a tracking branch would quietly destroy the
attributability the style exists for. A branch checkout fast-forwards in place, because silently
detaching somebody's branch would be its own surprise.

### 2. A run with no argument may only fast-forward, and that refusal is the whole guard

This box is deployed onto bench branches **on purpose** — ADR 0164 ran the station on
`adr-0164-the-on-path` for a full cycle. A routine "just update the server" that silently yanked such
a checkout back to master would destroy the experiment in progress, during the least suspicious
command in the project. So a default-target run refuses when `HEAD` is not an ancestor of the target,
and names the remedy:

```
update: refusing to move this checkout.
        HEAD is 9a864fd and is NOT an ancestor of
        origin/master (2a794ac), so this would not be a fast-forward — this box is
        carrying commits that origin/master does not have. ...
        To update onto it anyway, name it:
            ./update-radio-server.sh origin/master
```

Naming the target is how the operator says they mean it, and it is also how a bench branch is
deployed in the first place.

**A pleasant consequence, and it deletes a manual ritual.** Once a bench branch merges, its commits
*are* in master, the ancestor test passes, and the bare no-argument update starts working again on
its own. Every ADR in this arc has carried a *"when PR #NNN has merged, put both checkouts back on
the mainline"* paste-block. That step is now just "run the updater".

### 3. `uv` is resolved, not assumed

`uv` lives in `~/.local/bin`, which the **login profile** puts on `PATH`. So a bare `uv` works when a
human runs the script from an interactive shell and fails under `ssh box ./update-radio-server.sh`,
cron, or a wrapper — measured: `command -v uv` on this box returns nothing non-interactively. This
was a second, latent failure sitting directly behind the first one, and it would have surfaced the
moment decision 1 fixed the git step. The script now takes `$UV`, then `command -v`, then the known
install locations, and explains itself if it finds none rather than dying on `command not found`.

### 4. The extras stay named, and a test says so

Unchanged and load-bearing: `uv sync` is exact and uninstalls every extra not named on the
invocation, which is how the Mumble link kept losing pymumble. `tests/test_update_script.py` asserts
all three are still in the file, because that is a property a stubbed run cannot demonstrate.

## Verification

`tests/test_update_script.py` — **9 tests**, against a throwaway bare origin and clone with `uv`,
`npm` and the restart stubbed. It covers the reported failure (detached HEAD fast-forwards, and
stays detached), a branch fast-forwarding in place, the non-fast-forward refusal actually not moving
`HEAD`, an explicit target overriding it, a bench branch ceasing to need the argument once merged, an
unknown ref, already-at-target still syncing, and the missing-`uv` message.

**Red run against the old script: 8 failed, 1 passed.** The detached case fails with the operator's
own error text — *"You are not currently on a branch"* — which is the point. The 1 that passes is the
extras source-check, a regression pin rather than a red.

`uv run pytest` **2253 passed, 5 skipped** (baseline 2244/5, +9). `npx vitest run` **14 files, 146
tests**, untouched.

**What the tests do not prove:** nothing here runs a real `uv sync`, `npm run build` or
`systemctl restart` — those are stubbed, and the test file says so in its docstring rather than
letting a green suite imply more than it measured.

**One test isolation bug, found and fixed rather than shipped:** the missing-`uv` case originally
scrubbed only `$UV`, which left `command -v uv` free to find the developer's own `uv` — the test
passed while proving nothing. It now scrubs `PATH` and `HOME` too. A guard tested in one direction
is a guard tested for the case you happened to be in (`server-notes.md`, ADR 0162).

## Bench — the station, measured

Fixed by hand first, since the broken script could not deliver its own fix:

| | |
|---|---|
| before | `a6a4cd4` detached, 0 ahead / **9 behind** `origin/master`, tree clean |
| after | **`2a794ac`** = `origin/master`, still detached |
| service | `systemctl --user restart radio-server` → **active**; HTTPS **200** on 8090 |
| bundle served | **`index-CI6vVew4.js`** — byte-for-byte the ADR 0168 build, so the new Second receiver card is live |
| boot log | *"uvk5: broadcast FM is off; the station can hear its own channel"*, *"uvk5: demodulating FM (raw 0)"* |

`radio.toml` and `radio-secrets.toml` are gitignored (`git check-ignore -v` confirms), so the switch
could not clobber them.

**The witness (8091) was deliberately not moved.** It is a separate checkout with different extras
(`--extra kv4p`, not `--extra mumble` — syncing it with the station's extras removes `opuslib` and
every RF stage of `acceptance.py` fails at once) and it carries **uncommitted local edits** to
`radio_server/audio/dtmf.py`. Recorded as an open item rather than tidied away in a cycle that was
asked to fix an update failure.

## Consequences

- **`./update-radio-server.sh` is now the documented one command** in `deployment.md` and
  `server-notes.md`, and the hand-written long form no longer starts with `git pull`.
- **The script is still station-only.** It hard-codes `--extra hardware --extra tts --extra mumble`;
  the witness needs its own command, and `server-notes.md` now says so at the point of use.
- A default update can no longer move a box that is carrying unmerged commits. Anyone who *wants*
  that must type the ref.

## Findings carried forward

1. **The witness checkout has uncommitted `dtmf.py` edits and is on an unrecorded commit.** Anything
   quoting it as a measuring instrument should check it first.
2. **`restart-radio-server.sh` is two lines with no `set -e` and no health check** — it restarts and
   prints `systemctl status`, so a unit that starts and immediately dies still exits 0. The updater
   reports the deployed commit but not whether the service survived.
3. **Nothing in CI runs any of this.** These 9 tests are the first thing that executes
   `update-radio-server.sh` other than an operator.

## Out of scope

The witness's local edits and its extras drift; the systemd unit's `ExecStart` missing `--extra kv4p`
(ADR 0165, still open); a health check in `restart-radio-server.sh`; and the outer multi-repo wrapper
that invoked the script, which is not in this repository.

## Source of truth

`update-radio-server.sh` and `tests/test_update_script.py`. The deployment procedure:
[server-notes.md](../server-notes.md) and [deployment.md](../deployment.md).
