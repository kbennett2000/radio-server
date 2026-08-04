# ADR 0184 — The SIGKILLs stopped ten days before we credited them

Status: Accepted

## Context

[ADR 0183](0183-a-carrier-that-outlives-the-process.md) recorded that across **422 stops only 249
finished teardown and 133 timed out into SIGKILL**, called it *"roughly seven a day"*, and named it
the dominant route to a process that dies without unkeying. That went into `docs/HANDOFF.md` and was
used to brief this cycle: *"a third of service stops never finish teardown."*

The counts are accurate. Dividing them by the whole window was not. **The window contains a fix.**

The brief asked for the sample split around ADR 0181's deploy. That split point turned out to be an
artifact, so this ADR reports the split the data actually has.

## The rate collapsed — and ADR 0181 gets no credit for it

`journalctl --user -u radio-server.service`, whole retained window (2026-07-16 → 2026-08-04):

| era | stops | completed | SIGKILL | rate |
|---|---|---|---|---|
| 2026-07-16 → 07-24 | 155 | 4 | **126** | 81 % |
| 2026-07-25 (transition day) | 104 | 92 | 7 | 6.7 % |
| **2026-07-26 → now** | **244** | 233 | **0** | **0 %** |

**The last SIGKILL in the journal is `2026-07-25T13:08:45`** — ten days and forty ADRs
before ADR 0181 reached the bench (`2026-08-04 02:23:17 UTC`, from the deployed repo's reflog).
Splitting at 0181's deploy as briefed gives 133/412 → 0/91, which reads as a dramatic result and
credits the wrong change: the collapse had already happened, and 0181's deploy merely sits inside the
quiet half.

**Confidence.** Zero events in 244 stops. By the rule of three the 95 % one-sided upper bound on the
current rate is **3/244 = 1.2 %**, against a pre-fix 81 %. If the rate were still even the
whole-window 32 %, P(zero in 244) ≈ 10⁻³⁷. This is not a small sample reported as a pass — contrast
ADR 0183's honest 0-in-75, where P(zero) was 34 % and proved nothing. The difference is the base
rate, not the diligence.

## Lengthening the timeout was already tried here, and it did nothing

The brief said not to lengthen `TimeoutStopSec` as the fix. The station's own journal already
contains that experiment. Measuring the interval from `Stopping radio-server.service` to
`State 'stop-sigterm' timed out` recovers the timeout in force at the moment each kill fired:

```
rounded histogram of kill deadlines (n=134):  {10s: 91, 11s: 11, 20s: 25, 21s: 6}
first kills: 2026-07-16 @ 10.0 s        last kills: 2026-07-25 @ 20.0 s
```

`TimeoutStopSec` was **raised 10 → 20 mid-window, and 31 more SIGKILLs followed at the new
deadline.** The longer timeout moved the deadline and changed nothing else — which is the whole
objection, measured in the field rather than argued.

What stopped them was [ADR 0127](0127-bounded-graceful-shutdown.md): `GRACEFUL_SHUTDOWN_SECONDS =
5.0` passed as uvicorn's `timeout_graceful_shutdown`. Before it the parameter was unset — *wait
forever* — so any open WebSocket held uvicorn's `_wait_tasks_to_complete()` loop indefinitely, the
lifespan teardown never ran, and systemd's deadline was the only thing that ever ended the process.
Nothing in the app tells a connected client to go away: every WS handler parks on an unbounded
`queue.get()` in `stream_until_disconnect`, and uvicorn's bounded cancel is the only mechanism that
ends an idle socket. The deployed unit's own comment, mtime `2026-07-25 13:10:55` — **130 seconds
after the last SIGKILL** — says it:

```ini
# ADR 0127 (the 20 s SIGKILL hang is fixed in code, not hidden here).
SuccessExitStatus=143
```

## The stall was bounded, not removed — and the check that guards it was looking at the wrong client

Median time from `Stopping` to `Application shutdown complete`, per day:

```
2026-07-25  n=93   median 5.00s   (63 stops at exactly 5 s, 27 at 6 s)
2026-07-26  n=48   median 5.00s
2026-08-01  n=31   median 5.00s
2026-08-03  n=28   median 0.00s   max 1.00s
2026-08-04  n=100  median 0.00s   max 1.00s
```

For the first week after ADR 0127 **every stop burned the whole graceful window**. Then it stopped,
and the last `Waiting for connections to close` in the journal is `2026-08-01 17:32:11`. Read
forward, that says the stall went away. It does not: the recent stops were driven by scripts, and a
script is a friendlier client than a browser tab.

Three arms on the deployed station, 20 stops each, measuring `systemctl --user stop` wall time:

| arm | client | median | teardown completed |
|---|---|---|---|
| A | none | **0.35 s** (0.25–0.48) | 20/20 |
| B | `websockets` library on `/audio/rx` + `/events` | **0.34 s** (0.21–0.45) | 20/20 |
| C | WS handshake completed, then never reads and never answers the close | **5.36 s** (5.20–5.42) | 20/20 |

Arm C is a browser tab. Arm B is what `acceptance.py`'s `stop under WS load` check has always used —
and the `websockets` library runs a background reader that answers the server's close frame the
instant it arrives, so the graceful window is never touched. **The check that exists to catch this
regression cannot see it**, which is how a station accumulated 133 SIGKILLs under a green
`stage_systemd`. That is the same defect class as
[ADR 0178](0178-checks-that-silently-answer-the-safe-looking-default.md): a check quietly answering
the easy case.

n=20/arm is chosen for what is being compared. This is the central tendency of a quantity that lands
on either ~0.3 s or ~5.4 s: the arms are separated by 16× and their ranges do not touch (arm B max
0.45 s, arm C min 5.20 s) across 40 trials. That is not the frequency of a rare event, which is where
the rule-of-three arithmetic above is the right tool instead — using either on the other's question
is how a lifetime average got read as a current rate in the first place.

So the answer to "is it still happening" is **two different answers**: the SIGKILL is gone, and the
stall that caused it is not. It is bounded now, which is why it stopped being fatal.

**Re-run after the fix — and it is a regression check, not a before/after.** All three arms on the
deployed branch, 10 stops each: A **0.37 s**, B **0.34 s**, C **5.33 s**, teardown completed 30/30.
Identical, which is the expected and correct result: this diff contains no change to the WebSocket or
graceful-shutdown path, so presenting these as an "after" arm for the fix would be
[ADR 0181](0181-a-hung-disk-stalls-the-transmitter.md)'s redundant-arm mistake. What they establish
is that bounding the joins did not lengthen a stop.

## The budget does not close

The brief asked whether `TimeoutStopSec` and `GRACEFUL_SHUTDOWN_SECONDS` are consistent. Nobody had
checked. Summing the teardown's own declared bounds, in lifespan order:

| step | bound |
|---|---|
| uvicorn graceful window | 5.0 |
| `DStarBridge.stop()` — vocoder close + task join, `tx_hang + 2.0` each at `tx_hang = 1.0` | 6.0 |
| `MumbleBridge.stop()` — task join | 2.0 |
| `stop_cadence()` thread join (refcounted, so paid once across both bridges) | 2.0 |
| `holder.stop()` — scan join 1.0 + pump 2.0 + reader 0.25 + `Controller.close()` 3 × 1.0 | 6.25 |
| `event_log.close()` | 2.0 |
| **total** | **23.25 s** |

Against `TimeoutStopSec=20`. It is over by 3.25 s **before** counting `radio.ptt(False)` and
`radio.close()`, which carry no bound at all.

> **CORRECTED by [ADR 0185](0185-the-stop-budget-fits-its-deadline.md).** This table is an
> **undercount**: it misses the D-STAR gateway reader join (1.0), the pymumble library join (2.0),
> the uvk5 transport reader join inside `radio.close()` (1.0), the TX pacer join — paid inside
> **both** `ptt(False)` and `close()` (5.0 each) — and `PolledGate.stop()`'s join inside the pump's
> cancellation path (1.0). The honest figure was **≈32 s**. The structural reason the last one was
> missed is worth keeping: *a bound expressed in async time does not cover synchronous work done
> inside it*, so on-loop thread joins are additive terms rather than absorbed ones. `docs/deployment.md` claims "worst case well under
10 s"; that is corrected here. The worst case needs D-STAR and Mumble both up and both wedged, which
is why it has never fired — but "has not happened yet" is not the same as "cannot".

**The remedy is not a bigger number.** The histogram above is the evidence: 20 s was already tried.

## What a failed teardown actually costs, now

`HUPCL` covers the carrier since ADR 0183, so this stopped being a stuck-transmitter question. What
is genuinely lost on a SIGKILL:

- **Queued ledger records** — up to `DEFAULT_LEDGER_QUEUE_MAXSIZE` = 1205, named by
  [ADR 0181](0181-a-hung-disk-stalls-the-transmitter.md) as the cost its own fix introduced.
- **The DV Dongle.** ADR 0104's original and still-live concern: a SIGKILL mid-operation wedges the
  vocoder until re-open or power-cycle. This is the hardware cost and the reason the budget exists.
- **A live TX session's Part 97 sign-off ID.** `TxSession` sends the closing ID at session end; an
  abrupt death skips it. The carrier still drops, so this is a missed closing ID rather than an
  unattended transmitter — but it is the licensee's station either way (guardrail 5).
- **NOT the carrier**, and this ADR should not be cited as saying otherwise.

The honest summary is that a failed teardown is much cheaper than it was two ADRs ago. The reason to
keep fixing it is the `rebuild` path below, where there is no process exit to clean up after.

## The two defects the trace found

The brief said to find what holds the loop by tracing rather than guessing at candidates. Tracing
`holder.stop()` found a teardown that skips its own end, and a bound that is not a bound.

### 1. A teardown that stops partway through

`holder.stop()`'s docstring claims *"each step independently guarded"*. Two of the five steps were
bare awaits, with `radio.close()` behind them:

```python
if self.scan_runner is not None:
    await self.scan_runner.stop()      # unguarded
if self.rx_pump is not None:
    await self.rx_pump.stop()          # unguarded
...
close()                                # never reached if either raises
```

And `ScanRunner.stop()` caught only `CancelledError`, so anything else the task died of came back out
at the join. `ScanEngine.tick()` tunes over serial (`_tune` → `set_frequency`) and has no try/except,
so a fault ends the task with the exception stored on it; nothing clears `_task`, so the **next**
teardown cancels an already-finished task (a no-op) and re-raises at the join.

On process shutdown this costs little — the kernel cleans up and `HUPCL` drops the line. It matters on
**`holder.rebuild()`** (`POST /radio/select`), which runs the same `stop()` with no process exit: a
skipped `close()` leaves the serial device `TIOCEXCL`-claimed by the very process trying to let go of
it, so the swap fails against a port nothing else can take.

### 2. `wait_for` is not a bound

Worse, and not the defect this cycle went looking for. ADR 0104 gave the teardown's task joins a
deadline so a wedged task could never spend the stop budget. Three of them — the RX pump's task, and
the Mumble and D-STAR bridges' task sets — were written as
`await asyncio.wait_for(task, timeout=...)`, each with a comment naming exactly the case it defends
against: *"a task parked in a non-cancellable blocking call"*, *"abandon a still-parked task
instead"*. (`ScanRunner`, defect 1 above, is the fourth and had no deadline at all.)

`wait_for` cannot do that **for a Task**. On expiry it **cancels the awaited task and then waits for
the cancellation to be delivered** — so against a task that cannot take a cancel, the only case worth
bounding, the guard blocks for exactly as long as the thing it was guarding:

```
wait_for(bound=0.5s): returned=False after 3.00s     <- 3.0 s is the observer giving up
wait(bound=0.5s):     returned=True  after 0.50s
```

The reachable form is not exotic. A coroutine inside a *synchronous* call cannot take a cancel until
that call returns: `ScanEngine.tick()` is documented as fully synchronous and tunes over serial, and
the bridges do blocking sends. `wait_for` waits out the blocking call in full — unbounded, which is
precisely what ADR 0104 set out to remove.

**Scoped, not swept.** The D-STAR bridge's `wait_for` around `run_in_executor(None, vocoder.close)`
is left exactly as it is, because there `wait_for` genuinely bounds: cancelling the asyncio wrapper
around an executor future succeeds immediately whether or not the worker notices. Measured at
**0.50 s against a wedged 30 s worker**, so that deadline holds and the thread is abandoned, which is
what ADR 0099 intended. The distinction is Task versus executor future, not `wait_for` being wrong
everywhere — checking it was cheaper than assuming either way.

**And `test_shutdown_budget.py` could not catch it.** Its three tests model the worst case as a task
that catches `CancelledError` and keeps sleeping — the right model — but they never let the task
start, and a task cancelled before its first step ends as `CANCELLED` without ever reaching its own
`except`. So the "stubborn" tasks were ordinary cancellable ones, and an unbounded join passed just
as happily. Adding one `await asyncio.sleep(0.05)` to each makes all three **hang forever** against
the old code (killed at 100 s) and pass in 1.3 s against the new.

## Decision

1. **`radio_server/shutdown.py`** — a shared `join_bounded(tasks, timeout)` built on `asyncio.wait`,
   which reports the deadline and touches nothing. Same semantics ADR 0104 intended (one shared
   deadline for the set, abandon on timeout, never raises); only the primitive changed. Used at all
   four task-join sites. It **returns** what any finished task died of rather than swallowing it: a
   self-review catch, because `wait_for` re-raised and `gather(..., return_exceptions=True)`
   consumed, and plain `asyncio.wait` does neither — so an unretrieved exception would have started
   showing up as asyncio's "Task exception was never retrieved". Now it is consumed *and* each call
   site logs it.
2. **`ScanRunner.stop()`** — bound the join at `SCAN_JOIN_TIMEOUT_S = 1.0`, **derived**: a cancel can
   only land at this loop's `await asyncio.sleep(self._poll)`, so the bound must clear one
   `DEFAULT_SCAN_POLL` (0.5 s) comfortably or a healthy runner mid-sleep is abandoned on every stop —
   the ADR 0183 mistake in a different module. Pinned by an identity test. A dead-task exception is
   **logged, not re-raised**: a teardown is not where an engine fault gets to surface, and the
   `stopped` event still fires so the UI drops to idle.
3. **`holder.stop()`** — guard the two remaining steps, so no single failure can end a teardown, and
   correct the docstring that claimed a property the code did not have.
4. **`scripts/bench/acceptance.py`** — hold the sockets with a raw socket that completes the WS
   handshake and then reads nothing, so `stop under WS load` exercises arm C instead of arm B.
5. **`docs/deployment.md`** — correct the `TimeoutStopSec` rationale and record the real budget sum.
6. **ADR 0183 and `docs/HANDOFF.md`** — correct the 422/133 headline in place.

## Consequences

- The stop budget's worst case is **finite for the first time** — the scan join was the one ADR 0104
  never bounded, and `wait_for` made the other three finite only against tasks that did not need
  bounding. It still does not fit in 20 s (23.25 s — an undercount; ADR 0185 measured ≈32 s), and
  that is now written down rather than assumed away.
- `holder.stop()` reaches `radio.close()` on every path, which is what the `rebuild` swap depends on.
- `acceptance.py`'s stop check will now take ~5.4 s instead of ~0.3 s. That is the point: it is
  measuring the graceful window, which is the thing that used to overrun. The 15 s threshold still
  separates "bounded" from "SIGKILLed at 20 s".
- **This cycle did not remove the 5 s stall.** Bounding it was ADR 0127's job and it holds. Removing
  it means telling connected clients to disconnect at shutdown instead of waiting for uvicorn to
  cancel them — named below, not built, because it changes the WS contract and deserves its own
  cycle.
- pytest **2464 passed / 5 skipped** (from 2454/5); vitest **14 files / 163 tests** unchanged.

## On the bench, after deploy

`acceptance.py` full run: **`systemd` PASS** — the stage under test, now holding with the
unresponsive client and reporting **5.36 s** where it used to report 0.32 s. `presets`, `rx`, `dtmf`,
`auth`, `tx`, `split`, `services` PASS; `web` FAIL on the known witness `kv4p GET /healthz 404`;
`split-minus` SKIP. `--only systemd` re-run against the final commit: PASS at **5.24 s**.

One more thing that check taught, and it is the same lesson twice. On the first post-deploy run the
handshake raced a just-restarted server, the hold raised, `elapsed` was set to `-1.0`, the timing
assertion was **silently skipped, and the stage still passed**. That is exactly the shape being fixed
one line up. The hold now has its own check, so failing to establish it is a failure rather than a
quiet skip.

**181 stops on the station across this cycle's runs: 180 reached `Application shutdown complete`,
0 SIGKILL, 0 SIGSEGV.** (The one is the stop still in flight when the count was taken.)

Station left on 145.145 / TX 144.545 / 107.2 / FM / low, links down, verified by read-back before and
after a restart. `uvk5_tune_persist = true` reported, not changed.

## Carried, named and not fixed

- **A server-initiated WS close at shutdown** — the only thing that removes the 5 s window rather than
  bounding it. The natural shape is a shutdown flag the `stream_until_disconnect` fan-out watches.
- **`Controller.close()`'s 3 × 1.0 s multimon joins** and **`stop_cadence()`'s 2.0 s thread join**,
  both synchronous on the event loop inside an awaited teardown. Bounded, and at shutdown nothing
  else needs the loop — lower severity than the stalls ADRs 0181/0182 removed, but they are 5 s of
  the over-subscribed budget above.
- **`radio.ptt(False)` and `radio.close()` carry no bound** in `holder.stop()`. Guarded, so they
  cannot skip a step, but a wedged device close can still spend the budget.
- **The out-of-process supervisor** and **the radio's own TOT** — the only cover for power loss and
  host death (ADR 0183). Still named, still not built.
- **`Recorder.write`** and the **~1.3 s synchronous key-up** — carried from ADRs 0181/0182.
