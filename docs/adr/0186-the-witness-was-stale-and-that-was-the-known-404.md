# ADR 0186 — The witness was stale, and that was the "known" 404

Status: Accepted

## Context

PR #245 merged. The box's updater took the main instance cleanly and **refused the kv4p witness**:

```
! Local edits detected — stashing before update.
! Your local edits conflict with upstream changes. Rolling back.
x Update SKIPPED for radio-server-kv4p — local edits conflict with upstream.
```

[ADR 0185](0185-the-stop-budget-fits-its-deadline.md) is the proximate trigger: `eab4296` is the
**only** upstream commit to `radio_server/audio/dtmf.py` since the witness's pin, and the witness has
a local edit to that file. But the condition it exposed is much older — and it has been quietly
distorting this arc's evidence.

## What the witness actually was

| | |
|---|---|
| HEAD | `a6a4cd4` — PR #222, **ADR 0165** |
| origin/master | `1fece27` — PR #245, ADR 0185 |
| behind by | **78 commits / ~23 PRs** |
| branch | **detached HEAD** — hence the updater's *"branch: HEAD"* |
| its own `origin/master` ref | `54f9e56` (PR #237) — its fetch was stale too |
| reflog | `reset: moving to origin/HEAD` → rollback, repeatedly, since **2026-08-01** |

It had been trying and failing to update for days. The pin was never deliberate.

## The finding: this *is* the "known witness 404"

`/healthz` was added by `e295e0e` — **ADR 0166**. The witness sat at **ADR 0165**: exactly one PR
before the endpoint existed.

So `acceptance.py`'s `web` stage failing on `kv4p GET /healthz 404` — recorded as *"the known witness
404"* in **ADR 0183, ADR 0184 and ADR 0185**, three times, by me — was never a quirk of the witness.
It was a deployment that had silently stopped updating, presenting exactly as a known-failing check.

That is [ADR 0178](0178-checks-that-silently-answer-the-safe-looking-default.md)'s defect class with
a new face: **a stale instrument and a known failure look identical from the outside.** Worse than a
check that answers the easy case — a check that *is* failing, correctly, about something real, and
gets filed as background noise because the first person to see it guessed and everyone after quoted
the guess. Three ADRs deep, nobody had run `git -C <witness> log -1`.

Of the 78 missing commits, only **three** touch `backends/kv4p/`, and two are improvements to the
witness itself: `/healthz` (ADR 0166) and `f82a657` "the witness backend surfaces its transport too".
The instrument was being denied precisely the commits that make it a better instrument.

## The three local edits, three different verdicts

| file | edit | verdict |
|---|---|---|
| `radio_server/audio/dtmf.py` | `NATIVE_REVERSE_TWIST_DB` `4.0 → 10.0` | **Redundant.** The witness's `radio.toml` already sets `dtmf_reverse_twist_db = 10.0`, and `controller/engine.py` threads it into the live decoder — added by **ADR 0075**, which *predates* the pin. The hack had been dead weight for months, and it is the file ADR 0185 touched. |
| `update-radio-server.sh` | replaced wholesale | **Clobbered.** It held `update_radio_kv4p()`/`header`/`$APPS`/"Phase 21.11" — a fragment of the `~/bin/update-services.sh` orchestrator, saved over a tracked repo file. A guaranteed conflict against ADR 0169's upstream change to the same path. |
| `radio_server/link/entries.py` | `"(radio-server)" → "(radio-server-kv4p)"` | **A real gap.** `link_username()` hardcoded the name and there was **no config key**. Zero upstream commits since the pin, so it was not conflicting *yet* — but it is why the checkout could never be clean, and therefore why it could never update. |

The third is the root cause of the whole episode: **a per-deployment string that had no config key, so
the deployment edited the source, so the checkout was permanently dirty, so it stopped updating, so
the instrument went stale, so a real failure became folklore.**

The first is the same shape already solved — the reverse-twist hack is redundant *because* ADR 0075
gave that value a key. One got a key and stopped causing trouble; one did not.

## And the witness was outside the budget ADR 0185 had just proved

`radio-server-kv4p.service` had **`TimeoutStopSec=10s`** — the pre-ADR-0104 value, **a third** of the
32.70 s the budget requires — and no `SuccessExitStatus=143`. ADR 0185's identity test and its
acceptance check both looked only at the primary unit. A budget only one of two identical deployments
is held to is not a budget.

## Decision

### 1. `mumble.instance_name` — give the tag a key

Defaults to `DEFAULT_MUMBLE_USERNAME`, so shipped behaviour is unchanged. `link_username(callsign,
name)` takes it; the app and `doctor` pass it from settings.

**Deliberately not named `mumble.username`.** ADR 0042 removed that key when the flat `[mumble]`
connection settings became `[[mumble.servers]]` entries, and its migration guard still fires on it.
Reviving the name would let a pre-0042 config be silently reinterpreted with a new meaning; a
different name keeps the guard honest, and a test now pins that it still fails loud.

**Nor does this undo ADR 0042's decision that the nick is fixed.** The licensee half stays derived
from `station.callsign` — an identity claim under Part 97 (guardrail 5), and no config key may put a
different one on the air. Only the parenthetical software tag moves. ADR 0042's migration message
asserted the nick is *"always '<callsign> (radio-server)', not configurable"*, which is now partly
untrue, so it is **corrected in place** rather than left to mislead the next reader.

### 2. Make staleness legible in `acceptance.py`

- **The witness's revision is checked against the radio's**, and a mismatch FAILs. Read from **disk**
  via the unit's `WorkingDirectory` — not over HTTP. There is no version endpoint, and more to the
  point *a check that asks the deployed code its version cannot detect code too old to answer*, which
  is exactly the failure mode here. `git` is also the only thing that can say how far behind.
- **The installed-`TimeoutStopSec` check now covers both units**, closing ADR 0185's gap.

### 3. The bench (a deployment change, recorded here)

Local edits backed up to `/tmp/kv4p-local-edits-0186/` first, then: restore the three tracked files;
reattach to `master` with an upstream set; update; add `instance_name = "radio-server-kv4p"` to the
witness's `radio.toml`; align its unit to `TimeoutStopSec=35` + `SuccessExitStatus=143`.

**Ordering mattered and is worth keeping:** the config key had to be added *after* the code that
defines it, or the stale server would have refused an unknown setting on restart. The witness's
`radio.toml` was dry-run against current code before any of it, which is what made the update safe to
attempt rather than hopeful.

## Consequences

- **`GET /healthz` on the witness answers 200** for the first time since ADR 0165, and `/status` now
  reports `transport: {alive: True, ...}` — the health signal ADR 0166 added and the witness never
  had. `acceptance.py`'s `web` stage passes.
- The witness checkout is clean and on a tracked branch, so it updates like the primary.
- A second instance no longer needs a source edit to be distinguishable.
- pytest **2479 passed / 5 skipped** (from 2475/5); vitest **14 files / 163 tests** unchanged.

## On the bench, after

```
[systemd]
  ok radio-server.service TimeoutStopSec covers the budget      35s   want >= 32.70s
  ok radio-server-kv4p.service TimeoutStopSec covers the budget 35s   want >= 32.70s
  ok witness runs the same revision as the radio    80b1d8e vs 80b1d8e   want identical
[web]
  ok kv4p GET /healthz                200   want 200
```

**Every stage PASSES.** `systemd`, `web`, `presets`, `rx`, `dtmf`, `auth`, `tx`, `split`, `services`
— and `RESULT: INCOMPLETE` now comes solely from `split-minus` SKIP (the bench lacks the
`Bench Split Minus` preset), which is the honest exit code ADR 0161 introduced for
"attempted-everything-passed, something could not be attempted".

**This is the first acceptance run since ADR 0165 in which no stage FAILED.** Every run in between
reported `RESULT: FAIL`, and every one of those was this.

The witness also now reports `transport: {alive: True, port: ...}` on `/status` — the health signal
ADR 0166 added and it had never had.

**One more thing the run made visible:** `stage_presets` applies `2m Simplex 147.555`, so acceptance
*itself* retunes the station. That is why ADR 0185 found the station drifted at 147.555 — its cycle
restored and then ran acceptance again. The restore genuinely has to be the last bench action, and in
this cycle it is.

## Carried, named and not fixed

- **`~/bin/update-services.sh` is outside the repo.** It reported the failure correctly and rolled
  back safely — but its rollback leaves the checkout stuck, and it never says *how far behind* it is.
  A "SKIPPED" that repeats for 23 PRs should escalate. Bringing the orchestrator, or at least a
  staleness report, into the repo is a cycle of its own.
- **The second-instance provisioning procedure ("Phase 21.11")** is not in the repo either; the
  clobbered `update-radio-server.sh` is a symptom of that gap.
- ADR 0185's carried list stands: the signal-then-join restructure, the server-initiated WS close,
  `rx/pump.py`'s reader executor, `abort()` vs draining `stop()`, `TxSlot.release()`'s missing
  ownership check, and the standing `sign_off` / `UNLINK_URCALL` gaps.
