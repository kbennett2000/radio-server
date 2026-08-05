# Handoff

## The stop budget fits its deadline (2026-08-04, latest)

ADR 0185, branch `adr-0185-the-budget-fits-its-deadline`, from `origin/master` **7018ac4**.

**Two things ADR 0184 got wrong, both found by measuring.**

**1. 23.25 s was an undercount — the honest figure was ≈32 s.** Missed: the D-STAR gateway reader
join (1.0), the pymumble library join (2.0), the uvk5 transport reader join inside `radio.close()`
(1.0), the TX pacer join paid inside **both** `ptt(False)` and `close()` (5.0 each), and
`PolledGate.stop()`'s join inside the pump's cancellation path (1.0). Keep the reason the last one
hid: **a bound expressed in async time does not cover synchronous work done inside it** —
`asyncio.wait`'s timer cannot fire while the loop thread sits in `thread.join()`. On-loop thread
joins are additive terms, not absorbed ones.

**2. A bound that expires does not release the process.** ADR 0184's own `shutdown.py` docstring
blessed `wait_for(run_in_executor(...))` as *"measured at 0.50 s against a wedged 30 s worker"* — true
of the **await**, false of the **process**. On the station's Python 3.14.4:

```
default executor:  await 0.50s   asyncio.run() 30.00s   process exited 30.14s
dedicated pool:    shutdown(wait=False) 0.02 ms   ...   process exited 20.13s
daemon thread:     await 0.50s   asyncio.run()  0.50s   process exited  0.61s
```

`Runner.close()` joins the default executor with `THREAD_JOIN_TIMEOUT = 300`, and
`concurrent.futures.thread`'s atexit hook joins every pool with **no timeout**. The hang only moves
from the lifespan, where it is logged, to interpreter shutdown, where it is not. **Only a daemon
thread walks away** — which is why `call_bounded` uses one, and why summing an executor-backed bound
asserts something false.

**Instrumented first, then derived.** Nothing in the package had ever timed a teardown step, which is
why every bound was a round number. `timed()` landed first and deployed; over n=19 stops:
`radio.close` **median 110 ms / max 209 ms**, pump 6/18 ms, everything else 0. **`ptt(False)`
collected zero samples** — a quiescent shutdown never has the arbiter transmitting — so its bound is
structural and says so. n=0 is not a measurement.

**The budget is now computed and tested.** `stop_budget_seconds()` sums it; an identity test asserts
the shipped deadline covers it; `scripts/radio-server.service` is **in the repo** (the number lived
in a fenced block, and `test_docs_contract.py` deliberately blanks those, so no test could see it);
and `acceptance.py` reads the **installed** `TimeoutStopUSec`, because shipping a file does not make
a box adopt it.

| | s |
|---|---|
| signal delivery (measured max, n=201) | 0.20 |
| uvicorn graceful window | 5.00 |
| teardown bounds | 25.25 |
| exit reserve (measured) | 0.25 |
| explicit margin | 2.00 |
| **required** | **32.70** |
| **shipped** | **35** |

**`TimeoutStopSec` 20 -> 35 on the deployed station** — a deliberate deployment change; the previous
unit is at `/tmp/radio-server.service.pre-0185` on the box. This is sized to a finite enumerated sum,
which is the opposite of the 10->20 raise against an *unbounded* stall that this station's journal
already refuted (31 more SIGKILLs followed that one).

**Severity, found honestly rather than inherited.** A SIGKILL now costs **0-2 ledger records**, not
1205 (the writer writes on dequeue; organic load is 1-2/s). ADR 0104's DV Dongle wedge traces to
ADR 0100's **DVAP** — a different device, wedged by restarting an external gateway, never measured
under this trigger, and the dongle is not plugged in. `StationId.sign_off` is called by **no**
shutdown path, clean or SIGKILL, so that is a standing Part 97 gap rather than a SIGKILL cost. So the
reason to bound the teardown is availability — an unbounded teardown is an unbounded stop — not
damage. Say that instead of inheriting the dongle framing.

**Concurrency was refuted with a price attached**: gathering the two bridges saves 2.0 s of ~32 (6 %)
because the synchronous joins hold the one loop thread, and pays with two RF races —
`TxSlot.release()` has no ownership check, and D-STAR's synchronous `ptt(False)` can land inside
Mumble's `await await_tx_ready` -> `ptt(True)` window (ADR 0099's 15 s stuck key, resurrected).

**A process finding worth keeping:** the station was found at 147.555 / no split / no tone / high
power at the start of this cycle. ADR 0184's cycle restored it correctly and *then* ran two more
`--only systemd` passes, which restart the service. **The restore must be the last bench action.**

**Bench, after deploy.** Per-step teardown over n=59 stops: `radio.close` median 114 ms / max
236 ms against its 2.0 s bound, pump 7/35 ms, ledger 0/86 ms, everything else 0 — and **0 abandoned
workers**, so no bound has ever fired on a healthy close. Stop wall time unchanged (no client 0.32 s,
stubborn client 5.34 s, 24/24 complete) — a regression check, not a before/after, since this diff
touches nothing in the WS path. `acceptance.py`: **`systemd` PASS** including
`installed TimeoutStopSec covers the budget: 35s want >= 32.70s`, 7 stages PASS, `web` FAIL on the
known `kv4p /healthz 404`, `split-minus` SKIP.

pytest **2475 passed / 5 skipped** (from 2464/5); vitest **14 files / 163 tests** unchanged.

**Carried:** the signal-then-join restructure (~9.75 s of on-loop joins -> ~2 s, which is exactly what
would let a 20 s deadline stand; five modules and a partial ordering); a server-initiated WS close at
shutdown; `rx/pump.py`'s reader executor (same abandon hole, but the RX hot path); uvk5's
`_restore_rx_frequency`; `abort()` vs draining `stop()`; `TxSlot.release()`'s missing ownership
check; and the standing `sign_off` / `UNLINK_URCALL` gaps.

## The SIGKILLs stopped ten days before we credited them (2026-08-04)

ADR 0184, branch `adr-0184-the-sigkills-stopped-before-we-credited-them`, from `origin/master`
**df078b9**.

**Start here: the number this cycle was briefed on was wrong, and the way it was wrong is the
lesson.** ADR 0183 recorded 422 stops / 249 completed / **133 SIGKILL** and called it "roughly seven
a day". The counts are right. Dividing them by the whole window was not — **the window contains the
fix**. Split by date: 2026-07-16→24 is **126 kills in 155 stops (81 %)**, 07-25 is the transition
day, and there are **zero in the 244 stops since**. Rule of three puts the current 95 % upper bound
at **1.2 %**. A lifetime average was quoted as a current rate, it reached HANDOFF, and it briefed a
cycle. When you record a rate, record its window.

**And it was not ADR 0181.** The last SIGKILL is `2026-07-25T13:08:45`, ten days and forty ADRs
before 0181 reached the bench (`2026-08-04 02:23:17`, from the deployed repo's reflog — that reflog
is the reliable way to date a deploy). Splitting at 0181 as briefed gives 133/412 → 0/91, which looks
decisive and credits the wrong change.

**The forbidden fix was already tried on this station, and the journal proves it did nothing.** The
interval from `Stopping` to `stop-sigterm timed out` recovers the deadline in force at each kill:
`{10s: 91, 11s: 11, 20s: 25, 21s: 6}`. So `TimeoutStopSec` went **10 → 20 and 31 more kills followed
at the new deadline**. What stopped them was ADR 0127's `GRACEFUL_SHUTDOWN_SECONDS = 5.0`; the unit
file's mtime is `13:10:55`, **130 s after the last kill**.

**The stall was bounded, not removed — and the check guarding it watches the wrong client.** Per-day
medians sat at a flat 5.00 s for a week, then dropped to 0.00 s, which reads as "fixed". It is not.
Three arms × 20 stops on the station:

| arm | client | median |
|---|---|---|
| A | none | 0.35 s (0.25–0.48) |
| B | `websockets` library — **what `acceptance.py` uses** | 0.34 s (0.21–0.45) |
| C | handshake completed, then never reads, never answers the close | **5.36 s** (5.20–5.42) |

Arm C is a browser tab. The `websockets` library's background reader answers the close frame
instantly, so `stage_systemd` stayed green through 133 SIGKILLs. Re-run after the fix (10/arm):
A 0.37 s, B 0.34 s, C 5.33 s, 30/30 completed — identical, and deliberately reported as a
regression check rather than an "after" arm, because this diff touches no part of the WS or
graceful path. `acceptance.py` now holds with a raw
socket that never answers — expect that check to take ~5.4 s, and that is correct, not a regression.

**The budget does not close.** 5.0 graceful + 6.0 D-STAR + 2.0 Mumble + 2.0 cadence + 6.25 holder +
2.0 ledger = **23.25 s** against `TimeoutStopSec=20`, before `radio.ptt()` and `radio.close()`, which
have no bound at all. `deployment.md`'s "worst case well under 10 s" is corrected. It needs D-STAR and
Mumble both up and both wedged, which is why it has never fired.

**Two live defects, found by tracing.**

1. `holder.stop()`'s docstring claimed "each step independently guarded"; two of five steps were bare
   awaits with `radio.close()` behind them. `ScanRunner.stop()` caught only `CancelledError`, so a
   scan task that died of a serial fault (`tick()` tunes via `set_frequency`, no try/except) leaves
   the exception on the task, nothing clears `_task`, and the **next** stop re-raises it at the join.
   Reachable on `holder.rebuild()` (`POST /radio/select`): no process exit, so the skipped `close()`
   leaves the port `TIOCEXCL`-claimed by the process trying to release it.
2. **`wait_for` is not a bound for a Task.** Three of ADR 0104's four teardown task joins used
   `await asyncio.wait_for(task, timeout=...)`, each with a comment naming the case it defends
   against ("a task parked in a non-cancellable blocking call"); the fourth, `ScanRunner`, had no
   deadline at all. On expiry `wait_for` **cancels and then waits for the cancel to be delivered** —
   which is exactly what that task cannot do. Measured: `wait_for(0.5s)` had not returned after
   **3.00 s**; `wait(0.5s)` returned at **0.50 s**. The reachable form is a coroutine inside a
   synchronous call, which cannot take a cancel until the call returns. **Scoped, not swept:** the
   D-STAR vocoder-close `wait_for` over a `run_in_executor` future is deliberately unchanged —
   cancelling the asyncio wrapper succeeds whether or not the worker notices, measured at 0.50 s
   against a wedged 30 s thread. Task versus executor future is the distinction.

**`test_shutdown_budget.py` could not have caught #2, and that is worth remembering.** Its three
tests model the worst case correctly — a task that catches `CancelledError` and keeps sleeping — but
never let the task start, and a task cancelled before its first step ends as `CANCELLED` without
reaching its own `except`. One `await asyncio.sleep(0.05)` each makes all three **hang forever**
against the old code (killed at 100 s) and pass in 1.3 s against the new. If you write a test with a
stubborn task, yield to the loop first or you are testing nothing.

**Fixed:** `radio_server/shutdown.py`'s `join_bounded` on `asyncio.wait` at all four sites; a derived
`SCAN_JOIN_TIMEOUT_S = 1.0` (two `DEFAULT_SCAN_POLL` periods) with a dead-task exception logged
rather than re-raised; guarded holder steps; the `acceptance.py` hold.

**Cost of a failed teardown, stated honestly:** not the carrier (`HUPCL`, ADR 0183) — up to 1205
queued ledger records, a wedged DV Dongle, a live session's Part 97 sign-off ID, and the rebuild path.

pytest **2464 passed / 5 skipped** (from 2454/5); vitest **14 files / 163 tests** unchanged.

**Bench:** `acceptance.py` full run — `systemd` **PASS** (the stage under test, now reporting
**5.36 s** where it used to report 0.32 s), 7 other stages PASS, `web` FAIL on the known
`kv4p GET /healthz 404`, `split-minus` SKIP. `--only systemd` against the final commit: PASS at
5.24 s. **181 stops across this cycle's runs: 180 completed, 0 SIGKILL, 0 SIGSEGV.** Station left on
145.145 / TX 144.545 / 107.2 / FM / low, links down, verified by read-back before and after a
restart; `uvk5_tune_persist = true` reported, not changed.

**A trap worth keeping:** on the first post-deploy run the hardened hold raced a just-restarted
server, raised, and the stage **silently skipped its own timing assertion and passed**. That is the
same defect it was just hardened against. The hold now has its own check. If you add a setup step to
a bench check, give the setup a check too.

**Carried, named, not fixed:** a server-initiated WS close at shutdown (the only thing that *removes*
the 5 s window rather than bounding it); `Controller.close()`'s 3 × 1.0 s multimon joins and
`stop_cadence()`'s 2.0 s thread join, both synchronous on the loop; `radio.ptt(False)` and
`radio.close()` carrying no bound; the out-of-process supervisor and the radio's own TOT.

## A carrier that outlives the process (2026-08-04)

ADR 0183, branch `adr-0183-a-carrier-that-outlives-the-process`, from `origin/master` **2c4964e**.

ADR 0182 filed six `status=139` exits as "recorded, not fixed" with one sentence of consequence.
That sentence held **two** defects; this cycle separates them.

**The consequence, first — and the answer was already there, by accident.** On the AIOC **PTT *is*
DTR**. An abrupt death runs no `close()`, no `atexit`, and no `TotRadio` — its watchdog is a
`daemon` `threading.Timer` that dies with the process, so it gives **zero** coverage here (ADR 0117
said so and named an out-of-process supervisor it never built). What is left is the tty layer, which
lowers DTR at last close **iff `HUPCL` is set**. Measured: `stty` reports **`hupcl` set** on the
station — and pyserial 3.5 contains **zero** references to it, so it is inherited verbatim from
whatever last configured the port. **The station has been protected by a kernel default that nothing
in this repo sets, asserts, tests or documents.** One `stty -hupcl`, a getty or ModemManager and it
becomes a carrier that runs until the radio is power-cycled, with nothing that would notice.
`ensure_hangup_on_close()` now asserts it at open, beside `claim_port_exclusive`, and warns when it
finds the flag clear.

Of the brief's three candidates: **unkey-at-open is already built** and does nothing for a crash
during a `stop` (five of six); the **supervisor** cannot hold the port while the server does
(`TIOCEXCL`, confirmed live) so it must poll-then-open; the **radio's own TOT** covers even power
loss but is a menu setting the server cannot read or verify. `HUPCL` is the only one that does not
depend on the process surviving *without* adding a process.

**Two measurements worth keeping.** A raw `os.open` of this tty **does** raise DTR; pyserial's
`.dtr=False` preset holds it down through the open (False before and after, sampled at 200 ms) — so
the existing factory idiom works and the station's own open does not key. And **last-close is not
observable from userspace**: any observer must open the port, and a bare open keys it. The two-arm
`kill -9` test's idle calibration arm caught exactly that and returned INCONCLUSIVE, so the evidence
for the drop is the flag plus documented kernel behaviour, **not** a direct reading. Said in the ADR
rather than glossed.

**The crash, root-caused.** The kernel names the same thread all six times — `rx-read_0`, the ADR
0130 capture reader — faulting in `libasound`, `libportaudio` **and** `libc` at varying addresses:
a use-after-free, not one bad pointer. `RxPump.stop()` never waited for it. Cancelling a task parked
in `run_in_executor` cancels only the asyncio wrapper; the underlying `concurrent.futures.Future` is
already RUNNING so its `cancel()` fails, and **`stop()` returned in 0.06 ms with the reader still
inside `receive()`**. Abandoning a live reader was not ADR 0104's exceptional path — it was the
**only** path — and `radio.close()` three lines later frees the stream underneath it. **The
asymmetry is the finding:** `SoundCardTxPacer.stop()` already joins its writer before the stream
closes and names this hazard in as many words; ADR 0130 dismissed it by timing alone. Fixed by
holding the submitted future and a **bounded** wait (0.25 s ≈ 12 capture blocks) before
`shutdown(wait=False)`, keeping ADR 0104's abandon-on-timeout escape.

**The acceptance baseline's real state, recorded.** Across **422** stops only **249** reached
`Application shutdown complete`; **133** timed out into SIGKILL and 6 segfaulted. So ~41 % of stops
never finish teardown, **the SIGKILL — not the segfault — is the dominant abandoned-carrier route**
(roughly seven a day, exactly ADR 0181's predicted path), and `stage_systemd` is intermittently red
on its own. "9/10, the known witness 404" has been read as a constant for several cycles and is not
one. That matters because this arc has twice leaned on `acceptance.py` to catch what pytest
structurally could not.
>
> **CORRECTED by ADR 0184 (below).** "Roughly seven a day" is a lifetime average over a window
> in which the defect was fixed on day 9. The last SIGKILL in this journal is `2026-07-25T13:08:45`
> and there have been **zero in the 244 stops since**; the collapse tracks ADR 0127's deploy, not
> anything in this arc. `stage_systemd` is NOT intermittently red — do not read a `systemd` failure
> as expected noise.

**Self-review caught a fault of the same family before it shipped.** The first cut of the wait was
`concurrent.futures.wait`, which blocks the event loop for the whole bound — the very thing ADR 0181
and ADR 0182 spent two cycles removing, and `stop()` is reachable from `holder.rebuild` on a live
server. Now `asyncio.wait_for(asyncio.wrap_future(...))`; measured worst loop gap across a full
bound is **5.2 ms**, the probe's own tick. Pinned by a test.

**Could not reproduce on hardware, and that is reported rather than glossed.** `0 reproductions in
75 attempts` of `stage_systemd`'s recipe on master's code. The arithmetic says why it proves
nothing: the historical rate is 6/422 = **1.42 % per stop**, so 75 trials expects 1.07 events and
**P(zero) = 34 %**. A powered arm needs ~200 per side (P(zero) = 6 %) — over an hour each way, judged
disproportionate against evidence that does not rest on it. **So the fix is NOT verified by
reproduction; do not read the green acceptance run as that verification.** One live confirmation did
land: after deploying, the `HUPCL was CLEAR` warning did not fire, exactly as the `stty` measurement
predicted, exercising that path on the station for the first time.

**Counts.** pytest **2454 passed / 5 skipped** (from 2446/5, +8 = the new file exactly, with
`test_shutdown_budget.py` still green); vitest **14 files / 163 tests**, unchanged.

**`acceptance.py` after the fix:** `systemd` **PASS** (the stage under test), plus `presets`, `rx`,
`dtmf`, `auth`, `tx`, `split`, `services` — 8 PASS. `web` FAIL is the known `kv4p /healthz 404`;
`split-minus` SKIP. Zero `rx-read_0` segfaults since the deploy. Station restored and verified by
read-back **twice**, before and after a restart: 145.145 / TX 144.545 / 107.2 / FM / low, links down,
`uvk5_tune_persist` reported as found (`true`), witness untouched.

**An operational trap that has now cost three cycles — write the fix down, not the warning.** A
`pgrep -f "<script>.py"` wait-loop **matches its own command line**, so it never exits and reports a
finished process as still running. This session had a leftover one from the ADR 0182 cycle still
spinning hours later, and it silently blocked this cycle's restore. ADR 0182 recorded the warning and
the next loop was written the same way anyway. The fix is to match the **interpreter**, not the
script: `pgrep -f 'python3.*bench/acceptance.py'`, or use a pidfile.

**Next.** The out-of-process supervisor and the radio's own TOT are the only cover for power loss
and host death. `holder.stop()`'s two unguarded steps (`scan_runner.stop()`, `rx_pump.stop()`) sit
*before* `radio.close()`, which holds the only unconditional unkey — a raise in either skips it.

## The TX lockout wait owns the event loop (2026-08-04)

ADR 0182, branch `adr-0182-the-tx-lockout-wait-owns-the-loop`, from `origin/master` **9ed8771**.

**The redirect, first, because it is the point.** This cycle was briefed to fix `Recorder.write`
(ADR 0181's carried finding) and to run the loop-blocking sweep a third time, to "establish whether
there is a fourth". There is, and it outranked the assigned target, so the cycle was redirected with
the user's agreement. **That is the sweep working, not a deviation** — the brief asked the question
whose answer moved the cycle, and the ADR records it that way.

**The hazard.** `AiocBaofeng._await_tx_lockout` did `time.sleep(min(remaining, 6.5))` from inside
`ptt(True)` (`aioc_baofeng.py:660`, reached via `_key_on:1150`), and the loop-side keying paths call
into that synchronously from the event loop.

**Severity, established before designing, and it is NOT ADR 0181's.** The sleep runs *before*
`_key_on_locked`, which is what opens the stream and asserts the line — so the PTT line is low and
nothing is keyed. An **availability** fault, not a stuck carrier, and the ADR says so rather than
borrowing severity. Secondary: the arbiter is already latched `TRANSMITTING` (`_key_up` acquires
before `ptt(True)`), and `_reserve_the_wire()` brackets the sleep, so the cadence pollers are muted
across it too.

**It fires on this station, by configuration.** `tx_ready_at` has one assignment site, reached only
from `HybridTuner.apply()`'s persist branch — the existing tests already say `# storing costs the
lockout`. The station runs `uvk5_tuner = "hybrid"` and the **non-default** `uvk5_tune_persist = true`
(reported as found, not flipped), so every tune arms it. Each of `/frequency`, `/split`, `/tone`,
`/mode`, `/power` arms it **per call**; `apply_preset` collapses to one; scan and DTMF services arm
nothing. The station's own journal: **78** `holding key-up` lines over 19 days, **2.7 s to the full
6.5 s**, six of them in the three hours before the cycle started.

**Measured on hardware, two arms, same 6.49 s lockout:** `POST /ptt` (plain `def` → threadpool) served
**142** concurrent `/status` probes at **12.9 ms** worst; `POST /transmit` (`async def` → loop) served
**17**, worst **7123.5 ms**. Same wait, same duration — the fault is loop ownership, not the wait.

**Shipped.** The wait is **not shortened** — ADR 0142 shipped without it and every carrier row failed
on attempt #1. ADR 0177's bounded barrier does **not** reuse: it is bounded because on expiry the
station keys anyway, which is the opposite meaning for a hardware precondition. So the wait is made
awaitable through the seam that already existed (`tx_ready_in()`, already public and in `status()`),
and `_await_tx_lockout` is left untouched, finding nothing left to wait for. An optimisation of
*where*, never of *whether*: a call site that forgets degrades to the old blocking-but-correct
behaviour, never to an early key-up. Wired at `/audio/tx`, `POST /transmit`, and the Mumble relay.

**Green, with the residual attributed rather than excused.** 7123.5 → **1227.9 ms**, 119 probes
instead of 17. Then, with **no lockout armed at all**, the same route still blocked **1298.8 ms** — so
the remaining block is not the lockout but the pre-existing ~1.3 s synchronous key-up (wire barrier,
`0x0879` frame, ALSA open). The lockout's contribution to loop-block went from ~5.9 s to within noise
of zero; the rest is recorded as its own cycle, not claimed fixed.

**Still blocking, deliberately, named in the enumeration test:** the D-STAR bridge (`_emit_rx_pcm` is
sync inside two async callers, so the change is small — but it is the crossband keying path ADR
0090-0099 hardened, and that crossband is DISABLED pending a cold-boot re-proof this bench does not
run); and `POST /services/{digit}` / `POST /auth/session`, whose no-await shape is load-bearing for
serialization against the RX pump's `controller.step`.

**Counts.** pytest **2446 passed / 5 skipped** (from 2437/5, +9 = the new file exactly);
vitest **14 files / 163 tests**, unchanged — no web change.

**`acceptance.py`: `tx`, `services`, `auth`, `split`, `presets`, `rx`, `dtmf` all PASS**, `web` FAIL
is the known `kv4p GET /healthz 404`, `split-minus` SKIP. **`systemd` FAILED once and was chased, not
accepted**: the check was `stop under WS load: result = exit-code`, and the journal named it —
`status=139`, i.e. **SIGSEGV** in ALSA/PortAudio teardown. Re-run alone it passed **3/3**, and the
journal has **six** `status=139` exits all time with **five predating this branch** (07-26, 07-31,
08-01 x2, 08-03; branch deployed 08-04 16:51). Pre-existing and intermittent — which also means the
9/10 baseline is flaky, and a SIGSEGV skips the rest of the teardown including the unkey.

**`keyup_race.py` refused and was not substituted for.** Its witness precondition returned
`the witness did not answer /status (401)` — the kv4p witness on 8091 has its own token — and the
check runs *before* it stops the service, so nothing was disrupted. Not chased, because the diff
provably cannot move that instrument: `keyup_race` stops the service and drives
`create_radio("baofeng", ...)` directly, so it never loads `api/app.py`, `link/bridge.py` or `tx/` —
which is the entire diff. ADR 0181's "do not build a redundant before arm" reasoning, applied where
it genuinely applies.

**Two false negatives worth not repeating.** `systemctl is-active radio-server` and
`journalctl -u radio-server` both answered as though the station were down and had no logs — the units
are **user-scope** (`systemctl --user`), and the system scope holds a stale same-named unit whose
journal ends 2026-07-16. Separately, an `until ! pgrep -f "keyup_race.py"` wait-loop matched **itself**
and reported a finished process as still running. Test the capability, not the output text.

**Next.** `Recorder.write`, with the analysis already done (see ADR 0182 Findings): it is on the loop
at `rx/pump.py:267` and `tx/session.py:244`, waits only because `recording.enabled = false`, costs
**2.7 `write()` + 2.7 `seek()`** syscalls per frame because `wave` patches the header every time, and
needs `end_segment` **and** the capture timestamp carried in-band or segments land in the wrong file
with the wrong time. A FIFO cannot stage that wedge — `OSError: [Errno 29] Illegal seek`.

## A hung disk stalls the transmitter (2026-08-03)

ADR 0181, branch `adr-0181-a-hung-disk-stalls-the-transmitter`, from `origin/master` **5760390**.

**The hazard**, carried by name out of ADR 0180. `EventLog.handle` runs in `app.py`'s `_drain_log`
task **on the event loop** and called `JsonlSink.write` — a blocking `write` + `flush()` — inline.
Its docstring promised the ledger "never breaks the flow or a transmission", which is true of a disk
that RAISES (ADR 0018's isolation, and it works) and false of one that BLOCKS. The docstring did not
distinguish the two; both it and the class docstring above it are corrected in this PR.

**The blast radius, swept as the brief asked.** `EventLog.handle` has exactly two callers, both the
lifespan's (`_drain_log`, and the shutdown drain). No keying path calls it in its own stack — but the
loop it blocks carries `/audio/tx`, which calls `session.feed()` inline, so `TxSession._key_up` keys
from the loop and the `finally: session.close()` that **drops PTT** needs the loop to run. That is the
path named in the ADR: **a keyed transmitter the server cannot unkey.** `POST /transmit` is on the
loop too; `POST /ptt` is a threadpool route but is still blocked, because uvicorn must dispatch it on
the loop first. The only backstop is `TotRadio._fire` on its independent timer thread —
`tx.tot = 180.0` and `uvk5.tot = 180.0` on the deployed station, so up to a 180 s unattended carrier
that ends by *latching* streaming TX off. `tx.tot = 0` removes it entirely.

**Shipped.** `ThreadedSink` in `eventlog/sink.py` — ADR 0040's bounded-queue-plus-daemon-writer, the
shape `MultimonStream` already uses, applied to the last blocking write left on the loop. **Not**
`asyncio.to_thread`: its multi-worker executor reorders records, and awaiting each write to restore
order would make `_drain_log` suspend, destroying the property ADR 0180 proved and turning the ledger
into a slow consumer of a `DROP_OLDEST` queue — silent Part 97 loss. Measured, the one-way hand-off
costs **1.03 us** against **86.3 us** for a `to_thread` round-trip, i.e. less than the 1.9 us write it
replaces. Overflow drops the **newest**, inverting every other queue in the tree, so what survives is
a contiguous prefix and the last `ts` plus `dropped_records` pins the gap. `GET /status` gains a
`ledger` block; `write_errors` counts sink failures for the first time.

**The bound is derived, and the measurement overturned the plan.** `241 x 5 = 1205`. 241 is the
busiest one-second bucket in the station's own `radio-server.jsonl` (71,455 records over 455.9 h;
organic traffic is 1-2/s and the peak is a bench driver key-cycling `POST /ptt`). 5 s is
`GRACEFUL_SHUTDOWN_SECONDS`, now pinned in `test_entrypoint_tls.py` for the first time. The plan had
proposed inheriting ADR 0180's 160 — at 241/s that absorbs 0.66 s, so the measurement killed it.

**Red run, recorded.** A key-up waited **502 ms** behind a sink blocking 500 ms; PTT stayed asserted
**1000 ms** after the talker let go (a key-up queues two records, `arbiter_mode` + `tx_key_up`); the
loop was unavailable **500 ms**. After: **1.3 ms / 0.1 ms / 5.3 ms**, the last being the probe's own
5 ms tick — the loop was never blocked.

**Counts.** pytest **2436 passed / 5 skipped** (from 2420/5). vitest **14 files / 163 tests**,
unchanged — there is no web change in this cycle.

**BENCH — the hazard and the fix, both watched on hardware.** A second instance on port 8099 with
`server.backend = "mock"` (no RF, no hardware contention, the station's config untouched) whose
`logging.path` is a **FIFO with a holder that opens it read-only and never reads**, buffer shrunk to
one page with `F_SETPIPE_SZ`. It fills after exactly 65 records. What did *not* work is recorded too:
a full filesystem gives `ENOSPC`, which **raises** rather than blocks (the case ADR 0018 already
handles), and `logging.path` is not env-settable, so the rig needs its own config file.

| | red (`15dad1a`) | green (`3a2b5af`) |
|---|---|---|
| `GET /status`, n=15 | **inf / inf / inf** — every probe timed out | **min 5.3 · median 5.5 · max 6.2 ms** |
| `POST /ptt` completed | died at **#65** | **all 1500** |
| listen socket | `Recv-Q 14` — connections never accepted | served normally |
| SIGTERM | **did not stop in 30 s; SIGKILLed** | **teardown ran to completion** |

Red did not merely get slow: it stopped answering entirely, failing even the **TLS handshake** —
which is negotiated on the event loop — with 14 completed TCP connections sitting unaccepted in the
kernel's queue. Then it could not be stopped by SIGTERM, which in production means systemd SIGKILLs
at `TimeoutStopSec` and **skips the unkey**.

Green **watched the cap engage**, which is what ADR 0180 could not get on hardware: `queued` and
`deepest_queue` stop dead on `queue_maxsize` (1205) and `dropped_records` starts counting (229), with
`write_errors: 0` beside it — the sink was **blocking, not raising**, which is this ADR's whole
premise, measured. `events` stayed `deepest_queue: 2` / `dropped_deliveries: 0` across 3000 published,
so ADR 0180's bound never came near firing. The shutdown journal reads
`station ledger writer did not finish within 2.0s; 1205 record(s) were still queued and are lost.
The sink is blocked, not slow.` — the bounded join giving up, saying what was lost, and letting the
teardown finish.

**Key-up latency, on ADR 0177's own instrument** (`keyup_race.py --forced 0`, deployed branch):
1000 Hz recovery **0.989 / 0.990** against ADR 0177's control **0.989**; active span 4.51-4.52 s
against 4.42-4.52 s; witness 32/55 polls (0.58) against 48/83 (0.58); `keyed_with_wire_busy: 0`.
There is no separate before arm and that is deliberate — the diff contains **no backend change at
all**, so it would be measuring byte-identical code.

**Acceptance** 9 of 10 PASS; `web` FAILs only on the known `kv4p GET /healthz 404` (witness 42 behind,
deliberately not moved) and `split-minus` SKIPs. Identical to the master baseline. Station restored
and **verified by read-back twice**, once after a `systemctl restart`: 145.145 / TX 144.545 / 107.2 /
FM / low, links down. `uvk5_tune_persist` reported as found (`true`), **not flipped**; so were
`tx.tot = 180.0` and `uvk5.tot = 180.0`. Witness left at `a6a4cd4`.

**One contaminated run, reported rather than dropped:** the first green attempt was launched twice, so
two processes fought over 8099. Caught by `address already in use` in the rig's log; re-run clean, and
only the clean run's numbers are quoted.


**Carried, not fixed:** `Recorder.write` is the same hazard one module over — `rx/pump.py` calls it
from the pump's loop task, so a hung disk stalls RX capture, the browser fan-out and DTMF decode the
same way; found by this cycle's blast-radius sweep, its own cycle. `tx.tot = 0` removes the only
backstop. The lifespan's shutdown drain is still unbounded in count. A SIGKILL now loses queued
records where the synchronous flush did not — bounded by the write latency, stated in `api.md`, and
the price of the trade. `PolledGate` still has no staleness expiry. The ledger is 95% bench noise
(68,139 of 71,455 records are `tx_key_down` from drivers calling `POST /ptt {"on": false}`).

## EventHub's unbounded queue (2026-08-03)

ADR 0180, branch `adr-0180-eventhubs-unbounded-queue`, from `origin/master` **5c2a688**.

**The hazard.** `EventHub.subscribe()` handed out a plain `asyncio.Queue()` — no `maxsize` — and
`publish()` a plain `put_nowait`. A subscriber that was still registered and still slow grew its
queue forever. It was a *stated* choice that outlived its reasoning: `AudioHub`'s docstring names it
as the contrast it departed from, and the `/events` handler's own comment had already written the
finding down and left it. ADR 0171 reaped the zombie subscription; a peer that is still there and
still slow is what was left.

**The policy, argued.** Drop-oldest (AudioHub's) was rejected: a PCM frame is fungible and an event
is not — dropping the oldest is dropping the `alarm` to keep the `status`, and it leaves a
*connected* client rendering stale state with no way to know. A cap that raises was rejected
outright: `QueueFull` would reach a REST handler, the RX pump or the arbiter, and ADR 0018's rule
holds verbatim — a passive consumer is never a place a fault comes from. **The subscriber is dropped
instead**, and the thing that makes that cheap is the `status` snapshot `/events` already sends on
connect: **a reconnect is a resync.** The client gets an `overflow` frame saying what it missed, a
`1013` close, and correct state about a second later. The backlog is *discarded*, not delivered —
draining it to a consumer that slow takes longer than reconnecting does.

**The log drain, checked as briefed, and the answer is "no".** `_drain_log` cannot be the slow
consumer: `asyncio.Queue.get()` does not suspend on a non-empty queue and `EventLog.handle` is
synchronous, so it spins its queue to empty inside one event-loop step and its backlog is bounded by
the largest *synchronous* publish burst (two), not by time. It also must never be dropped — no
socket, no snapshot, and dropping it would stop the Part 97 log permanently and silently. So the
policy is a property of the **subscriber**, defaults to the conservative `DROP_OLDEST`, and every
call site is pinned by a source scan in `test_relay_subscribers`'s idiom.

**The bound is derived, not chosen: `4 × 40 = 160`.** 4 is measured — the busiest one-second bucket
on `/events` through a full `acceptance.py` run on the deployed station (25 frames in 116 s; an idle
station published *nothing at all* in 60 s beyond the connect snapshot). 40 s is ADR 0171's measured
`ws_ping_interval + ws_ping_timeout`. So a queue can only fill by being slower than real time for
longer than the server takes to give up on a peer that is not there at all.

**`GET /status` grows an `events` block** — the ADR 0179 scattered shape — with six counters named
for what they count: `published` (the denominator), `subscribers`, `queue_maxsize`, `deepest_queue`
(a high-water mark, **not** a current depth — the standing evidence for whether the arithmetic still
holds), `dropped_subscribers`, `dropped_deliveries` (per-subscriber deliveries, **not** distinct
events). Each has a documented nonzero meaning in `api.md`. **Nothing renders it**, and that is
argued: the drop self-heals in ~1 s, and a row that is a reassuring zero on every healthy station is
a row people stop reading. The journal line per drop is the durable trace.

**FAIL-FIRST red, on the behaviour and not a signature.** The first red was a `TypeError` on the new
keyword, which proves nothing about queues; the parameter was added accepted-and-ignored so the
recorded red is `a subscriber that never drained holds 250 events after 250 publishes — the queue is
unbounded`, with `maxsize=0` in the failure's own repr.

pytest **2420 passed / 5 skipped** (from 2402/5); vitest **14 files / 163 tests** (from 14/160).

**BENCH — and the honest headline is that the cap did NOT engage on hardware.** Deployed **15dad1a**
(`0 0` against the pushed branch, clean; `queue_maxsize: 160` read back live, so the new code is the
running code; the JS bundle hash is unchanged because the only web change is a test). It took three
attempts to build a genuinely slow consumer, and each failure is a finding: a `websockets` client
that never calls `recv()` is not slow (the library drains the socket — 8000 events, `deepest_queue`
peaked at **2**); `transport.pause_reading()` reports `is_reading() == False` and still produces no
backlog (server `Send-Q` stayed **0**), because asyncio's SSL transport keeps reading the raw socket
underneath. A raw TLS socket with a 4 KB receive buffer that never reads finally wedges — and the
0.2 s timeline says what really happens:

```
  t=  0.0s  subs=2 deepest=  4  kernel_send_q=0
  t= 11.7s  subs=2 deepest=  4  kernel_send_q=1791406   <- send buffer saturated
  t= 40.1s  subs=1 deepest=  4  kernel_send_q=1791406   <- the subscription is gone
```

Between 11.7 s and 40.1 s, ~8,700 events were published to a peer with 1.79 MB stuck in its socket
and **its queue never went past 4**; `send_json` returned every time and server RSS was flat. **uvicorn
accepted those sends and discarded them** — the same silent-drop this repo measured for a *reset*
transport in ADR 0170/0171, now measured for a *wedged* one. And `subs 2→1` at **t=40.1 s** is ADR
0171's reap landing exactly on its measured `ws_ping_interval + ws_ping_timeout`, watched live for
the first time.

So on this stack the `DROP_SUBSCRIBER` path is **unreachable through a WebSocket peer**. The mechanism
is proven in-process (162 published → 3 frames delivered → `1013`) and **not** on hardware, and the
ADR says so rather than dressing it up. What the hardware run does prove: the bound holds live
(`deepest_queue` 4 of 160 across 133,828 published events), all six counters read correctly through
the API, and the thing protecting this station from a wedged peer today is the 40 s reap. The change
still stands — the queue's boundedness now comes from this repo's code instead of resting on an
undocumented discard in somebody else's server — and the *reachable* overflow path was never the
socket but a subscriber slow for its own reasons, which is the ledger drain, which is why it keeps
`DROP_OLDEST`.

The driver was `POST /ptt {"on": false}` ~11,000 times, and it **never keyed**: `wire.key_ups` 0
before and 0 after, `transmitting` false, `transport.alive` true.

**Carried, not fixed:** `JsonlSink.write` does a blocking `write` + `flush()` on the event loop, so a
hung disk stalls everything including PTT — the ledger's *real* hazard, found by the check the brief
asked for, and its own cycle. `PolledGate` still has no staleness expiry. Nothing throttles a
reconnect flap. `overflow` is not a ledger record. `rx_demand.requested` read `1` on an idle station
with no browser open during the BEFORE probe — consistent with ADR 0171's bounded-not-instantaneous
window, but unconfirmed. **Events lost below the hub are invisible to the hub** — a slow client can
miss thousands inside uvicorn while `dropped_deliveries` stays `0`; `api.md` now says so beside the
number, because this cycle added the counter and so owns saying what it cannot see.

## Three instruments nobody can read (2026-08-04)

ADR 0179, branch `adr-0179-three-instruments-nobody-can-read`, from `origin/master` **0b21d57**.

**The point was a reader, not more measurement — and this diff adds no counter anywhere.** ADR 0178
closed by recording that the counters already in the tree reached nobody, and refused to add a fourth
for that reason. This gives the existing ones a surface, a documented nonzero meaning, and a
rendering decision.

**The map was corrected before anything was built, and both corrections shrank the cycle.** ADR
0177's `wire` counters are not unread, they are **unrendered** — they have been on `/status` since
0177 and `api.md` already documents each field, including the distinction this cycle was briefed to
draw (`wire_busy_at_key_up` = the race fired and the drain handled it; `keyed_with_wire_busy` = the
drain expired and the station keyed anyway, "the only case that can still reach the air damaged").
That prose is right and was left alone. And **`PolledGate` has no `stats()` at all** — ADR 0178 gave
it log lines only, on purpose. So the unreachable numbers were **nine**, not three instruments: six
from `RssiPoller.stats()`, which had zero production callers, and three of six the bridges compute
and drop at the last step before HTTP.

**Placement: scattered, each number beside the state it explains, inside its own nested block — no
diagnostics block.** `pa`/`transport`/`wire`/`broadcast_fm` are already that shape; ADR 0170 already
decided against grouping when it kept `rx_demand` out of `slots`, so one block would not lend an
un-reaping number its neighbours' trustworthiness, and a diagnostics block does that in reverse.

**Shipped.** `RadioStatus.rssi_cadence` — `polls`/`unknown`/`skipped`/`pause_errors`/`age_s`/
`stale_after_s`, `null` where nothing polls, and **deliberately no reading**: `status.rssi` is the one
signal-strength number and two that read alike get confused. `deafened_polls`/`deafened_skipped`/
`deafened_pause_errors` on both bridges. Every counter documented in `api.md` with what a nonzero
value means **and what it does not** — `pause_errors` says the guard is broken, never that a
transmission was damaged. Rendered: **only the two that mean something is wrong now**, zero rows on a
healthy station; the other ten stay in `/status`, argued rather than omitted.

**Numbers.** pytest **2402 passed / 5 skipped**, from 2384/5. vitest **14 files / 160 tests**, from
14/155. An existing exact-shape assertion in `test_link_api.py` went red on the new keys, which is
that check working.

**Bench — the surface was proven, not assumed.** Station **54f9e56 → 4a58875** (`0 0`), bundle
`index-CUx4HnZr.js` → `index-DSwQ1qxo.js`. `rssi_cadence` **absent on master and present on the
branch**, read over `https://`. One 1.2 s over moved `wire.key_ups` **0 → 1** and
`rssi_cadence.skipped` **0 → 3** while `unknown` **stayed at 9** — the "a deliberate skip is not a
failed poll" claim holding on hardware. After a full acceptance run: `polls 143 / unknown 40 /
skipped 118`, `key_ups 7`, every race counter still `0`. The expiry case was **caught live** rather
than argued — `transmitting=False, rssi=None, age_s=3.85 > stale_after_s=1.5` — after a first attempt
printed the conclusion from a fixed string while its own sample showed `age_s: 0.75`. The three
bridge keys were read through `GET /link/status` over a link to the **Murmur on the box itself**, not
the public demo server, then taken down and asserted down. `acceptance.py` **9/10**, `split-minus`
SKIP, `web` re-run alone confirming only the known `kv4p /healthz` 404. The witness is **0 ahead / 42
behind and dirty — not moved**, which is also why that 404 persists.

**Bench left as found.** 145.145 / TX 144.545 / 107.2 / FM / low, frequency before split, verified by
read-back **after a service restart**; `tx_ok: true`, transport alive, links and D-STAR down asserted
from the endpoints. `uvk5_tune_persist` reported **as found (`true`)**, not flipped. 74 `uvk5: tuned
rx` lines this session — under `hybrid` with persist on, each is an EEPROM write, said that way
because the journal has no eeprom-specific line to count.

**Open findings, recorded not fixed.**
- **`PolledGate` still has no staleness expiry.** Unchanged from ADR 0178 and restated so nobody
  reads this cycle as having closed it. Its successor's shape — `RssiPoller`'s `STALE_AFTER`
  return-`None` — is now *published* as `stale_after_s`, so the contrast is visible in one response.
- **`pause_errors` is structurally `0` on both shipped cadences**, because `cadence_paused` is two
  attribute reads and cannot raise. An instrument for a hook that does not exist yet; `0` here is not
  evidence about anything else, and `api.md` says so.
- **`RxPump`/`PolledGate` duty and drop counts are unmeasured** — the numbers that would have made
  the ADR 0125 fault visible without a bench script. Carried named rather than added.
- **The bridges' own `tx_stats()` counters have no renderer either** — the same shape `wire` was in,
  one subsystem over. Deliberately not widened to.

## Checks that silently answer the safe-looking default (2026-08-03)

ADR 0178, branch `adr-0178-checks-that-answer-the-safe-looking-default`, from `origin/master`
**54f9e56**.

**The class, not the instance.** ADR 0177 found `getattr(radio, "transmitting", False)` inert against
a class with no such attribute. This audits all **85** `getattr`/`hasattr` sites in `radio_server/`
against a rubric — reachable / which way the default points / consumed / **distinguishable** — where
a "harmless by construction" verdict must **name its enforcer**, because nothing type-checks this
repo and a `Protocol` declaration is therefore not one.

**The structural question is answered in the negative, and it is measured rather than argued.**
`isinstance(TotRadio(MockRadio()), Radio)` is **False** on this interpreter: every production radio is
`TotRadio`-wrapped, `TotRadio` forwards through `__getattr__`, and CPython's protocol
`__instancecheck__` resolves members with `inspect.getattr_static`, which by design does not invoke
it. A conformance guard at the composition root would call all four backends non-conforming,
unanimously and wrongly. ADR 0158's guard generalises only to **tuners**, which are not wrapped.
Static typing forecloses itself, and `TotRadio` already owns the `__getattr__` slot. So defensive
`getattr` is right here — and "explicit rather than defaulted" turns out to be a rule the repo
already keeps: of 85 sites only **four** let a value default stand in for a real answer.

**Fixed (5 live, none on the keying path — nothing under `radio_server/tx/`, no `_key_on`, no
`ptt`/`transmit` body, no `docs/api.md` or `test_docs_contract` delta).** One resolver for the pause
source, so the warning and the cadence cannot disagree, plus the `POST /radio/select` call site the
warning never had — and the **test it never had**, which is ADR 0167's shape one level out.
`PolledGate`'s two silent failures made readable **as log lines only**: counters there reach no
endpoint, which would repeat the sin this cycle names against itself. `pause_errors` on both
cadences, `null` where no hook is wired — **nonzero means the guard is broken, NOT that a
transmission was damaged**. A 501 that asserted the uvk5 backend has no serial transport when
`Uvk5Transport` has `alive`/`reader_error`/`reconnect()`. And a docstring stating the opposite of its
own code.

**Two instruments, because prose does not re-check itself.** A source scan pinning every
value-defaulted probe — which **caught the cycle that wrote it**, since extracting the resolver took
the count from four to three — and a cross-backend **consequence** assert (does the composition root
warn about this radio), green by design, that goes red the day `Uvk5Radio` gains a
`probe_broadcast_fm`. ADR 0177 recorded that hazard in a HANDOFF paragraph; a paragraph is true the
day it is written and nothing re-checks it.

**Two things the plan got wrong, corrected by checking.** `volatile` at `aioc_baofeng.py` looked like
the least-protected keying-path default; it is the best protected — declared on `Uvk5Tuner`, which
*is* isinstance-checked, and pinned by an existing test that was verified to actually bite (a tuner
missing only `volatile` fails the check). And `PolledGate` is **not** live on the deployed station:
the bench's `[baofeng]` block sets `squelch_mode = "audio"`; the `"cat"` that builds a `PolledGate` is
in the `[uvk5]` block, one `POST /radio/select` away.

**One test passed for the wrong reason and was fixed before it could lie** — the
broken-hook-vs-working-hook comparison, because `age_s` is a live `monotonic()` delta and the two
dicts differed by microseconds. Both new instruments were then proved capable of firing rather than
merely observed green.

**Numbers.** Red run **20 failed / 17 passed** plus a collection error. pytest **2384 passed /
5 skipped**, from 2351/5. vitest **14 files / 155 tests**, untouched.

**Bench — the station is back on master.** Before: `19cf0f5` on `adr-0177-the-key-up-race-measured`,
0 ahead / 2 behind, clean. After: **`54f9e56`, `0 0`**, new bundle served. The bare
`update-radio-server.sh` did not refuse, because the merged branch is now an ancestor of master —
that guard's designed "back to the mainline" path. **ADR 0177's control arm reproduces on master:**
0.989 / 4.52-4.62 s / **45 of 81** carrier polls against the branch's 0.989 / 4.42-4.52 s / 48 of 83,
with 438074 B the exact top of ADR 0177's recorded range. Cadence arm: **0 of 3 polls reached the
wire**, all three `SKIPPED (paused)`, reservation cost **0.01-0.02 ms**. `acceptance.py` **9/10**,
`split-minus` SKIP, `web` re-run alone confirming only the known `kv4p /healthz` 404. The **witness
was not moved** — it is 42 behind and dirty, which is also why that 404 persists.

**Bench left as found.** Station restored to **145.145 / TX 144.545 / 107.2 / FM / low**, verified by
read-back (rssi 158, transport alive, `tx_ok: true`), links and D-STAR down, `uvk5_tune_persist`
reported **as found (`true`)**, not flipped. 45 EEPROM writes this session. Note for the next cycle:
a restart reports `frequency: null` rather than a remembered value — that is ADR 0155's "a
reconnecting host asserts; it does not assume", not a regression.

**Open findings, recorded not fixed.**
- **`PolledGate` has no staleness expiry.** The log lines cover the *event*; the *standing state* is
  still uncovered, so an hour after its thread dies a gate reads exactly like a quiet band and no log
  line reaches whoever is looking. `RssiPoller.reading()`'s `STALE_AFTER` return-`None` is the
  successor's shape. **This cycle did not close `PolledGate`.**
- **Three instruments measure things nobody can see**: `RssiPoller.stats()` has no production caller,
  the ADR 0177 `WireStats` counters have no operational reader, and both bridges drop `skipped` and
  `polls` — ADR 0176's whole deliverable. One small cycle of its own, **not another counter**.
- `Uvk5Radio` still exposes no `transport_health`/`reconnect_transport`, so ADR 0166's mechanism is
  off on a shipped backend; only the false 501 sentence was corrected.
- The broadcast-FM capabilities are earnable only from the constructor's boot assert; the log now
  says so and names the remedy.
- `detects_signal` defaults trusting in two places; `disarm_rescue` is ADR 0164 finding 2's shape;
  the `@property`-raising-`AttributeError` trap is empty rather than absent.

## The key-up race is real, and it costs the whole transmission (2026-08-03)

ADR 0177, branch `adr-0177-the-key-up-race-measured`, from `origin/master` **257daef**.

**What this closes.** ADR 0176's audit found both cadences guarding the shared AIOC wire with a
check-then-act pause and recorded it as narrow, on the grounds that 0176's clean paused arm bounded
it. That was right about the rate and wrong about everything else.

**Reading `_key_on` moved the window before anything was measured.** The flag was set *after* the
DTR assert; `open_playout_stream` opens **and starts** the audio stream two steps before the line
goes high; and nothing serialises the halves — `_wire` guards frames while the assert is a lockless
`TIOCMBIS` ioctl on the same fd.

**Sampling is the wrong instrument here.** The natural rate is ~0.5 % of key-ups (a poll must start
in the ~2.5 ms between the key-up's own frames releasing the wire and the assert), so seeing it
naturally needs ~600 key-ups. `scripts/bench/keyup_race.py` makes the collision certain instead:
exactly one exchange timed to straddle the assert, overlap **evidenced** from timestamps at both
ends, one exchange only so it cannot be confused with "more traffic hurts".

| arm | 1000 Hz | audio | **witness carrier** |
|---|---|---|---|
| control `--forced 0` | 0.989 | 4.42-4.52 s | **48 / 83 polls** |
| hazard `--forced 1` | **0.000** | **0 B / 0.00 s** | **0 / 81 polls** |
| fix `--collide-via cadence` | 0.989 | 4.42-4.53 s | **47 / 82 polls** |

The control matches ADR 0176's baseline **to the byte** (434852 B), which is what says the
borrowed-port rig reproduces the deployed station. **The finding is not damaged audio — the
witness's hardware carrier detect never saw RF.** One exchange in flight across the assert and the
station does not transmit, while `transmit()` returns normally and the pacer reports no error. The
carrier poll was added *because* the first run returned 0 bytes and a capture-only rig cannot tell
that from "no RF"; those are different findings and only one is true.

**The fix.** `_key_on` reserves the wire first: a `_keying` **counter** (a flag would let concurrent
key-ups from the station ID, `/transmit` and a bridge disarm each other) raised before any refusal,
so no new poll can start; then a **bounded** barrier for what was already in flight, which on expiry
**keys anyway** and counts that separately. `KEY_UP_WIRE_DRAIN_S = 0.3` is derived — 3x the measured
96.3-97.7 ms exchange, a twentieth of the lockout `_await_tx_lockout` already accepts, and
deliberately short of the transport's ~3.0 s pathological bound. **Added key-up latency measured:
0.01 ms with the wire free, 96.8 ms draining a real in-flight poll.**

**Two things were tried and backed out, both on evidence.** Bounding the key-up's *own* dock frames
turns a delayed key-up into a **refused** one (a wire timeout reads as "may be deaf" under ADR
0161) — silence where guardrail 5 requires a transmission. And the instrument's first version closed
its interval when the lead-in was queued, recording a deliberately forced collision as **zero** on
all three trials; it now runs to un-key.

**Live-service arm, 40 key-ups:** `key_ups_with_wire_traffic: 0`, `keyed_with_wire_busy: 0` —
nothing got past the reservation — with `key_ups_that_waited_for_the_wire: 18` and a longest wait of
41.7 ms.

**Numbers.** Red run 10 failed / 0 passed. pytest **2351 passed / 5 skipped**, from 2330/5. vitest
**14 files / 155 tests**, untouched. `acceptance.py` **9/10 PASS**, `split-minus` SKIP, `web` FAIL on
the known witness `kv4p /healthz` 404 (re-run alone to confirm it is that check and nothing else);
`tx` 0.989 / 4.52 s, `services` 5.2 s / speech band 0.98.

**Open findings, recorded not fixed.**
- **ADR 0176's pause hook is inert on the `uvk5` backend**: `app.py` resolves it through
  `getattr(radio, "transmitting", False)` and `Uvk5Radio` has no such attribute, so it answers
  `False` for ever. Harmless only because that backend has no `probe_broadcast_fm`. This cycle adds
  a WARNING for the silent-degradation class rather than fixing the instance.
- The `uvk5` backend has the same physical hazard and no reservation anywhere (carried from 0176).
- A key-up can still be delayed by a concurrent **tune**, bounded by that caller's own budget.
- `key_ups_that_waited_for_the_wire` ran at 18/40, above a naive duty-cycle estimate; the cadence's
  wake times are correlated with the key-up cycle. Not chased, and it does not affect the race rate.

**Bench left as found.** Station restored to **145.145 / TX 144.545 / 107.2 / FM / low** and
verified (rssi 161, transport alive, `tx_ok: true`), links disconnected, `uvk5_tune_persist`
reported **as found (`true`)**, not flipped.

## The broadcast-FM cadence gets the same transmit exception (2026-08-03)

[ADR 0176](adr/0176-the-broadcast-fm-cadence-must-not-read-while-transmitting.md) · branch
`adr-0176-the-broadcast-fm-cadence-must-not-read-while-transmitting`

**ADR 0175 paused the cadence it had just built and left the older one alone.** Its own finding 1
said what should have happened next: *anything else that puts steady serial traffic on the AIOC
needs the same treatment or the same measurement.* `BroadcastFmPoller` is that anything else — same
handle, `0x0879` every 2.0 s, no pause of any kind — and its lifecycle is the worst one for this
hazard: the refcount is raised on **any bridge connect**, unconditionally, and held for the life of
the bridge. It runs straight through every relayed over and every unattended station ID.

**The numbers did not transfer, so they were measured.** At 38 bytes per exchange against 32, a
quarter as often, it might not have reproduced at all. Three arms, because two would not have
settled it: this cadence cannot be switched on without a bridge, and a bridge is itself a confound.
The lever was a Murmur already running in Docker on the box and the station's own local entry.

| arm | code | link | cadence | `tx` audio | announcement |
|---|---|---|---|---|---|
| **B** baseline | master | down | not running | **4.42 s** | 5.4 s |
| **A** hazard | master | up | unpaused | **2.01 / 2.61 s** | 0.8 / 1.6 s |
| **A′** control | branch | up | **paused** | **4.42 s** | 5.2 s |

A was run twice to show it reproduced; A′ lands on B to the byte (434852 against 434854). **Bridge
exonerated, cadence convicted.** The failure's *shape* differs from 0175's and says why: tone
recovery stays at 0.98 while barely half the audio arrives — the transmission is **cut short**, not
garbled. Four contention events per over instead of nine, each hard enough to end the stream. The
poller's own `unknown` counter went 8→13 across arm A and sat at 3 in A′: those unknowns *were* the
mid-over polls.

**Skipping costs nothing, and unusually the claim is exact.** Checked from source rather than
inherited from 0175, on three legs: no key-up path can reach the poller (`clear_broadcast_fm` is the
documented sole writer of the block `refuse_if_deafened` reads); the mute is RF→network only (three
call sites, all in `_rx_to_mumble`/`_rf_to_reflector`); and — decisively — the firmware refuses
`0x0879` with `ERR_TX` for the whole time the station is keyed, so **every skipped poll could only
ever have returned `None`**. A paused cadence and an unpaused one reach the first post-over poll in
identical states. That kills the trailing-edge objection this cycle expected to price, and the
front-panel `F+0` window does not widen. Polling once on resume would close a window master also
has — an improvement beyond the fault, available and deliberately not taken.

**The wiring trap.** `AiocBaofeng` grows a public `transmitting` property documented as a plain
no-I/O flag read, because the poller is built one layer up over a generic `radio` and both obvious
spellings do I/O: `status().transmitting` performs a **serial register read** on the `uvk5` backend,
and `ptt_line_asserted` reads the kernel's line state. A pause check that does I/O to decide whether
to do I/O rebuilds the fault one layer up.

**The 18/18 citation is corrected in both places it appears.** `uart_while_streaming.py` asked one
direction of harm — does a running sound card break the UART? — and every round trip answered. It
never asked the reverse, and the reverse is where the damage is. It licenses **sharing the handle**,
which is all it ever claimed; it does not license putting frames on the wire mid-over. The
experiment's own record is left intact and ADR 0142's row gets a scope annotation. *(Also corrected:
0175's "12-byte register read" — 12 is `build_frame`'s overhead with params excluded; the exchange
is 16 out and 16 back.)*

**Audit, reported not fixed.** The two pollers are the only scheduled writers on that handle and
both are now inhibited. Key-up frames all land before the DTR assert by construction; the transport
reader never writes; `TransportWatcher` deliberately never polls the radio. Scan and
`PolledGate`/`CatBusyGate` are **structurally unreachable** on this backend (no `Capability.SCAN`;
`squelch_mode = cat` rejected at load), so the audit closes them rather than deferring them.

**Findings.** `RssiPoller`'s own guard is **check-then-act** — it reads `paused()` then issues a
request with a 1.0 s timeout, while `_transmitting` is set only just before the DTR assert, so a
poll can still be holding the wire as the line goes high. Narrow, real, and a defect in ADR 0175's
fix found by auditing it; closing it means asserting DTR while holding `_wire`, which is a keying-path
change deserving its own measurement. Also: the cadence starts on any bridge connect including
receive-only entries; `controller.poll` is a dead config key here; and the `uvk5` backend has the
same physical hazard with no pause anywhere.

**Numbers.** Red run **8 failed / 21 passed**. pytest **2330 passed / 5 skipped** (from 2322/5);
vitest **14 files / 155 tests**, untouched. `acceptance.py` **9/10 PASS** with the link up,
`split-minus` SKIP, `web` FAIL on the known witness `kv4p /healthz` 404. Station restored to
**145.145 / TX 144.545 / 107.2 / FM / low**, link left disconnected as found, `uvk5_tune_persist`
reported **as found (`true`)**.

## Signal strength on the deployed backend (2026-08-03)

[ADR 0175](adr/0175-signal-strength-on-the-deployed-backend.md) · branch
`adr-0175-signal-strength-on-the-deployed-backend`

**`status.rssi` was `null` on the backend the station runs, and the reading was one `0x0851` away
the whole time.** `AiocBaofeng` already hands its own open AIOC handle to a `Uvk5Transport` for
tuning, and `uart.c:1504-1523` dispatches a register read in the ordinary non-blocking path —
outside `0x0870` full control, arming no lockout, changing no state. What stood in the way was a
written position refusing register reads on this path three times over (`aioc_baofeng.py:441-447`,
ADR 0155, ADR 0132's *"take whatever state you find"*). That refusal is about **seeding a belief the
radio owns** — modulation, split, band — where a leftover value becomes a wrong decision later.
RSSI is a measurement nothing decides on, so it does not reach it.

**Measured before anything was designed, with the radio under test never keyed.** Service stopped to
borrow the port, opened through `transport.open_serial` so DTR stays low (it is this station's PTT
line). Silence **103-114** raw counts, witness carrier **310-311**, **72/72 answering each way**,
populations separated by 196 counts, and 48/48 answering with the sound card streaming. That
carrier figure is the same ~311 ADR 0122 measured under *full dock control*; the floor matches ADR
0132's per-band 107 on 445.800, and the deployed station reads **161-170 on 145.145** — **the floor
moves with the band**, so nothing may hardcode one.

**Then the bench rebuilt the design.** The first deploy shipped a 0.5 s cadence with no transmit
exception, and `acceptance.py` failed three stages where the last cycle failed one. Bisected against
master, same station, same frequency, same witness, minutes apart: the witness's 1000 Hz tone
recovery went **0.989 → 0.026**, a 4.4 s transmission arrived as **0.70 s** of audio, the time
announcement came out **0.9 s instead of 5.3 s**. The AIOC is one USB composite device — CDC serial
and audio share a cable, a controller and the K1 jack contacts — and a 32-byte register exchange every
half second wrecks the isochronous audio-out feeding the transmitter. `uart_while_streaming.py` had
measured dock frames surviving a running sound card 18/18, which is a **different claim**: it proved
the frames got through, never that the audio did, and `aioc_baofeng.py:495-504` cites it for the
broader one. The cadence now **pauses entirely while `_transmitting`**, which costs nothing because
`status()` reports `null` there anyway, and both stages came straight back to master's numbers.

**What shipped.** `Uvk5Transport.read_register` (one match predicate, `Uvk5Radio` delegates
unchanged); `SetVfoTuner.read_rssi` with `wire_timeout=0` so it always yields to a key-up, reporting
a raw `0` as **no reading** (ADR 0132 measured 0 as the receiver switched off, and the floor is
~107); `RssiPoller`, a `BroadcastFmPoller`-shaped 0.5 s cadence that pauses while keyed and
**expires** after 3 intervals — the one thing `PolledGate` and `BroadcastFmPoller` do not need,
because they hold a verdict about a state that persists and this holds a measurement of a moment;
and `doctor --rssi` is no longer uvk5-only, so the instrument that took the measurements is the one
that ships. **`busy` stays `False` and the squelch stays VAD** — this is a diagnostic number, not a
carrier-detect. No UI, no new API fields, no firmware.

**Numbers.** Red run **10 failed / 4 passed** (the four are negative pins that pass trivially while
`rssi` is always `null`). pytest **2322 passed / 5 skipped**, from 2305/5. vitest **14 files / 155
tests**, untouched. Bench: `/status.rssi` 161-170 on the restored 145.145; `rf_listen.py` through
the deployed HTTPS API at 445.800 read **silence 48/48 polls, 104/111, median 106** and **carrier
48/48, 317/331, median 327**, with `squelch open 0/48` both ways as the pin that `busy` was not
touched. `acceptance.py` exit 1, **9/10 PASS**, `split-minus` SKIP, `web` FAIL on its one known
check (`kv4p /healthz` 404 — the witness runs older code, since `update-radio-server.sh` does not
update it). Station restored to **145.145 / TX 144.545 / 107.2 / FM / low**, verified;
`uvk5_tune_persist` reported **as found (`true`)**, not flipped.

**`tests/test_baofeng_rssi.py` is the harness that did not exist** — the first test anywhere to wire
`FirmwareFakeSerial` into `create_radio("baofeng", ...)`, so backend, `HybridTuner`, transport and
codec are all real. `test_aioc_baofeng_tuning.py`, all 82 KB of it, injects a `SpyTuner` over a
serial fake with no `read`/`write` at all. Making that work needed the fake to answer `0x0877` and
`0x0879`, which the deployed **F9** radio answers and the fake did not — it was modelling a radio
older than the bench's, and charging every such test a 3 s timeout for it (45.7 s → 3.5 s).

**Open, recorded in the ADR rather than fixed.** Ten `0` samples in the first probe's streaming stage
were **never reproduced** — 391 further samples across three deliberate attempts produced none, and
the open/close-cycling hypothesis is refuted; the design is conservative regardless. `kv4p`'s `rssi`
is non-null but reads `0` while cleanly demodulating, which is a worse version of the fault this
cycle refused to commit. `aioc_baofeng.receive()` still discards the xrun flag ADR 0125 fixed on
`uvk5` only. An S-meter, a live squelch threshold and scan-stop-on-activity are now **reachable** on
this station; none of them was built.

## The transmitter must not key on an unknown frequency (2026-08-03)

[ADR 0174](adr/0174-the-transmitter-must-not-key-on-an-unknown-frequency.md) · branch
`adr-0174-no-keying-on-an-unknown-frequency`

**The model was lying to a guard that was already there.** `Uvk5Radio.__init__` seeded
`_frequency` from BK4819 registers 0x38/0x39 and adopted whatever came back, so a register file
answering `0` said *the frequency is 0 Hz* — a different statement from *I do not know where this
radio is*, and the only one of the two that gets past `_key_on`'s `self._frequency is None`. ADR
0173 measured `POST /ptt` **200** on a radio reporting 0 and correctly refused to probe it.

**What that would have done is settled from source, with nothing keyed.** `Dock_ForceTx`
(`uart.c:778`) never calls `BK4819_SetFrequency` and the dock TX chain validates no frequency, so
the synthesiser stays wherever the host left it. At 0 the host's own `_correct_tx_band` reads it as
VHF and forces the VHF LNA bit and gain byte while the firmware raises the PA rail on a bias
calibrated for a different frequency: **PA up, band chosen, synthesiser at DC.** No BK4819 datasheet
in either tree, no divider for 0 — whether the PLL never locks or the VCO sits at a rail is
undocumented, and that uncertainty is the argument for never reaching the write. The backend was not
silent either: `_correct_tx_band` has logged *"transmitting on 0 Hz … radiated power is not
characterised"* since ADR 0132 and keyed anyway. Knowing was never wired to refusing.

**So the fix is upstream of the guard.** `_seed_frequency` runs the existing `_validate_frequency`
over the read-back and reports `None` when it is not a frequency — the posture `_seed_reg30` (ADR
0132), the never-seeded split (ADR 0133) and `_pa` (ADR 0134) already take, and the rule `base.py`
had already written down: *0 Hz is not a frequency*. A guard on `ptt` would have had to write the
same validation, fixed one call instead of five, kept two definitions of "a frequency", and left
`/status` telling an operator the station was tuned to 0 Hz for the life of the process. **"Unknown"
means the read is not a frequency, not that the host has not tuned**: a valid read-back is the
radio's own VFO and keys exactly as before.

**Where it was live is narrower than expected, and worth knowing.** The server was safe by accident
of `uvk5.frequency` being REQUIRED. `doctor --key-test` and `--tx-tone` keep a `frequency: None`
baseline when that key is unset — the exact state a first bring-up is in — so the two shipped RF
modes built for that moment were the ones that keyed, and `--key-test` **reported PASS on a key-up
at 0 Hz**. Both now report the refusal and name `uvk5.frequency`.

**`AiocBaofeng` has the same state and not the same defect.** Measured on the station after this
deploy: `frequency: null`, and it keys anyway — correctly, because there the *radio* owns the
frequency, an untuned host is the ordinary post-restart state, and refusing would stop station ID
and every voice service after every restart. Unchanged, and said out loud rather than buried.

**Collateral was one test, and the number is the finding.** With the fix in and the fake still
unseeded the suite was **1F/2304P/5S** — the single failure a *no warnings were logged* assertion,
not a keying test. Every uvk5 test that keys already tunes first, so the suite had **never once
exercised a key-up on a radio it had not tuned**. That is how a dead guard stays invisible.
`FirmwareFakeSerial` now seeds 0x38/0x39 to 147.390 MHz for the reason its 0x30 comment already
gives.

Red run **10F/1P** (the pass is the healthy-seed pin). `uv run pytest` **2305/5** (from 2294/5);
vitest **14 files / 155**, untouched. `acceptance.py` **exit 1**: 8 of 10 PASS, `split-minus` SKIP,
`web` FAIL re-run alone and reproducing only the witness's known `kv4p /healthz` 404. `POST /ptt`
was not probed on the station — its backend has no frequency precondition, so it would key a
transmitter to learn nothing.

**Two things for the next cycle.** (1) An early "read-only" probe here reported the station untuned;
it had read a **stale `/tmp/st.json`**, because 8090 serves **HTTPS** and the `http://` request was
closed with an empty reply. Use `https://` (and `-k`) against 8090 and 8091 — `acceptance.py`
already does. The station was in fact tuned to **145.145 / TX 144.545, tone 107.2, power low**, and
was restored to exactly that; `tune_persist` is **true** as found and was not flipped. (2) The
remaining findings — `Kv4pHt` not validating its seeded tuning, `_reassert_channel` being unable to
re-assert a channel it does not know after a restart, and the three mid-TX `RuntimeError`s that
should be `RadioBusy` — are each their own cycle.

## Not ready is not a bad request (2026-08-01)

[ADR 0173](adr/0173-not-ready-is-not-a-bad-request.md) · branch
`adr-0173-not-ready-is-not-a-bad-request`

**The brief's premise was stale, and correcting it is the cycle.** It said the untuned-station
refusal on `POST /mode` still reached the client as a 500. It did not: [ADR 0172](adr/0172-the-mock-stops-accepting-what-the-radios-reject.md)
merged as `586a021` at 17:03, and its `except ValueError` arm caught **both** cases from the moment
it shipped. What that arm actually did was hand the readiness refusal the **bad-value code**. A 422
says *change a value and resend*; **no value makes an untuned station tuned**. And ADR 0164 had
already ruled on the identical condition one layer down — the radio's own `ERR_OFF` is *"a state
conflict, not a bad number"* and a **409** — so the host was contradicting itself: 409 when the radio
noticed the precondition, 422 when the host noticed it.

**`RadioNotReady` in `base.py`, and the two things it is not are load-bearing.** Not a `ValueError`
subclass: the per-route 422 arms run before any app-wide handler, so it would go straight back to
the wrong code, and those six arms would stop meaning "the value is wrong". Not a `RadioUnavailable`
subclass: that is a 503 the moment a route forgets, the accident of inheritance ADR 0164 created
`RadioBusy` to escape. It shares **409** with `RadioBusy` because the pair divides on what the caller
does next — busy clears by itself, so wait and resend unchanged; not-ready never does, so make the
call the `detail` names and then resend unchanged. Both are *resend unchanged*, which is exactly what
separates them from a 422.

**App-wide handlers, and that is not a reversal of ADR 0172.** All four of 0172's reasons were about
`ValueError` being *Python's* class. Its decisive one — *a global handler does not make the next
route remember, it makes the next route's forgetting invisible* — holds when a route had something to
do and skipped it. Here a route has nothing to do: whether the radio is tuned is the backend's state,
and no route could check it without reimplementing the backend's model. **ADR 0172 finding 4 survives
literally unchanged** — this cycle adds zero `ValueError` arms, and `/tuning/persist` and the
broadcast-FM `off` path are strictly better off, answering 409 correctly without either gaining one.
`RadioBusy` gets the same handler, closing the **503-by-accident-of-inheritance** trap its own
docstring named two ADRs ago. **No route body changed.**

**Blast radius measured on the station before deploying**, reached by restarting the service — the
ordinary path to untuned, not a contrived one. `/mode`, `/tone`, `/split` **422 → 409**; `/mode` with
`AM` still **422**, because both faults are true at once and the bad word is the more useful answer;
`/modulation` and `/power` **200**, exempt by design (ADR 0150 §7 and the `set_power` carve-out);
`/channel` and `/scan` **501**. `GET /status` unmoved across every refusal on both builds.

**Two predictions the measurement killed.** The uvk5 500s are **dead code** — `Uvk5Radio.__init__`
seeds `_frequency` from the radio's own VFO, so a freshly built radio reports `0` and `POST /ptt`
returns **200**, keying on 0 Hz. And "no test asserts on this" was a bad grep. **Collateral was
exactly one test**, and what it was really asserting is the line after the `raises`: that a refused
setter writes *nothing* to the radio. Untouched; the exception class was scaffolding and is
load-bearing now.

**Stated rather than oversold: the browser cannot tell.** `web/src/api.js` branches on 401/501/503
only, so 409 and 422 both render the same `detail`. The message was always right; only the code lied.

Red run **5F/3P** (three `422 == 409`, the no-movement check, and `503 == 409` for the uncaught
`RadioBusy`; the 3 passes are pins). `uv run pytest` **2294/5** (from 2283/5); vitest **14/155**,
untouched. `acceptance.py` **exit 1** — 8 of 10 PASS, `split-minus` SKIP, `web` FAIL re-run **alone**
and reproducing only the witness's known `kv4p GET /healthz` 404. Station left verified on 147.555,
FM, simplex, no tone, persist off, broadcast FM off, reader alive.

**Seven for the next cycle.** (1) **`tune_persist` off does not survive a restart** — `radio.toml`
sets `uvk5_tune_persist = true`, so the persist-off restore every recent cycle has performed and
verified is undone by the next `systemctl restart`, silently. Either the config default is wrong or
the restore is theatre. (2) **`Uvk5Radio`'s two readiness guards are unreachable**, converted and
pinned anyway. (3) **A radio reporting `0 Hz` passes the readiness guard and keys** — `_frequency is
None` is not the same test as "on a usable frequency", and the backend's own log line says so.
(4) **The three mid-TX `RuntimeError`s** (`aioc_baofeng.py:610`, `:1139`, `:1160`) are textbook
`RadioBusy` and now one line each — held back because busy and not-ready are different claims.
(5) **`Uvk5KeyingError` still carries a policy gate (`tx_allowed`) and a hardware fault (no TX
confirm)** that want different codes, both 500s on a `POST /ptt` with no exception handling at all.
(6) **`MockRadio` models no ordering constraint**, and encoding one means choosing between three
backends that disagree — the same intersection decision as `set_tone`. (7) **`test_docs_contract.py`
still cannot tell whether the prose is true**, so this cycle's status-code rows are a review item.

## The mock stops accepting what the radios reject (2026-08-01)

[ADR 0172](adr/0172-the-mock-stops-accepting-what-the-radios-reject.md) · branch
`adr-0172-the-mock-stops-accepting-what-the-radios-reject`

**Closes the two findings ADR 0154 carried and ADR 0160 measured on the station — they were always
one defect.** `POST /mode {"mode":"AM"}` was **500** on every real backend and **200** in the suite,
because `MockRadio.set_mode` accepted anything while its siblings `set_power` and `set_modulation`
had validated all along, each with a docstring stating exactly why a double must. **The order of the
cycle is the argument:** the mock is fixed first because its honesty is the *instrument* that makes
the route's defect visible; the route second, because a strict mock with an unguarded route turns a
wrong 200 into a crash. Neither half is shippable alone.

**Three states, measured rather than predicted.** master → **200**, `status.mode == "AM"`, a
bandwidth no radio in this project can be in. Mock tightened, route untouched → the `ValueError`
**escapes** at `app.py:1610`, a literal **500 / `Internal Server Error`** under
`raise_server_exceptions=False` — the bench's 500 reproduced in the suite on a mock, with no radio
attached. Both → **422** naming both words.

**The accepted set is the fleet intersection, deliberately narrower than `uvk5`**, which also takes
`WIDE`/`NARROW`. A double must be a **lower bound** on what the fleet accepts, never a superset of
one backend, or a green test here is a lie on the other two. `_require_cat` stays **before** the value
check — an audio-only radio must answer "I cannot do this at all", never "that word is wrong" — and
only a *bad* value can tell the two orders apart, which is what the pin uses. The 422 is **local, not
an app-wide `ValueError` handler**: a global arm answers 422 whether or not a route validated
anything, so it does not make the next route remember, **it makes the next route's forgetting
invisible**.

**The collateral the brief expected is zero, and the zero is the finding.** With the mock strict and
the route unguarded the full suite was **1F / 2282P / 5S** — the one failure this cycle's own new
route test. Nothing across 2276 tests and seventeen ADRs relied on the permissiveness. Zero red is
the shape of a blind spot, not a clean bill of health: the FM/NFM contract that `presets.VALID_MODES`
publishes, `api.md` already documented **as true**, three backends enforce and ADR 0154 trimmed the
web select to, had **no test standing behind it at the mock**. It also licenses the ordering — because
the mock change is provably free, the route's red run is unambiguous evidence about the *route*.

**Numbers.** `uv run pytest` **2283 passed / 5 skipped** (baseline 2276/5). `npx vitest run` **14
files / 155 tests**, unchanged — ADR 0154 already fixed the browser half, and that a UI-only fix left
the server free to 500 is precisely why this survived. Red run 1: **4 failed / 3 passed**, the three
passes named as pins. Red run 2: **1 failed / 2282 passed / 5 skipped**.

**Bench.** `POST /mode {"mode":"AM"}` on the station: **HTTP 500 `Internal Server Error`** before
(`0e1f9cf`) → **HTTP 422 `{"detail":"mode must be FM or NFM, got 'AM'"}`** after (`db30fb9`), with
`GET /status` **identical across the call on both sides** — the value is refused before it reaches
the radio, in the broken shape and the fixed one alike, which is what made this safe to run on a live
station. ADR 0160 finding 13 is closed on the hardware that raised it. `acceptance.py` **exit 1** —
8 of 10 PASS, `split-minus` SKIP, `web` FAIL on the witness's known `/healthz` 404, **verified by
re-running that stage alone** rather than inferred from the summary; the station's own `/healthz` is
200 with `radio serial reader: alive`. `rx`, `dtmf`, `tx`, `split`, `auth` and `services` all PASS —
the real-RF proof that teaching a route to refuse a bad value did not cost it a good one. Station
left on **147.555**, FM, simplex, no tone, `tx_ok: true`, persist **off**, broadcast FM **off**,
unit `active` (the acceptance run leaves it on 445.800 with persist **on**; both were restored
deliberately and verified, not assumed).

**The bench found a second 500 on the same route, and only the bench could have.** On an **untuned**
station — what every host restart leaves behind — `POST /mode {"mode":"nfm"}` hits `AiocBaofeng`'s
*other* `ValueError`, `"set a frequency before a split, tone or mode"`, equally uncaught and equally
a 500 on master. It is a **422** now by the same arm. So the defect was never really about `AM`: the
route was blind to *every* `ValueError` its backend could raise, and the likelier path in practice
needs no bad word at all, just a restart. **The mock could not have found this one** — it is a
divergence of *sequencing*, not vocabulary, and `MockRadio.set_mode` has no ordering constraint to
violate. The mock and the bench each caught what the other could not.

**Retired from ADR 0154's carried list:** items **1** (`/mode` returns 500 not 422) and **2**
(`MockRadio.set_mode` accepts anything) are both closed by this cycle. Item **3** (`GET /presets`
does not serve `modulation`, so the preset highlight lies) is **still open** and was explicitly out
of scope — it is a response-field change.

**Four for the next cycle.** (1) **`MockRadio.set_tone` is the next one and it is a real decision** —
three backends disagree three ways, and its fix deletes `class _Picky(MockRadio)` in `test_api.py`, a
standing workaround that exists only because the mock will not raise. (2) **`POST /channel` has no
`ValueError` arm, but the prior question is the capability**: no real backend implements
`SET_CHANNEL` at all, while the mock accepts any int *and advertises it* — a 422 today would ship an
unreachable branch. (3) **`/tuning/persist` and the broadcast-FM `off` arm also lack the catch and
should NOT grow one** — recorded with the reason so nobody "fixes" them by symmetry. (4) **The mock's
frequency/split setters stay permissive deliberately**: a hardcoded band limit is a hardware claim
the mock cannot make (guardrail 1); if ever wanted it must be *configurable*, never a constant.

## A dead RX listener gets reaped on a quiet channel (2026-08-01)

[ADR 0171](adr/0171-a-dead-rx-listener-gets-reaped.md) · branch
`adr-0171-a-dead-rx-listener-gets-reaped`

**ADR 0170 measured this leak and carried it; this closes it.** A dropped `/audio/rx` listener stayed
counted at **+50 s**, past uvicorn's keepalive and through a signal that woke every other reader,
pinning the single capture reader open for the life of the process. Send-based liveness detection
stays **ruled out by measurement** — nothing here tries to learn anything from a send.

**A probe came before the design, because the whole idea rested on a claim nobody had checked.** It
held: **the disconnect was always being delivered and nobody was listening.** ASGI posts
`websocket.disconnect` on the receive channel, and all four streaming handlers never called
`receive()`. The control arm — master's send-only loop — was **still parked at 55 s**, while the
proposed shape saw the disconnect at **20.00 s**, identical with and without explicit ws kwargs
(which also confirmed the pinned values *are* the inherited defaults).

**That refines ADR 0170's lesson rather than contradicting it.** "One loop has a bounded await and the
other does not" is true of TX, but the timeout is not what makes TX safe — **reading the receive
channel at all** is. On RX a silent queue is *legitimate*, so a timeout could only busy-loop or drop a
listener doing nothing wrong.

**Report the reap as a WINDOW, never a figure.** 20 s is not `interval + timeout`: a ping *write* to a
reset socket fails at once. Measured both edges — **19.7 s** for a peer that RSTs, **40.0 s** for one
that goes silent with its socket still open (reproduced without root by handshaking over a raw socket
and then never speaking again). Both fall out of the two pinned uvicorn settings and neither moves
without the other.

**One helper, four call sites.** The brief said *"three hand-written variants is how the fourth gets
written wrong"* — and the fourth already existed: `/events` **predates all three RX paths** and leaks
worst, since `EventHub`'s queue has no cap where `AudioHub` drops oldest at 64.

**Off the audio path by construction, not convention.** The watcher publishes nothing and subscribes
to nothing. That is what killed the keepalive-frame alternative: a synthetic tick reaches D-STAR's
`_rf_gate` **before** `_deafened()`, mutating a Part 97 control's hysteresis state.
`test_relay_subscribers` is a source-text scan a rename can defeat, so it was **re-run against the
tree rather than trusted** — still exactly 3 files, 1 hit each.

**The ping is pinned because the reap depends on it**, not to document a default: a version bump could
have silently restored the leak, and the symptom would be listeners quietly staying counted again.
`10/10` is recorded as the available knob with its price, not picked before the bench spoke.

**The red run first passed for the wrong reason — worth knowing about.** `wait_for(handler())` cancels
the handler, which swallows `CancelledError` by design (ADR 0122) and unwinds its context managers on
the way out, so the cleanup under test happened, driven by the harness rather than the code. A test
that cannot fail, dressed as one that passed. `asyncio.shield` fixed it; `ws.receives >= 1` says it
plainest — **master never asks**.

**Numbers.** `uv run pytest` **2276 passed / 5 skipped** (from 2265/5). `npx vitest run` **14 files /
155 tests**, unchanged — no UI change was needed and none was invented. Red run **7 failed, 2 passed**,
the 2 passes named as pins. Real-uvicorn smoke PASS: clean close **0.04 s**, RST **19.67 s**, five
960-byte frames delivered byte-for-byte.

**Bench, before and after on the same box:** clean close 10.01 s → **0.01 s**; RST **still counted at
55 s** → **19.58 s**; and the zombie that used to outlive the next listener is gone. `acceptance.py`
**exit 1** — 8 of 10 PASS, `split-minus` SKIP, `web` FAIL on the witness's known `/healthz` 404,
**verified by re-running that stage alone** rather than inferred from the summary. `rx`, `dtmf`, `tx`,
`split` and `services` all PASS: the real-RF proof that the reaper does not drop working listeners.
ADR 0170's one-off zero-carrier `tx`/`split` FAIL **did not recur**. Station left on **147.555**,
persist **off**, FM **off**, `tx_ok: true`.

**Three things for the next cycle.** (1) **The reaper kills zombies, not backpressure** — a
*live-but-slow* consumer still grows `EventHub`'s unbounded queue without limit; that is a different
failure, and capping it was excluded **deliberately** because it would silently discard status events.
(2) **The bench instrument is flaky and nearly got blamed on the server:** `websockets.sync.client`
times out intermittently (3/12, 5/12), but the old-code witness shows it too, HTTP showed no
event-loop stall, and a raw TLS upgrade was **20/20 at 3–4 ms**. (3) **The witness still carries
uncommitted edits to three files** and still fails one acceptance check every run until somebody
decides what to do with them.

## A stranded slot stops being invisible (2026-08-01)

[ADR 0170](adr/0170-a-stranded-slot-stops-being-invisible.md) · branch
`adr-0170-a-stranded-slot-stops-being-invisible`

**ADR 0167 closed the strand and carried its own finding: nothing reported slot state.** `TxSlot` was
one bare bool. `/status` had no occupancy field for any of the three talk slots — and the two fields
that *look* like they answer are decoys: `transmitting` is PTT state, `busy` is squelch state, and
**both read `false` while a slot is stranded**. The card positively suggested an idle station while
every Talk press was refused.

**This finishes a design rather than inventing one.** `"busy"` sat in `EVENT_TYPES`
**reserved-but-never-published** since ADR 0011 built the event surface, 159 ADRs ago. Say so to the
next reader: they are completing something, not adding something.

**The brief's first question, answered with measurement before anything was built.** Against real
uvicorn with the deployed `squelch = "audio"` gate, a dropped `/audio/rx` listener **survives its
socket**: cleanly closed, still counted at +3 s on a quiet channel; RST'd, still counted at **+50 s**
— past uvicorn's 20 s keepalive — and **still counted after a signal that woke every other reader**.
The handler parks on an unbounded `queue.get()` and only ever *sends*, and a reset transport drops
sends silently rather than raising. **Two variables in my first rig were not production** (keepalive
disabled, RST only); both were corrected and the answer re-measured before it was written down.

**The same question of a talk slot is what turned the design.** Clean close **43 ms**; RST
**1.85 s** — freed by `wait_for(..., tx.idle_timeout)`, not by a disconnect the app never sees.
**One loop has a bounded await and the other does not, and that is the whole difference.** So
`/status` grew **two** blocks, not one:

- `slots` — the three talk slots, self-reaping, this is liveness. Holder, monotonic `held_s`, derived
  wall-clock `since`, `stale_after_s` from the TOT, and `refused` counted **per claimant** (the
  Mumble relay refuses at frame rate; ADR 0153's rule says that must not bury a single browser
  refusal).
- `rx_demand` — `{"requested": N, "reader_running": bool}`. **`requested`, never `listeners` or
  `active`**, and deliberately not a fourth entry in `slots`. A note in `api.md` does not travel with
  the value to the card that renders it; the label is the only thing that does.

**The UI stops lying in two places.** The refusal carries holder/age/threshold and the phrasing
*changes* past the TOT — "held for 4h 20m" only reads as wrong to somebody who already knows what
normal looks like. An older server gets the **old sentence back**, not an invented one. And a
standing `Talk slots` row, because a refusal message only ever reaches the operator who presses Talk.

**Numbers.** `uv run pytest` **2265 passed, 5 skipped** (baseline 2253/5). `npx vitest run`
**14 files, 155 tests** (from 14/146). Red run against master **11 failed, 1 passed** — the 1 pass is
the reaping pin, named as a pin rather than counted. Real-uvicorn smoke PASS, including the refusal
line surviving `basicConfig`: `talk slot tx refused: held by browser for 0.3s`.

**Bench, on the deployed station (`962f847`).** Holder named while held; **closed tab freed in
0.93 s, yanked connection in 2.0 s**; ledger `{browser: 1}` → `{browser: 2}` surviving both releases;
next talker `ready`. The surface tracks reality, not the intent.

**`acceptance.py` is exit 1, not the clean 3 — and the cause is not this cycle.** The only failing
check is `kv4p GET /healthz → 404`. The witness (8091) is on **`a6a4cd4`**, three ADRs stale, and
`/healthz` arrived with ADR 0166 — `grep -c healthz` on its own HEAD returns **0**. ADR 0169 already
flagged that checkout: it carries **uncommitted edits to `radio_server/audio/dtmf.py`,
`radio_server/link/entries.py` and `update-radio-server.sh`**. Updating it would discard that work,
so it was left alone. Every station-side check passes, including its own `/healthz` and
`radio serial reader: alive`. **Somebody needs to decide what to do with those edits** — the witness
cannot be brought current until then, and it fails one acceptance check every run in the meantime.

**A `tx`/`split` FAIL that did not reproduce, reported rather than buried.** First full run: zero
carrier at the witness. Second full run: both PASS (`kv4p saw carrier 10`, RMS 12309, 1000 Hz 0.965,
CTCSS 0.0217). `tx` in isolation also passes. **No cause claimed.** The run before it was a restart
storm, the same neighbourhood as ADR 0169's one-off `systemd` FAIL.

**The station could not deploy itself.** `update-radio-server.sh` on the box is still the pre-0169
script, because 0169's fix has not reached it — so the documented long form was used. This is the
last cycle that should need it.

**Carried, with its difficulty named so it is not read as trivial.** Bounding the `/audio/rx` await
is the obvious fix, but **an idle wakeup or keepalive frame changes what travels the audio path, and
that path feeds a Part 97 control** (ADR 0162's broadcast-FM relay mute and the DTMF decode both sit
on it). Its own fail-first cycle, not a corner of an observing one. **The same unbounded shape is in
`/audio/mumble/rx` and `/audio/dstar/rx`** — smaller blast radius (a subscription, no reader demand
pinned), but fix three loops rather than one.

**Station leave-state:** deployed on this branch at `962f847`, **147.555**, tune persist **off**,
broadcast FM **off**, `tx_ok: true`, service active. It stays on the branch — ADR 0169 removed the
put-it-back ritual, and the bare update command works again on its own once this merges.

## The updater and the deployment disagreed about git (2026-08-01)

[ADR 0169](adr/0169-the-updater-and-the-deployment-disagreed-about-git.md) · branch
`fix-update-script-detached-head`

**The report was "updating the server fails".** It failed with `You are not currently on a branch.`

**`update-radio-server.sh` has never worked on the box it was written for.** It began with
`git pull`; `server-notes.md` deploys the box with `git switch --detach <ref>`. Two documented paths
that cannot both be right, and the one that runs lost. Nothing caught it because nothing runs it in
CI and a human who hits it just pulls by hand.

**The detached deploy is the RIGHT half, and the fix keeps it.** A bench measurement has to be
attributable to an exact commit — every ADR in the 0156→0168 arc records *"the station runs
`<sha>`"* — and a branch that fast-forwards underneath you between two measurements makes that
unanswerable. So the update is now **fast-forward to a target ref**, which means the same thing
detached and on a branch, and it **preserves whichever mode the checkout is in**.

**Read this before you "fix" a detached box by hand.** The reflexive `git checkout master` would
have succeeded and deployed a build **168 commits stale** — the local `master` branch had been
sitting at `ba134b0` for months. The reflog showed how the box got detached: a run of
`git checkout origin/<branch>` bench deploys, then a `git reset --hard origin/HEAD` against a stale
symbolic ref, which is the same failure `server-notes.md` already records under ADR 0162.

**A no-argument run may only fast-forward, and that refusal is the point.** This box gets deployed
onto bench branches on purpose (ADR 0164 ran the station on `adr-0164-the-on-path` for a full cycle).
A routine "just update the server" that yanked one back to master would destroy the experiment during
the least suspicious command in the project. **And it deletes a ritual:** once a bench branch merges
its commits are in master, the ancestor test passes, and the bare command works again on its own — so
the *"when PR #NNN merges, put both checkouts back on the mainline"* paste-block every ADR in this arc
carried is gone.

**A second latent failure sat directly behind the first**, and would have surfaced the moment the git
step was fixed: `uv` lives in `~/.local/bin`, which only a login shell puts on `PATH`. **Measured on
the station:** `command -v uv` returns **nothing** non-interactively — so a bare `uv` works for a
human and fails under cron, a wrapper, or `ssh box ./update-radio-server.sh`. Resolved explicitly
now; the fallback was verified on the box itself (`/home/kb/.local/bin/uv`, uv 0.11.16).

**Numbers.** `uv run pytest` **2253 passed, 5 skipped** (baseline 2244/5, +9 in the new
`tests/test_update_script.py`). `npx vitest run` **14 files, 146 tests**, untouched. Red run against
the old script **8 failed, 1 passed** — the detached case failing with the operator's own error text;
the 1 pass is the extras source-check, a regression pin.

**Caught in our own test rather than shipped.** The missing-`uv` case first scrubbed only `$UV`,
which left `command -v uv` free to find the developer's own `uv`: the test passed while proving
nothing. It scrubs `PATH` and `HOME` now. *A guard tested in one direction is a guard tested for the
case you happened to be in.*

**Deployed — the station is back on the mainline.** `a6a4cd4` detached / 0 ahead / 9 behind →
**`2a794ac`** (`origin/master`, the #228 merge), still detached, tree clean. Service **active**,
HTTPS **200** on 8090, serving `index-CI6vVew4.js` — byte-for-byte the ADR 0168 build, so the new
Second receiver card is live. Boot log clean: *"broadcast FM is off; the station can hear its own
channel"*. Fixed by hand, because the broken script could not deliver its own fix.

**The witness (8091) was deliberately NOT moved**, and needs a look: it carries **uncommitted local
edits to `radio_server/audio/dtmf.py`**, sits on an unrecorded commit, and takes `--extra kv4p`
rather than `--extra mumble` (sync it with the station's extras and `opuslib` disappears). Anything
quoting it as a measuring instrument should check it first.

**Also open:** `restart-radio-server.sh` is two lines with no `set -e` and no health check, so a unit
that starts and immediately dies still exits 0. And nothing in CI runs any of this — these 9 tests
are the first thing other than an operator ever to execute `update-radio-server.sh`.

## The second receiver is a control, not a consent form (2026-08-01)

[ADR 0168](adr/0168-the-second-receiver-is-a-control-not-a-consent-form.md) · branch
`adr-0168-second-receiver-is-a-control` · closes issues **#225**, **#226**, **#227**

**What shipped.** One React component. `BroadcastFmPanel.jsx` loses the arm/confirm, the
"Turning this on:" notice and the repurposed-keypad banner (#225), and gains the thing that makes
**Retune** mean anything: the frequency and band controls stay on screen while the receiver runs.
**No Python changed.**

**#226 and #227 were one defect, and it is worth carrying the shape.** The inputs were gated on
`{canSet && !on}` and Retune on `{on}` — **one control gated on `!on`, its only consumer gated on
`on`.** The moment the receiver came up the only two inputs that could change what Retune sends
unmounted, so Retune could do nothing but re-send the frequency the radio was already on: 200, same
`hz`, card visibly dead. That is #226. #227 is the same sentence from the operator's side.

**Read this before the next UI cycle.** It shipped green. `BroadcastFmPanel.test.jsx` had 14 cases
and the string **`"tune"` appeared in none of them** — ADR 0164's B3 measured `tune` over `curl` on
the real radio, so both ends were proved and the wire between them never was. The cheap guard is a
test per *action string* the component can send. `"tune"` is now in 6.

**The backend already did what #227 asked for.** `action: "tune"` on a running receiver has been
first-class since ADR 0164. It was a UI gap, not a missing feature.

**The reversal is recorded as a reversal.** ADR 0164 §7 argued the confirm and both notices from
measurement and those arguments still hold; they are gone because the operator asked. The residual
risk is stated rather than softened — **one click now silences both bridges** until the next
transmission. **What was never the protection:** the relay mute (0162) and the pre-key-up clear
(0161), neither in the UI, neither touched. Nobody should re-add the confirm "for safety" without
saying which property they think it was holding. The four consequences now live in `api.md` and in a
new `troubleshooting.md` section — that file had **zero** mentions of broadcast FM, so the operator
staring at two silent links had nothing to search for.

**Numbers.** `npx vitest run` **14 files, 146 tests** (baseline 14/138); `BroadcastFmPanel.test.jsx`
14 → 22. `uv run pytest` **2244 passed, 5 skipped**, unchanged — run as a regression check, offered
as nothing more. Red run **16 failed, 6 passed**, every failure behavioural (missing `/frequency/i`
label, missing `role=status`, the notice/confirm assertions, the tune spy's payload); the **6 that
pass on the old component are named as regression pins, not counted as reds**.

**Bench: a real browser, no radio.** A live `python -m radio_server` on `MockRadio` serving the
**built** `web/dist`, driven by **headless Chrome over CDP** — 9 legs, all PASS. Plus the route's own
legs, including **422** on an off-raster 98.55 MHz. Three things to carry:

- **The mock cannot stand in for the radio on the refusal paths.** `MockRadio.set_broadcast_fm`
  treats `tune` exactly like `on`, so a tune against a *stopped* mock receiver returned **200** where
  F9 returns **409 `ERR_OFF`**. It models no `ERR_TX`/`ERR_BAND` either.
- **A jsdom test cannot see a three-line button.** `.tune-row` is a `96px 1fr auto auto` grid for
  label+field pairs; the lone ON button had been landing in the 96px label column and wrapping to
  three lines since ADR 0164 — visible in #225's own screenshot, invisible to all 146 tests. Fixed
  with the existing `.btn-row`; an audit found this card was the only misuse.
- **The first harness run looked like a real defect and was not.** The frequency box appeared to
  vanish after a retune; a 22 s observation at 100 ms showed the card's DOM never changed once. The
  harness was deleting a `[role=status]` node out from under React. Recorded because the diagnosis,
  not the symptom, is the reusable part.

**Also corrected:** `docs/uvk5-setup.md` still said *"Nothing on the server drives this yet"* about
broadcast FM — stale since ADR 0157/0161/0164.

**Not deployed.** No hardware measurement was taken this cycle and none is implied.

## A claim and its release are one scope (2026-07-31)

[ADR 0167](adr/0167-a-claim-and-its-release-are-one-scope.md) · branch
`adr-0167-dropped-upgrade-strands-a-slot` · PR **#224**

**What shipped.** `radio_server/api/talkers.py` — a `talker_slot()` async context manager the three
talk endpoints now claim through, plus an `rx_listener()` for `/audio/rx`'s subscribe/demand pair.
`_acquire_rx` starts the pump **before** counting the demand. `MumbleBridge._end_session` releases
the slot in a `finally`. One rule: **a release is a scope exit, never a statement that can be
skipped.**

**Read this before writing the next websocket handler.** Claim the slot through `talker_slot`. Do
not call `try_acquire()` and then `await` anything — `TxSlot` has no owner, no timestamp, no timeout
and no watchdog, so nothing ever reclaims a slot whose holder went away, and the station needs a
restart. `TxSlot`'s docstring now says this; the guarantee lives in the context manager, not in the
flag.

**The brief's trigger does not reproduce, and the ADR says so.** A dropped upgrade — closed tab,
flaky phone, reload mid-handshake — does **not** make `accept()` raise. Measured against real
uvicorn 0.51.0 on the production `websockets` sans-io path, four drop modes (RST, FIN, delayed FIN,
truncated headers): `accept()` succeeded every time. `websocket.connect` is queued before the ASGI
task starts, the accept write is skipped rather than raised when the transport is closing, and the
impl never cancels the app task. The failure surfaces later, as a `WebSocketDisconnect` the handler
already catches. Only shutdown `CancelledError` reaches that window — where the process is dying.

**What IS reachable, and why the cycle still matters.** ADR 0166 made a raising `ptt(False)` an
ordinary event. `session.close()`, `end_operator_over()`'s UDP write, and `link/bridge.py`'s close
each ran *immediately before* the release, on a live process. `_end_session` was the worst: six call
sites, four of them exception handlers and one a `finally`, so a raise there also replaced the
exception being handled and took the relay loop down. And `/audio/rx` counted `rx_demand` before
starting the pump, pinning the single reader permanently if the start failed.

**Numbers.** `uv run pytest` **2244 passed, 5 skipped** (baseline 2229/5); `npx vitest run`
**14 files, 138 tests** (unchanged — no UI). Red run **13 failed, 2231 passed, 5 skipped**, all in
`tests/test_slot_unwind.py`, all behavioural. The 14th test there passes on master: a regression pin
for the new shape, not a red, and reported as one.

**Two things the tests would not have caught.** `test_relay_subscribers` — the Part 97 source scan
pinning every RF-hub subscriber — failed the instant `audio_hub.subscribe()` changed file. That is
its job. The `rx_listener` parameter is named `audio_hub` **deliberately**: the guard matches source
text, and a rename would hide the browser Listen path from a Part 97 control. And a real-uvicorn
smoke (not `TestClient`) confirmed the refusal log line survives `basicConfig` — ADR 0165 lost a
startup line exactly that way.

**Carried findings.** (1) **A stranded slot is invisible**: no holder, no timestamp, no counter, no
event, no log line; `"busy"` is a reserved-but-never-published event type, which is evidence the
surface was designed and abandoned; and the UI hard-codes *"Radio busy — another operator is
transmitting"*, which is false during a leak — that sentence needs fixing alongside the plumbing.
One `logger.info` on refusal is the only piece taken now; ADR 0085's counter shape is the remedy.
(2) **`/audio/rx` parks on an empty queue** and only learns its client left on the next `send_bytes`
— pre-existing, verified identical on unmodified master, documented in the handler as a mock case,
but a silent receiver on a live station (ADR 0166's dead reader) reaches the same state.

**No bench.** Software-only cycle: no hardware, no station, no firmware.

## A dead serial reader is not a healthy station (2026-08-01)

[ADR 0166](adr/0166-a-dead-reader-is-not-a-healthy-station.md) · branch
`adr-0166-dead-reader-is-not-healthy` · PR **#223**

**What shipped.** Liveness for the serial reader (`alive` / `reader_error` on both transports), a
`transport` block on `RadioStatus`, **`GET /healthz`** answering 503, **`POST /diagnostics/reconnect`**
for one bounded reopen, a 2 s watcher, a full-width UI banner, and `TIOCEXCL` on every serial open.
Closes ADR 0163 **finding 1** — the concurrent-tty hazard every prompt in this arc has had to carry
a manual "don't touch the tty" warning about.

**The defect.** `_read_loop` has one fatal path: any exception out of `read()` calls `_fail` and
returns. Nothing restarted it, nothing watched it. Meanwhile `status()` is pure attribute reads *by
design* — that is what makes it cheap enough to call per audio frame — so `/status` kept reporting a
frequency and `tx_ok: true` from a radio that had not spoken in an hour. **Lead cause is the one with
nobody behind it:** `A602RQT5` re-enumerates hourly on this box and leaves exactly that wreckage.

**Numbers.** `uv run pytest` **2229 passed, 5 skipped** (baseline 2199/5); `npx vitest run`
**14 files, 138 tests** (baseline 13/131); red run **23 failed, 2203 passed, 5 skipped**, all
behavioural. `acceptance.py`: **9 of 9 attempted PASS, `RESULT: INCOMPLETE`, exit 3.**

**Measured, not assumed.** pyserial's `exclusive=True` is an advisory `flock` that a plain
`serial.Serial(port)` walks straight past; `TIOCEXCL` appears **nowhere** in pyserial 3.5. Two
docstrings here claimed otherwise, which is why a bench script silently killing the reader was a
surprise instead of an expectation. `TIOCEXCL` was adopted only after **B0** proved on a real USB tty
that the flag clears on last close *including after SIGKILL* — a pty says the opposite and is a bad
model, and shipping on the pty result would have wrongly refused the whole prevention half.

**Reproducing the defect is harder than the record suggested.** A bare second open does not kill the
reader. Two readers have to **race for bytes**, so it needs live dock traffic — and at one intruder
*the intruder* died, not the service. Which process loses is a coin flip. It took three concurrent
readers to make the service lose.

**Blast radius — read this before the next bench session.** Every bench script and every `doctor`
dock probe against that tty now fails **EBUSY while the service runs**. That is the rule
`troubleshooting.md` already gave, finally enforced; the message names the remedy (`systemctl --user
stop radio-server`) rather than leaving a bare errno. **Root bypasses `TIOCEXCL`** — a `sudo`-ed
script still gets in and still kills the reader, which is exactly why the liveness surface and the
reconnect route still have to exist. Do not read the claim as making them redundant.

**Four things only the bench could find**

1. **`doctor`'s own error path crashed.** A blanket edit gave every "could not open the serial port"
   site the new explanation, and two live in `*_connect_probe(report, cfg, ...)` with no `port`
   local — a `NameError` *while reporting an error*. Pinned now, with a guard on all five sites.
2. **The "stop the service first" message only reached our own opens.** Three bench scripts call
   `serial.Serial()` directly and got pyserial's bare EBUSY. They now share `open_tty()`.
3. **The witness backend never surfaced its transport.** `Kv4pTransport` got `alive` but
   `Kv4pHt.status()` never asked it, so 8091's `/healthz` was blind to the same defect — on the
   instrument every other measurement here is taken against.
4. **One `acceptance.py` run had `systemd` FAIL**, seconds after a SIGKILL/restart storm. Not
   reproducible; passes in isolation and on the re-run. Recorded rather than dropped.

**Open and carried forward**

- **The `reopened` recovery leg was not run on the station**, and the reason is the prevention
  working: `TIOCEXCL` removed the only non-root way to kill the reader and the box has no
  passwordless sudo. It is proven against a real transport with a real reader thread in pytest, and
  recorded as that rather than as a station measurement.
- **`client.status()` is dead code** in the web client — a latent second up-signal if anyone wires
  it, which would defeat the `/healthz` split.
- **`Bench Split Minus` preset still missing** — still the only SKIP.
- **The witness unit's `ExecStart` still excludes `--extra kv4p`** (ADR 0165), so its codec survives
  only because `uv run` is not exact.

**Deployed:** both units run `f82a657`, on **147.555**, `tune_persist` off (re-applied after the
restarts — it never survives one), broadcast FM off, `rescues` 0, both active, `radio.toml`
byte-identical (`ead78a44…` station, `f9be6bb5…` witness).

## The API token stops appearing in the journal (2026-08-01)

[ADR 0165](adr/0165-the-token-stops-appearing-in-the-journal.md) · branch
`adr-0165-token-out-of-the-journal` · PR **#222**

**What shipped.** `radio_server/logsafe.py` — credential redaction installed at the `LogRecord`
factory, plus a third verdict for `acceptance.py`. The seven WebSocket routes authenticate with
`?token=` in the query string (a browser cannot set a header on a `WebSocket`), uvicorn appends the
query string verbatim to its log line and prints it on every accept, so the LAN bearer token went
into journald once per socket connect. Closes ADR 0164 **finding 2** and ADR 0161 **finding 8**.

**Severity was higher than the brief assumed, and lower in one direction.** The token is **not** in
ADR 0164 or this file — both read `token=<the token>`, a placeholder, and the working tree has zero
occurrences of the live value. It **is** in the public git history (`9e9742e`, `1dea8d7`, removed by
`2e60c6b`, in `scratchpad/*.py`), and **both repos are public**, which no doc edit reaches. The repo
had already learned this rule for scripts — `dual_tx_watch.py:25-26`, *"no token is baked into the
repo"* — and never generalized it to the log plane.

**Numbers.** Leak, 24 h before → after: station **1068 → 0** (1051 `/audio/rx`, 14 `/events`, 2
`/audio/mumble/rx`, 1 `/audio/tx`), witness **40 → 0**, with 14 redacted lines including the 403 a
*wrong* token produces. `acceptance.py`: **9 of 9 attempted PASS, `RESULT: INCOMPLETE`, exit 3** —
the first clean bench in five cycles that does not print FAIL. `uv run pytest` **2199 passed, 5
skipped** (baseline 2169/5); `npx vitest run` **13 files, 131 tests**, unchanged — no web change was
expected or made. Red before implementation: **25 failed, 2174 passed, 5 skipped**, all behavioural.

**Read the token on the box, not here.** It was rotated after the fix was live (the order is forced:
rotating first means the replacement starts leaking immediately) and the value is deliberately not
recorded in this file, the ADR, the PR or any commit message. `grep api_token
~/applications/radio-server/radio-secrets.toml`.

**The rotation was reverted by the operator** at 02:38:58 (witness) and 02:39:18 (station) — writes
this cycle did not make. **The station is currently split:** both units started at 02:35 and still
hold the rotated value in memory, so the on-disk value returns 401 on both ports, and the next
restart of either unit puts the old 9-character token back into service. That value is in the public
git history.

**Four things only the bench could find**

1. **The witness instance was three cycles behind and leaking too.** `radio-server-kv4p` is a
   separate checkout with its own config and secrets, on `d779aca` (PR #218). Now on the same commit.
2. **One rotation is a two-unit operation here.** `acceptance.py` authenticates to both stations with
   a single `RADIO_API_TOKEN`, so they have always shared one value; rotating one 401'd every RF
   stage until the other matched and restarted.
3. **The station's documented `uv sync` breaks the witness.** It is exact, and the witness is a kv4p
   node: syncing it with the station's extras removes `opuslib`, and `POST /transmit` answers 500.
   Caused and fixed here; `docs/server-notes.md` now carries the witness's own command.
4. **`basicConfig` sat too late to log anything at startup.** Every startup line, including this
   cycle's own arming report, went nowhere. Found by running the real entrypoint, not by a test.

**Open and carried forward**

- **NEW: `tune_persist` has never survived a restart.** `aioc_baofeng.py:109` says so outright — it
  is a runtime switch that does not write config — and `radio.toml` has said `uvk5_tune_persist =
  true` throughout, in a file byte-identical to the one ADR 0164 hashed while recording
  "tune_persist off". Every restore note in this arc describes a state that dies at the next restart.
- **NEW: the witness unit's `ExecStart` names extras that exclude `kv4p`**, so its codec survives
  only because `uv run` is not exact. One `uv sync` away from breaking again.
- **`Bench Split Minus` preset still missing** from the deployed `radio.toml`. The banner fix covers
  every future skip; the preset fixes this one instance.
- **The `?token=` query string is unchanged.** Still visible in browser history, DevTools and to any
  page script via `ws.url`. A subprotocol handshake is the real fix and was explicitly out of scope —
  this was a logging change, not an auth change.
- **Corrected:** ADR 0164's restore line recorded "tune_persist off" alongside a config hash whose
  file says `true`.

**Deployed:** both units run `1ba81c3` on branch `adr-0165-token-out-of-the-journal`, on **147.555**,
`tune_persist` off, broadcast FM off, `rescues` 0, both active, `radio.toml` byte-identical
(`ead78a44…` station, `f9be6bb5…` witness). `docs/server-notes.md` carries the paste-ready commands
for both checkouts once #222 merges.

## The ON path — broadcast FM becomes a thing an operator can use (2026-08-01)

[ADR 0164](adr/0164-the-on-path.md) · branch `adr-0164-the-on-path` · PR **#221**

**What shipped.** `POST /broadcast-fm` in both directions, a `Radio`-level `clear_broadcast_fm`, a
new earned `set_broadcast_fm` capability, and a **SECOND RECEIVER** card in the web UI. This closes
three findings that were one missing piece seen from three sides and had been open four cycles —
ADR 0158 **R4**, ADR 0160 **finding 3**, ADR 0161 **finding 2**: *"pressing Talk is the operator's
only way out of broadcast FM from the host side"*, which on an unattended LAN station means nobody.

**The brief's own third consequence was wrong, and the bench settled it.** It said the transmitter
is refused by F9. It is not, on any host path: `_clear_if_deafened` runs first in `_key_on`, so by
the time PTT asserts the interlock has nothing left to interlock. **B2 measured `POST /ptt` returning
200 with `rescues: 0 → 1`** and the journal line *"switched the second receiver off before keying
(rescue #1)"*. A transmission does not fail — it **takes the receiver back**, station ID included.
That is what the UI says.

**Numbers.** ON → **200 in 101 ms**, read-back `{on: true, hz: 104300000, band: 0, blocks_tx: true}`
(F9's interlock bit on the wire for the fourth cycle running). The mute armed in the **same 2 s
sample**: browser **2 208 000 B / RMS 3957** unaffected while **1150 frames** were withheld from
Mumble over 22 s, **zero** unknowns. TUNE to 98.5 MHz on a running receiver → 200. OFF → **200 in
106 ms**, `deafened` back to `False` on the next sample, both links resumed. Four refusals against
the real radio: off-raster **422** host-side and no frame; 64.0 MHz under band 0 **422 carrying the
radio's own `ERR_BAND`**; band 4 **422** host-side; tune-while-off **409**.

**Two defects only the bench could find, both fixed and re-verified on hardware.**

1. **The ON capability could never be earned.** The first deployment advertised `clear_broadcast_fm`
   **and nothing else** — the flag was set only inside `set_broadcast_fm`, a method reachable only
   through a route gated on the capability it would earn. The card would never have rendered. Both
   pytest tests passed because both reached the flag through the one call that cannot run in
   production; the new test asserts the property from outside.
2. **The operator's own OFF was counted as a rescue** (`rescues` 0 → 1 → 2 on the bench). The rescue
   flag is armed by a probe, and since ADR 0163 the cadence probes every 2 s, so a deliberate clear
   always found it armed. `docs/api.md` documents that number as key-ups rescued. Fixed with
   `disarm_rescue()`; re-verified — `rescues` stays 0 across a full ON → OFF.

**Open and carried forward**

- **The browser leg RAN.** The operator drove the card from a real browser on the LAN
  (`192.168.1.30`): ON at **00:47:39** with the mute logging its first withheld frame in the same
  second, **3 805 440 B** of browser audio unaffected while **1952 frames** were withheld from Mumble
  with **0** unknowns, OFF at **00:48:42** with the links resuming and **`rescues` still 0** — the
  finding-2 fix on the operator's own path. B1/B3 were therefore measured twice, once REST-identical
  and once by hand.
- **`acceptance.py`: 9 of 9 attempted PASS**, `split-minus` still SKIP so the banner still prints
  `RESULT: FAIL` (ADR 0161 finding 8, unmoved for a **fourth** cycle). `auth` passed. It covers none
  of this cycle's code and is not offered as though it does.
- **`0x0878` reports `tx_ok = 1` while F9 refuses to key** — a firmware defect, unmoved (ADR 0163
  finding 2). **F9 is still not on fork `main`** (`d086a23` is F8), so a `main` build has
  `0x0879`/`0x087A` and no TX interlock.
- **A second process on the AIOC tty kills the dock link and the transport never recovers** (ADR 0163
  finding 1). Unmoved, and the reason nothing in this cycle touched the tty.
- **`GET /status` and `/link/status` legitimately disagree** during the front-panel window. Unmoved.
- **NEW: browser RX is squelch-gated.** `RxPump` publishes only frames the gate opens on, so a quiet
  channel yields **zero** browser bytes and that is not a fault. Every browser-audio number in this
  arc was taken with broadcast FM running, which is loud and continuous. A future cycle measuring a
  quiet channel will read zero and think something broke.
- **NEW: the API token is written to the journal in cleartext.** The `/audio/rx` and `/audio/tx`
  sockets authenticate with `?token=` in the query string and uvicorn's access log prints the whole
  path — `"WebSocket /audio/rx?token=<the token>" [accepted]`. Anything that can read the user
  journal can read the LAN token. Guardrail 4 is explicit that this is gated access rather than
  secure access, so it is not an emergency, but a credential in a log file is its own small cycle
  (a log filter, most likely — the query-string handshake is a websocket constraint, not a choice).
- **Corrected:** `docs/server-notes.md` said the bench radio runs **F8**; it runs **F9**, measured.
  `docs/api.md`'s 503 row still prescribed a server restart as the broadcast-FM remedy (ADR 0161
  dropped the latch), and its `ERR_TX` note still said "an open squelch is most of an active QSO"
  (ADR 0163 refuted that from source). Both fixed.
- **`acceptance.py` does not cover any of this** — ADR 0163 established why (its restart drops the
  link, so no bridge and no cadence). Run as a regression check and reported as one, never as
  coverage.
- **`rescues` will read LOWER** on this station than it did yesterday. That is finding 2's fix, not a
  regression.

**Deployed:** the station runs `5eccc21` on branch `adr-0164-the-on-path`, on **147.555**,
`tune_persist` off, broadcast FM off, both units active, config files byte-identical
(`radio.toml` `ead78a44…`, `radio-secrets.toml` `ae86f7f1…`). `docs/server-notes.md` carries the
paste-ready command to put it back on the mainline once #221 merges.

## A cadence for the probe (2026-07-31)

[ADR 0163](adr/0163-a-cadence-for-the-probe.md). **The relay mute now fires — and the probe answers a
coarser question than anyone had noticed.**

[ADR 0162](adr/0162-broadcast-fm-must-not-reach-the-bridges.md) shipped the mute *armed and blind*:
`Dock_FmOff()` clears `gFmRadioMode` first, so a block measured at key-up can never say `on=True`.
The brief asked the right thing before any of it — **does the probe work in the state it exists to
detect, or does `ERR_TX` make it permanently blind?** Nothing was built until that was measured.

**Measured first (service stopped, every status byte reported)**

- **M1** — 35 probes over 35 s with the second receiver running: **35/35 `ERR_BAND`, zero `ERR_TX`**.
  `gCurrentFunction` during broadcast FM is `FUNCTION_FOREGROUND`, and `FUNCTION_MONITOR` is
  *unreachable* in FM mode. The blindness hypothesis is dead on the wire, not just in source.
- **M4** — a front-panel `F+0` caught live: 47 × `ERR_OFF` → 93 × `ERR_BAND`, **one** transition.
- **M3, which changed the cycle** — `gFmRadioMode` has four writers and **none is
  `APP_StartListening`**, which tears the BK1080 down for a real over without clearing the flag. With
  the witness keying 1000 Hz on the station's own channel the AIOC recovered it at **power 0.995**
  while the probe still said `ERR_BAND`. **So `ERR_BAND` means "broadcast FM is selected", not "deaf
  right now" — and the mute therefore withholds real overs too.** Deliberate, decided with Kris, and
  stated in the ADR's own voice rather than left to be discovered.

**The prerequisite the brief could not have known about**

`Uvk5Transport` had **no wire lock**, and both `probe_broadcast_fm` and `clear_broadcast_fm` match
`isinstance(m, BroadcastFmReply)` and nothing else — so once anything polls, a key-up's clear can
consume the poll's refusal, read it as "the radio refused to clear", and **refuse the key-up**. That
is ADR 0161's defect rebuilt by the cadence itself. ADR 0125's thread-safety argument does not
transfer: it relied on `CatBusyGate` matching `m.register == reg`. One frame in flight now; the
key-up path blocks for the wire and always wins, the poller skips its round instead of queueing.

**Failure rule, argued on its own terms rather than inherited from `tx_ok`:** failing open puts a
broadcast onto somebody else's repeater; failing closed mutes on every over, because unknowns are
*routine* here. **A non-answer is not a state transition** — hold the last definite reading, surface
its age.

**Bench, on the deployed station**

- **The mute fired on a real radio for the first time.** Browser `/audio/rx` **4 571 520 B / RMS
  4221.2** unaffected while **2299 frames** were withheld from Mumble; `deafened: false → true`, zero
  unknowns. ADR 0162 could only measure this with a stub block.
- **`rescues: 0 → 1` on hardware**, closing the gap ADR 0162 recorded as unstageable. Muted 23:13:53,
  rescue #1 at 23:15:02, **relay resumed 23:15:05**.
- **Contention priced:** 20 tunes in 2.0 s against the running cadence — 0 failures, 0 new unknowns.
- **`acceptance.py`: 9 of 9 attempted PASS**, `split-minus` still SKIP (ADR 0161 finding 8, unmoved
  for a third cycle). **Two things reported rather than smoothed:** `auth` failed **1 of 3** full runs
  (detail not captured, **no cause claimed**), and **acceptance does not cover the cadence at all** —
  its restart drops the Mumble link, and with no bridge relaying there is no poller.

**Two corrections**

- ADR 0161's *"`MONITOR` makes that window far wider than during an over"* is **wrong**: an ordinary
  open squelch is `FUNCTION_INCOMING`/`FUNCTION_RECEIVE` and trips nothing.
- ADR 0162's `0x0874` control was **vacuous** — an empty `0x0873` is refused `ERR_SHORT` with every
  field zeroed, so it could not have differed between states.

**Carried forward, no code**

- **`0x0878` reports `tx_ok = 1` while F9 refuses to key — a firmware defect** to fix alongside
  whatever merges F9 to fork `main`. ADR 0159's rule that a published flag must not lie was applied
  to `0x087A` and not to the F7 frame carrying the same field. **F9 is still not on fork `main`**
  (`d086a23`); it lives on branch `f9-fm-tx-interlock` / pre-release `radio-server-f9-v5.7.0`, and a
  build from `main` has no TX interlock.
- **A second process on the AIOC tty kills the dock link and the transport never recovers** — the
  reader thread stops on `multiple access on port` while `/status` keeps answering. The cadence's own
  `deafened_unknown` climbing was the only visible symptom. Pre-existing fragility, newly observable,
  and a candidate for its own cycle.
- **Open question, deliberately not answered:** `GET /status`'s `broadcast_fm` block (the key-up
  snapshot) and `/link/status`'s `deafened` (the cadence) legitimately disagree during the
  front-panel window, and the bench showed it. Whether `/status` should render the cadence's reading
  is its own cycle.

`uv run pytest` **2117 passed, 5 skipped** (from 2088/5). `npx vitest run` **12 files, 116 tests**.
Red run 23 failed / 199 passed, all behavioural.

**Deployed:** the station runs `357a487` on branch `adr-0163-cadence-for-the-probe`, on **147.555**,
`tune_persist` off, broadcast FM off, both units active, config files byte-identical. The cadence
polls **nothing** until a link is brought up — that is the design, not a fault.

## Broadcast FM must not reach the bridges (2026-07-31)

[ADR 0162](adr/0162-broadcast-fm-must-not-reach-the-bridges.md). **The relay side gets a gate — and
the read ADR 0161 said did not exist turns out to be one frame nobody had sent.**

[ADR 0161](adr/0161-the-host-asks-the-radio.md) measured a station in broadcast FM relaying a
commercial station to `/audio/rx`. One `AudioHub` feeds three subscribers and two of them are
bridges, so that broadcast goes onto Mumble and onto a D-STAR reflector whose far end may be somebody
else's RF repeater. F9 stops the *local* transmitter and knows nothing about an internet link, which
makes this **97.113(b)** rather than untidiness.

**What shipped**

- **The mute lives in each bridge's relay loop, not at the hub — and the brief was wrong to ask for
  the hub.** [ADR 0085](adr/0085-mumble-rx-guard.md) decided this exact question for this exact
  asymmetry with the same two exemptions (browser Listen and the recorder), so those stay untouched
  *by construction*. The decisive reason is not precedent though: a hub can only deliver-or-not, and
  D-STAR needs *drop this frame **and reap the outbound over***.
- **The D-STAR placement is the whole change.** A bare `continue` leaks an over that nothing can ever
  close — FM arrives at full frame rate so the hang never fires, it is loud so the level gate never
  closes, and `_last_tx_feed` only moves inside the call we skipped. `_mode` stays `"tx"` for ever,
  and since `send_operator_audio` latches on it, **the browser operator could no longer key the
  reflector either.** The tidy one-line version mutes the broadcast and jams the D-STAR TX path in
  the same edit.
- **A probe, on the key-up path only**, closing ADR 0161 finding 5: a pre-key-up clear rescued a deaf
  station and could never report it. `rescues` now rides in the `broadcast_fm` status block.
- **Counters carry a tri-state.** `rx_deafened: 0` beside `deafened: null` means *nobody asked*;
  beside `false` it means *measured hearing*. A bare zero renders those identically, which is the
  trap `broadcast_fm` is a tri-state to avoid, one layer up.
- **A test pins the set of `audio_hub.subscribe()` sites**, so a fourth subscriber fails CI. That is
  the hub seam's one real advantage answered rather than dismissed.

**Two corrections to ADR 0161, both measured**

- **`broadcast_fm.on is True` is unreachable on F8/F9.** `Dock_FmOff()` clears `gFmRadioMode` as its
  first statement, unconditionally, and the reply reads state back from that variable. So the mute
  **ships armed and blind**, and the ADR says so in its first paragraph rather than shipping a Part
  97 control with a green suite and no on-air effect.
- **"The wire offers no read that is not also a repair" is wrong.** `Dock_SetFm` checks
  `TUNE && !gFmRadioMode → ERR_OFF` **before** the band limits, so an out-of-band TUNE returns
  `ERR_OFF` (hearing) or `ERR_BAND` (deaf) having touched nothing. Both statuses were already decoded
  and the guard is in F8 — no firmware needed. ADR 0161 reasoned from the action table instead of
  sending the frame, and `broadcast_fm_on.py --status`, described in its own help as *"a read
  probe"*, had been sitting in `scripts/bench/` since ADR 0160.

**Bench, on the deployed station**

- **Refuse-or-clamp answered first, because everything else was built on it.** `ERR_OFF` and
  `ERR_BAND` differ in **exactly the status byte**, a repeat is byte-identical, and the following OFF
  reported the receiver still on 104.3 MHz. The fork's host tests *force* `ERR_BAND`, so that branch
  had never executed anywhere, on host or radio, before this run.
- **The relay, on real broadcast audio.** `None` → Mumble 4111.5 RMS + **197 AMBE frames to the
  reflector**; `{on: true}` → **0.0 RMS and 0 frames**, browser unchanged at 122 880 B; `{on: false}`
  → relays again. ADR 0161 inferred the reflector leg; this measured it.
- **The `0x0874`/`0x0878` byte diff the brief asked for is a null result** — byte-identical with the
  BK1080 running, which is the point. It did surface that **`0x0878` reports `tx_ok = 1` on a station
  F9 will refuse to key** (ADR 0159 R4, answered empirically).
- Fall-through measured against a **genuinely keyed** radio (`ERR_TX`, witness caught the carrier at
  t=2.45s). `acceptance.py` **9 of 9 attempted stages PASS**; `split-minus` SKIP still prints
  `RESULT: FAIL` (ADR 0161 finding 8, unmoved, read past twice now).
- **No rescue fired on hardware and could not have** — staging one needs broadcast FM switched on
  *after* boot with the service running, which only the front panel can do. Same unrunnable-by-nature
  shape as ADR 0161's bench items 8 and 9; pytest-proven, recorded as an operator item.

**Green:** `uv run pytest` **2088 passed, 5 skipped** (from 2070/5). `npx vitest run` **12 files, 116
tests** — unchanged, and expected to be. Red run **8 behavioural failures**, after a first attempt
was discarded for putting a declaration inside a `runtime_checkable` protocol and cascading into nine
unrelated tests.

**Safety-relevant divergence, now written down:** **F9 is NOT on fork `main`** (`main` is `d086a23`,
F8). F9 is `d903881` on branch `f9-fm-tx-interlock`, PR
[#7](https://github.com/kbennett2000/uv-k1-k5v3-firmware-custom/pull/7) (open), pre-release
`radio-server-f9-v5.7.0`. **A build from `main` has `0x0879`/`0x087A` but no TX interlock and will
key while playing broadcast FM.** Nothing said so; the fork README now does.

**And a standing rule, promoted from an observation:** the mock cannot model `gCurrentFunction`, so
the whole `ERR_TX` class is invisible to pytest **by construction** and `acceptance.py` is the only
guard on it. That is why the fall-through was measured against a keyed radio rather than a fake.

### The deployed station is ahead of master ON PURPOSE — and here is how to put it back

`origin/master` relays broadcast FM to both bridges, measured on this station, so redeploying it
would restore a 97.113(b) hazard for tidiness.

- **Deployed commit:** `ef5f5c9` on branch `adr-0162-fm-not-to-the-bridges`
- **PR:** #219
- **When that PR has merged, run this on the bench box:**

```sh
cd /home/kb/applications/radio-server \
  && git fetch origin \
  && git switch --detach origin/master \
  && ~/.local/bin/uv sync --extra hardware --extra tts --extra mumble \
  && (cd web && npm ci && npm run build) \
  && systemctl --user restart radio-server
```

**Check the state with `git rev-list --left-right --count HEAD...origin/master`, not with the guard
ADR 0161 certified.** That guard (`merge-base --is-ancestor`) returns 0 for *behind* master as well
as *on* it, and was only ever tested against an "ahead" checkout. Between the two cycles a
`git reset --hard origin/HEAD` against a stale symbolic ref left this box **six commits behind**,
reporting healthy. `server-notes.md` now carries the two-number version.

## The host asks the radio instead of remembering (2026-07-31)

[ADR 0161](adr/0161-the-host-asks-the-radio.md). **The transmit interlock's two halves, connected —
and both of them measured on a radio for the first time.**

[ADR 0159](adr/0159-the-radio-refuses-to-transmit-while-deaf.md) put the interlock in the firmware
and reported it on `0x087A` bit 1; [ADR 0160](adr/0160-the-bench-answers-back.md) then measured that
the host cannot see broadcast FM at all, because `tuner.broadcast_fm` is written in exactly one place
— the boot assert — and that assert **clears the very condition the gate tests**. The radio held a
live answer nothing read; the host held a boot-time one and acted on it.

**What shipped**

- **`frames.py` reads bit 1**: `FLAG_FM_BLOCKS_TX`, `BroadcastFmReply.fm_blocks_tx`, `.will_key`.
  Goldens transcribed from `tests/host/test_dock.c:1405` at fork **`d903881`** — and note that the
  brief's "fork main at the F9 commit" is one merge optimistic: **PR #7 is still OPEN**, `main` is F8.
- **`_clear_if_deafened` replaces the attribute read at the head of `_key_on`.** The wire offers **no
  read that is not also a repair** — `0x0879` has no read-only action, `ClearBroadcastFm` builds only
  OFF, and state is reported *after* acting — so the one frame that answers "is this station deaf" is
  the frame that gives it its ears back. A key-up on a deaf station is **repaired and allowed**, and
  the refusal survives for a radio that was asked and did not stop. Gated on the *earned*
  `CLEAR_BROADCAST_FM`. Deliberately **not** on a poll (a GET that mutates; `ERR_TX` during every
  over; one serial link shared with tuning traffic).
- **ADR 0158's latch is dropped**, recorded as a reversal and not an about-face: it was never a
  mechanism, only the emergent property of never re-reading, and with the firmware refusing, a host
  lockout that cannot clear stops protecting anything and starts refusing key-ups the radio would
  allow. Pressing EXIT is now the whole remedy.
- **The wording in three places stops claiming the host is the thing refusing**, branching on bit 1
  because the consequence is a property of the *image*.
- **Every refusal in this arc now reaches the browser.** `RadioUnavailable` inside the `/audio/tx`
  frame loop was caught by **nothing** — three cycles of carefully written reasons reached the 503
  body, `tx_failed` and both bridges, and the operator saw *"Transmit connection dropped."*

**Bench, on the deployed station (this cycle ran on hardware)**

- **F9 confirmed on the wire**: `flags=0x03` with broadcast FM on. The brief asserted it; guardrail 1
  measured it.
- **ADR 0159's headline claim met a radio for the first time.** Witness at 445.800:
  **RMS 1065.9 → 0.0 while deaf → 965.5.** Carrier, no carrier, carrier.
- Three key-ups produced **exactly three** `0x0879` frames, each logged *before* the tune.
- **ADR 0160's open question is settled: the RX pump DOES relay broadcast-FM audio to `/audio/rx`** —
  RMS 3841.2 and **768 000 bytes**, against controls of **zero bytes** either side. A deaf station
  feeds a commercial broadcast station to the browser and both bridges, and nothing marks it as
  not-the-channel. See ADR finding 3.
- **The bench found a defect this cycle introduced**, which is what a bench is for. `Dock_SetFm`
  refuses `0x0879` with `ERR_TX` when `gCurrentFunction` is TRANSMIT **or MONITOR** — an open
  squelch, i.e. most of an active QSO — and treating that as a fault turned a busy receiver into a
  station that would not transmit. It took the **Part 97 station ID** down twice in four minutes
  before anything noticed. Fixed with `TuneBusy`: keeps the last reading, does not refuse, still
  refuses a station already known to be deaf.

**One draft claim corrected against the journal rather than shipped:** "no bench tune wrote EEPROM"
was false — **eight** did. The manual B1-B4 tunes wrote none (persistence off, `(not stored —
instant)`), one was the deliberate restore to 147.555, and **seven were `acceptance.py`'s**, because
its own `systemd` stage restarts the service and `tune_persist` is a *runtime* switch. ADR 0160
finding 6's remedy is defeated by the one suite that tunes most; `server-notes.md` now says so.

**Green:** `uv run pytest` **2070 passed, 5 skipped** (from 2043/5). `npx vitest run` **12 files, 116
tests** (from 11/110). Fail-first in two runs, because run 1's 33 failures were all "a name is
absent" and buried the evidence rather than being it; run 2 is **16 behavioural failures**.

### The deployed station is ahead of master ON PURPOSE — and here is how to put it back

`origin/master` still contains the boot-snapshot behaviour this cycle measured as wrong on hardware,
so redeploying master would restore a known defect for tidiness.

- **Deployed commit:** `8679bd5` on branch `adr-0161-host-asks-the-radio`
- **PR:** #218
- **When that PR has merged, run this on the bench box:**

```sh
cd /home/kb/applications/radio-server \
  && git fetch origin \
  && git switch --detach origin/master \
  && ~/.local/bin/uv sync --extra hardware --extra tts --extra mumble \
  && (cd web && npm ci && npm run build) \
  && systemctl --user restart radio-server
```

The deploy-state guard in [server-notes.md](server-notes.md) was **run against this exact state** and
is what to check first next cycle: `git log --oneline -1` and `git status -sb` both report the drift
as *nothing at all*; only `git merge-base --is-ancestor HEAD origin/master` answers, by exit code.


## The bench answers back (2026-07-31)

[ADR 0160](adr/0160-the-bench-answers-back.md). **The first cycle in the arc to run on hardware. No
`radio_server/` code changed, nothing fixed, nothing flashed, the fork untouched.** ADRs 0148→0158
each ended "nothing flashed, no hardware claim"; the operator flashed **F8 by hand**, and this cycle
measures what eleven cycles of reasoning claimed.

**Read this before quoting the firmware level: the radio is on F8, and two places in this repo said
otherwise.** `HANDOFF.md:810` ("the radio is not flashed with F7") and `server-notes.md` (F6) were
written by cycles that never ran on hardware, so nothing updated them after a manual flash. Both are
corrected in this PR. Confirmed over the AIOC: `0x0878` **and** `0x087A` both answer, `capabilities`
carries `set_modulation` + `clear_broadcast_fm`. The dock reports `F4HWN v5.7.0` — the Fusion *base*
version, which does **not** distinguish F6/F7/F8; only the opcode answers do.

**Two pre-flight findings killed the brief's premise before item 1.** The deployed checkout was on
`transmit-power` @ `4ad0a87` = **ADR 0146, twelve ADRs behind master**, running five days with none
of the code under test — *nothing in this project checks that*. And **`docs/BENCH.md` does not exist
here**; it is the fork's file, and no twin was created.

**What is now measured, not reasoned**

- **AM audio traverses the AIOC**, as two deliberately separate claims: an FM broadcast
  envelope-detected in AM (88.1 MHz, RMS 9653/8738/8298 vs 0.0 on dead channels), *and* a true AM
  airband transmission — 760-channel sweep, **738 reading exactly 0.0**, voice at 126.100
  (RMS 13351, speech **0.983**). The reciprocal control settles it: **124.775 AM → 7405.7,
  FM → 0.0**, same frequency, adjacent dwells.
- **The AM PTT refusal**, bracketed presence→absence→presence in one setup on 445.800:
  FM 200 / witness 69% / RMS 1298.5 → **AM 503 / 0% / 0.0** → FM 200 / 67% / 1275.6.
- **Raw `0x0878` bytes**: `7808040000010100` (AM, flags `0x00`) vs `7808040000000001` (FM, `0x01`).
  F7's `tx_ok` prediction, confirmed at the bit.
- **ADR 0155 and 0157's boot asserts** both hold against real hardware, including a genuinely deaf
  radio. The `hz` field reads back `104300000` where a clean boot read `64000000` — measured, not
  fabricated. **ADR 0156's `on=True` + `tx_ok=True` pair is real**, and the OFF-leg flash stall is
  **ttfb ≤ ~0.1 s**, settling a fork placeholder sourced only from a firmware comment.
- **Broadcast-FM audio reaches the AIOC**: `arecord` at the sound card, **RMS 3308.5 / 3028.7 ON vs
  107.1 / 108.7 OFF**, controls both sides. The fork's §8 placeholder, answered. The *browser-relay*
  half is **not** established and is recorded as not run.
- **The front panel, photographed.** `F`+`0` → `FM 104.3 VFO 87.5-108M`; `EXIT` → `445.800 FM H W
  SQL1 12.50K`, so **item 7's channel restore is confirmed at the panel with no restart**. And
  **ADR 0156's "dead keypad" premise is wrong** — digits type a frequency into the *broadcast* VFO
  (`147.-` under an `87.5-108M` band line) and `M` offers `CH-01 SAVE?`. The keypad is not dead, it
  is repurposed, which is worse: typing a frequency moves the wrong receiver.
- **Station afterwards:** `acceptance.py` **9/9 attempted stages PASS** (exit 1 only from
  `split-minus`, unattemptable — no `Bench Split Minus` preset, a gap predating this cycle);
  `pytest` **2043 passed, 5 skipped**, identical to master. Restored to **147.555**, broadcast FM
  off, both units active.

**Findings carried forward**

1. **ADR 0158's interlock cannot fire on this station, and items 8/9 are recorded UNRUN because of
   it.** `broadcast_fm` is written only in `AiocBaofeng.__init__`, and the boot assert **clears the
   condition the gate tests**. FM switched on at the panel afterwards leaves the block `{on:false}`
   and `tx_ok` true, so a key-up is not refused — it transmits blind. Verifying the host refuses
   *was* the test; the answer is that it cannot see. This confirms 0158's own finding 1 on hardware
   without putting a carrier on air, and it relocates the successor's brief: 0158's finding 2 named
   the latch, but the gate never engages, so there is nothing to un-latch. 0157's R2 pre-key-up
   re-read fixes both, and now has a measured cost (**≤ ~0.1 s**, not the 3.0 s feared).
2. **`status.rssi` is `null` on the `baofeng` backend** — a `uvk5`-dock field with no source in
   `aioc_baofeng.py`. There is **no host-side signal-strength read on the deployed operating mode at
   all**, which is why the airband hunt had to be audio-RMS, and why `server-notes.md`'s "check
   `.rssi` first" advice does not apply here.
3. **Deployment drift is unchecked.** No cycle, and no stage of `acceptance.py`, verifies the box is
   on master before testing it.
4. **`POST /radio/select` answers 200 throughout but blocks**: a `/status` landing in the window took
   **9.752 s of a 10.473 s** rebuild. "No longer blocks" is true as "no longer fails", false as
   "answers promptly".
5. **The preset highlight lies** (every compared field identical across FM→AM) and **`/presets`
   carries no `modulation` key**, so the fix spans server *and* browser — not UI-only.
6. **There is no host route to clear broadcast FM.** The boot assert is the only caller, so the
   documented operator remedy is the only remedy.
7. **`server-notes.md` was wrong three times in one day** — firmware level, operating frequency
   (147.555, not 445.800) and D-STAR (`configured: true`, 27 076 RX frames, not disabled).
8. **`POST /radio/select` persists to `radio.toml`** (`api/app.py:1763`). Timing a backend switch
   therefore rewrites the deployment's hand-annotated config. Net delta here was two previously
   implicit defaults made explicit; all 41 presets and 203 comment lines survived. Document it in
   `docs/api.md` or stop writing on a live switch.
9. **Fork-side `⚠ CONFIRM AT BENCH` placeholders this cycle settles are listed in the PR body**, not
   applied — the fork was off-limits. A follow-up cycle should apply them.
10. **A second session switched the branch under this one mid-cycle.** Both were working in the same
    checkout; HEAD moved to `adr-0159-…` with this cycle's edits in the tree. Recovered by restoring
    their tree untouched and moving to a `git worktree`. **Concurrent cycles need separate worktrees**
    — the deconfliction rules cover files, not the shared working tree.
## The radio refuses to transmit while deaf (2026-07-31)

[ADR 0159](adr/0159-the-radio-refuses-to-transmit-while-deaf.md). **Firmware cycle — no
`radio_server/` code changed**, nothing flashed, no hardware claim. Reasoning here, code in the fork;
ADR 0149's split. The transmit interlock's firmware half: a UV-K5 whose second receiver is running
will no longer key. Fork PR
[#7](https://github.com/kbennett2000/uv-k1-k5v3-firmware-custom/pull/7), pre-release
[`radio-server-f9-v5.7.0`](https://github.com/kbennett2000/uv-k1-k5v3-firmware-custom/releases/tag/radio-server-f9-v5.7.0).

**Ground truth, re-read rather than inherited — and one item overturned the plan's worry.** The
AIOC's DTR line drives `GPIO_PIN_PTT`, the **same pin** the rubber PTT button drives; there is no
separate external-PTT path, so `GENERIC_Key_PTT` -> `gFlagPrepareTX` -> `RADIO_PrepareTX` gates both.
The FM screen's `goto start_tx` is **not** a bypass — it jumps forward to the label that sets that
same flag. Dock `REG_30` keying **is** a bypass, exactly as ADR 0149 recorded.

**What shipped**

- **One clause in `RADIO_PrepareTX`**, appended after the AM gate. ADR 0158's severity-first ordering
  deliberately does *not* transfer: in firmware both causes set one state and produce one beep, so
  there is nothing to order.
- **`0x087A` `flags` gains bit 1 — a bit, never a ninth byte.** Three consumers hard-require the
  8-byte reply and one is the deployed `frames.py` this cycle could not touch; a ninth byte would
  have made every F9 radio unparseable to the server running today.
- **It reports blocking, not readiness.** `flags` blanks to 0 on refusals and pre-F9 firmware answers
  0, so a readiness bit would let a lost frame or an old radio stop a transmitter. The `tx_ok` rule,
  on the wire. Bit 0 keeps its published meaning; read `will_key = TX_OK && !FM_BLOCKS_TX`.
- **One predicate, three consumers** — gate, flag, and host tests — in a new include-free header. F7
  reported `TX_OK` through a hand copy of `radio.c`'s AM gate; doing that twice would let a host be
  told the radio will key after the radio had refused.
- **Fusion only, opt-IN — this revises the brief**, which asked for the AM gate's opt-OUT `#ifndef`
  idiom. That idiom leaves the gate active when the flag is *undefined*, which would have switched
  three F4HWN editions' front-panel behaviour. See finding 1.
- **Dock keying is not covered and the ADR does not pretend otherwise.** A term in `Dock_ForceTx` was
  *rejected, not deferred*: `0x0850` sends no reply, so a refusal there is a host that keys, radiates
  nothing, and is told nothing.

**Fail-first, four runs, and the harness failed one of them.** `dock.c` carrying only bit 0: **4
failures / 155** — the blanking case, the OFF-vector case and the two bit-1-clear combinations stayed
green and are named, not counted. Predicate hardwired `false`: **1 failure**, Fusion compilation only,
which is correct since `false` is the right answer for the other two shapes. Predicate hardwired
`true`: **5 failures across all three compilations** — and this run exposed a defect in the test
harness itself, `|| exit 1` stopping at the first failing binary so two compilations never ran.
Recorded rather than quietly fixed; the target now ORs the exit status and the mutation was re-run.
Interlock without `ENABLE_FMRADIO`: CMake `FATAL_ERROR` fires.

**Green.** Host tests **161 checks, 0 failures** (155 dock, up from 144, plus 6 predicate across three
compilations). `uv run pytest` **2043 passed, 5 skipped — unchanged, no `radio_server/` code
changed.** FLASH **106,152 B / 118 KB (87.85%)**, **+16 B** over F8, 14,680 B free; the
interlock-**off** build links at **exactly 106,136 B**, F8's figure to the byte, so the feature
compiles out.

**Findings carried forward**

1. **The narrowing to Fusion is a decision, not an omission.** ADR 0158's *correction, not regression*
   was argued for a host-controlled station whose operator cannot hear what they are keying over. It
   does not transfer to a handheld operator who deliberately opened the FM radio and deliberately
   pressed PTT. The fork's guardrail 5 (*"nothing in the radio's own operation changes until a host
   sends `0x0870`"*) now names F9 as its one deliberate, confined exception rather than standing as a
   rule the tree violates. The opt-in/opt-out polarity difference is **provenance, not debt** —
   comments at both gates say so, because the obvious cleanup would silently widen a behaviour change.
2. **The host cannot read bit 1 yet, so it still guesses.** `frames.py` was out of scope, so the
   server keeps predicting from a boot-time snapshot while the radio holds a live answer. The
   instrument exists and nothing reads it. This is the most obvious successor.
3. **ADR 0158's latch is now a liability, not a backstop.** With the firmware refusing, the host's
   latching lockout stops being a safety mechanism and becomes a **UI accuracy problem**: it can
   refuse a key-up the radio would allow, on a station that has heard fine since the operator pressed
   EXIT. That raises R2 (the earned-capability pre-key-up re-clear) from "named successor" to the
   thing that should land before the host gate causes more trouble than it prevents. **Someone reading
   ADR 0158 alone will get this backwards.**
4. **`uvk5`/dock keying is now the ONLY ungated keying authority.** 0158 R3 stays open. The honest fix
   is a reply-bearing key opcode, not a silent term in `Dock_ForceTx`.
5. **`Dock_ModulationCanTx` is still a hand copy** of `radio.c`'s AM gate. F9 fixed this pattern for
   its own flag and left F7's as it found it; folding `TX_OK` onto the shared predicate is a small
   mechanical firmware cycle that removes the last place a reported flag and the behaviour it reports
   are written twice.
6. **Nothing still checks fork/`frames.py` byte-compatibility** (ADR 0148, open since). F9 added a
   wire bit by hand on one side, which is exactly the shape that cross-check exists to catch.
7. **The fork's CI is dead weight and was found so while looking for somewhere to run the new tests.**
   It runs no host tests, builds the `Custom` preset rather than `Fusion` (so it never compiles the
   dock at all), uploads an artifact path a commented-out packing step stopped producing, and calls a
   script that passes `docker run -it` on a TTY-less runner.
8. **`BENCH.md` §9's expectations were rewritten, not just extended.** It previously read "Assert the
   AIOC's DTR line. Expected: **also transmits.**" — a bench run against that on an F9 image would
   report the feature as a fault. One new `⚠ CONFIRM AT BENCH` item added; **existing markers
   untouched**, since the bench session is holding those updates for a follow-up.
9. **`docs/api.md`'s prose remains unguarded by any test.** Carried unchanged.

Everything carried by ADR 0158 below stays carried, except its R1 (this cycle) and its R6 (the
published `0x0879` OFF vector).

---

## The host refuses to key a station that cannot hear itself (2026-07-31)

[ADR 0158](adr/0158-the-host-refuses-to-key-a-station-that-cannot-hear-itself.md). **The transmit
interlock, host half — no firmware, nothing flashed, no bench claim, still no ON path.** ADR 0157
built the instrument; this spends it. A station whose second receiver is known to be running now
refuses every key-up: browser Talk, both bridges, the controller's station ID and announcements,
`POST /ptt` and `POST /transmit`.

**Read this before quoting ADR 0157: F8 is merged, and 0157 says it is not.** Fork `origin/main` is
now **`d086a23`**; `git merge-base --is-ancestor 5f4c581 origin/main` returns **true**; PR #6 merged
**2026-07-31T15:38:12Z**, about 75 minutes *before* PR #214 (ADR 0157) landed at `16:53:07Z`. So
0157's verification was true when it was run and stale when it was merged, and its "no radio in
existence runs F8" must not be quoted forward. The earned-capability decision it supports is
unaffected — earning the capability is right either way — but the firmware successor is now
**unblocked**, not blocked. Nothing was flashed this cycle and no bench result is claimed.

**What shipped**

- **One predicate, and the shared helper is the deliverable.** `refuse_if_deafened` in
  `backends/base.py`; `AiocBaofeng` and `MockRadio` both consult it. Not a factoring convenience —
  the *message* is what this cycle ships, and two copies of a message is exactly how two distinct
  causes converge into one undiagnosable "cannot transmit".
- **Only a definitive `on=true` refuses.** A `null` block never does. The `tx_ok` rule for the
  `tx_ok` reason: an unmeasured field must never lock a transmitter. Pinned in both directions at
  the predicate, the backend, the API, both bridges and the browser.
- **First in `_key_on`**, ahead of the channel re-assert. Cheaper than the step it displaces;
  severity-first, which `__init__` already argued for the boot asserts and a test already pins; and
  the deciding reason — `_reassert_channel` can raise its own `TuneError`, and an unrelated tune
  failure must not be able to mask the worse fault.
- **Nothing else changed to carry it.** No route, no exception type, no counter, no event, no second
  mechanism. `RadioUnavailable` already reached the 503 handler, both bridges' `except` ladders,
  `Controller._keying` and `TxSession`'s unwind. That those worked untouched is *asserted*, not
  assumed.
- **`MockRadio` enforces it**, and the line against its `tx_ok` stance is written down: the AM
  refusal predicts what a PTT pin will do and there is no pin here; this is server policy about a
  state the mock genuinely models, and server policy must be drivable without hardware.
- **TalkControl gains the cause and, in its sub-line, its own limit** — it cannot see broadcast FM
  switched on at the radio's keypad, so an enabled Talk button is not proof the radio is hearing.
- **Finding 7's recorded fix was a no-op.** The protocol already declared the method; *nothing ever
  checked the protocol*. The boot assert now guards on `isinstance(tuner, Uvk5Tuner)` — verified
  empirically on Python **3.12.13**, data members included — skipping to `null` plus a WARNING
  naming the type, never to "off", and deliberately **not** applied to the demodulator assert.

**Fail-first and green.** Red run 1: **13 behavioural failures + 1 collection error** (13 failed,
303 passed, 1 error) across two bridges, a controller, both REST key paths, `TxSession` and the
backend — including the one reproducing finding 7's `AttributeError` out of a constructor. The
collection error is **weak evidence** (a missing name, not wrong behaviour) and is labelled so.
Seven tests that passed on master are named as regression guards, **not evidence**. Browser: 9 new
vitest tests, **5 red** without the JSX change (verified by restoring the component and re-running),
4 passing trivially. Green: `uv run pytest` **2043 passed, 5 skipped** (from 2014/5, **+29**);
`npx vitest run` **11 files, 110 tests** (from 101, **+9**).

**Findings carried forward**

1. **The firmware half is unblocked, and its lead argument has changed.** A `gFmRadioMode` term in
   `RADIO_PrepareTX`. The case for it is no longer divergence-versus-correction — it is that **the
   host gate cannot see the front panel**. F+0 on the radio leaves the station deaf with a live Talk
   button and this gate silent, and a host gate also fails open on a crash. Position taken:
   **correction, not regression**; blind transmit has no defensible operating practice. Costs stand
   (upstream divergence per ADR 0148; changed front-panel behaviour). PR #6 has merged, so this
   waits only on a decision and a cycle.
2. **This interlock LATCHES, and un-latching it is the named successor.** Nothing re-reads the
   block, so clearing broadcast FM at the radio does not clear the refusal until the process
   restarts. First refusal here an operator cannot fix from the front panel. The fix is ADR 0157's
   R2 pre-key-up re-clear, and the numbers matter: done naïvely it is **3.0 s** of dead air before
   every over on firmware that cannot answer, then a `TuneError` that would refuse every key-up on
   every station. Gated on the **earned** `Capability.CLEAR_BROADCAST_FM` it costs a frozenset
   membership test on such firmware and one dock round trip elsewhere — and the circularity that
   forbids that gate at boot does not apply in the key path.
3. **`AiocBaofeng` advertises `CLEAR_BROADCAST_FM` with no `Radio`-level method behind it**, while
   `MockRadio` has one. Harmless today (no route calls it) but it is the shape guardrail 3 forbids,
   and it is the missing piece of an in-band remedy for finding 2's latch. The route cycle should
   fix it rather than discover it.
4. **The `uvk5` backend has the identical hazard, no assert and no gate**, and holds full control
   (`0x0870`) so `0x0879` answers `ERR_BUSY`. Its block is permanently `null`, so the predicate is
   an honest no-op there. Structurally unfixable over this wire today.
5. **Firmware-version negotiation** — the empty-`0x0879` probe is still the real fix for paying a
   round trip against firmware that cannot answer.
6. **The fork still publishes no OFF vector.** Derived and labelled derived in ADR 0157; now that
   PR #6 has merged, publishing it is a small firmware-repo cycle.
7. **New obligation on tuner fakes, now enforced rather than remembered.** A double advertising
   `SET_MODULATION` must satisfy `Uvk5Tuner` or its broadcast-FM assert is skipped with a warning
   naming it — previously an `AttributeError` out of a constructor.
8. **`docs/api.md`'s prose remains unguarded by any test.** Every edit here was hand-checked; the
   contract test only verifies that each capability *string* appears somewhere.
9. **Process, not code: an argued decision that is not written down did not happen.** ADR 0157 was
   asked to evaluate moving the boot asserts to `build_radio` and to argue it either way. The
   evaluation happened in that cycle and never reached the record, so it had to be asked again.
   ADR 0158 settles it: **they stay in the constructor** — `backend_kwargs` is Settings-derived and
   cannot express the `tuner=` / `_serial_factory=` / `_audio=` DI seams, `build_radio` returns a
   `TotRadio` wrapper, and moving them would newly run the asserts against `MockRadio`.

Everything carried by ADR 0157 below stays carried.

## A station that cannot hear itself (2026-07-31)

[ADR 0157](adr/0157-a-station-that-cannot-hear-itself.md). **Host side of broadcast FM — no
firmware, nothing flashed, no hardware claim.** The server can now turn broadcast FM **off** and
report what the radio said. There is deliberately **no way to turn it on**; that is the cycle after,
and it needs the transmit interlock that does not exist yet.

**Verify this first, because it shapes everything below: F8 is not merged.** Fork `origin/main` is
`4e1d9dc`; `git merge-base --is-ancestor 5f4c581 origin/main` returns **false**; `0x0879` has zero
hits in that history and `DOCK_CMD_SET_FM` zero in its tree. F8 lives only on branch
`f8-dock-broadcast-fm` at **`5f4c581`**, behind **open PR #6**. Every golden was read from that
commit — reading `main` would have produced *nothing at all* rather than an error. So **no radio in
existence runs F8**, and a silent `0x0879` is the normal case rather than a fault.

**What shipped**

- **`frames.py`: `0x0879`/`0x087A`.** `ClearBroadcastFm` has **no action parameter**, so "this server
  cannot turn broadcast FM on" is a property of the code. `BroadcastFmReply` parses **every** state
  including ON — you can always learn the radio is deaf, you can only tell it to stop. **Only the
  reply is in `_DISPATCH`**, and that is not bookkeeping: without it `0x087A` decodes to
  `RawMessage`, the `isinstance` match never fires, and every clear times out against a radio that
  answered correctly.
- **`Capability.CLEAR_BROADCAST_FM`, earned rather than configured.** It appears only once a radio
  has answered `0x087A`. A static member in `SETVFO_CAPS` would have every station advertising a
  firmware generation nobody is running (guardrail 1). **Any reply earns it, including a refusal** —
  a refusal proves the opcode exists; only silence earns nothing. **Re-earned on every reply**, not
  once at boot, because a boot-only probe would leave a radio switched off at startup missing the
  capability for ever with **no operator remedy**. First capability with **no route**, said plainly
  in `api.md`.
- **`RadioStatus.broadcast_fm`, a tri-state block** (`PaState` precedent): `null` = does not know,
  `{on:false}` = knows, and the station can hear. Those must never render the same. **`ERR_NO_HAL`
  maps to a definitive `on:false`** — no `ENABLE_FMRADIO` means no BK1080 driver, so it *cannot* be
  deaf; the one status on this wire that is a certainty.
- **Two startup asserts, broadcast-FM first, each with its own `try`/`except`.** ADR 0153's lesson
  where it would bite universally: `0x0879` times out on every radio today, so a shared handler
  would silently cost every station the ADR 0155 demodulator assert as collateral.
- **`holder.py`: both factory calls moved to `asyncio.to_thread`.** `rebuild` called it synchronously
  inside `async def` and `_restore` again on rollback. `_restore` is now `async` (private, two call
  sites).

**Fail-first twice, and the weak run is labelled weak.** Run 1, before any `radio_server/` change:
**3 collection errors** — import-level, which proves the names are absent, not that any behaviour is
wrong. Run 2, after the wire layer landed: **18 failed, 1996 passed, 5 skipped**, behavioural and
for the right reasons. Green: **`uv run pytest` 2014 passed, 5 skipped** (1981/5 before, **+33**);
**vitest 11 files, 101 tests, unchanged**.

**Named as NOT evidence:** every new frames golden (the codec was written in the same step), plus
four tests that passed **vacuously** in the red run because nothing was being sent yet —
`test_a_failed_broadcast_fm_clear_does_not_skip_the_demodulator_assert`,
`test_an_eeprom_tuner_is_never_asked_to_clear_a_receiver_it_cannot_reach`,
`test_a_plain_uv5r_is_not_asked_about_a_receiver_it_does_not_have`,
`test_clearing_broadcast_fm_arms_no_transmit_lockout`. Regression guards from here, not proof of
this cycle.

**Two arguments this cycle got wrong and recorded rather than quietly dropped.** Both concerned
assert ordering, and a wrong reason in the record is worse than none because the next cycle inherits
it. (a) *"Modulation-first re-deafens the radio via `Dock_RestoreFmAudio`"* — no: that helper
restores audio for a BK1080 **already** running, so it preserves deafness and cannot create it. This
was the cycle's own first argument. (b) *"OFF-first stalls the next frame behind the flash erase and
it gets dropped"* — no: `dock.c` replies as its **last** statement, after the HAL returns, and the
tuner is synchronous request/reply, so the erase is absorbed by the round trip. The surviving reason
is severity-first, and the ADR says both orders converge.

**Findings carried forward**

1. **The transmit interlock, and it is an open question, not a task.** `gFmRadioMode` does not gate
   `RADIO_PrepareTX`, so a host-side TX gate is the only protection against transmitting while deaf
   **and it fails open** — on a crash, and on any direct API call reaching the keying path. Firmware
   (a term in `RADIO_PrepareTX`; also changes front-panel behaviour and diverges further from
   upstream), host alone (cheap, testable, wrong the moment anything keys without it), or both.
   Unresolved on purpose.
2. **And the asymmetry that sharpens it:** `_reassert_channel` already re-asserts the *modulation*
   before every key-up, so this cycle gives the **more** dangerous state only a boot snapshot. A
   pre-key-up clear is the host half of finding 1. Cheaper than it looks — `SETTINGS_WriteCurrentState`
   short-circuits on `memcmp`, so clearing a receiver that was never on writes no flash.
3. **The `uvk5` backend has the identical hazard**, no assert, and holds full control (`0x0870`), so
   `0x0879` would answer `ERR_BUSY`. Structurally unfixable over this wire today.
4. **Firmware-version negotiation** — the empty-`0x0879` probe is published and byte-pinned here, and
   is the real fix for paying a 3 s round trip against firmware that cannot answer. Moving the call
   off the loop does not make the round trip unnecessary.
5. **The fork publishes no OFF vector.** `PROTOCOL.md` has four `0x0879`/`0x087A` vectors and none of
   them is the frame this server actually sends; it was **derived** here and labelled as derived.
   A firmware cycle should publish it.
6. **`docs/api.md`'s prose counts are unguarded by any test** — the contract test checks only that
   each capability *string* appears. The "nine members of `CAT_CAPS`" sentence and the 501 handler
   list were fixed by hand.
7. **New local-fake obligation.** A fake tuner advertising `SET_MODULATION` is now asked to clear
   broadcast FM; one lacking the method raises `AttributeError` **out of a constructor**, which no
   `(TuneError, OSError)` handler catches. `FakeSetVfoRadio` has the same trap via its `msg.rx_hz`
   fallthrough. Extend the fakes **before** the backend.
8. **No UI, and not for scope** — the server never re-reads, so a status row would say "off" while an
   operator's own FM keypress left the station deaf.

Everything carried by ADR 0156 below stays carried.

## A deaf station can still transmit (2026-07-31)

[ADR 0156](adr/0156-a-deaf-station-can-still-transmit.md). **Firmware cycle — no `radio_server/` code
changed**, nothing flashed, no hardware claim. Reasoning here, code in the fork; ADR 0149's split.
Adds `0x0879`/`0x087A`, the dock's reach into the radio's **second receiver**.

**The gap.** The UV-K5 carries a BK1080 commercial-FM chip (64–108 MHz) on the same I²C bus as the
BK4819 every other opcode drives. `0x0850`/`0x0851` cannot reach it — those are BK4819 register
access and the BK1080's registers are not in that space — so the capability has been sitting in the
Fusion image, compiled and working, with no way to ask for it. **Zero prior mentions in this repo:**
`BK1080`, "broadcast FM", "commercial FM" and "88-108" have no hits anywhere.

**The thesis, and it inverted what this cycle expected.** Turning broadcast FM on puts the BK1080 on
the speaker line **the AIOC listens on**, so the station stops hearing its own channel. It does
**not** stop transmitting — traced, not assumed: `RADIO_PrepareTX` has no broadcast-FM term anywhere
in its refusal chain, `MAIN_ProcessKeys` refuses every key *except `KEY_PTT` and `KEY_EXIT`,
whitelisted by name*, and `GENERIC_Key_PTT` is an explicit `goto start_tx` on the FM screen. So the
radio transmits normally into a channel it cannot monitor, including the station ID guardrail 5
requires. That is **worse than the AM fault** the 0149→0155 arc spent five cycles on: AM produced
silence over the air, which is at least detectable at the far end.

**`tx_ok` is carried and is orthogonal — that is why it is carried.** The cycle expected
`gFmRadioMode` to gate TX and to get the host side nearly free from it. It does not. The flag reports
the BK4819 demodulator, which the BK1080 does not touch, so it answers exactly what it would without
F8. It stays because a host holding one frame must be able to read `state = ON` and `tx_ok = true`
together — not a contradiction to explain away, but the actual and dangerous state of this radio.
**There is no bidirectional interlock and the ADR does not describe one.**

**What shipped**

- **Census re-run before a number was picked** — all three disagreeing sources, union of every
  claimed opcode; `0x0879`/`0x087A` appear in none, `0x0875/6` stay claimed-not-free.
- **Hz on the wire, off-raster frequencies refused not rounded.** `0x0873` truncates sub-10 Hz
  because 10 Hz of a repeater channel is nothing; 100 kHz of the broadcast band is a whole adjacent
  station. Band is on the wire because `FM_Band` is a **two-bit field** that turns a 4 into a 0
  inside the assignment with no diagnostic, and out-of-band **underflows** `channel = freq - loLimit`
  in uint16.
- **Three different blanking sentinels**, because `0` is a real reading of `state` (OFF) and `band`
  (87.5–108) but not of `freq_hz` — `0x0878`'s lesson applied field by field rather than copied.
- **Refused while keyed: one interlock, two halves.** `ACTION_FM`'s guard is mirrored *including
  MONITOR*, but it cannot see the dock's own REG_30 key, so `dock.c` refuses on `ctx->tx_on` too.
- **Command path writes no flash; the OFF leg does** — see finding 1 below for why that is a
  correction rather than an exception.
- **The screen follows the radio**, because PTT keys off `gScreenToDisplay`.
- **One `Dock_RestoreFmAudio` helper on all four dock paths**, since `RADIO_SetupRegisters` mutes a
  running BK1080 and never clears `gFmRadioMode`.

**Fail-first twice.** An always-succeeds stub: **139 checks, 18 failures**. Both goldens, the
binding-reached-once check and the read-back checks **stayed green** against it — recorded so nobody
cites them as evidence the validation works. Plus a free compile-time red: the eighth `dock_hal_t`
member fails `-Wextra -Werror` until all four positional initialisers move. **One failure in that run
was a defect in the new tests, not the stub** (the F7-golden regression check omitted
`g_mod_force_flags = 0`), recorded rather than quietly fixed.

`make -C tests/host run` **144 checks, 0 failures** (98 before), plus `check-fm-restore` reporting
`4 RADIO_SetupRegisters, 4 Dock_RestoreFmAudio — paired`. Goldens derived from an **independent
reference framer** that reproduces both published F7 vectors byte-for-byte before its new output was
trusted. `0x0874`/`0x0878` pinned byte-identical. FLASH **106,136 B / 118 KB (87.84 %)**, **+560 B**,
14,696 B free; the `ENABLE_FMRADIO=off` build links clean, so `ERR_NO_HAL` is a real firmware shape.
`uv run pytest` **1981 passed, 5 skipped — unchanged.**

### Carried forward

1. **A host crash or unplugged cable mid-FM leaves the radio booting into broadcast FM.** The firmware
   persists FM mode behind the host: `app.c:1761-1767` calls `FM_Start()` — flash write and all —
   about five seconds after any squelch close, armed by `functions.c:106-108`. **No command path can
   prevent it**, which is why the OFF leg writes `CURRENT_STATE = 0` and why that is a correction to
   the "no EEPROM write" rule rather than an exception granted to it: clearing a bit the firmware set
   is the only way to undo one. A crash never sends an OFF. **The fix belongs to the host cycle** —
   extend ADR 0155's startup assert to assert broadcast-FM OFF as well as a demodulation.
2. **The OFF-leg erase blocks the UART handler for an unmeasured time.** `PY25Q16_WriteBuffer` calls
   `SectorErase` then spins in `WaitWIP`. The driver's own comment says *"Erase takes ~300ms"* and
   **that comment is the only source** — a bench item, not a number to design around (guardrail 1).
   Bounded by choice, not luck: it is on the OFF leg where the audio is being torn down anyway, and
   never on TUNE.
3. **`SETTINGS_WriteCurrentState` flushes more than `CURRENT_STATE`** — `SCAN_LIST_DEFAULT`,
   `SCAN_LIST_ENABLED`, `SCANLIST_PRIORITY_CH[0..1]`, `CHAN_1_CALL` — so an unrelated RAM/flash
   divergence rides along on the OFF leg.
4. **`uart.c` now duplicates ~8 lines of `fm.c`.** `Dock_FmOn` is `FM_Start` minus the flash write; if
   upstream changes `FM_Start`, this does not follow. The alternative was splitting `fm.c` and
   repointing `app.c`'s two restore paths — three upstream files, and it breaks the one legitimate
   persistence case.
5. **The audio-restore guard is a grep, not a test.** `tests/host/Makefile` compiles `test_dock.c` and
   `dock.c` only; `uart.c` is not host-compiled by design (guardrail 4), so no host test can see these
   functions. `check-fm-restore` counts call sites and was verified to fail on a negative control. It
   catches a forgotten fifth site; it cannot tell anyone the restore works.
6. **The opcode census is still in three places that disagree** — third cycle running that it has been
   read and worked around rather than reconciled. This cycle also found the **`frames.py:113-140`
   citation is stale** (the enum is `96-165`), carried in ADR 0149, ADR 0150 and this file.
7. **Everything ADR 0155 carried is still carried**: `doctor` builds no tuner, the EventHub does not
   exist at backend construction, the AM-mismatch branch, a radio powered on after the server,
   `POST /mode`'s 500, `MockRadio.set_mode`, `GET /presets` omitting `modulation`, the `REST_PATHS`
   drift, and the three websocket talker-slot leaks (`app.py:1873`, `:1995`, `:2095`).
8. **`docs/uvk5-setup.md`'s flash recommendation still points at the F6 release**, now two levels
   behind. Whether an F7 or F8 release tag exists is a release fact this cycle cannot verify from
   here, and guardrail 1 forbids asserting one from memory.

---

## A restart must not inherit the demodulator (2026-07-31)

[ADR 0155](adr/0155-a-restart-must-not-inherit-the-demodulator.md). Host cycle — **no firmware, no
UI, no API change**, nothing flashed, no hardware claim. Closes the last path in the AM arc that
never asserted a demodulator: **process start.**

**The gap.** The UV-K5 keeps its modulation in the dock **session** — RAM a host restart does not
touch, and only the radio's own power switch reseeds. `AiocBaofeng.__init__` sent **no frame to the
radio at all**, and `_reassert_channel` returns early on `self._tuned is None`, before its modulation
block. So a server restarted against a radio left on AM reported `modulation=null, tx_ok=null` —
and then **every layer above behaved correctly and the fault still landed**: `_refuse_if_tx_disabled`
refuses only a *measured* `False`, the ADR 0154 UI never locks on `null`, and the first key-up drove
DTR into a transmitter `VFO_STATE_TX_DISABLE` had already disabled. Silence on the air, `status()`
reporting `transmitting`, and the transmission it ate was the station ID. `presets.py` already
stated the rule — *"a reconnecting server must state what it wants rather than assume FM"* — but only
of the path through `apply_preset`, which **boot does not run through**.

**Write beats read, and the losing option's cost is recorded.** There is no get-modulation opcode
(adding one means a fork, a flash and a bench proof to *learn* what one frame can *make* true);
inferring from BK4819 over `0x0851` reads the firmware's leftover rather than its VFO truth — the
argument already written for `_pa` and for split; and even a *successful* read is ADR 0132's "take
whatever state you find". A cycle whose goal is a true belief must not get there by reversing the
rule that made the belief honest.

**What shipped**

- **`BOOT_MODULATION = "FM"` + `_assert_boot_modulation()`**, the last statement of
  `AiocBaofeng.__init__`, after the `atexit`. One `0x0877`, no HELLO, no `0x0873`, no flash write,
  nothing keyed, **no lockout armed**.
- **The constructor, not the lifespan**, even though lifespan is the house pattern for best-effort
  boot work: `RadioHolder.rebuild` and `_restore` each construct a fresh backend, so a lifespan step
  would let a **live backend swap reopen the same gap**.
- **Best-effort**, unlike the fail-loud `apply_port_settings` twenty lines above. The asymmetry is
  the point: that one fails when the *handle* is unusable and there is nothing to degrade to; this
  one fails when a good handle has a radio switched off at the far end, and the failure is
  **representable** as the `None` the tri-state was built for.
- **Gated on `Capability.SET_MODULATION`**, mirroring `presets._apply_fields` — never on `hasattr`,
  which every tuner in the package satisfies and one of which exists only to raise.
- **`except (TuneError, OSError)`, argued member by member**, including two exclusions. `Uvk5Timeout`
  is already converted upstream and is pinned by a test *instead*, because widening the tuple would
  **hide** a refactor that dropped the conversion. `ValueError` is loudly out: the only way to get one
  is a typo'd constant, which must fail at construction.
- **Fail-first 1 red**: `assert None == 'AM'`. The test asserts the belief **equals the radio's
  state**, not that it equals `"FM"` — the literal would still pass on a host that guessed right by
  luck.

**Two decisions taken with their residuals stated as accepted cost, not oversight:**

- **No config key**, so a restart *always* pulls the radio out of AM with no opt-out. A
  modulation-only key would be **the sole member of an incomplete set** — no boot frequency, no boot
  tone — so an unattended airband monitor would return on the right demodulator and the wrong
  frequency, which is not a working station. The whole-shaped successor is a **boot preset** reusing
  `apply_preset`. Future item, not this cycle.
- **A failed assert logs and nothing more, for correctness rather than economy.** `null` renders as
  `—`, and `—` is **true**. A UI channel would explain *why* the value is unknown; it would not make
  it known.

**Three existing tests changed, and how matters.** All three pinned a state this cycle removes for a
capable tuner. Two were repaired by reaching that state the only way it still exists — a startup
assert that **failed** — and **not** by resetting a fake after construction, which would fabricate a
state production can no longer produce. The third's edit was its *comment*: it must still distinguish
the FM we asserted and had confirmed from the FM the firmware seeds, or the next reader concludes ADR
0132 was reversed. **The capability gate sorted every other fake in the suite with no edit at all** —
five advertising `TUNING_CAPS`, one advertising `SET_MODULATION` — which is itself evidence it is the
right seam.

`uv run pytest` **1981 passed, 5 skipped** (1973/5 before — 8 new, 3 repaired in place).
`cd web && npm test` **101 passed, 11 files — unchanged**, no web touched.

### Carried forward

1. **`doctor` builds no tuner, so `--key-test` keys with zero modulation knowledge.**
   `doctor.py:1305-1312` passes five kwargs and no `uvk5_tuner`, so the capability gate returns
   immediately. The one tool whose entire job is a deliberate key-up is the one place this cycle does
   not reach. The fix changes what `--key-test` costs — a doctor cycle.
2. **The EventHub does not exist at backend construction.** `build_app` builds the radio *before*
   `create_app` wires the hubs, so **no boot-time diagnostic of any kind can reach the operating
   log** — not this one, and not the next cycle's. Named so whoever wants one finds it written down
   rather than rediscovering it.
3. **A radio that refuses to leave AM leaves the belief `None`, and one branch discards a truth the
   radio sent.** Precisely one: the firmware blanks a *refusal* reply's modulation to `0xFF`, so
   there is nothing to keep, but a *mismatch* reply carries what it read out of its own VFO and
   `set_modulation` raises without recording it. Recording it inverts the record-only-on-success rule
   three tests pin, and needs its own ADR. Pinned as-is by a test.
4. **A radio powered on *after* the server is never re-asserted.** Small, because `apply_preset`
   writes modulation unconditionally, so a preset tap re-asserts it. What is left is exactly: powered
   on late, no preset applied, no modulation set.
5. **`docs/uvk5-setup.md`'s firmware table stopped at F6** — F7 shipped in ADR 0149 and the row was
   never added. Added this cycle. **The flash recommendation on that page still points at the F6
   release and was left alone**: whether an F7 release tag exists is a hardware/release fact this
   cycle could not verify, and guardrail 1 forbids asserting one from memory.
6. **Everything ADR 0154 carried is still carried**: `POST /mode`'s 500, `MockRadio.set_mode`
   accepting what every real backend rejects, `GET /presets` omitting `modulation`, the
   `REST_PATHS` drift, and **the three websocket talker-slot leaks** (`app.py:1873`, `:1995`,
   `:2095`) — still from ADR 0153, still untouched.

## Two controls must not both say FM (2026-07-31)

[ADR 0154](adr/0154-two-controls-must-not-both-say-fm.md). Web UI cycle — **no backend change, no
firmware**, nothing flashed, no hardware claim. This is the cycle ADR 0153 unblocked: AM reaches a
screen. Before it, `grep -rn "modulation\|tx_ok" web/src` returned zero hits.

**The design problem came first.** `ModeControl` said "Mode" over a select that POSTs `/mode` —
wide/narrow **bandwidth** — and the new control offers FM and **AM**. The argument was already in
the repo twice, in `Capability.SET_MODE`'s docstring and in `docs/api.md`. Both labels changed, so
neither survives with its meaning altered: **Bandwidth** (`Wide (FM)` / `Narrow (NFM)`) and
**Demodulation** (`FM` / `AM`). Keeping "Mode" for the demodulator is the ham convention and was
rejected as the one option where muscle memory lands on the wrong control. The parentheticals are
deliberate — the only join back to the raw `FM`/`NFM` still spelled in `radio.toml`, in presets and
on the face LCD.

**What shipped**

- **Demodulation control** in the Tune card, greyed on a missing `set_modulation` and on a runtime
  501 through the existing `useAction → onUnsupported → disabledCaps → hasCap` path. **No Set button
  and no draft state**: `apply_preset` rewrites the modulation on *every* apply, so a draft would
  show the operator's last pick while the radio had been moved out from under it.
- **The bandwidth select was already offering AM** — it listed AM/USB/LSB/CW, all four of which every
  real backend raises on. Trimmed to two.
- **`tx_ok: false` locks Talk out**, folded inside the existing `source === "rf" && !talking` guards.
  **The parentheses are load-bearing and pinned by tests.** `null` never locks.
- **No backend-name check went into the browser**, though `docs/api.md` says the refusal only bites
  the `baofeng` keying path: `uvk5/radio.py` passes neither field, so the dock reports `null` and the
  tri-state rule *is* the backend gate, structurally. Recorded with its expiry condition.
- **The face LCD**, because a correct select beside a misleading LCD is not a fixed collision — it
  printed a bare `state.mode`, so a radio demodulating AM read "FM" in the largest text on screen.
  Tokens now say which setting they are (`DEMOD AM` / `BW NFM`).
- **Fail-first three times.** Lockout + Spacebar → **6 red**. Treating `null` as `false` → **5 red**,
  and the blast radius is the finding: it also broke **four pre-existing `tx_ready_in` tests**,
  because an unmeasured field would lock out essentially every radio.

**Shipped beyond the brief, recorded not blamed:** the Spacebar keydown never consulted the lockout
at all, so a greyed Talk button has always still keyed from the keyboard — cosmetic at six seconds,
permanent once AM can cause one. Closing it also closes that pre-existing `tx_ready_in` bypass. The
keyup was verified and **deliberately left unconditional**: it can stop an over the Spacebar did not
start, but refusing to stop is the dangerous direction in a transmitter.

`cd web && npm test` **101 passed, 11 files** (71 before). `npm run build` clean. `uv run pytest`
**1973 passed, 5 skipped — unchanged**, no Python touched.

### Carried forward

1. **`POST /mode` returns 500, not 422, on a value the backend rejects.** `app.py:1270-1278` catches
   only `UnsupportedCapability` and `ModeBody.mode` is a bare `str`; `/tone`, `/frequency` and
   `/modulation` all catch `ValueError → 422`. **Verified empirically** with an uncommitted probe:
   `{"mode":"NFM"}` → 200, `{"mode":"AM"}` → **500**, `/modulation` bad value → 422. Precedent one
   route above at `app.py:1253-1256`: *"escape as an unhandled ValueError (HTTP 500, seen on the
   bench)"*.
2. **`MockRadio.set_mode` accepts anything, and that is *why* nothing caught (1).** Every API and UI
   test runs against the mock, so `{"mode":"AM"}` is 200 in tests and 500 on the bench.
   `MockRadio.set_power` validates for exactly this stated reason; `set_mode` never did. **It will
   hide the next divergence too** — a finding in its own right.
3. **`GET /presets` does not serve `modulation`, so the preset highlight will now lie.** Presets
   apply it unconditionally and `activePresetName` cannot compare it. Apply an FM preset, switch to
   AM by hand, and the preset stays highlighted. **Unfixable in the UI** — the field is not on the
   wire.
4. **`vite.config.js` `REST_PATHS` drift.** `/power`, `/presets`, `/split`, `/tuning`, `/dstar`,
   `/dvap` all missing, and `/audio/mumble/tx` + `/audio/dstar/tx` are not proxied — they hit the SPA
   in dev. Only `/modulation` was added. Nothing tests that file, which is why it rotted.
5. **The three websocket talker-slot leaks** — `app.py:1873`, `:1995`, `:2095` — still carried from
   ADR 0153, still untouched, still their own cycle.

## One frame must not take the relay down (2026-07-31)

[ADR 0153](adr/0153-one-frame-must-not-take-the-relay-down.md). Host cycle — **no firmware change,
no UI**, nothing flashed, no hardware claim.

**This closes the dependency ADR 0151 recorded and ADR 0152 carried forward. AM is now safe to make
operator-selectable — the UI cycle is unblocked.**

ADR 0151 made a refused key-up unwind cleanly *inside* `TxSession`. The refusal still propagates
**out of** `session.feed()`, and neither relay loop survived it: `link/bridge.py` caught only
`AudioFormatMismatch`, and `dstar/bridge.py` that plus `ArbiterStateError`. One refused frame killed
the Mumble→RF task, or the drain loop and with it the whole crossband.

The argument was already written in the repo, in the `ArbiterStateError` catch — *"an unhandled
raise here would kill the drain loop — the whole crossband — over one contended frame"*. Same
sentence, different exception. That catch is the shape copied, not a new one invented.

**Four things worth carrying forward:**

1. **Two counters, never one.** An AM refusal is a *standing condition* — it recurs on every frame
   until the demodulator changes. An `OSError` from a yanked cable is a *fault* — rare, and each one
   matters. A shared counter lets 40 000 refusals bury the single unplugged cable that actually
   needed a human. `dropped_key_refused`/`relay_errors` and `rx_key_refusals`/`rx_relay_errors`,
   both surfaced in status the `rx_guarded` way, both documented in `api.md` with *which one means
   fix it at the radio and which means investigate the hardware*.
2. **Narrow-only was rejected, and not for diff size.** A yanked cable kills the loop identically to
   the refusal. Catching one exception type is a **partial** fix, not a smaller one.
3. **Except ORDER is load-bearing and is pinned by tests.** The broad backstop goes last; a
   malformed frame and a held arbiter must each still take their named path with both new counters
   at zero. Without that pin a reorder would silently absorb ADR 0102's drop-and-retry — and lose
   the self-healing it exists for, since the backstop ends the over instead of retrying per frame.
4. **D-STAR ends the over where the arbiter path retries, deliberately.** Contention is transient;
   a refusal is a standing *station* condition, and holding the session, the slot and the `rx` latch
   for an over that cannot happen blocks the browser talker. The new causes stay outside
   `("end", "teardown")` so a still-flowing stream re-latches once the radio can key.

**Fail-first: 7 red across both bridges**, asserting **loop survival** — a frame injected after the
radio recovers must reach RF — rather than a counter moving.

**Two defects surfaced by those runs, both recorded rather than folded in silently:**

- `overs_keyed` came back **`1` for zero overs**. It was incremented *before* the key-up was
  attempted, while `tx_stats` documents it as transmissions the bridge **keyed**. Moved to fire on
  the first successful feed. Beside the new refusal counter the old pair would have read as nonsense.
- **My own:** the first fix called `log.warning` in `link/bridge.py`, which **has no module logger**.
  The `NameError` raised *inside* the `except` block — which a sibling `except` cannot catch — and
  killed the loop exactly as the original bug did. The tests caught it on the first run. A module
  logger was added.

**`uv run pytest`: 1973 passed, 5 skipped** (1965/5 before — 8 new tests).

### The named next finding

**Three websocket talker-slot leaks**, verified this cycle and deliberately not fixed:
`app.py:1873` (`tx_slot`), `:1995` (`mumble_talk_slot`), `:2095` (`talk_slot`). All three:

```python
acquired = <slot>.try_acquire()
await websocket.accept()          # can raise; OUTSIDE the try/finally that releases
```

A client vanishing mid-handshake holds the slot for the life of the process, and every later talker
then gets `1013 "busy"`. Three slots, one mechanism, one file — a different shape from this cycle's,
deserving its own fail-first.

## A login is not its announcement (2026-07-31)

[ADR 0152](adr/0152-a-login-is-not-its-announcement.md). Host cycle — **no firmware change, no UI,
no bridges**, nothing flashed, no hardware claim.

**Last cycle shipped a defect and a test that pinned it.** ADR 0151 gave `_keying` a `strict` flag
that re-raises after emitting, and applied it to *both* API entry points. `open_session` was the
wrong one: `gate.open()` commits **before** the announcement is attempted, so a refused announcement
left the gate open and the station ID armed with **neither `auth_accepted` nor `session_open`
emitted**, and handed the caller a 503. An authenticated session that existed in memory and **not in
the ledger** — while `GET /status` reported `session_open: true` and contradicted the 503.

`test_open_session_still_raises_so_the_api_can_report_503` asserted that raise, so the suite encoded
the bug rather than catching it. It is replaced, and ADR 0152 says so plainly rather than quietly
rewriting it.

**`step()` was checked, not assumed by symmetry.** Its ACCEPTED branch never passed `strict`, so it
took the default `False` and always kept both its session and its records. Only the API path lost
them — the kind of divergence a shared helper with a per-call-site flag invites.

**The line ADR 0151 drew was on the wrong axis.** It is not API-versus-over-the-air. It is **does
the caller's request name a transmission?**

- `trigger` *is* "transmit this" — the "play ID" button. A 200 with nothing on the air is a lie, so
  it keeps re-raising. **Untouched this cycle**, and `test_trigger_still_raises` staying green is
  the proof the correction did not overshoot.
- `open_session` is "open my session". The announcement is a side effect *of* the open, not the open
  itself. It now emits both lifecycle events, records `tx_failed`, and reports the failure in its
  own response body as a **200**.

**Rollback was considered and rejected, and the objection to it does not land where you'd expect.**
A retrying caller would be fine — `gate.open()` returns `True` again, the announcement fails again,
it rolls back again, a clean loop with no half-state. It is rejected because rolling back only in
`open_session` breaks the same RF/web equivalence in the opposite direction, and because ADR 0151's
all-or-nothing protects a *hardware* resource where a half-state strands a transmitter. A session
has no such hazard.

**`announced` is tri-state**, following `tx_ok` (ADR 0150): `None` is *not attempted*, `False` is
*attempted and refused*. `api.md` documents it as **pairs** with `opened`, because neither is
unambiguous alone — `null` means "already open" beside `opened: false` and "nothing configured"
beside `opened: true`. The smaller failure-only shape was rejected: a key that appears only on
failure is the silent-failure class ADRs 0149/0150/0151 exist to close.

**Fail-first asserts the invariant, not the remedy** — either the gate is closed or both events were
recorded, never open-and-unrecorded — so it survives a later change of mind about the fix. Red on
master with `Session(state=AUTHENTICATED)` and neither event seen.

**`uv run pytest`: 1965 passed, 5 skipped** (1961/5 before — 4 new, one replaced one-for-one).

### Still carried forward — the DEPENDENCY from ADR 0151

**The two bridge relay loops must be hardened before AM is selectable by an operator, i.e. before
the UI cycle.** A raising key-up still propagates out of `session.feed()`, and `link/bridge.py` /
`dstar/bridge.py` catch only `AudioFormatMismatch` (plus `ArbiterStateError` in the D-STAR case), so
the Mumble→RF task and the reflector→RF drain loop die on the first refusal. Pre-existing and
orthogonal; unfirable while F7 is unflashed. The shape is already in the repo: a counted, logged
drop surfaced in `/link/status`.

Also still open from ADR 0151 (same acquire-then-act shape, each needs its own reproduction):
`tx_slot.try_acquire()` before `await websocket.accept()` — a client vanishing mid-handshake holds
the single-talker slot forever, the likeliest of the three to bite; `audio_hub.subscribe()` before
`await _acquire_rx()`; and `RxPump`'s `begin_receive()` outside its own `try`.

## A failed key-up must give the radio back (2026-07-30)

[ADR 0151](adr/0151-a-failed-key-up-must-give-the-radio-back.md). Host cycle — **no firmware
change**, nothing flashed, no hardware claim.

Last cycle's `tx_ok` refusal made `ptt(True)` raise as a matter of course on the deployed station,
for the first time. That exposed a latent bug one layer up: `TxSession.feed` claimed the shared
arbiter and *then* keyed, so a raising `ptt(True)` left it latched `TRANSMITTING` with `_keyed`
still `False` — and `close()` guards its release on `_keyed`, so **nothing ever gave the radio
back**. Reproduced in memory before anything was planned:

```
feed raised: demodulating AM, refuses its own PTT path
after close -> arbiter.transmitting = True | mode = transmitting
```

That latch is shared. The RX pump stops pulling `receive()` (no RX audio, no DTMF decode, no
controller), the scan engine stops advancing, and the D-STAR bridge counts every reflector frame as
a conflict against a keyer that no longer exists. **One refused over took the station off the air
until restart.**

**Key-up is now all-or-nothing** — the ADR 0093 `_key_on` shape, one layer up. Before the line is
up, release the arbiter and re-raise; after it is up, `close()` unwinds, including the paired
`on_key(False)` the ledger needs to close its `tx_key_up` record. A failed **key-up ID** unwinds
too rather than being swallowed: `key_up_id` returns audio only when an ID is *due*, so carrying on
would put an un-ID'd over on the air.

**The audit found more than the shape it was sent for — read this part before the next cycle:**

- **A ninth station-keying site**, in `services/dispatch.py`: every DTMF voice service. It is the
  most-travelled keying path in normal operation and a controller-shaped search never reaches it.
  All nine were swallowed silently, because `Controller.step` is driven by the RX pump's bare
  `except Exception: pass` — no log, no event, no ledger record, in the one place guardrail 5 makes
  mandatory.
- **A Part 97 defect with no guarding involved:** `StationId.transmit` advanced `_last_id` *before*
  the radio call, so a failed announcement-with-ID **marked the ID as sent** and suppressed the next
  one for ten minutes. `identify`/`check`/`sign_off` were already ordered correctly.

**Three decisions worth carrying forward:**

1. **The state invariant lives in `StationId`; the guard lives in the controller.** No caller can
   repair state it cannot see, so a call-site-only guard would have left the suppression in place —
   and the event channel is the controller's, so duplicating one inside `StationId` would have been
   a second `on_event`.
2. **`strict` re-raises on `trigger`/`open_session` and nowhere else.** The DTMF loop cannot report
   a fault to anyone; those two callers can, via ADR 0143's app-wide 503. An operator who clicked
   "play ID" must not be handed a 200 for an over that never happened. The `tx_failed` event fires
   **before** the re-raise on both paths, so the record is never what gets traded away.
3. **`tx_failed` reaches the JSONL ledger, not just the live event stream.** That is the durable
   Part 97 artifact: absence of a `station_id` record is not evidence of anything.

**Fail-first, three times.** The arbiter leak → **8 failures across 4 files**, and the RX-resume
proof *hung* rather than failed, which is the blast radius stated as plainly as it can be. The
`_last_id` ordering → red by construction. A guard that swallows silently → **7 failures**, with the
survival assertions and **three return-value honesty tests staying green** — recorded so nobody
later cites `id_sent is False` as proof the failure is visible. It is not; only the `tx_failed`
tests and the ledger test are.

**`uv run pytest`: 1961 passed, 5 skipped** (1939/5 before — 22 new tests).

### Carried forward as a DEPENDENCY, not a note

**The two bridge relay loops must be hardened before AM is selectable by an operator — i.e. before
the UI cycle.** A raising key-up still propagates out of `session.feed()`, and `link/bridge.py` /
`dstar/bridge.py` catch only `AudioFormatMismatch` (plus `ArbiterStateError` in the D-STAR case), so
the Mumble→RF task and the reflector→RF drain loop die on the first refusal. This is pre-existing
and orthogonal — ADR 0151 neither causes nor worsens it — and it cannot fire yet, because F7 is
unflashed and AM is not reachable from the UI. The moment a modulation control ships, it can. The
shape is already in the repo: a counted, logged drop surfaced in `/link/status`, as ADR 0085's
`rx_guarded` and the D-STAR bridge's own `rx_arbiter_conflicts` counter both do.

Also reported and **not** fixed (same acquire-then-act shape, different resources, each needs its
own reproduction): `tx_slot.try_acquire()` before `await websocket.accept()` — a client vanishing
mid-handshake holds the single-talker slot forever and every later talker gets 1013 "busy", the
likeliest of the three to bite; `audio_hub.subscribe()` before `await _acquire_rx()`; and `RxPump`'s
`begin_receive()` sitting outside its own `try`, correct today only because `_start_gate` swallows
everything internally.

ADR 0150 gained an **addendum** recording that its `tx_ok` key-up refusal was a behavioural change
to an existing keying path and was not asked for by that cycle's brief. Not reverted — the
behaviour is right — but recorded, so a later cycle finds it stated rather than assuming the scope
was agreed.

## The host learns to listen to AM (2026-07-30)

[ADR 0150](adr/0150-the-host-learns-to-listen-to-am.md). Host cycle — **the firmware fork is not
touched**, nothing is flashed, and no hardware claim is made.

F7 shipped `0x0877`/`0x0878` last cycle and **nothing here could speak either frame**, so the
capability sat on the radio out of reach: airband impossible, presets unable to name a demodulator.
This adds the codec, `Capability.SET_MODULATION`, a `modulation` field on presets, and
`POST /modulation`.

**The wire contract was read out of the fork's `dock.h`/`dock.c` at the merged F7 commit**, not out
of ADR 0149's description of it. ADR 0148's cross-repo drift is still unguarded, so reading the
source *is* the check. Both golden vectors were transcribed from the fork's own host harness
(`tests/host/test_dock.c` cases 25/26) and then **re-derived from the independent reference framer**
already in `tests/test_uvk5_frames.py` — they match byte-for-byte both ways.

**The name was already taken.** `DockCommand.SET_MODULATION = 0x0872` / `class SetModulation` are
the classic Dock's `CMD_0872_t` — defined in stock firmware, never dispatched, never sent. Renamed to
`STOCK_SET_MODULATION` / `StockSetModulation` so the plain names go to the F7 pair, and every stale
call site fails loudly on the new one-field signature. One test pins **both** values together,
because the interesting failure is them drifting into each other.

**Four decisions worth carrying forward:**

- **`SET_MODULATION` is its own capability.** `SET_MODE` is wide/narrow *bandwidth*; this is *FM/AM
  demodulation*. Both spell one value `"FM"`, which is why they are split in the vocabulary rather
  than in a docstring. Forced cost, taken deliberately: `len(FULL_CAPS) == len(Capability)` is
  asserted, so it joins `CAT_CAPS` and `MockRadio` implements it — the `SET_POWER` path.
- **`TUNING_CAPS` is no longer one shared frozenset.** `EepromTuner` writes a hardcoded-FM record
  into *stock* firmware with no `0x0877` case, so `setvfo`/`hybrid` get `SETVFO_CAPS` and it keeps
  the five — and it **raises** rather than returning, because not advertising is what gives the 501
  while raising is what stops a direct call reporting success for a radio still on FM.
- **Absent `modulation` means FM — the opposite of `power`.** A level belongs to the *station*; a
  demodulator belongs to *what you are listening to*. So it is written on every apply, which is also
  the assert-at-connect `dock.h` requires: the firmware's sticky value outlives a host restart.
- **`tx_ok` is reported on every backend and it refuses a key-up.** AIOC-DTR keying runs into
  `VFO_STATE_TX_DISABLE`; dock REG_30 keying does not. Left unenforced, an AM key-up is silence with
  `status()` reporting `transmitting` — and the transmission it swallows is the **station ID**.
  Only a *measured* `False` refuses; `None` never blocks. The key-path re-assert now sends the
  modulation **before** the channel, so the flag is a measurement rather than a memory.

**Fail-first, three times, run against the bug:** a wrong command byte → **2** failures; `tx_ok`
never reading the flag → **5** across three files; `EepromTuner` silently returning → **exactly 1**,
with the capability-split test **staying green** — recorded so nobody later cites the split as proof
the refusal works. `uv run pytest`: **1939 passed, 5 skipped** (1884/5 before).

**Deployed config confirmed** this cycle: `server.backend = "baofeng"`, `baofeng.uvk5_tuner =
"hybrid"`. So `HybridTuner` is the code that actually runs, and with `persist` off `volatile` is
true — the re-assert is on the live key path, not a defensive branch.

### Next

- **The web UI** is the next cycle: a modulation control, greyed on the capability, and something
  that surfaces `tx_ok: false` before the operator presses a PTT button that will 503.
- **The radio is not flashed with F7.** Until it is, none of this has moved a demodulator: AM audio
  over the AIOC and the PTT refusal are still `⚠ CONFIRM AT BENCH` in the fork's `BENCH.md`.
  > **CORRECTED 2026-07-31 — do not quote this forward.** The operator has since flashed the radio
  > **by hand, to F8**. [ADR 0160](adr/0160-the-bench-answers-back.md) confirmed it over the AIOC
  > (both `0x0878` and `0x087A` answer) and **measured both** items called out above: AM audio over
  > the AIOC reaches the host, and the PTT refusal fires with the witness reading no RF. This line
  > was true when written and was never revisited, because no cycle in the arc ran on hardware.
- Still open and untouched: `doctor` reports F6 only; `vfo.py:339`'s `_MODULATION_FM` (now
  *consistent*, since `EepromTuner` does not advertise the capability); the three-way opcode census
  that disagrees with itself.

## The radio gains AM, and a tune stops forcing FM (2026-07-30)

[ADR 0149](adr/0149-a-new-opcode-is-cheaper-than-a-changed-one.md). Firmware cycle — **no code under
`radio_server/` changed**, and the host side is deliberately its own cycle.

The dock could tune the radio and could not change its demodulator. `Dock_ApplyVfo` wrote
`vfo->Modulation = MODULATION_FM` on every `0x0873`, and `vfo.py:339` does the same thing host-side,
so AM was not gated or degraded — it was **absent**.

**The opcode was not free, and neither was the last one.** The obvious `0x0875/6` is recorded by our
own [ADR 0111](adr/0111-uvk5-dock-transport-and-control-path.md):52 as the classic Dock's AM-emulation
pair. Reading that line turned up something worse: **`0x0873/4` is on the same list, as backlight**,
and ADR 0140 allocated it after ruling out `0x0872` alone. That is shipped, on hardware, in 186
measured tunes, and **cannot be walked back**. The census lives in three places — `ADR 0111:52`,
`ADR 0119:46`, `frames.py:113-140` — that do not agree, and nothing reconciles them. So: **`0x0877`
command, `0x0878` reply**, checked against the census before allocating rather than after.

**A new opcode, not a 14th byte on `0x0873`.** An added field changes bytes on a frame radio-server
already sends and breaks *both* directions silently — an old host's 13-byte frame is refused
`ERR_SHORT` and reads as a tuning failure; a new host's 14-byte frame decodes fine on old firmware,
which then tunes on a modulation nobody set. ADR 0148 named that drift as unguarded; this cycle
declines to widen it. An empty `0x0877` is the F7 probe, safe for the same reason the `0x0873` probe
is.

**The modulation is sticky and `0x0873` applies it**, which is the part worth arguing. Not a
convenience: [ADR 0131](adr/0131-dock-link-robustness.md) established that **this link drops frames**,
so "tune, then set modulation" as two frames can leave the radio on the right channel in the wrong
demodulator, reporting success both times. Seeded FM **from a constant, never read back off the
radio** — that would be [ADR 0132](adr/0132-dock-band-and-the-register-model.md)'s fault, and would
tune a repeater channel in whatever the front panel was last left on. **A refusal never moves it**;
committing during decode would let a refused `ERR_BUSY` become what every later tune applies.
`0x0873`'s **wire bytes are untouched** — one literal became a table lookup whose default is that
literal.

**A successful set-AM stops the transmitter.** Traced, not assumed: `GENERIC_Key_PTT` →
`generic.c:192 gFlagPrepareTX` → `app.c:2612 RADIO_PrepareTX()` → `VFO_STATE_TX_DISABLE` for any
non-FM modulation without `ENABLE_TX_WHEN_AM`. That is the path the **PTT pin** drives — where the
`baofeng` backend keys from, via the AIOC's DTR line. Dock REG_30 keying bypasses it entirely, so one
firmware state means "dead transmitter" on one backend and "normal" on another, decided by a build
flag a host cannot see. `0x0878` carries it as a flag byte. **`ENABLE_TX_WHEN_AM` stays off — AM is
receive-only, and the flag reports the condition rather than removing it.**

Refusals blank to **`0xFF`, not `0`**: copying `0x0874`'s zero-blanking would have answered a refusal
with a plausible claim that the radio is on FM, because `0` *is* FM. The wire names its own modulation
values because `MODULATION_UKNOWN` **moves with `ENABLE_BYP_RAW_DEMODULATORS`**.

**Fail-first, twice, because there were two bugs to fail against.** An always-succeeds stub scored
**98 checks / 15 failures**; a build with the command fully correct but `0x0873` keeping its FM
hardcode scored **exactly 2** — the stickiness pair and nothing else, which is what makes the green
run mean something. Restored: **98 checks, 0 failures** (66 before). The `0x0874` byte-exact golden
stays green as the gate on `0x0873`'s wire. New goldens were generated by an **independent framer**
validated against the two published vectors first — a vector produced by the implementation it checks
is a transcript, not an oracle. Two tests are commented to say they stay green against the stub, so
nobody later cites them as proof the refusals work.

FLASH **105,576 B of 118 KB (87.37%)**, +240 B over F6, 15,256 B headroom. `uv run pytest` **1884
passed, 5 skipped**; fork host tests **98 checks, 0 failures**.

**Not flashed. No hardware result is claimed** — the F7 pre-release's `PROVENANCE.md` says so in those
words rather than carrying F6's measured-on-hardware paragraph forward. AM audio over the AIOC and the
PTT refusal are new `⚠ CONFIRM AT BENCH` placeholders in the fork's `BENCH.md`.

**For the next cycle:** the host side — `frames.py` gains the two opcodes, and inherits a name
collision, since `DockCommand.SET_MODULATION = 0x0872` and `class SetModulation` already exist for the
classic Dock's never-dispatched command. `vfo.py:339`'s own `_MODULATION_FM` hardcode is the same
defect on the `eeprom` path. And the opcode census still wants reconciling into one source before the
next allocation repeats `0x0873`.

---

## AGENTS.md in both repos (2026-07-27)

A follow-on to ADR 0148, no new ADR — correcting agent-facing guidance, not deciding anything.

**radio-server `AGENTS.md`, four defects.** One was **created by the ADR 0148 cycle itself**: the
overview still called `uvk5` "a Quansheng UV-K5/K6 on Dock firmware", the exact singular-firmware
claim that cycle corrected in five other documents. Also: the `doctor` flag list named **4 of 14**
mode flags (and omitted `--rx-noise`, which the new setup guide sends people to); the documentation
map skipped `uvk5-setup.md`, `kv4p-setup.md`, `dstar-setup.md` and `troubleshooting.md`; and
**guardrail 3 asserted something now false** — *"In Baofeng mode the CAT methods do not exist"* —
which stopped being true at ADR 0142 and is how this station actually runs. **`CLAUDE.md` carried the
same drifted guardrail** and was corrected identically. Both now say: read `capabilities()`, never
infer the surface from the backend name.

Added a **sibling-repository** section: the firmware is not in this repo, the two are coupled by
byte-compatibility between `frames.py` and `dock.c`, a wire change is a cross-repo change, and
capabilities are gated on firmware level.

**The fork had no `AGENTS.md` at all** ([PR #3](https://github.com/kbennett2000/uv-k1-k5v3-firmware-custom/pull/3)) —
nor `CLAUDE.md` or `CONTRIBUTING.md`. It now documents the headless `-it` build trap, the host test
target, the byte-compatibility contract, that the reasoning lives in *this* repo's ADRs, the F-level
table, and six guardrails. First: **do not fill in `BENCH.md`'s `⚠ CONFIRM AT BENCH` placeholders from
inference** — five numbered items plus the resume-RX behaviour, unconfirmed on the radio, in firmware
that can brick one.

**Worth noting for the next cycle:** the drifted overview line proves ADR 0147's locks don't cover
`AGENTS.md`/`CLAUDE.md` prose either, and that a cycle can leave its own documentation stale in the
act of fixing everyone else's.

`uv run pytest` **1884 passed, 5 skipped**; fork host tests **66 checks, 0 failures**. No code changed
in either repo.

---

## The firmware is a product too (2026-07-26)

[ADR 0148](adr/0148-the-firmware-is-a-product-too.md). Triggered by one question after PR #203: *is
the firmware we flashed in this repo?* It is not, and chasing that found two things worse than
anything the audit did.

**`docs/uvk5-setup.md` sent V3 owners at firmware that cannot run on their radio** — nicsure's Dock
`0.32.21q` targets the **DP32G030**; the bench radio is a **PY32F071**. ADR 0118 established that
eight cycles earlier and the guide was never told. **ADR 0147's locks could not catch it** — every
link resolved, every endpoint was named. It was just false. That is the "absence, not wrongness"
limit, demonstrated.

**The fork was invisible.** `main` was still pristine upstream at `3bd3ebb` — all ten dock commits on
stacked branches, so *cloning the repo got you none of the work*. README byte-identical to upstream's.
No F6 release, though F6 is what is flashed. Operational knowledge only in this repo's ADRs.

**And the guide documented one of three paths.** Since ADR 0141 the `eeprom` tuner drives a UV-K5 on
**stock** firmware — a reader with a working radio was being told to flash for a capability they had.

**radio-server:** the guide is restructured around all three paths with the no-flash one leading;
firmware split by MCU; the V3 tell states only what is confirmed (USB DFU vs PTT+flashlight
bootloader, bootloader 7.00.07) and marks the rest verify-on-hardware. Classic-Dock claims qualified
across README/install/architecture/deployment/troubleshooting.

**The doctor gains an F-level probe** — an **empty** `0x0873`. Safe because `dock.c` checks length in
the *first* branch: `ERR_SHORT` before a field is decoded, before the VFO binding is called,
frequencies blanked. **The refusal is the affordance.** Fail-first: a probe that always claims F6
failed **2 of 3** gates.

**Fork ([PR #2](https://github.com/kbennett2000/uv-k1-k5v3-firmware-custom/pull/2)):** `main`
fast-forwarded to F6 (clean, zero divergence, no rewrite), README section above F4HWN's own,
`PROTOCOL.md` written, and
**[radio-server-f6-v5.7.0](https://github.com/kbennett2000/uv-k1-k5v3-firmware-custom/releases/tag/radio-server-f6-v5.7.0)**
cut. Every golden vector cross-checked against `dock.c` **and** `frames.py`. The released binary is
the one actually flashed; a clean rebuild from `181cb4c` differs in **5 bytes of 105,336** — the
embedded build timestamp — recorded in `PROVENANCE.md` rather than claimed as determinism.

`uv run pytest` **1884 passed, 5 skipped**; fork host tests **66 checks, 0 failures** (no firmware
code changed).

**Left open:** nothing checks that `PROTOCOL.md` and `frames.py` stay in step — ADR 0147's problem one
level up. `BENCH.md`'s five `⚠ CONFIRM AT BENCH` items still need a person at the radio.

---

## Docs audit — the drift, and the locks (2026-07-26)

[ADR 0147](adr/0147-the-docs-drifted.md). A full audit of every doc against the code. The findings
were not cosmetic:

- **Four documents still told the operator to tune by hand on a Baofeng** — the capability ADRs
  0142–0146 delivered, over the cable `install.md` claimed could not carry it.
- **18 ADRs (0097–0114) had no index row**; ADR 0139's row was missing its Status column.
- **`deployment.md` named three WebSockets; the app registers seven** — an nginx config from that list
  silently kills browser Mumble and D-STAR audio.
- **`using-it.md` promised "you're never left with a dead station"** about a backend switch that
  SIGSEGVs.

**The cause is structural.** Docs are hand-updated at cycle end and nothing fails when they aren't.
The two contracts that *were* locked (ADR 0058's README↔`install.sh`, `radio.toml.example`↔`spec.py`)
had not drifted at all.

**`tests/test_docs_contract.py`** — 7 checks: every ADR file ↔ index row, every internal markdown link
resolves, every REST path / WebSocket / `Capability` appears in `docs/api.md`. Routes come from
`app.openapi()` over the existing `create_app(MockRadio(...))` seam.

**Fail-first: 5 of 7 failed** against the pre-fix tree — 18 unindexed ADRs, 1 malformed row, 1 broken
link, 10 undocumented endpoints, 2 undocumented WebSockets. **The capability check passed when I had
predicted it would fail**; recorded as a miss in the ADR rather than dressed up, because a substring
search cannot see a prose list that named five of seven. It is kept for the next capability added.

**What the locks cannot do:** they catch *absence*, never *wrongness*. "There is no CAT" would have
passed all seven, and four of this cycle's worst findings were false sentences about endpoints that
were already documented. A green suite is not a claim that the prose is true.

**New: [`docs/dstar-setup.md`](dstar-setup.md)**, written warning-first. The reflector→RF crossband
stranded the transmitter keyed **≥5 times**, its joint dummy-load re-proof has **never passed**, and
since ADR 0089 removed `operator_tx` there is **no browser-only mode** — setting `dstar.callsign` and
clicking Connect arms RF keying. The guide documents the gate; it does not open it.

Also: 5 ADR Status lines gained supersession markers (0097/0107/0137/0138/0139/0142/0144), two
`spec.py` description strings were wrong and `radio.toml.example` was regenerated, three stale in-code
docstrings corrected, `server-notes.md` gained a dated current-state block.

**Verified by hand, not asserted:** 146 ADR files / 146 index rows; 7 `.websocket()` decorators;
39 REST paths; `CAT_CAPS` = 7. `uv run pytest` **1880 passed, 5 skipped**; `npm test` **71 passed**.

**Left open on purpose:** the `POST /radio/select` baofeng→uvk5 SIGSEGV (documented as a hazard in
three docs, not fixed — the operator's call); and `create_app`'s `dstar_max_over` defaulting to `0.0`
while `build_app` injects 60 s, so a non-`build_app` embedder gets no per-over ceiling.

No hardware was touched: prose, one config description, one new test file.

---

## Transmit power (2026-07-26)

[ADR 0146](adr/0146-transmit-power.md). **Power is settable — `low` / `mid` / `high` — from the Tune
card, per channel, or in config.** It was plumbed to the radio from the first tuner and hardcoded
above the `VfoImage` seam; the radio read back **`power=7` on all 186 tunes** before this.

`baofeng.uvk5_power` (default **high**, so nothing moves for anyone who does not touch it) sets the
boot level; `POST /power` and the UI change it live; a `[[presets]]` entry may name its own and moves
the station level when tapped, so there is exactly one visible level.

**Absent on a preset is deliberately NOT "off".** The split and the tone belong to the channel, so a
preset omitting them means none (ADR 0133). A power level belongs to the *station*, so omitting it
means "however I am set" — forcing a default would undo the operator's own choice on every tap.

**Baofeng mode is the calibrated path.** The firmware runs `RADIO_ConfigureSquelchAndOutputPower`
per band from calibration in its own flash — the exact thing the dock backend's raw `0x36` bias
write cannot do (ADR 0128/0134). **What a level is in watts is claimed nowhere in this repo**, and
must not be: the host cannot read that calibration.

`SetVfoTuner` now **checks** the `0x0874` power read-back rather than logging it. `out->power` is
`gEeprom.VfoInfo[0].OUTPUT_POWER` after the firmware applied it, and the scale is a trap that already
bit once — the wire's 0/1/2 assigned raw lands "high" on `LOW2` (ADR 0142).

### Measured

| Gate | Result |
|---|---|
| `power_levels.py -n 3`, real build | **9/9**, exit 0 — low/mid/high → `OUTPUT_POWER` **1 / 6 / 7** |
| same gate, `set_power` stubbed to swallow the request | **exit 1** — `low → high`, `mid → high` |

`uv run pytest` 1873 / 5 skipped; `npm test` 71.

### The second claim was NOT measured, and do not read it as flat

"Less power radiates less" is **unmeasured on this bench.** The witness reported RSSI **0
throughout, including its idle floor** — this kv4p firmware reports `latest_rssi` as 0 even while
cleanly demodulating, which [ADR 0132](adr/0132-dock-band-and-the-register-model.md) already
established and which had already produced two confident-and-wrong "FLAT" verdicts from
`uvk5_pa_sweep.py`. I proposed that instrument without checking what was already written down about
it.

`power_levels.py` now prints **NO MEASUREMENT** in those words when the witness reads zero, and names
what it would take: a field-strength meter, or the person-with-a-handheld route
`uvk5_audible_sweep.py` takes. **A flat line from a broken meter looks exactly like a flat line from a
real null.** This does not close or narrow ADR 0137's absolute-power gap.

**Bench state:** `/home/kb/applications/radio-server` on branch `transmit-power`,
`uvk5_tuner = "hybrid"`, `uvk5_tune_persist = true`, service active. `uvk5_power` is not in
`radio.toml` there, so it takes the `high` default. Move to `master` once the PR merges. Config
backed up at `radio.toml.pre-0145`.

**`tls/` is now in that deployment's `.git/info/exclude`** (ADR 0145) — but still **do not run
`git reset --hard` in that working tree**; use `git checkout <branch>`.

## Instant by default (2026-07-26)

[ADR 0145](adr/0145-instant-by-default.md). **Storing a channel on the radio is now the operator's
choice, off by default, and instant tuning is safe to transmit in.**

Storing is the only thing that costs the firmware's six-second TX lockout, so whether to pay it is a
question about the afternoon, not about the radio. `HybridTuner.persist` (default **off**),
`POST /tuning/persist`, `RadioStatus.tune_persist`, a "Save to radio" switch in the Channels card.
`baofeng.uvk5_tune_persist` sets the boot value; flipping the switch does not write config back.

| | instant (default) | stored |
|---|---|---|
| transmit after a change | **at once** | after ~6.5 s, counted down in the UI |
| radio switched off | boots on the last *stored* channel | boots on this one |
| first key-up after that | **correct** — re-asserted | correct |
| listening after that | **stale until a channel is tapped** | correct |

**`baofeng.uvk5_tuner` still defaults to `off`, and must stay that way.** That is what keeps a plain
UV-5R byte-identical to pre-0142 — no tuner built, no dock baud, bare `SHARED_CAPS`. Defaulting the
global to instant would fire dock frames at a jack with no UART on it.

### The hazard instant creates, and why the fix is cheap

A RAM-only tune is gone when the radio is switched off, but `status()` still reports the channel the
server chose and the UI still highlights it — so the next over goes out on a stale frequency with
nothing detecting it. Tuners now declare `volatile`, and `_key_on()` re-asserts with one `0x0873`
**before** the line goes high; a radio that will not confirm gets a 503 with nothing opened and
nothing keyed. `0x0873` needs **no HELLO**, and a HELLO arms six seconds of mute (`uart.c:355`) — so
re-tuning the radio costs strictly less than asking it where it is.

`EepromTuner.reassert` is an empty body on purpose; its `apply` reboots the radio.

### Corrects ADR 0144

The lockout was armed only when flash changed. But the HELLO (`uart.c:355`) and every read (`393`)
arm it too, and `write_channel` always does both — so a store that wrote nothing reported a **muted**
radio as ready. Hidden because `commit_tuning` short-circuits a re-tap before the tuner; it surfaces
after a service restart, when the server has forgotten the channel and the radio has not.

### Measured on hardware

**Fail-first, re-measured this session rather than cited:** the persistence row on merged `master`
(17ad15b) with `setvfo` scored **0/6, carrier 0.00 s, exit 1**; on this branch, same radio, same
config, same script, **6/6**.

| Gate | Mode | Result |
|---|---|---|
| persistence | branch `setvfo` | **6/6** (master: 0/6) |
| differential 8 rows + persistence | hybrid **instant** | **16/16 + 6/6**, exit 0 |
| storage contrast, 5 rows x 5 | hybrid | **25/25**, exit 0 |
| persistence | hybrid **stored** | **14/15** — 5/6 then 9/9, both reported |

That 5/6 was one 0.30 s blip on a silence row against a 0.25 s bar (a real carrier reads 2.21 s), on
a path this cycle did not touch: with storage on, `volatile` is False so no re-assert runs and the
key path is ADR 0144's exactly. Not re-run until green — both runs are in the ADR.

The storage-contrast row is what separates storage from the re-assert, now that the persistence row
cannot: after a reboot the radio **hears the stored decoy** (tone 0.96) and is **deaf on the instant
channel** (0.00) — *and the key-up still lands there* (2.18-2.23 s).

### Read this before trusting a green row

**Two rows were wrong before the code was.** One read a *draining* lockout as a freshly armed one and
failed a correct implementation (6.49 s, then 6.39 s a tenth of a second later). One was contaminated
by the probe immediately before it — 0.22 against a 0.05 floor, where a settle took it to 0.00 on all
five. Fixed with a drain and a settle, never a wider threshold, and "deaf" stays *second* so
carry-over can only ever produce a false fail.

**And the persistence row's meaning changed.** It now passes under `setvfo` too, because the key-up
re-assert satisfies it as well as storage does. A passing `setvfo` there is the fix, not a broken
gate. The script says so at the function.

**Bench state:** `/home/kb/applications/radio-server` on branch `instant-by-default`,
`uvk5_tuner = "hybrid"`, `uvk5_tune_persist = true`, service active. Move to `master` once the PR
merges, and **set `uvk5_tune_persist` back to `false`** if you want the shipped default. Config backed
up at `radio.toml.pre-0145`.

**I broke the bench again, the same way, and it is now fenced.** `git reset --hard` on the deployment
deleted the staged `tls/` certificates and the service crash-looped on
`server.tls_cert=... is not a readable file` — exactly what `git stash -u` did in ADR 0143. Recovered
from `git show "stash@{0}^3:tls/radio-cert.pem"`, and `tls/` is now in the deployment's
`.git/info/exclude` so no checkout, reset or stash can eat it a third time. **Do not run
`git reset --hard` in that working tree**; use `git checkout <branch>`.

## Instant and persistent (2026-07-26)

[ADR 0144](adr/0144-instant-and-persistent.md). **A channel change is now 1.1 s instead of ~14 s,
and still survives the radio being switched off.** Bench runs `baofeng.uvk5_tuner = "hybrid"`.

| | `eeprom` | **`hybrid`** | `setvfo` |
|---|---|---|---|
| change a channel | ~14 s | **1.1 s** | instant |
| same channel again | ~14 s | **7.6 ms** | instant |
| survives a power cycle | yes | **yes** | **no** |
| firmware | stock | F6 | F6 |

`0x0873` retunes the synthesiser in place (`Dock_SetVfo` ends in `RADIO_SelectVfos();
RADIO_SetupRegisters(true)`) but writes RAM. Hybrid does RF first, then storage, and skips the
reboot — the reboot only ever existed to make the firmware load the record.

**The one firmware fact everything rests on:** the six-second TX lockout is armed at exactly four
sites in `app/uart.c` — 355 (HELLO), 393 (EEPROM **read**), 447 (write), 586 — and **the dock
opcodes do not arm it**. `SerialConfigInProgress()` refuses a key-up *and* cuts an over in progress.
So the lockout is published (`tx_ready_at` → `RadioStatus.tx_ready_in`) and waited out in
`_key_on()`, not slept through in the tune: a listener is not charged for a talker's wait. The Talk
button disables itself with a countdown that ticks locally, because nothing pushes a status event
while the radio sits muted.

**`POST /diagnostics/reboot-radio`** — a reset, not a tune; refused mid-TX, 501 without a tuner. It
exists so persistence is testable unattended.

### The gate caught itself — read this before trusting any new row

The persistence row was written specifically to fail under `setvfo` (RAM-only). Run against
`setvfo`, **it passed 2/2** — because the radio's storage already held that channel from an earlier
hybrid run. "It persisted" and "it was already there" are indistinguishable when only one channel is
involved. Rewritten as decoy → reboot → test → reboot, it now fails `setvfo` **0/6** (carrier
0.00 s) and passes `hybrid` **6/6**.

Writing a gate with the failure in mind is not the same as checking it fails. Run it against the bug.

**Measured:** differential 16/16 (hybrid), persistence 6/6 (hybrid) / 0/6 (setvfo), eeprom cold +
recovery 4/4 after the `write_channel` refactor. `uv run pytest` 1828 / 5 skipped, `npm test` 58.

**Bench state:** on branch `instant-and-persistent`, `uvk5_tuner = "hybrid"`, service active. Move
to `master` once the PR merges. Config backed up at `radio.toml.pre-0144`.

## A tune must know it has a session (2026-07-26)

[ADR 0143](adr/0143-a-tune-must-know-it-has-a-session.md). **ADR 0142 below reported 80/80 and the
operator still could not set a frequency.** Read that section knowing this one exists.

Every `POST /presets/apply` returned **500**, `TuneError: no answer to an EEPROM read at 0x8808`.
Not the cable (symlink unmoved, fd still held, no re-enumeration) and not the radio (a direct probe
answered `HELLO` with `F4HWN v5.7.0` on the first ask).

**The firmware answers EEPROM frames only inside a session, and refuses silently** — every handler
in stock `app/uart.c` opens with `if (pCmd->Timestamp != Timestamp) return;`. So "no answer" *is*
the radio saying we have no session, and it is indistinguishable from a dead cable unless the host
asks. `_hello()` used `send()` where it needed `request()`, ignoring the `0x0515` reply
`frames.py` already decoded, then **latched `_hello_sent` for the life of the process** — so one
HELLO fired into a radio that was mid-flash wedged every tune for three hours, including long after
the radio came back. Restarting the service was the only cure.

**Why the 80/80 gate could not see it:** it ran as one continuous session against a warm,
already-HELLO'd radio and re-established the session after each of its own resets. It never entered
the case where the session dies for a reason the server did not cause — a flash, a battery swap, the
power switch. *A gate that cannot fail the way the product fails is not a gate.*

**Now:** the handshake is verified before anything is believed; silence costs **one** re-handshake
and retry rather than a service restart; `apply()` pre-flights before a sequence that reboots the
radio; the post-reset HELLO is retried because boot time is not a constant.

**And a hardware fault reaches the operator as a sentence.** New `backends.base.RadioUnavailable`
(the hardware could not, as against `UnsupportedCapability`'s the mode cannot), rendered by one
**app-wide** FastAPI handler as **503 with the message**. `TuneError` was previously caught nowhere,
so a switched-off radio reached the web UI as `Request failed (500): Internal Server Error`.

**Measured on hardware** (`scripts/bench/tune_survives_a_reboot.py`, service stopped, restarted in a
`finally`):

| Row | Result |
|---|---|
| cold tune, no prior session | **3/3** |
| tune → `Reset()` out-of-band → tune again | **3/3** |
| the same gate against the pre-0143 tuner | **0/1**, `no answer to an EEPROM read at 0x8808` — the operator's exact error |
| `tune_follows_preset.py -n 2` (regression) | **16/16**, all eight carrier/silence/RX rows |

That third row is the point: the gate was checked against the bug before being trusted about the fix.

**Bench state:** `/home/kb/applications/radio-server` is checked out on the branch
`tune-must-know-it-has-a-session`, service active, `uvk5_tuner = "eeprom"`. Move it to `master` once
the PR merges. Its previous working tree is in `git stash@{0}` ("pre-0143 deployed working tree") and
the config is backed up at `radio.toml.pre-0143`. **Note for next time:** that stash swallowed the
untracked `tls/` certs and the service crash-looped until they were restored with
`git checkout stash@{0}^3 -- tls/`.

## The server picks the repeater (2026-07-26)

[ADR 0142](adr/0142-the-server-picks-the-repeater.md). **Baofeng mode can tune the radio now.**
`POST /presets/apply` moves a UV-K5 on the other end of the AIOC — frequency, split, CTCSS, mode —
with no flash and nobody touching the radio.

**Turn it on:** `baofeng.uvk5_tuner` in the `[baofeng]` block.

| value | mechanism | firmware | cost |
|---|---|---|---|
| `off` (default) | none; TX/RX only, as before | any | — |
| `eeprom` | write the channel, soft-reset onto it | **stock, what is on the radio now** | ~15 s + a flash write |
| `setvfo` | one `0x0873` frame | **F6 fork only** | instant |

**Verified on the bench, `eeprom` path, `scripts/bench/tune_follows_preset.py`.** Differential, not
a repeater tail: apply a preset, then check where the carrier is **and where it is not**. A radio
stuck on one frequency passes the carrier rows by accident and fails the silence rows. The split row
is the repeater case — carrier on the **transmit** leg (446.400), silence on the receive leg. RX
tested by reversing roles: `tone_power` **0.96** tuned versus **0.000** detuned.

**Three firmware bugs, all found by writing the `0x0874` reply rather than by flashing:**

1. **Units.** The wire spoke Hz into a VFO stored in **10 Hz units**. Silent, because
   `FREQUENCY_GetBand()` clamps — 4.485 GHz would have resolved to band 7 and mis-calibrated the PA
   with no error anywhere.
2. **Power.** `0/1/2` written raw into `{USER, LOW1..LOW5, MID, HIGH}`, so **"high" meant `LOW2`**.
   Correct tune, radio keys, repeater stays shut — the exact failure being chased.
3. **Five silent rejections**, including an unresolvable tone falling through to *no tone*.

**ADR 0137 was wrong about the TX lockout:** `gSerialConfigCountDown_500ms = 12` is armed by
`CMD_0514` (Hello) and `CMD_051B` (**read**), not only `CMD_051D` (`app/uart.c:355, 393, 447`). Any
dock conversation mutes TX for six seconds, so the tuner waits it out before returning — the bench
showed this precisely, every carrier row failing attempt #1 and passing after.

**Two traps worth remembering:** `busy` is hardwired `False` on this backend (ADR 0015), so any RX
probe polling it scores a working receiver as deaf; and a shared serial handle needs the transport's
**read timeout**, not just its baud, or `read(4096)` blocks for a full buffer and short replies are
never dispatched (`apply_port_settings`).

**Still open:** no real repeater opened by the server yet (K0PRA 448.525 is confirmation, not the
gate); **2 m unverified** — 36 of the 38 repeater presets are 145/144 MHz and the witness is
SA818-UHF; `setvfo` unproven on hardware until F6 is flashed
(`~/Downloads/f4hwn.fusion.v5.7.0.f6-dock-set-vfo.bin`, sha `b5c07e3b…`); `POST /radio/select`
baofeng→uvk5 still segfaults (139).

## Half the overs go out on the other VFO (2026-07-26)

[ADR 0140](adr/0140-the-first-key-is-always-lost.md). **Read before trusting any RF number here,
including ADRs 0138 and 0139.**

**Root cause.** The radio alternates which VFO it transmits from. Dual watch flips `gRxVfo` on its
own clock and `RADIO_SelectCurrentVfo` (`App/radio.c:715-721`) makes `gCurrentVfo` follow it, so
whichever VFO the radio is listening on when PTT asserts is the one it transmits on. Roughly half the
overs leave on the *other* VFO — and the kv4p is SA818-UHF, so if that VFO is on 2 m the witness
cannot hear it at all.

**The evidence that settles it — the hit rate depends on the gap between keys:**

| gap | rate |
|---|---|
| 6 s | **9/10** (and 10/10 earlier the same day) |
| 19 s | 5/12, strictly alternating |
| ~21 s | 5/10, strictly alternating (failed on exactly 2,4,6,8,10) |

A broken transmitter does not care how long you wait between keys. Aliasing against a periodic
switch does exactly this, and no connector, cable or battery can alternate.

**radio-server is exonerated at the pin.** `GET /diagnostics/ptt-line` reads the line back through
`TIOCMGET` (pyserial's `.dtr` only echoes what you assigned): **DTR HIGH 16/16 samples on 10/10
overs**, low when idle, while only 3/10 produced RF.

**Line choice retested properly** (interleaved, because block order ranks candidates by when they
ran): **dtr 5/10, rts 0/10, both 0/10.** ADR 0138's `ptt_line = "dtr"` was right — but it had been
decided on one sample per candidate.

**Retracted:** the battery. It reads 8.3 V / 99 % on the charger. That was the second unmeasured
hardware excuse this session; both were wrong.

**The fix is already written and not yet flashed.** F6 `0x0873` sets **both** VFOs (fork branch
`f6-dock-set-vfo`, PR #1) — added as a precaution, now the actual fix. Host tests 48/0, builds clean.

**Still open:** the second frequency has never been read (no VHF receiver, firmware not flashed), and
`POST /radio/select` baofeng→uvk5 segfaults the server (139).

Reusable bench tools from this cycle: `trials.py` (N-of-M, nothing single-shot), `keyup_reliability.py`,
`truth_table.py` (PTT pin + squelch + audio on one over), `ptt_line_shootout.py` (interleaved).

## The repeater instrument, and the pre-flight that stopped it lying (2026-07-25)

[ADR 0139](adr/0139-opening-a-real-repeater.md). `scripts/bench/repeater_openup.py`.

**The measurement.** Witness on the repeater's **output**, station transmitting on its **input**.
The verdict comes from the window **after our own carrier drops** — a witness inches from a keyed HT
can be desensed 5 MHz away, so `busy` *while* we transmit is exactly what a dead repeater also
shows; `busy` *after* we stop cannot be us. `busy` is the SA818 SQ pin (hardware carrier detect,
~560 Hz polling). `t0` is the transmit request's **return** — late on purpose. Three overs, each
with its own quiet check, **≥2 tails** to call it opened. The over **is** the Part 97 station ID
(`StationId.identify()`, CW).

**The pre-flight is the finding.** ADR 0138 said nothing can read the front-panel VFO and left it
there — which silently gives every Baofeng-mode result three causes and one output. So the script
first points the witness at the repeater's **input** and keys once.

| step | result |
|---|---|
| pre-flight on 443.525 (K0PRA input) | **0.00 s** carrier → **INCONCLUSIVE**, refused to proceed |
| keying per candidate to split the causes | **445.800: 1.21 s carrier, audio RMS 3816.5** |

**The radio was never moved to K0PRA.** Without the pre-flight this run would have published a
confident false **NO RESPONSE** about somebody's repeater — the ADR 0136/0137/0138 error class a
fourth time, caught before the output for the first time.

Two things fell out:

- **`POST /radio/select` un-wedges the radio.** It reaches `Uvk5Radio.close()` → `ExitHwMode`
  (`0x0871`), and the radio keyed first try afterwards. The ADR 0138 wedge is specific to a *service
  stop* landing mid-handshake — **not** the mode switch operators actually use.
- `transmit()` blocked **1.51 s** = the 1.0 s TX lead + 80 wpm CW `AE9S`. Blocking contract and
  audio path both perfect while the run produced nothing, which is the shape that reads as a dead
  transmitter.

**Also shipped:** the bench box's parked `[baofeng]` block is **active**, on the *same* AIOC and
sound card as `[uvk5]` (one cable, one radio, two mutually exclusive drivers; `server.backend` still
rests at `uvk5`). The UI stops hiding where it transmits — `TuneControls` names the contract instead
of "audio-only backend", and `StatusPanel` gains a **Transmits on — set on the radio, not readable
from here** warning row, because losing `set_frequency` also hides the face's LCD.

### Next — finish the acceptance

One human action, then one command. Put the UV-K5 on **K0PRA 448.525** (VFO 448.525, −5.000 MHz
offset, TX tone 100.0, FM wide), then:

```bash
ssh kb@192.168.1.62
cd ~/applications/radio-server
export RADIO_API_TOKEN=$(grep -oP '^api_token\s*=\s*"\K[^"]+' radio-secrets.toml)
curl -sk -X POST -H "Authorization: Bearer $RADIO_API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"backend":"baofeng"}' https://127.0.0.1:8090/radio/select
.venv/bin/python scripts/bench/repeater_openup.py --repeater "K0PRA448.525" \
  --i-will-transmit --i-am-the-licensed-operator --restore-backend uvk5
```

If it reports NO RESPONSE, run `--source operator` before believing it: that leg asks whether the
witness can hear this machine's output at all, and without it "the UV-K5 does not reach it" and "the
instrument cannot receive it" are the same output.

### Still the open gap

**Absolute RF power, unmeasured.** If K0PRA opens, that retires the question in the only unit that
matters without a wattmeter. Nothing on 2 m either — the kv4p is SA818-UHF, so the pre-flight, the
tail measurement and the instrument check are all unavailable on the 15 VHF repeaters.

## Baofeng mode is proven on the UV-K5 — the dock is optional for repeaters (2026-07-26)

[ADR 0138](adr/0138-baofeng-mode-proven-on-the-uvk5.md). Both of ADR 0137's gates ran against hardware.

**Gate 0 — the AIOC keys this radio.** `scripts/bench/aioc_ptt_gate0.py`

| line | witness RMS |
|---|---|
| baseline | 0.0 |
| **DTR** | **1926.8**, repeat **1970.5** |
| RTS | 0.0 |

**Gate 1 — and it talks.** `scripts/bench/baofeng_mode_acceptance.py` drives the real `AiocBaofeng`
class through a full `transmit()`. Recovered 1000 Hz across four runs: **909.88 / 927.19 / 924.39 /
910.84**, against an untransmitted 1600 Hz control (5–66x; only the noise band moves).

The tone went out through the AIOC sound card, the radio's own firmware transmitted it, and it came
back. No dock, no register writes, no CAT.

### The wedge — read this before trusting any "no RF" result

radio-server holds the radio in the dock's `0x0870` full-control loop, which blocks the firmware main
loop — and **the main loop is what samples the hardware PTT pin** (`app.c:1441`, ADR 0120). A service
stop landing mid-handshake leaves the radio wedged there, deaf to its own PTT input:

| dock session up for | then stopped, DTR asserted |
|---|---|
| ~8 s | **deaf** at +2 s, +8 s, +20 s |
| ~40 s | keys first try, twice |

A wedged radio and an AIOC that cannot key look identical at the witness. Both scripts now refuse to
stop a session younger than `SETTLED_SECONDS` (30 s), and Gate 1 re-tests a bare PTT assert and says
**INCONCLUSIVE** rather than "failed" when that is silent too.

**Dock mode and Baofeng mode are mutually exclusive.** Also only one AIOC serial interface exists
(`ttyACM0`), so the dock link and the PTT line cannot both be open.

### Next — the real acceptance

Nothing here has opened a repeater. Everything is bench frequencies into a witness inches away. The
acceptance is a **courtesy tone coming back**: set `server.backend = "baofeng"` against the AIOC,
operator selects a repeater channel on the radio, service transmits.

### Still the open gap

**Absolute RF power, unmeasured.** A witness this close would hear a microwatt, so no number in ADR
0137 or 0138 speaks to whether this radio reaches a repeater. Nothing on 2 m either (SA818-UHF).

## Repeater key-up: four hypotheses killed BY MEASUREMENT, and the architecture question (2026-07-25, latest)

[ADR 0137](adr/0137-let-the-radio-be-a-radio.md). First cycle in this arc that measured anything.

**Bench access works and always did** — `ssh kb@192.168.1.62 true` returns 0. Prior cycles' "no
shell" blocker was a misread host-key failure plus a pre-auth ASCII MOTD. Never trust output text
for an access claim; check the exit code.

**Measured, through the kv4p witness, with the UV-K5 driven as a preset drives it:**

| what | result |
|---|---|
| CTCSS tone ON vs OFF @141.3 | **149x** over floor (controls 19.11 / 18.79 agree) |
| Split: witness on TX leg 446.000 | 2872.7 RMS |
| Split: witness on RX leg 445.800 | **0.0** — clean null |
| All 41 presets applied + read back | every RX, TX leg and tone correct |
| Tone frequency, all six tones | constant +0.372% ratio, spread 0.00054 = a **clock**, not the radio |

**The under-deviation hypothesis is dead.** So are wrong-frequency, broken-split and bad-preset.
Four ruled out by measurement; none ruled in.

`scripts/bench/repeater_evidence.py --tone-control|--tone-accuracy|--split|--presets`. `--presets`
never keys, so it is safe against real repeater presets.

### Next: Gate 0 — `scripts/bench/aioc_ptt_gate0.py`

Does the AIOC's serial PTT line key this radio? If yes, the dock register path is optional for
repeater work: the operator picks the channel, `AiocBaofeng` (exists, bench-proven ADR 0029) keys
DTR, and the radio's own firmware sets up TX. That is the project brief's "Baofeng mode".

Needs ONE human action first — the radio parked on 445.800, because DTR keying transmits on whatever
the front panel says and nothing in this repo can read that back. The script refuses non-bench
frequencies, aborts if the witness already hears a carrier, and restarts the service in a `finally`.

```bash
ssh kb@192.168.1.62
cd ~/applications/radio-server
export RADIO_API_TOKEN=$(grep -oP '^api_token\s*=\s*"\K[^"]+' radio-secrets.toml)
.venv/bin/python scripts/bench/aioc_ptt_gate0.py --i-will-transmit
```

### Not measured, and it is now the obvious gap

**Absolute RF power.** The witness is inches away and would hear a microwatt, so every number above
is silent about whether the radio puts out enough to reach a repeater. No instrument for it exists.
Also: nothing on 2 m has ever been measured (SA818-UHF witness); 15 VHF repeaters stay inferred.

### Bench facts

HTTPS not HTTP on `:8090` (uvk5) / `:8091` (kv4p) — plain `http://` fails silently. Token
`api_token` in `radio-secrets.toml`. Both are **user** services (`systemctl --user`). Repo at
`~/applications/radio-server`. One AIOC serial interface only (`ttyACM0`), so the dock and the PTT
line cannot both be open.

## CORRECTION: there is no bench-access blocker, and there never was (2026-07-25)

Three cycles ended with "no RF measured — no shell on the bench box". **That was false.**

```bash
ssh kb@192.168.1.62 'true'; echo $?    # -> 0
```

It works. It always did. Two things went wrong, and both are mine:

1. Earlier attempts used `ssh kb@home`, which failed a **host-key check**. I read that as an
   authentication failure and generalised it to "no access exists".
2. The box prints a large ASCII-art MOTD *before* auth. One session I read that banner as a
   successful login; the next I treated a banner-only response as proof of failure. **Neither time
   did I check the exit code**, which is the only thing that answers the question.

The operator told me directly that I had SSH'd to this box many times. I recorded my own inference
over his testimony and then designed two cycles of hand-keyed bench procedure around a blocker that
did not exist.

**Rule: an access claim is a tested claim.** `ssh ... true; echo $?`. Never a conclusion drawn from
output text. Anything below dated earlier that says "blocked on bench access" is void.

### Live bench facts (read over that shell, 2026-07-25)

- radio-server serves **HTTPS**, not HTTP: `https://127.0.0.1:8090` (uvk5) and `:8091` (kv4p).
  Plain `http://` gets a silent connection failure, which is what made it look unreachable.
- Auth: bearer token, key `api_token` in `~/applications/radio-server/radio-secrets.toml` (0600).
- Repo on the bench lives at `~/applications/radio-server`, hostname `ubuntuserver`.
- UV-K5 is on the AIOC: `/dev/serial/by-id/usb-AIOC_All-In-One-Cable_da3441ac-if04`.
- `/capabilities` -> `ptt, receive, scan, set_frequency, set_mode, set_split, set_tone, status,
  transmit`. 41 presets load. Radio idle on 147.555, `tone: null`.

## The deviation probe was measuring the window, not the transmission (2026-07-25, latest)

[ADR 0136](adr/0136-the-probe-measured-the-window-not-the-transmission.md). The instrument ADR 0135
shipped was never run against hardware before shipping. The operator set up a reference handheld,
followed the procedure, keyed for six seconds — and nothing recorded it.

**Fault 1, usability.** `--reference` printed `KEY NOW` and started a 6 s capture on the same line.
Reading the prompt and picking up a radio came out of the measurement. It now opens a **45 s**
window (`--window`) and measures the loudest `--seconds` inside it: no cue to hit, no way to be too
slow.

**Fault 2, and this is the one that matters.** The probe's output is a *ratio* between the
operator-keyed leg and the script-keyed leg, meaningful only because the receive chain's unknowns
are common to both. They were not. Each leg carried a different amount of dead air — the operator's
reaction time on one side, `key_and_listen`'s 0.3 s lead-in and 1 s tail on the other. `band_rms` is
an RMS over whatever it is handed, so dead air drags a leg's amplitude down, and **a leg dragged
down reads as under-deviation: the exact finding the probe exists to test for.** The instrument was
biased toward confirming its own hypothesis.

It was not reproducible either. `band_rms` applies a Hann window, so a transmission near either end
of a capture is attenuated by the taper on top of the dilution. Identical transmissions:

| keyed | unsliced | sliced |
|---|---|---|
| early | 1726 | 11584 |
| mid-window | 7687 | 11584 |
| late | 1726 | 11584 |

**4.5x from reaction time alone**, against a 0.5x verdict threshold. `loudest_slice()` now picks the
transmission out of the capture, and `measure_transmission()` is the only path to a recorded number
so both legs are treated identically. 10 tests, four mutations caught, suite 1670 -> **1680/5**.

### Bench access — see the correction at the top of this file

No RF measured *at the time this section was written*, on a blocker that turned out not to exist.

### The run order, unchanged from 0135

On the bench box, with the service up (ADR 0127 — do not stop it):

```bash
export RADIO_API_TOKEN=...
.venv/bin/python scripts/bench/uvk5_tx_regs.py --i-will-transmit --frequency 445800000 --tone 141.3
.venv/bin/python scripts/bench/deviation_probe.py --reference baofeng-mini      # key by hand
.venv/bin/python scripts/bench/deviation_probe.py --reference uvk5-front-panel  # key by hand
.venv/bin/python scripts/bench/deviation_probe.py --under-test --i-will-transmit
.venv/bin/python scripts/bench/deviation_probe.py --compare
```

All three legs in one sitting — the geometry must not move between them. `--sweep` only if
`--compare` does not settle it.

The reference channel is 445.800 simplex, **141.3 Hz CTCSS encode only** (CHIRP `Tone`, not `TSQL`),
wide FM, no offset.

## Repeater key-up: the power hypothesis is retracted, and the bench now has an instrument that could see the fault (2026-07-25, latest)

[ADR 0135](adr/0135-ctcss-deviation-and-the-instrument-gap.md) is the account. Read it before the
section below, which it partly retracts.

### The correction that matters

The previous cycle ranked **the radio's front-panel TX power** as the leading survivor, on the
strength of reading `L1` off a photograph of the radio's screen. The operator then opened these same
repeaters, through this same antenna, with an **ID-51A and a Baofeng Mini at microwatts**.

That retires power, feedline, antenna and the repeaters themselves at once. It also retires the
reasoning: a photograph is not a measurement, and ADR 0134 said exactly that about itself and then
leaned on it anyway. **Do not re-raise TX power without new evidence.**

### Nine hypotheses are now dead. Stop reading source.

Four more died this cycle, all in the **compiled** driver. `App/driver/bk4819.c` is in the firmware
tree but is **not** in `App/CMakeLists.txt` and does not ship — `bk4829.c` is what runs, and earlier
analysis quoting `bk4819.c` was reading dead code.

| Hypothesis | Verdict |
|---|---|
| `Dock_ForceTx` wipes host CTCSS via `ExitSubAu` | **DEAD** — `PrepareTransmit` is `ExitBypass`+`ExitTxMute`+`TxOn_Beep`, nothing more (`bk4829.c:1222-1236`) |
| `Dock_ForceTx` retunes to the front-panel VFO | **DEAD** — no `SetFrequency` in the call chain |
| Our CTCSS code word is wrong | **DEAD** — byte-identical to `bk4829.c:586`, decodes back within 0.03 Hz on all eight tones |
| TX power | **DEAD** — operator, empirical |

On paper the register programming is correct. Five of the nine died to source reading; another pass
will not find this.

### The actual finding: the bench could not have caught this

The only over-the-air CTCSS check is `tone_power()`, a **dimensionless fraction**. Against synthetic
signals with known tone amplitude:

| CTCSS deviation | `band_rms` (new) | `tone_power` (what the bench used) |
|---|---|---|
| 100% | 5656 | 0.999987 |
| 10% | 565 | 0.998757 |

A tone at **one tenth** of correct deviation moves the old metric by 0.001, against a pass floor of
0.01 — it passes the bench and opens nothing in the field, which is the reported symptom. And that
check runs only inside split stages that currently **skip**. Deviation has never been measured here
in any units, ever.

### What to run, in this order

> **VOID — see the correction at the top of this file. SSH to the bench box works.**

Nothing below has been run — there is still no shell on the bench box.

1. `scripts/bench/uvk5_tx_regs.py --i-will-transmit --frequency 445800000 --tone 141.3` — reads
   `0x51`/`0x07`/`0x40`/`0x43` off the chip while keyed. Prediction on record: `0x40` reads `0x3516`,
   the firmware's boot constant and the **only** REG_40 write in the whole firmware
   (`bk4829.c:151`), which means the front panel transmits with the identical deviation word.
   Anything else is a real new finding.
2. `scripts/bench/deviation_probe.py` — three legs, in one sitting, radios not moved between them:
   `--reference baofeng-mini`, then `--reference uvk5-front-panel`, then
   `--under-test --i-will-transmit`, then `--compare`. Handheld settings: **445.800 FM wide,
   141.3 Hz CTCSS encode, RX tone off, dead carrier ~6 s, say nothing.**
   - A ≈ B ≫ C → our dock writes under-deviate (a bug we own)
   - A ≫ B ≈ C → the radio under-deviates in both firmwares (not a code bug)
   - A ≈ B ≈ C → deviation is fine; go back to ADR 0134's remaining survivors
3. `--sweep`, only if step 2 does not settle it.

### Bench access is the blocker

> **VOID — see the correction at the top of this file. SSH to the bench box works.**

`ssh kb@home` offers `publickey,password`; this machine has no private key and the agent no
identities, so password auth cannot be driven non-interactively. `sshpass` is installed but the
password is not available to the cycle. One-time fix, either:

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519 && ssh-copy-id kb@home
# or leave the password where the cycle can find it, once:
printf '%s' 'THEPASSWORD' > ~/.bench-pw && chmod 600 ~/.bench-pw
```

### Two things deliberately NOT changed

- **REG_51 gain.** We write `0x904A` (gain 74); the firmware writes `0x9040` (gain 64). Ours is
  *hotter*. Under-deviation is a live hypothesis, so lowering our gain to match would move the wrong
  way. Settle it after measurement, not before.
- **`radio_server/` is untouched by this cycle.** Bench scripts, a guard and tests only.

### The guard was one CHIRP import away from a live repeater

`bench_frequency_only` now carries a preset deny-list on top of its bench allow-list. The hazard the
allow-list cannot see is a real repeater imported onto a bench frequency. Sources are **unioned**
(a restarted server answers `200 []` — a successful read of nothing); the exemption is per **preset**
(every leg must be a bench frequency) not per frequency, and never by **name**. Unreadable presets
refuse. `tests/test_bench_guard.py` is the first coverage `scripts/bench/` has ever had; all four
mutations caught, one of which exposed a test that had been passing for the wrong reason.

`uv run pytest` **1670 passed / 5 skipped** (baseline 1661/5).

---

## No repeater keys up — the two obvious causes are ruled out, and the survivors are now visible (2026-07-25, later still)

Field report: on a good antenna with clear LOS, **not one of the 41 presets opens a repeater**, while
simplex TX is confirmed good on the air. [ADR 0134](adr/0134-repeater-keyup-in-the-field.md) is the
account. **This cycle does not claim a root cause** — it kills four hypotheses with evidence, makes
the two survivors observable, and closes the test gaps that let the whole class hide.

### Read this first: the leading suspicion was wrong

The brief's hypothesis was a duplex-sign error, and it was a good one: ADR 0133's only RF proof was
**one** preset at a **positive** +600 kHz offset, while 34 of the 37 real repeaters are negative. A
sign fault is invisible against that fixture and fatal against every repeater.

Measured read-only against the live station (`GET` only, nothing keyed):

| Check | Result |
|---|---|
| `GET /presets` | **41** |
| Duplex sign, all 41 | **0 mismatches** — 34 × −0.600/−5.000, 3 × +0.600, every sign correct |
| CTCSS tone, all 41 | **0 mismatches** — 107.2 / 100.0 / 103.5 / 141.3 / 123.0 / 88.5 |
| `GET /capabilities` | includes `set_split` |
| `GET /status` | `frequency 147555000`, `tx_frequency null`, `tone null` |

Four hypotheses died there: the duplex sign, the CHIRP tone-column mapping (`TSQL` → `cToneFreq` is
CHIRP's own model and every one of the 26 affected rows took the right column), the browser PTT path
skipping the split (**every** key path — `/audio/tx`, `POST /transmit`, `POST /ptt`, the automatic
station ID — converges on `_key_on`, which reads the split and tone at key time), and the backend not
advertising `SET_SPLIT`.

**So the fault is not in the data and not in the split plumbing.** It is in what leaves the antenna,
or in whether the split is still armed when the operator keys.

### The two survivors, and why nobody could see either

**1. The PA is set up from the radio's own front panel, not from what the host tuned.**
`Dock_ForceTx` takes its band from `gCurrentVfo`, and `_correct_tx_band` **deliberately keeps the
firmware's bias byte** — that bias is per-band calibration in an SPI flash the dock cannot read, and
inventing one is the mistake ADR 0128 removed. So transmitting outside the radio's VFO band uses the
**other band's** calibration.

The radio's VFO is on **445.800 (UHF)**; the 37 repeaters span both bands. **At most one band can
ever be correctly biased.** `RadioStatus` now carries `pa` (bias / gain / `band_matched` / TX leg),
recorded at key-up from the reg-0x36 read `_correct_tx_band` was already doing, and the log goes
INFO → **WARN** and says the power is uncharacterised. Same argument as `rssi`: a station reaching
nothing is indistinguishable over the API from one that works — every call returns 200.

**2. The armed split is process-local, and three ordinary actions clear it silently.** A scan hop
(`scan/engine.py:240`), the Tune card (`POST /frequency`), and **every `systemctl restart`** each
null `_tx_frequency`. The only prior signal was a Status row that *disappears* — not something an
operator holding Talk can notice. `TalkControl` now states it both ways:
`SPLIT — transmits on 144.5450 MHz` / `SIMPLEX — transmits on 145.1450 MHz`. **The SIMPLEX half is
the point**; a card that announced only splits would have been exactly as silent in the failure case.

### The gaps that let this hide, now closed

- **All 37 CHIRP rows recomputed independently.** Only 4 were pinned by value before, and a
  sign/column fault is uniform — it lands on all 37 or none, which is the one shape spot-checks
  cannot rule out. Mutation-verified: flipping `DUPLEX_SIGNS` and swapping the TSQL column each turn
  it red.
- **`split-minus`**, the exact mirror of `split` (rx 446.400 / tx 445.800) — the negative sign, on
  the same two bench frequencies, keying nothing new. Both are now one parameterised proof rather
  than a copy that could drift, and each fails loudly if its fixture preset does not match the legs
  it claims to test.
- **A key-up carrying a tone *and* a split.** Every split test keyed tone-less; every tone test keyed
  simplex — so the shape all 37 presets use had no coverage. Mutation-verified: deleting
  `*self._tone_pairs()` from `_key_on` fails this test and **no other test in the suite**.

### Three keying-safety holes the new fixture opened, found in review and closed

Adding a second split preset at 445.800 made three latent hazards reachable. All three answer the
pre-split question "where is the radio pointing?" instead of the current one.

- **`stage_presets` restored "home" by `frequency` alone** — and `Bench Split` is also on 445.800,
  so it could restore home *with a split armed to 446.400*, passing its own green check. `stage_tx`
  would then key the armed leg while the witness sat on 445.800: zero carrier, a red X identical to
  a dead transmitter. Now matched with `not tx_frequency` and checked for it.
- **Every early return in the split stage skipped the restore.** Harmless for the plus stage (its RX
  leg *is* home); it would strand the minus stage on 446.400. Restore moved into a `finally`.
- **`_set_session` keyed via `POST /services/99` with no `bench_frequency_only`** — pre-existing, and
  the last hole in that guard. `rf_witness` only compares *receive* frequencies, so it would pass a
  station with a split armed to a real repeater input. Guarded now.

### ⚠ What is NOT done, and what the next cycle needs

> **VOID — see the correction at the top of this file. SSH to the bench box works.**

**No RF was measured and no register was read.** There is no shell on the bench box from the cycle
environment — `ssh kb@192.168.1.62` returns `Permission denied (publickey,password)`, `~/.ssh` holds
no private key, and a password attempt was blocked by the permission classifier. Every claim above is
read-only HTTP against the live station or a unit test.

**One-time unblock, on the dev box:**

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
ssh-copy-id kb@192.168.1.62
```

Then, in order — bench frequencies only, never a repeater input:

1. `uvk5_tx_regs.py --frequency 445800000` and `--frequency 147555000`, keyed. Record reg 0x36
   bias/gain and reg 0x33 LNA bits on **both** bands.
2. Repeat with the radio's OUTPUT POWER menu at Low, then High. **If the bias byte moves, the field
   fault is a menu setting, not code** — measured bias is 12, and the operator's photo of the radio
   reads as `L1` beside each VFO line. Reading a photograph is not a measurement; this is.
3. Add `Bench Split Minus` (rx 446400000 / tx 445800000 / tone 100.0) to the deployed `radio.toml`
   and run `split-minus`. Without it the stage SKIPs — and a skipped stage is not a pass.
4. Three consecutive clean full acceptance runs.

Verification this cycle: `uv run pytest` **1661 passed / 5 skipped** (was 1653/5). `npm test`
**50 passed** (was 40).

---

## Repeater split works, and Kris's 37 repeaters are on the server (2026-07-25, later still)

Presets were simplex by explicit decision; ADR 0115 named the follow-on and
[ADR 0133](adr/0133-repeater-split-and-chirp-import.md) is it. The station can now transmit offset
from where it listens, with the CTCSS a repeater needs, and the operator's CHIRP channel list was
imported rather than retyped.

### Read this first: PR #188 merged mid-push, and master lost the fix

**PR #188 was merged at 13:19 MDT at commit `8ded8d0`. Four commits landed on that branch between
13:21 and 13:31 — after the merge — and never reached `master`:**

| | |
|---|---|
| `39592f6` | **uvk5: reg 0x33's output-enable bits are not optional** — the fault-3b fix |
| `47e7f8e` | ADR 0132's account of it |
| `d2e5697` | `uvk5_band_ab.py`, the which-radio-did-you-hear script |
| `5123dec` | the "premise was never verified" writeup |

`39592f6` is the one that made the transmitter audible at all. Without it `_correct_tx_band` writes
a keyed reg 0x33 with **no `0x9000` pin output-enables** — `0x0020`, PA_ENABLE on a switched-off pin
driver — so the power amplifier never comes up **on either band** and only the bare modulator
reaches the antenna. The bench checkout was on the branch, so the *station* was fine; `master` was
not. **PR #189 lands those four commits**; this cycle's branch carries them cherry-picked so it did
not have to wait. Merge #189 (or this PR) before deploying anything from `master`.

### What the split does, and the one thing that makes it safe

The transmit leg is applied **inside the radio's own key path**, because `set_frequency` refuses to
run while keyed (ADR 0132) — so it cannot be "retune, then key".

- **Key-up prepends `0x38/0x39` to the existing key batch.** Safe because one `_write_registers` is
  one frame and a corrupt frame is dropped *whole*: the tune and the TX-enable cannot land
  independently, so the carrier can never come up on the frequency we are listening to. **Split that
  list into two calls and the guarantee is gone** — the comment saying so lives next to the code.
- **Key-down is two frames.** The first is byte-for-byte the simplex un-key; the second is literally
  `set_frequency`'s batch (the one write shape with a citation) and lands strictly after
  `Dock_EndTx`, so the window where the PA ramps down while the synthesiser sits on the repeater's
  *output* is structurally zero.
- **`set_frequency` clears the split.** Fail-safe direction: a TX leg outliving a retune would let an
  unattended station ID key a repeater uplink from a frequency nobody chose.

**The fakes had to be fixed before any of this was testable.** `ForceTxFake` evaluated the
`Dock_ForceTx`/`Dock_EndTx` edge once per serial *write*, after every pair of a frame had landed — so
it would have sampled the un-key edge with the synthesiser already back on RX and **reported the
hazardous ordering as normal.** The firmware's hook runs per register pair; the fake now does too,
and records what the synthesiser held at each edge.

### Measured on the bench

| | |
|---|---|
| CTCSS survives the witness's audio path? | **yes** — 0.000026 tone-off → **0.109** tone-on, a 4250x ratio, so the tone is checked at RF and not just in a register (`scripts/bench/ctcss_probe.py`) |
| Keyed on a split, kv4p on the **TX** leg (446.400) | RMS **8188**, 1000 Hz **0.882**, 100 Hz CTCSS **0.109**, carrier on 16/24 polls |
| Same over, kv4p on the **RX** leg (445.800) | RMS **0** |
| Ratio | **infinite** — the carrier moved completely; the near-field bleed I expected at 600 kHz is not there |
| Live presets | **41** = 3 bench + `Bench Split` + the operator's 37; 38 carry a TX leg, 29 an `rx_tone` |

The RX-leg control is the check that matters. Everything else is equally true of a radio that
ignored the split and transmitted where it was listening.

### The runner caught a real bug

A tone-less preset did **not** clear the previous channel's CTCSS — `services` failed on speech-band
energy **0.31** (want > 0.50) because a 100 Hz tone was sitting under the announcement. Same fault
shape as a leaked split, from the same guard (`if preset.tx_tone is not None and ...`). A preset
describes a *complete* channel: no tone means no tone. It predates this work and was invisible while
no configured preset carried a tone; with 37 repeaters on seven different tones it is a live hazard,
because the wrong sub-audible tone is exactly the failure that does not announce itself.

The runner also **refuses to key anything but a bench frequency** now. It used to transmit "wherever
the radio is pointing", which was safe with three bench presets and is not with 37 real repeaters in
the list.

### One acceptance run failed, and it is worth reading

Not three-for-three. Run 3 of the first batch failed `rx` / `dtmf` / `auth` — and the shape of the
failure is the useful part. Those are exactly the three stages where **the kv4p transmits and the K6
receives**; `tx`, `split` and `services` (the K6 transmitting) all passed in the same run. So the
fault was one-directional.

It was not RF. Measured directly afterwards: the kv4p keyed and the K6's **rssi went 108 → 309 and
back**, so the carrier arrived and the receiver heard it. The failure was above RF — no audio through
`/audio/rx`, nothing decoded.

The journal names it, at `21:27:01`, inside run 3:

> `reg 0x30 read back 0x0000 at connect, which is not a receiving state (everything disabled).
> Seeding the stock RX word 0xbff1 and writing it, rather than adopting a value that would leave this
> radio deaf for the life of the process (ADR 0132).`

Run 3's `systemd` stage stops the service **under WebSocket load** and restarts it. That restart found
the radio with reg 0x30 = 0 — precisely where a lost un-key leaves it — which is ADR 0132 fault 3.
The repair fired and logged, exactly as designed. **It was not sufficient**: RX audio stayed dead for
that process lifetime and a further restart was what cleared it. Worth knowing — the reg-0x30 repair
is necessary and not the whole story, and the remaining piece is probably the RX-audio force-open
(ADR 0120/0122), which the same restart race can miss.

None of this is in code this branch touches. Two cautions for whoever reads a red run next:

- **A radio left in a bad state stays bad, and poisons everything after it.** Four isolated `dtmf`
  runs after run 3 all failed; a service restart made them pass immediately. Restart before
  concluding anything about a repeated failure.
- **I nearly mis-read my own probe.** The first direct test printed empty statuses and `rssi: None`
  and looked exactly like a dead receiver — it was **HTTP 401**, no token in that shell. Check the
  instrument answered before believing what it says. That is the third time this month.

### Still open / deliberately not done

- **kv4p split is a follow-on.** The device already carries separate `freq_tx`/`freq_rx`; the code is
  two lines. The *proof* is a mirror-image RF stage with the K6 as witness, and this project has
  already paid once for a capability that reported success at every layer while nothing useful left
  the antenna. It answers `501` until then.
- **RX tone squelch** — `rx_tone` is stored for round-trip fidelity and reported as unhonoured on
  every apply.
- **`_key_on`/`_key_off` have no lock**, so the pacer's error thread can interleave an un-key between
  two key-up frames. The split raises the cost (a repeater input, not a simplex frequency). The
  obvious `RLock` **deadlocks** — `_key_off` calls `pacer.stop()`, which joins the pacer thread,
  which would be blocked on the lock. Named, analysed, not shipped half-done.
- **One CHIRP row is an assumption, and it is printed:** `KE4GUQ145.34` is `Cross`, and a 9-column
  export has no `Cross Mode`, so `Tone->Tone` cannot be *known*. Worth checking against the radio.
- The uvk5 transmit-band allow-list is still deferred from the previous cycle.

---

## 147.555 works: the dock was keying the band the RADIO was on (2026-07-25, later)

Everything below this section was proven on **445.800**. Moved to **147.555**, the station went
dead in both directions while the API reported success throughout — 200s, real `tx_key_up` /
`tx_key_down` in the operating log. [ADR 0132](adr/0132-dock-band-and-the-register-model.md) is the
full account; four faults, and only two of them are about bands:

| # | Fault | Evidence |
|---|---|---|
| 1 | **`Dock_ForceTx` sets the PA up from `gCurrentVfo`** — the radio's own boot VFO — and never tunes the synthesiser, which the host owns (`uart.c:729-732`). On any frequency in the other band the carrier is right and everything else is set for somewhere else. | keyed on 147.555: reg `0x36 = 0x0CA2` (**UHF** gain byte), reg `0x33` with the **UHF** LNA |
| 2 | **`Dock_EndTx` never puts the LNA back**, and `set_frequency` was the only thing that ever wrote reg 0x33 — so VHF receive died at the first transmission. | `0x9046` before keying → `0x9048` after |
| 3 | **The backend adopted whatever radio state it found, for the life of the process.** `_reg30` was seeded from a bare read at connect and written back on every un-key and retune. | `0x30 = 0` drops RSSI **157 → 0**; a service started against that radio reports `rssi 0 / busy false` and passes **zero bytes**, indefinitely |
| 4 | The reg-0x33 clear mask never cleared the VHF bit, so VHF→UHF left **both** LNA paths on. | inherited from ADR 0112's self-inconsistent prose |

**Fault 3 is the important one.** It is band-independent, it is the better explanation for "the
server doesn't hear me" than anything about 2 m, and it is silent — the API is happy throughout.
`0x30 = 0` is exactly where a lost un-key leaves the radio. Connecting now *repairs* that instead
of inheriting it, and logs when it does.

### Where this stands: both bands work, and the premise was wrong

**Transmit and receive both work on 147.555 and 445.800.** Proven with the kv4p service stopped so
the K6 was the only possible source (`scripts/bench/uvk5_band_ab.py`): five bursts on each band,
self-identifying by beep count, **both legs heard**.

The two lessons are worth more than the fix:

- **"It works on 445.800 but not 147.555" was never verified.** Both bench radios sit on 445.800,
  so "I hear tones on 445.800" never said *which* radio. Hours of band-difference reasoning stood
  on that. `uvk5_band_ab.py` costs 100 seconds and is the only thing that ties an observation to a
  transmitter — **run it first, not last.**
- **The likely cause was fault 3b, which is not band-specific.** A `_reg33` rebuilt from a disabled
  radio produced a keyed value of `0x0020` — PA_ENABLE set in a register with no pin output-enables
  — so the PA rail was never driven on *either* band and only the bare modulator reached the
  antenna. Inches away the kv4p hears that fine; a handheld across the room hears a click.

### Historical: what "weak on 2 m" looked like before the enables were repaired

- **RX on 147.555 — proven over real RF.** Handheld keying from across the room, measured on the
  running station: **`rssi 267` vs a floor of 150, squelch OPEN, held ~12 s.**
- **TX on 147.555 — still open.** With a real antenna, the operator hears the tones on 445.800 but
  only **quarter-second bursts of static** on 147.555, and they follow the frequency when the server
  retunes. So the K6 *is* radiating and *is* on the right frequency — it is **under-driven**, not
  silent. The registers are correct; this is a power problem.
- **Leading suspect: the PA bias is still the firmware's, still derived for the wrong band.**
  `TXP_CalculatedSetting` uses per-band calibration and a *different* divider table for 2 m than
  70 cm (`radio.c:657-661`); here it is **12 of 255**, computed for UHF. The host cannot read the
  real VHF calibration — it is in SPI flash and there is no EEPROM-read opcode in the dock frames.
- **The next step is one round trip, and the tool is built.**
  `scripts/bench/uvk5_audible_sweep.py --i-will-transmit --frequency 147555000` steps the bias with
  each step announcing its own number in beeps first, so the answer is one sentence: "I could hear
  step 5 onwards."

**This bench cannot measure radiated power** — the kv4p is inches away, FM is constant-envelope so
audio level says nothing about power, and its RSSI reads 0 on this firmware even while cleanly
demodulating. `uvk5_pa_sweep.py` twice produced a confident "bias is not the knob" from that dead
meter; it now refuses to conclude when the witness reads zero throughout. **Neither of those runs
is evidence of anything.**

Fixed host-side; **no firmware flash**, and the radio's own screen no longer has to agree with the
server. The fork's `adr/0001-dock-force-tx.md` verify-on-bench item has been answered in place —
its guess that a band mismatch "would only mis-scale power" missed the receiver entirely.

### Standing facts from this round

- **`/status` now reports the raw RSSI.** Idle floors, measured on a running station: **107 on
  445.800, 154–156 on 147.555**. Both under the configured `squelch_threshold = 220`, so one scalar
  still covers both bands — a per-band setting was deliberately *not* added on speculation.
- **The bench cannot hear 2 m.** The kv4p is a single-module SA818-**UHF** board and it is the
  witness every RF stage in `acceptance.py` depends on. So VHF is proven at the **register level**;
  end-to-end RF is proven on UHF only. `scripts/bench/rf_listen.py` covers VHF with a human — it
  reports each signal live, so a long window costs nothing.
- **The runner's RF stages now SKIP with the reason** when the two radios are not on the same
  channel, and a skipped run exits non-zero. Before this they emitted zero bytes / zero duty / zero
  tone, which is indistinguishable from a broken receiver — that output cost real time in this
  session before it was recognised as a band mismatch.
- **Named, deliberately not done:** a transmit-band allow-list for uvk5. The range check is
  18 MHz–1.3 GHz, so a frequency typo'd into the 1.2 GHz band passes every check the server has
  (kv4p already models bands and rejects at config load). Wants its own ADR.

---

## Full autonomous stabilization — the bench is self-testing and green (2026-07-25)

**State: done and demonstrated.** Every Definition-of-Done item is verified by measurement on the
deployed system, not by inference. **No firmware flash is needed** — the F5 build was already on
the radio and is working (proved by register read-back, ADR 0128).

The proof is one command, and it is in the repo:

```sh
cd /home/kb/applications/radio-server
RADIO_API_TOKEN=<token> .venv/bin/python scripts/bench/acceptance.py
```

Eight stages, no human in the loop — it keys both radios itself and uses each as the other's
measuring instrument. **Three consecutive runs PASS (exit 0, 0, 0), the first immediately after a
cold reboot.**

### What was wrong, and what fixed it

| ADR | Fault | Number |
|---|---|---|
| [0127](adr/0127-bounded-graceful-shutdown.md) | `systemctl stop` never completed — uvicorn waited forever on a browser's open WebSocket, blew `TimeoutStopSec`, got SIGKILLed, and the lifespan teardown never ran. 7× in 14 h; one left the service down 8 min (the ConnectionRefused). | **20.0 s + SIGKILL → 5.48 s + `Result=success`** |
| [0128](adr/0128-dock-tx-measured-pa-owned-by-firmware.md) | Chain B closed **by measurement**. Register read-back while keyed shows `0x33 = 0x9028` (PA_ENABLE set) — radio-server never writes that bit, so the F5 firmware is live. The backend's own PA-bias write was dead code the firmware overwrites. | **kv4p carrier 3–4 polls with RF (F4: 0 with / 9 without), tone recovered 0.99. Dock TX radiates at PA bias 12.** |
| [0130](adr/0130-rx-read-off-the-event-loop.md) | The blocking sound-card read ran **on the asyncio event loop** (caught by `py-spy`), so every WebSocket frame and HTTP request delayed the next capture read. | **overs 5.70→3.88→3.28 s shrinking → 5.4–6.0 s stable; duty 100.3–100.8 %; 0 xruns while receiving** |
| [0131](adr/0131-dock-link-robustness.md) | The dock link drops frames and three call sites assumed it never would — including the F4 "first-key settle flake", now reproduced and fixed. | **~1-in-3 runs failing → 3/3 PASS** |

Plus: `POST /tone {"tone": 0}` was an unhandled 500 (0 now means "no tone", 422 for a bad one);
`[[presets]]` added to the deployed config (there were none, so the Channels card was empty);
the stashed `dtmf.py` hardcode dropped as superseded by `audio.dtmf_reverse_twist_db = 10.0` —
which a sweep proved this hardware genuinely needs (the same capture decodes as only `14` at the
stock 4 dB limit); watchers moved `scratchpad/` → `scripts/bench/` with env-driven credentials.

### Standing facts (don't re-derive)

- **The UV-K5's F5 dock firmware is flashed and working.** No flash is pending.
- **Dock TX radiates at PA bias 12** (`0x36 = 0x0CA2`). Low. The lever for more is the radio's own
  OUTPUT_POWER setting — the calibration lives in its SPI flash and the host cannot read it.
- **Stopping the service is cheap and clean now** (~0.5 s idle, ~5.3 s with WebSocket clients).
  There is no longer any reason to leave it stopped.
- Boot survival is real: `Linger=yes`, both units `enabled`, verified twice by actual reboot —
  both servers answering HTTPS ~85 s after power-on with **zero logged-in users**.
- If `tx` ever fails, escalate to `scripts/bench/uvk5_tx_regs.py --i-will-transmit` (needs the
  service stopped; always start it again). It prints a verdict on whether the PA rail is up.

Ops deltas made directly on the box are recorded in [server-notes.md](server-notes.md).


## UV-K5 V3 firmware F5 — dock TX PA fix closes Chain B (ADR 0126) (2026-07-24)

**radio-server docs-only; the fix is firmware. Branched fresh from `origin/master` (`134ce26`,
#184 merged) — not stacked.** F4 Chain B diagnosed that dock keying wrote BK4819 `REG_30` (CONFIRM
passed) but radiated no RF: a bare `REG_30` write lights the modulator yet never the external PA
rail (`REG_33` GPIO1 PA_ENABLE) or PA bias (`REG_36`). This cycle builds and ships the firmware fix
and closes Chain B from the radio-server side with [ADR 0126](adr/0126-uvk5-v3-dock-force-tx-chain-b-close.md).

**Correction carried into the ADR:** the F4 HANDOFF (PR #185) called the missing pieces "MCU-side
PA-enable + antenna-switch GPIOs." The firmware source shows this PY32F071 port has **no MCU GPIO in
the TX path** — PA-enable/antenna-switch/LNAs are all **BK4819 GPIOs via `REG_33`**, PA bias is
`REG_36`; dockable in principle, just never written by radio-server (unlike ADR 0120's genuinely
un-dockable `GPIOA8`). Mechanism (no PA) unchanged; the "un-dockable" label was imprecise.

**What shipped (external, fork [`kbennett2000/uv-k1-k5v3-firmware-custom`](https://github.com/kbennett2000/uv-k1-k5v3-firmware-custom), branch `f5-dock-force-tx` @ `79d522a`):**
- `App/app/dock.c`+`dock.h`: an optional `dock_hal_t.tx_set(on)` callback; `dock_dispatch`
  edge-detects the `REG_30` `ENABLE_TX_DSP` bit and fires it **before** completing the write. Force
  off at `0x0870` enter / `0x0871` exit. **Zero wire-byte change** → radio-server byte-identical, F2
  invariant holds.
- `App/app/uart.c`: `Dock_ForceTx`/`Dock_EndTx` bound to `tx_set`, adding exactly the stock
  `RADIO_SetTxParameters` PA steps (`PrepareTransmit` → `PickRXFilterPath` → `ToggleGpioOut(PA_ENABLE)`
  → `SetupPowerAmplifier(TXP_CalculatedSetting)`), dropping them in stock order on un-key, and
  re-opening the F3a RX audio path so a service announcement doesn't leave RX deaf. TOT untouched.
- `tests/host/test_dock.c`: 19 → **31 checks** (key/un-key edge fires `tx_set` before the write —
  `"1w"`/`"1w0w"` ordering; edge-detect suppresses repeats; non-`REG_30` writes never key;
  `0x0870`/`0x0871` force off). ARM Fusion build clean: **104,340 B / 118 KB (86.35%)**, +196 B over
  F3. Fork ADR `adr/0001-dock-force-tx.md`; `BENCH.md` F5 acceptance.
- Pre-release [`radio-server-f5-v5.7.0`](https://github.com/kbennett2000/uv-k1-k5v3-firmware-custom/releases/tag/radio-server-f5-v5.7.0):
  `f4hwn.fusion.v5.7.0.f5-dock-force-tx.bin` (sha256 `881ca3fd45c76ae2a093f56c04d8bad26c246dfd742e40e57245f3c4018a9934`)
  + `SHA256SUMS` + `PROVENANCE.md`.

**radio-server:** docs only — ADR 0126, this entry, README row. **No code; `uv run pytest`
unchanged (1566/5).**

**Bench acceptance (Kris, the number that closes Chain B):** flash F5 → dummy load → `doctor
--key-test` (CONFIRM) passes → `doctor --tx-tone` with `/tmp/dual_tx_watch.py` running → **kv4p
carrier `True` while keyed** where F4 showed 0-with / 9-without → browser TX + a service heard →
antenna range proof. Verify-on-bench (firmware): which `OUTPUT_POWER` level dock TX radiates + the
`gCurrentVfo` freq source (marked in the `Dock_ForceTx` comment). Carry-forward: the F4 first-key
settle flake (`REG_30=0xBFF1`, retry passes) — F5's `SYSTEM_DelayMs` settle points target it.

**Dependency note:** PR #185 (the Chain B *diagnosis*, doc-only) was still open when this cycle ran;
`origin/master` did not yet have its HANDOFF/server-notes Chain B section. This cycle branched from
`master` and ADR 0126 is self-contained. If #185 is still unmerged at review, merge it first; the
top-of-file HANDOFF insert may otherwise conflict (resolve by keeping both entries).

## F4 Chain B (TX) — DIAGNOSED: firmware PA-enable gap in dock mode, NOT radio-server code (2026-07-24)

**Doc-only follow-up to PR #184 (no code). Branched fresh from `origin/master` after #184 merged
(`134ce26`).** PR #184 shipped Chain A (the RX pump fix) and left Chain B (symptoms 2+4, "browser TX
not working" / "services not announcing") open, framed as the ADR 0112/0113 question "does
register-keyed TX carry usable RF." **Measurement answered it: the software TX path works; the physical
transmitter does not, because dock-mode keying never engages the PA / antenna-switch. The fix is
firmware, not radio-server code.** (An earlier draft of this entry wrongly concluded "the stack
transmits, resolved" off a single dummy-load reading — corrected below. The merged ADR 0125 entry's
"Chain B open/unverified" is now superseded by this diagnosis.)

**Method — the kv4p as an objective RF sniffer** (the HT gave unreliable data all session). The bench
kv4p (UHF SA818, on 8091) has a hardware carrier-detect: `status().busy` is the SQ/COS pin
([`backends/kv4p/radio.py:562`](../radio_server/backends/kv4p/radio.py#L562)). The failing runs were on
147.555 (VHF, the freq pinned for the RX work); the kv4p is **UHF-only**, so `radio.toml` was reverted
to **445.800** (the operating freq), which also put the UV-K5 in the kv4p's band, inches away.

**The evidence chain (reconciles every reading this session):**

| Measurement | Meaning |
|---|---|
| Browser PTT → `tx_key_up` 5.64 s; `_key_on` CONFIRM readback (`reg 0x30`→keyed) passes | Software keys **reliably** — not a radio-server bug; symptom (2) is not session/transport/code |
| Dummy-load run: kv4p saw a modulated carrier (RMS to 7427, voice bins) | The **BK4819 chip TX engages** — low-level RF, detectable only in dummy-load near-field |
| Antenna run, same confirmed **5.7 s** key (dual-poller `dual_tx_watch.py`): UV-K5 `transmitting=True`, kv4p carrier **False** the whole key (9 polls keyed WITHOUT RF, 0 with) | The **external PA / TX-RX antenna switch does NOT engage** — no usable radiated power |
| RX still works with the antenna on (Kris hears himself in headphones) | Radio **is** in dock mode (RX force-open + register interface alive); the dark path is specifically TX PA |

**Diagnosis — the TX mirror of ADR 0120.** There, dock mode didn't drive GPIOA8 (audio amp) —
"un-dockable" — so the fork added `Dock_ForceRxAudioAlive` to force the RX AF path. **TX has no
equivalent:** register-keying enables the BK4819 transmitter, but nothing drives the MCU-side
**PA-enable + TX/RX antenna-switch GPIOs**, so essentially no power reaches the antenna. RX got a
firmware force-open; TX never did. That is why RX works, the software keys, the chip produces
modulated RF (near-field only), yet the HT across the room hears nothing.

**Fix = firmware** (`kbennett2000/uv-k1-k5v3-firmware-custom`): a `Dock_ForceTx` routine driving the
PA-enable and antenna-switch GPIOs on key-up, the TX analog of `Dock_ForceRxAudioAlive`. **Not
radio-server code** — the backend's keying (register write + CONFIRM readback, ADR 0112) is correct
and proven. This closes the oldest open question in the arc (ADR 0112/0113: register-keyed TX does
**not** carry usable RF in dock mode, because the PA is never engaged).

**Confidence / one caveat.** The PA-gap diagnosis reconciles all data and matches the established
ADR 0120 "un-dockable GPIO" pattern. The single remaining ambiguity is that the dummy-load run *did*
detect a modulated carrier — explained here as the chip-level output coupling in near-field, not PA
output. A clean confirmation for the firmware cycle: put the dummy load back and re-run the kv4p
watch — if the carrier reappears with the dummy load but not the antenna, that pins it to
near-field-only chip RF (no PA). An RF power meter / SWR bridge on the antenna line would settle it
outright. Also carry forward the **key-test first-attempt flake** (`reg 0x30=0xbff1`, retry passed) —
a first-key settle race, likely the same GPIO-timing area.

**Method left for the firmware cycle:** the kv4p RF-loopback recipe (both radios on a UHF freq;
`/tmp/kv4p_carrier_watch.py` = carrier detect, `/tmp/kv4p_audio_probe.py` = modulation, `/tmp/dual_tx_watch.py`
= service-keyed-vs-carrier correlation) verifies dock-mode TX in software without an HT — see
`server-notes.md`.

**Server left clean:** `radio.toml` reverted to **445.800** (F4 VHF pin gone), UV-K5 service `active`
on 445.800, kv4p on 445.800, RX knob at the sane post-clipping level. Deployed checkout untouched.

## UV-K5 V3 RX pump — decouple the CAT squelch read (ADR 0125) (2026-07-24)

**Branched fresh from `origin/master` (`uvk5-v3-rx-pump-cat-gate-decouple`) after #183 merged
(`7cbe344`, confirmed the tip) — not stacked. No firmware change; `frames.py`/`transport.py`
untouched (F2/ADR 0119 invariant). This came as a direct kickoff (F4), not a GitHub issue — there is
no instruction issue to update.** F4 server-deployment triage: the uvk5 stack passed on the dev PC
(0120–0124) but degraded on the LAN server into four symptoms — (1) browser RX choppy, (2) browser TX
dead, (3) DTMF not decoded, (4) services not announcing. **Measure first (kv4p lesson).**

- **Two chains.** **Chain A — RX (1)+(3)**, this cycle. **Chain B — TX (2)+(4)** — the log shows the
  radio *keyed* and a service *dispatched* with audio reaching `TxSession.feed`, yet nothing heard:
  that is the never-settled **ADR 0112/0113** acceptance gate (does register-keyed TX carry the
  AIOC-injected K1 mic audio), **still OPEN**, pending the bench `--tx-tone` run. Not shipped here.
- **Chain A root cause, proven on the bench (numbers):** `/audio/rx` for 30 s delivered **0 bytes**;
  a faithful pump-loop repro read **14.3 %** duty with the CAT gate vs **98.5 %** without;
  `status()` p50 **100.2 ms** (pinned to `transport._READ_TIMEOUT`). ADR 0121 made `cat` the uvk5
  default squelch **one day earlier**, so `RxPump` calls `CatBusyGate` (a ~100 ms serial read) **once
  per 20 ms frame** on the single capture reader → reads ~1 frame in 7, overruns the 60 ms ALSA ring,
  and `busy` never True so `hub.publish` never ran (0 bytes); DTMF (raw pre-gate frame) gets 7
  frames/s → shredded. **One cause, both RX symptoms.** Dev PC "passed" because every 0120–0124 check
  ran through `doctor`, which bypasses the pump+gate.
- **Fix shape validated on hardware BEFORE writing it.** First hypothesis (inline caching) was
  **refuted** — tops at 84.4 % duty, still overflows, because any inline 100 ms read stalls the reader
  past the 60 ms ring. Decoupled shape measured **100.2 %**. So: new **`PolledGate`** runs the inner
  gate on a **background thread** (0.2 s interval), caches the verdict, `__call__` = a cached bool
  with **zero serial** in the audio path; `build_rx_gate` cat branch → `PolledGate(CatBusyGate)`
  (`.inner` re-pointed on a live switch). `RxPump` owns the poller lifecycle (start/stop if present,
  guarded, restartable) and **sleeps only on an empty read** (`sleep(0)` yield on a real frame). uvk5
  `receive()` now logs the previously-**discarded** ALSA `_overflowed` flag (rate-limited) — the
  reason the journal showed "zero xruns" through this whole failure.
- **Second, independent Chain-A fault (ops, on the box — no code):** RX audio was **clipping** (idle
  peak 32752/32767 after the knob went too far up), which shredded DTMF at the decoder via harmonics.
  Knob backed down to peak **28032 (−1.4 dBFS)** → `doctor --dtmf` then decoded **`1234#1234#`
  clean**. Recorded in [`server-notes.md`](server-notes.md), not the ADR.
- Tests: new `tests/test_polled_gate.py` + pump lifecycle/pacing + uvk5 xrun-log; three existing
  `isinstance(CatBusyGate)` assertions updated to unwrap `.inner`. `uv run pytest` **1566 passed, 5
  skipped**.

**NEXT CYCLE — Chain B (TX), the priority.** Run the bench `--key-test` + `--tx-tone --seconds 5
--freq 1000` (TTY-gated `CONFIRM`, dummy load) with Kris listening on the HT. **Tone heard** → AIOC
injection works, fault is downstream (playout/level/lead-in) → then browser Talk test. **Tone not
heard** → the injected K1 mic audio does not transmit in XVFO mode = root cause of both (2) and (4),
a hardware/firmware finding that closes the oldest open question in the arc. Also verify the ADR 0125
fix live on the server once merged: `/audio/rx` should now deliver ~96,000 B/s and the live server
should log `auth_*` when `1234#` is keyed (DTMF through the pump, not just doctor).

**Server state left clean:** service `active (running)`; `radio.toml` frequency pinned to
`147555000` (backup `radio.toml.bak-f4`) — both radios were on 147.550→.555; recorded in
`server-notes.md`. Deployed checkout untouched at `7cbe344` (this fix is a PR, not a hot-patch).

**Shipped this cycle (PR #184).**

## AIOC sound-card addressing — resolve ALSA card ids (ADR 0124) (2026-07-24)

**Branched fresh from `origin/master` (`aioc-alsa-card-id-resolution`) after #182 merged (`d1205c4`)
— not stacked. No firmware change; `frames.py`/`transport.py` untouched (F2/ADR 0119 invariant).**
Started as a server-ops task ("the AIOC udev rule didn't take"); the diagnosis inverted it.

- **The udev rule had already applied.** On the LAN server: `/sys/class/sound/card2/id -> AIOC_K6`,
  and `udevadm test /sys/class/sound/card2` shows the rule firing. It had merely been authored
  *after* the reboot it was tested against (boot ≈20:39, rules mtime 20:41), so the first look was
  stale.
- **The real defect — a three-layer name mismatch.** udev's `ATTR{id}` sets the ALSA card **id**
  (`AIOC_K6`); the card **name** comes from the USB product string (`All-In-One-Cable`) and udev
  cannot set it; PortAudio device names — the only thing `sounddevice` substring-matches — derive
  from that *name*. So `input_device = "AIOC_K6"` could never resolve, and **no udev rule could ever
  make it.** The `No input device matching 'AIOC_K6'` text is sounddevice's own, which is why it
  appears nowhere in this repo.
- **The fix (Kris's call: code conforms to `radio.toml`, not the reverse).** `resolve_device()` in
  the shared `backends/soundcard.py` seam both backends already funnel through, plus
  `doctor._check_audio` (which now reports the mapping). **Existing behaviour first** — int/None,
  then PortAudio name substring, and only then card id → `/sys/class/sound/card*/id` → index →
  the `(hw:N,…)` device with channels in the direction opened. Unresolvable strings pass through so
  sounddevice raises its own error. `radio.toml` **unchanged**.
- **Live acceptance (bench, service stopped then restarted):** `doctor --rx-noise` with the shipped
  `AIOC_K6` config → **peak 5299 RMS (-15.8 dBFS), avg 4123**, RX ALIVE. `doctor --backend uvk5` →
  serial + connect probe ALL PASS. Service confirmed `active (running)` afterwards.
- **Recorded so it isn't re-learned:** a bare `arecord -D hw:CARD=AIOC_K6` reads **floor (67 RMS)**
  and that is *not* a fault — only the backend runs the dock's `REG_47`→FM un-mute (ADR 0120/0122).
  Never use `arecord` as an RX test on this radio.
- **Ops (server, no PR):** `85-aioc-names.rules` tidied — `KERNEL=="card*"` scoping added (`ATTR{id}`
  exists only on the card node), second-AIOC line commented rather than live with a placeholder
  serial. Full rule text + Saturday's fill-in-the-blank step in new
  [`docs/server-notes.md`](server-notes.md).
- Tests: new `tests/test_soundcard_device.py` (15) + a `test_doctor.py` `_check_audio` case.
  `uv run pytest` **1548 passed, 5 skipped**.

**NEXT CYCLE:** AIOC #2 arrives 2026-07-25 — paste its serial into the commented rules line,
uncomment, reload+trigger, and set that radio's block to `AIOC_UV5R`. Two cosmetic server warts are
logged in `server-notes.md` (unit lacks `SuccessExitStatus=143` so a clean stop reads `failed`; the
deployed checkout carries an uncommitted `dtmf.py` edit).

**Shipped this cycle (PR #183).**

## UV-K5 V3 — `--rx-firststart-loop` harness fix + the real 20-count (ADR 0123) (2026-07-24)

**Branched fresh from `origin/master` (`uvk5-v3-rx-firststart-harness`) after #181 (ADR 0122
validation) merged (`74fdab8`) — not stacked. Host-side (`doctor`) only; no firmware change; transport
& backend untouched (F2/ADR 0119 invariant).** ADR 0122 proved the first-start dead-RX *fix* works
(10/10 completed opens ALIVE) but the *instrument* crashed at the ~6th open. This cycle fixes the
instrument and runs the real acceptance.

- **The defect:** in `_rx_firststart_loop`, `_build_backend` ran **outside** the per-iteration `try`.
  `Uvk5Radio.__init__` seeds its model with non-retransmitting register reads; on a rapid reopen one
  lands in the reset-on-open window and raises `Uvk5Timeout`, killing the whole run (~6th open).
- **The fix (three parts, doctor.py):** (1) construction moved **inside** the `try` — a surviving
  construct-timeout is counted `DEAD/RADIO` (`REG_47=------  construct timeout after N attempts`), never
  a crash; (2) new `_build_backend_settled(...)` catches **only** `Uvk5Timeout` and retries the whole
  build — the harness analogue of `connect()`'s probe retransmit (ADR 0111; 3 attempts / 0.25 s, both
  the loop and the step-0 F3 probe use it); (3) a **1.0 s inter-iteration settle** (before every
  iteration but the first) so each open is a genuine single-reset first-start, not a rapid-reopen reset
  pile-up. The settle is a **realism** decision that defines "N-clean" (marked `VERIFY ON BENCH`).
- **Live acceptance — `0 dead / 20`:** `--rx-firststart-loop 20` completed **all 20 iterations, 0
  dead** — F3 CONFIRMED, every iter `ALIVE`, `REG_47=0x6142`, peaks ~7.3–8.2 k. The loop that
  previously crashed at open 6 now runs clean to 20. **This is the item-1 acceptance ADR 0122
  deferred.** (Benign: one teardown-time `reader thread stopped on SerialException` stderr line before
  the loop; idle `RSSI=0` jitter at iter 8 — both documented, neither a failure.)
- **Tests:** +5 in `tests/test_doctor.py` (droppable-construction builder: retry-then-ALIVE,
  exhausted→DEAD/RADIO with the loop still finishing, settle fires `iterations-1` times not before the
  first, two `_build_backend_settled` unit cases). `uv run pytest` **1533 passed, 4 skipped** (was 1528/4).

**NEXT CYCLE (own kickoff):** still open from before — default-enable `capture_reopen_on_floor` only if
a live repro ever shows the host-audio leg (it never has: every completed open, now 20/20, is ALIVE).
Optionally tighten `_RX_FIRSTSTART_SETTLE_S` if a faster genuine-restart cadence is confirmed on the bench.

**Shipped this cycle (PR #182):** ADR 0123 + the fold into ADR 0122's addendum + README rows +
this entry + the doctor.py fix and tests. `radio.toml` untouched (bench-local, gitignored).

## UV-K5 V3 — ADR 0122 live validation re-run (radio now ON) (2026-07-24)

**Branched fresh from `origin/master` (`uvk5-v3-f3-validation`) after #180 (ADR 0122) merged
(`edd3344`) — not stacked. Docs-only** (ADR addendum + README + this entry); the one code-ish change is
a **local, gitignored `radio.toml`** threshold edit, reported below, not committed. The HT was off by
accident last cycle; now on. Ran the deferred live checks. **No TX from the server — the only RF was
Kris keying his handheld on 445.800 for item 4.**

- **Gate (STEP 0):** connect probe PASSED before and after — dock alive, `ReadRegisters(0x30)` answered.
- **Item 3 — stopwatch (PASS live):** `--rx-level --seconds 30` → **47,999 Hz** over 30.0 s (nominal
  48,000; within 0.2%; pre-fix was −0.91% ≈47,560). `--rx-noise` LOUD (loudest 5021 RMS) — RX chain +
  capture leg healthy.
- **Item 4 — RSSI (PASS live) → threshold set from data:** `--rssi` idle min 150 / mean 159 / max 163;
  keyed 445.800 ramp 254 → steady **~311**. **`uvk5.squelch_threshold` 40 (default) → 220** written to
  the local `radio.toml` (+57 over idle-max, −91 under keyed-steady); verified — unkeyed now reads idle
  every sample. Note: idle floor not perfectly reproducible (0 right after open, climbs to ~159); both
  far below 220. Only on-signal point is a strong local key — can lower toward the floor later for weak
  signals.
- **Item 1 — fix VALIDATED, harness defect BLOCKS the 20-clean acceptance:** two runs (warm + cold
  boot), step-0 **F3 CONFIRMED** both, **10/10 completed opens `ALIVE`** (REG_47=FM, peak RMS ≫ floor),
  **zero dead-RX**. But **both crashed at the 6th open** — `_build_backend` → `_read_register(0x38/0x39)`
  `Uvk5Timeout`: the reset-on-open race at construction, and `_build_backend()` sits **outside** the
  loop try ([doctor.py:2671]) so it crashes instead of recording a retryable iter. Radio never wedged
  (probe passed right after). The loop's rapid reopen over-stresses reset-on-open beyond a real
  single first-start. **The `_enter_hw_mode_verified` fix is confirmed good; the repro *harness* needs a
  follow-up cycle** (wrap `_build_backend` in the try + bounded reset-on-open retry + inter-iteration
  settle). No firmware implicated.

**NEXT CYCLE (own kickoff):** harness-fix for `--rx-firststart-loop` — make it tolerate/retry a
construction-time reset-on-open `Uvk5Timeout` and add an inter-iteration settle, then capture a real
N-clean count. Still open from before: default-enable `capture_reopen_on_floor` only if a live repro
ever shows the host-audio leg (it did not here — every completed open was ALIVE).

**Shipped this cycle (PR #181):** docs-only — ADR 0122 validation addendum + README note + this
entry. Local `radio.toml` `squelch_threshold=220` (uncommitted, gitignored).

## UV-K5 V3 F3 bench loose ends — reproduce, fix, instrument (ADR 0122) (2026-07-24)

**Branched fresh from `origin/master` (`uvk5-v3-f3-bench-loose-ends`) after #179 (ADR 0121) merged
(`8402d3e`) — not stacked.** Five host-side loose ends from the F3 bench, all RX/register-side, **no
TX, no firmware change**. `frames.py`/`transport.py` untouched (F2 invariant).

1. **First-start dead RX (diagnose-before-fix).** The RX force-open is firmware-side
   (`Dock_ForceRxAudioAlive`, ADR 0120), triggered by radio-server's single **fire-and-forget,
   unverified** `send(EnterHwMode())` (0x0870). Two legs: a 0x0870 lost in the reset-on-open boot race
   (radio leg) vs a capture stream opened against a settling USB device (host-audio leg). GPIOA8 is
   un-dockable, but the same firmware routine sets `REG_47`=FM → a host-visible **proxy**, so `REG_47`
   vs AIOC-RMS splits the legs. Shipped: `doctor --rx-firststart-loop N` (open→dump→measure→close,
   per-iter `ALIVE`/`DEAD/RADIO`/`DEAD/HOST-AUDIO` + `dead/N`, a **step-0 F3 probe** that stamps the
   run unreliable on a non-F3 dock, and a cold-boot fidelity caveat). Host-side fix in `radio.py`:
   `_enter_hw_mode_verified` (send 0x0870 → settle → read `REG_47`; re-send if not FM, bounded 3× —
   **shipped unconditionally**, a strict upgrade to a known-fragile fire-and-forget) + a **default-OFF**
   `capture_reopen_on_floor` (prime one block, reopen once if floor).
2. **Shutdown tidy.** Every `?token=` WS handler now catches `asyncio.CancelledError` alongside
   `WebSocketDisconnect` (the lifespan's own suppress idiom) → no teardown traceback.
3. **Doctor stopwatch.** `measure_rx_levels` primes one read before starting the clock, so stream-open
   latency no longer biases the true-rate low. The kv4p **+2%** finding (pure formatter, hardcoded
   inputs) is untouched — the fix sharpens it.
4. **RSSI readout.** New `doctor --rssi` live meter — raw reg-0x67 counts + busy verdict vs
   `uvk5.squelch_threshold`, with a min/mean/max/busy summary.
5. **HELLO quirk.** Reworded, not fixed: the V3 fork is always-encrypted and dropped the plaintext-
   0x0514 toggle (ADR 0119), so an unanswered plaintext HELLO is **expected** (dock-alive already proven
   by the register elicit). No firmware side is wrong → no pre-release bin.

**Live validation DEFERRED (honest).** The dev-PC UV-K5 was driven this cycle but the HT did **not**
answer the dock probe: the AIOC serial opens and its **sound-card capture leg is healthy** (`arecord`
succeeds), but the HT returned **zero bytes** and the register elicit timed out across **4 attempts** —
the same every time (not a transient boot-race; a persistently unresponsive HT: powered off or not on
dock firmware). Headless can't power/wake/flash it. So **no live first-start before/after counts (item
1) and no live RSSI (item 4)** — those are a bench acceptance for Kris (F1/F2/F3a pattern). Presence
(enumerated AIOC) ≠ responsiveness — verified empirically, not assumed.

**Shipped (radio-server, PR #180):** ADR 0122 + README row + this entry; `doctor` gains
`--rx-firststart-loop`/`--rssi`; `radio.py` gains `_enter_hw_mode_verified`/`_open_capture`
reopen/`_block_rms`; `app.py` WS handlers swallow shutdown cancel. `FirmwareFakeSerial` extended (F3
force-open + droppable-0x0870). All five proven hardware-free — `uv run pytest` **1528 passed, 4
skipped** (was 1510/4). No new deps.

**Bench acceptance (Kris):** with F3 flashed + HT on — `doctor --backend uvk5 --rx-firststart-loop 20`
right after a cold boot/replug (record F3 verdict + dead/N, re-run after → 0 dead where it failed);
`doctor --backend uvk5 --rssi` unkeyed (counts stream, busy tracks the threshold).

**Next (own kickoff required):** default-enable `capture_reopen_on_floor` (or wire it to a config key)
only if the live repro shows the host-audio leg; anything the live bench surfaces.

## Per-backend squelch mode — unbreak the mixed-radio box (ADR 0121) (2026-07-24)

**Branched fresh from `origin/master` (`per-backend-squelch`) after #178 (ADR 0120, F3a) merged
(`43770c4`) — not stacked.** A software/mock cycle (no bench claims).

**Problem (found live on the bench).** `audio.squelch` was a single global mode, and ADR 0074
validates *every configured backend* against it. So `audio.squelch=cat` (which the docked UV-K6
needs post-F3) plus **any** configured audio-only block (`[baofeng]`) was **unstartable**:
`validate_configured_backends` validated the inactive `[baofeng]` block against the global `cat`,
hit the "UV-5R has no busy line" guard, and boot aborted. Kris's box has a stale `[baofeng]` block
and runs uvk5 → the whole node wouldn't start.

**Fix — per-backend `squelch_mode`, the `uvk5.tot` pattern.** uvk5 and baofeng get their own
`squelch_mode` key with **backend-declared defaults** (`uvk5`=`cat`, `baofeng`=`audio`); any backend
without a key (kv4p/mock) falls back to the global `audio.squelch`. `resolve_squelch_mode(settings,
backend)` (in `activity/gate.py`, beside `load_squelch_mode`) is the one source of truth. Naming is
`squelch_mode` (not `squelch` — `[kv4p]` already owns `squelch`, an SA818 level 0-8; the flat
registry rules out an `[audio.<backend>]` table). Three wirings:
1. **Validation per effective mode** (`api/backend_config.py`): each backend is checked against
   `resolve_squelch_mode(...)`, so the stale-`[baofeng]`-blocks-`cat` failure is gone by construction
   (an *explicit* `baofeng.squelch_mode=cat` still fails). Misleading messages reworded to name the
   SECTION/key, never the active `server.backend`.
2. **Gate re-selected on live switch** (`activity/gate.py` + `api/holder.py`): `build_rx_gate`
   resolves the active backend and is rebuilt inside `RadioHolder.rebuild`/`_restore`, so a swap
   re-selects the gate type AND re-points `CatBusyGate` at the new radio — this also closes a latent
   stale-radio bug (the gate used to keep polling the closed previous radio).
3. **Recording-`off` safety rail** (`api/app.py`) reads the effective mode, not the raw global.

**Back-compat.** Realistic single-global configs resolve unchanged (uvk5+`cat`, baofeng+`audio`,
kv4p+anything). Documented divergence: a uvk5/baofeng box that relied on the global for a *non-default*
mode now uses its backend default; set the per-backend key to keep the old mode. For uvk5 the changed
default (`off`→`cat`) fixes the setup that was broken in dock mode.

**Shipped (radio-server, PR #179):** ADR 0121 + README row + this entry; 2 new specs
(`uvk5.squelch_mode`, `baofeng.squelch_mode`, Advanced), `DEFAULT_SQUELCH_MODE` constants in both
backends, `resolve_squelch_mode`, the three wirings above. Canary **90→92**; `radio.toml.example`
regenerated; no new deps. Tests: new `tests/test_squelch_per_backend.py` + a holder
gate-rebuild-on-switch test; inverted `test_multi_backend`'s old fail-at-load test to the new
boots-fine behavior (+ explicit-cat-still-fails); message/canary touch-ups in
`test_backend_wiring`/`test_recording`/`test_settings_api`. `uv run pytest` **1510/4** (was 1492/4).

**Next (own kickoff required):** the F3 bench loose ends named out-of-scope here — boot-race
tolerance for the audio force-open, shutdown `CancelledError` tidy, doctor stopwatch fix, RSSI
readout. No firmware change this cycle.

## UV-K5 V3, cycle F3a: RX audio was dead in dock mode — the audio-path amp (ADR 0120) (2026-07-23)

**Branched fresh from `origin/master` (`uvk5-v3-rx-audio-f3a`) after #177 (ADR 0119, F2) merged
(`63c4978`) — not stacked.** A live-hardware diagnostic cycle: with the F2 build flashed, TX worked
end-to-end and the connect probe passed, but received audio to the AIOC read the **noise floor**
(~110 RMS) where an early HT-keyed run read 12431.

**Root cause (proven with register dumps from a fresh power-cycle, actual runs never inferred).** RX
audio to the AIOC's speaker-line tap passes **two gates**: BK4819 `REG_47` (AF selector: MUTE
`0x6042` / FM `0x6142`, reachable over the dock) **and `GPIOA8`, the external audio-amp enable — an
MCU GPIO the dock protocol CANNOT reach**. radio-server holds the `0x0870` full-control blocking loop
for its whole lifetime, which starves the firmware's 10 ms timeslice, so `APP_StartListening()` (the
only path that raises GPIOA8 / unmutes `REG_47` on squelch-open) never runs — both stay frozen at the
idle state (GPIOA8 low, `REG_47` mute). Over the dock I forced `REG_30=0xBFF1` + `REG_47=0x6142` +
`REG_48=0xB3A8`, **confirmed all three by read-back**, and the AIOC still read 109 RMS (237 frames —
capture leg live, just silent). BK4819 fully RX-alive yet silent ⇒ the dead gate is outside the
BK4819 = GPIOA8. The healthy 12431 reading had caught the firmware mid-listen when full-control froze
it live. The `0x0871` resume was settled by the baseline→post-exit diff: `RADIO_SetupRegisters(true)`
returns the radio to normal *muted idle* RX (supersedes F2's `⚠` resume-RX flag). Phase 2 (TX-restore)
was subsumed — RX is dead from a fresh idle boot *before any TX*, so the unkey path is not the cause;
the post-fix "does a TX cycle re-kill RX" check is folded into BENCH.md.

**What shipped (radio-server, PR #<pending>):** ADR 0120 + `docs/adr/README.md` row + this entry, and
**the instrument only** — `radio_server/doctor.py` gains `--rx-noise` / `_rx_noise`: an **HT-free RX
self-test** that enters full-control, force-opens the receiver from registers, measures the AIOC, and
**restores every register it touched** (guardrail: no leaked force-open state). Verdict: ≥1000 RMS ⇒
RX chain + capture leg alive; floor ⇒ dead even force-open (suspect GPIOA8 / analog leg). `tests/
test_doctor.py` +4 tests (force/measure/restore + both verdicts + non-uvk5 skip + restore-on-failure).
Validated live: on the F2 build it reads 110 RMS → "DEAD" and restores cleanly. **The dock wire
protocol and `radio_server/backends/uvk5/{frames,transport,radio}.py` change ZERO — the F2 invariant
holds.** `uv run pytest`: **1492 passed, 4 skipped** (was 1487/5 — the 4 new tests plus one
environment-dependent skip that now runs).

**What shipped (external, fork [`kbennett2000/uv-k1-k5v3-firmware-custom`](https://github.com/kbennett2000/uv-k1-k5v3-firmware-custom), branch `f3-rx-audio-fix` @ `79f9b21`):**
- **The fix, all in the fork:** `App/app/uart.c` — `Dock_EnterFullControl` now calls
  `Dock_ForceRxAudioAlive()` (`GPIO_EnableAudioPath()` + `gEnableSpeaker=true` +
  `BK4819_SetAF(BK4819_AF_FM)` + `BK4819_SetRxAudioGain()`) before the blocking loop, so RX audio
  flows the whole full-control session. `0x0871` exit re-baselines via `RADIO_SetupRegisters(true)`
  (audio-path off, `REG_47`→MUTE) — no new restore path. All symbols already reachable in `uart.c`;
  the pure `App/app/dock.c` protocol core is untouched, so the host harness stays **19/19**.
- **Flash:** `+28 bytes` over F2 — **104,144 B / 118 KB (86.2%)**, ~16.3 KiB headroom. Headless docker
  build (F1/F2 invocation, ARM GNU 13.3.Rel1) at `~/Desktop/projects/uvk5v3-f1-build`; bin embeds
  commit `79f9b21`.
- **Pre-release `radio-server-f3-v5.7.0`:** `f4hwn.fusion.v5.7.0.f3-rx-audio.bin` (sha256
  `f9a3cc2dc9b79ac58b1770e0e52aff10227a775e866d9af320ee6dcd6bead855`) + `SHA256SUMS` + provenance.
- **`BENCH.md`** gained the F3 acceptance: flash → `doctor --backend uvk5 --rx-noise` reads thousands
  (the fix, was floor) → live RX with an HT → the post-fix TX check → the settled resume-RX note.

**Acceptance (Kris, bench — I never claim bench results):** flash the F3 bin over the F2 build, run
`doctor --backend uvk5 --rx-noise`; the jump from ~110 RMS (floor) to thousands (LOUD) **is** the fix.
Then confirm live RX (HT on 445.800 → browser Listen produces audio) and the post-fix TX check.

---

## UV-K5 V3 firmware, cycle F2: the dock protocol port (ADR 0119) (2026-07-23)

**Branched fresh from `origin/master` (`uvk5-v3-dock-protocol-port-f2`) off `0c62b58` after #176
(ADR 0118, F1) merged — not stacked.** F1's bench acceptance was confirmed (Kris: F1 bin boots/RX/
keypad identical) before starting, per the kickoff gate. F2 ports the dock control mode into the
fork so radio-server can drive the UV-K5 **V3** over the wire protocol it already speaks to the
classic Quansheng Dock. **The arc's invariant held: no `radio_server/` code and no uvk5/doctor test
touched — byte-compatibility with the untouched `FirmwareFakeSerial`/`Uvk5Decoder` is the definition
of done.**

**What shipped (radio-server, PR #<pending>):** ADR 0119 + `docs/adr/README.md` row + this entry.
**`uv run pytest`: 1487 passed, 5 skipped** (unchanged — docs-only).

**What shipped (external, fork [`kbennett2000/uv-k1-k5v3-firmware-custom`](https://github.com/kbennett2000/uv-k1-k5v3-firmware-custom), branch `f2-dock-port` @ `32c600b`):**
- **Tiny port surface** (registers do everything): `0x0850` write / `0x0851` read → `0x0951
  RegisterInfo`, `0x0870`/`0x0871` enter/exit full-control. No keypress/screen/scan/GPIO/AM/FSK/
  modulation — all of nicsure's dock dropped.
- **`App/app/dock.c` + `dock.h` — a PURE, host-compilable protocol core:** framing (`AB CD`/`DC BA`,
  LE size, 16-byte XOR, CRC-16/XMODEM), command-CRC validation, dummy `obf(FF FF)` replies, register
  dispatch, streaming deframer; all hardware behind a `dock_hal_t` (read_reg/write_reg/send). Derived
  from nicsure's Apache-2.0 `app/uart.c` (@ `4375c3e`) **with attribution**.
- **`App/app/uart.c` wiring** (`#ifdef ENABLE_DOCK`): HAL bound to `BK4819_Read/WriteRegister` +
  `UART_Send`; dispatch cases `0x0850/0x0851/0x0871 → dock_dispatch`; `0x0870` → a blocking
  full-control loop that re-enters the existing UART dispatch for in-loop register R/W (nicsure's
  `default: UART_HandleCommand()` shape). **Extends** the tree's `switch` (uart.c:807), byte-identical
  framing already present — not a parallel path. `enable_feature(ENABLE_DOCK app/dock.c)`, on in Fusion.
- **`tests/host/` — the spec is the fake:** a gcc harness (`test_dock.c` + `Makefile`, no hardware)
  mirrors `FirmwareFakeSerial`'s rules — malformed/oversize/zero-size dropped, drop-and-resync,
  streaming, command-CRC enforced, `0x0851`→`0x0951` per register, `0x0850` no-reply,
  `0x0870`/`0x0871` full-control — plus a **byte-exact `0x0951` reply vector** as an independent
  oracle. **19/19 checks pass.** This is the pre-flash proof of byte-compat.
- **V3 derivation (from the tree, not assumed):** no hardware watchdog to feed; no async ISR
  reprograms the BK4819 while the loop blocks (SysTick only sets flags; BK4819 IRQs polled in the
  same 10 ms slice, starved by the block) → blocking IS the quiesce, `RADIO_SetupRegisters(true)` on
  exit (verify-on-bench); UART already 38400; V3 hard-defines `bIsEncrypted=true` (toggle never on
  radio-server's dock path). **Flash fits: 104,116 B / 118 KB (86.2%), +572 B over F1.**
- **License/NOTICE:** updated to name `App/app/dock.c` as the ported file; GPL client stays spec-only.
- **BENCH.md:** gained the F2 acceptance sequence.
- **Delivery:** pre-release [`radio-server-f2-v5.7.0`](https://github.com/kbennett2000/uv-k1-k5v3-firmware-custom/releases/tag/radio-server-f2-v5.7.0)
  — `f4hwn.fusion.v5.7.0.f2-dock.bin` (sha256 `68208de1…`) + `SHA256SUMS` + provenance.

**Acceptance (Kris, bench, OUT OF BAND — I never claim bench results):** flash the F2 bin (DFU) →
`radio-server doctor --backend uvk5` connect probe (the `ReadRegisters(0x30)` elicit answering a
`RegisterInfo` **is** the port working) → the four F1 gates with the dock idle → green-lights **F3**.

**Out of scope (F2):** any radio-server change (a V3 wire difference is STOP-and-report — none found);
calib/EEPROM; upstream PR submission; bench claims.

**Follow-on: F3** — the full radio-server↔V3 end-to-end bench loop (drive tuning/keying over the dock,
fold real flash + resume-RX specifics back into `BENCH.md`). (Prior arc follow-ons still open:
out-of-process TX supervisor; split/offset.)

**Note:** this cycle's kickoff arrived as a direct prompt, not a labeled GitHub issue — no open
`instructions`-labeled issue to move to `cycle-summary`. The PR is the cycle record.

---

## UV-K5 V3 firmware fork, cycle F1: pin the base + prove the build (ADR 0118) (2026-07-23)

**Branched fresh from `origin/master` (`uvk5-v3-firmware-fork-f1`) off `58e1a1e` after #175 (ADR 0117)
merged — not stacked.** New arc, new ground: the bench radio is a **UV-K5 V3 (PY32F071)**, not the
DP32G030 that nicsure's Quansheng Dock firmware targets — that firmware can't run on it. So instead of
pinning someone else's dock firmware, we **add a dock control mode to the V3 community firmware ourselves**,
wire-identical to the classic Dock so radio-server's `backends/uvk5/*` and its 1487 tests change zero.
**F1 ports nothing — it's the build gate.** No radio-server code touched.

**What shipped (radio-server, PR #<pending>):** ADR 0118 + `docs/adr/README.md` index row + this entry.
**`uv run pytest`: 1487 passed, 5 skipped** (unchanged from ADR 0117 — F1 adds no code, no tests).

**What shipped (external, fork [`kbennett2000/uv-k1-k5v3-firmware-custom`](https://github.com/kbennett2000/uv-k1-k5v3-firmware-custom)):**
- **Fork + pin:** public, Apache-2.0, forked from `armel/uv-k1-k5v3-firmware-custom` (F4HWN "Fusion").
  Pinned tag **`v5.7.0`** / commit **`3bd3ebba2ceb553edc88c3f087ce0c7f420433b2`**. Branch
  `f1-dock-fork-scaffold`. Firmware source **unmodified** — only `BENCH.md` (new) + `NOTICE` (appended).
- **Reproducible build (headless):** the repo's Docker image (ARM GNU 13.3.Rel1). `compile-with-docker.sh`
  uses `-it` (fails without a TTY); the headless form is `docker build -t uvk1-uvk5v3 . && docker run --rm
  -u $(id -u):$(id -g) -v "$PWD":/src -w /src uvk1-uvk5v3 bash -c "cmake --preset Fusion && cmake --build
  --preset Fusion -j"` → `build/Fusion/f4hwn.fusion.bin` (103544 B, sha256 `651d057f…`).
- **Byte-compare recorded honestly — NOT bit-identical, and can't be:** (1) the official
  `f4hwn.fusion.v5.7.0.bin` (sha256 `66cd1777…`) is a **committed artifact** in `archive/`, built from
  commit **`0567f01`** — 2 commits *before* the tag (only `CMakePresets.json` version/flag lines differ →
  compile config matches the tag; a genuine tag-vs-asset **pin discrepancy**, recorded ADR-0110-style).
  (2) With identical source+config the build still diverges **8867/103544 B (8.56%), scattered** — the
  signature of `-flto=auto` + a different release build environment, not a config diff. Verified instead:
  clean build, valid `.bin/.elf/.hex`, size within 4 B, identical version/author/edition strings.
- **BENCH.md:** uvtools2 V3 flash runbook (USB DFU, FTDI cable, tab-conflict gotcha, calib-dump-after-first-
  boot). The four radio-specific specifics are flagged **`⚠ CONFIRM AT BENCH`** (guardrail 1 — not asserted
  from memory; Kris confirms on first flash, then the banner comes off).
- **License/NOTICE:** both armel's tree and nicsure's `quansheng-dock-fw` are Apache-2.0, so F2 may **port**
  `uart.c` with attribution (unlike the GPL `QuanshengDock` client — spec-only). `NOTICE` records this.
- **Delivery:** F1 bin + `SHA256SUMS` + full build provenance in fork pre-release
  [`radio-server-f1-v5.7.0`](https://github.com/kbennett2000/uv-k1-k5v3-firmware-custom/releases/tag/radio-server-f1-v5.7.0).

**Acceptance (Kris, bench, ~5 min, OUT OF BAND):** flash `f4hwn.fusion.v5.7.0.f1.bin` (from the pre-release)
over the release Fusion — radio boots / receives / keypad works **identical** → green-lights F2.

**Out of scope (F1):** all protocol code, any V3 UART/dock derivation, any `uart.c` reading beyond the
build, any radio-server change. No claim yet about V3 UART/dock behavior — that's F2.

**Follow-ons (the arc):** **F2** — derive the V3 dock protocol and port the four ops
(0x0870/0x0871/0x0850/0x0851→0x0951) into the fork from nicsure's `uart.c`, wire-identical to our pin so
`backends/uvk5/*` stays untouched. **F3** — bench loop: flash the dock-mode build, prove radio-server drives
the V3 end-to-end, fold real flash specifics back into `BENCH.md`. (Prior arc's follow-ons still open:
out-of-process TX supervisor; split/offset.)

**Note:** this cycle's kickoff arrived as a direct prompt, not a labeled GitHub issue — there was no open
`instructions`-labeled issue to move to `cycle-summary`. The PR is the cycle record.

---

## TX watchdog/TOT, cycle 8: the UV-K5 stuck-key gate (ADR 0117) (2026-07-21)

**Branched fresh from `origin/master` (`uvk5-tx-timeout-wiring`) off `aeba0bd` after #174 merged — not
stacked.** Reconnaissance-first cycle: the server-side transmitter time-out **already exists** (`TotRadio`,
ADR 0090, wrapping every backend at `build_radio`), so this was **extension/wiring, not invention** — the
docked UV-K6 (no device-side backstop, unlike kv4p's firmware ~200 s cutoff or a UV-5R's TOT menu) was
already covered; the gaps were observability, a mandatory cap, and honest residual docs. **No new deps, no
web change.**

**What shipped (code, PR #<pending>):**
- **Placement follows ADR 0090's decorator, NOT the arbiter** (`RadioArbiter` is a timer-free latch that
  misses `/ptt` and one-shot `transmit()`) — stated + cited in the ADR; nothing moved.
- **The full-restore unkey is already correct — proven, not fixed:** `TotRadio._fire()` calls `ptt(False)`,
  which on the UV-K5 routes to `_key_off` (RX registers restored FIRST, then audio teardown). New
  `tests/test_uvk5_tot.py` proves it on the firmware-accurate `FirmwareFakeSerial` (`registers[0x30] ==
  radio._reg30` after `fire_latest()`; final wire pair is the `_key_off` restore), + a normal-key-under-cap
  no-fire test.
- **Non-silent `"alarm"` event** (`radio_server/api/events.py`, payload `{kind:"tx_timeout", tot}`): the
  previously-unwired `TotRadio.on_timeout` hook now surfaces on the reactive `/events` path → operating log
  / Log card (no new UI). **Thread-safe:** it fires on `TotRadio`'s `threading.Timer` thread, so the app
  publisher hops the boundary with `loop.call_soon_threadsafe` (loop captured at lifespan startup). Wired at
  construction for a swapped radio (`build_radio(settings, *, on_tot_timeout=…)`) and post-construction for
  the initial one (new `TotRadio.set_on_timeout`, the controller-`on_event` hub-doesn't-exist-yet pattern);
  `create_app` guards on `radio_factory is build_radio` / `isinstance(radio, TotRadio)` so DI/test radios are
  untouched.
- **Mandatory per-backend `uvk5.tot`** (backend-declared `Uvk5Radio.DEFAULT_TOT = 180.0`; new
  `coerce_uvk5_tot` rejects ≤0 AND anything above the default — may shorten, never disable/weaken).
  `build_radio` resolves per-backend via new `resolve_tot(settings)`: uvk5 → `uvk5.tot`, everything else →
  global `tx.tot`. So `tx.tot=0` (still valid — disables the cap for backends with their own firmware/radio
  TOT) can never disable the UV-K6's. Settings canary **89→90**; `radio.toml.example` regenerated;
  `uvk5.tot` added to `_ADVANCED_KEYS`.
- **`TotRadio`** gained a read-only `tot` property + `set_on_timeout()`; hook/property unit tests in
  `tests/test_tx_tot.py`; app-level per-backend resolution in `tests/test_backend_wiring.py`
  (`app.state.radio.tot`).
- **Residual named honestly (ADR + `docs/uvk5-setup.md`):** in-process covers logic bugs / runaway sessions
  / leaks / `SIGTERM` (via `atexit`→`_key_off`); **NOT** `SIGKILL` / kernel panic / power loss — radio stays
  keyed until power-cycle. An **out-of-process supervisor** is the named follow-on. `uvk5-setup.md`'s old
  "no time-out" warning was rewritten to the accurate mandatory-TOT + residual-gap story.
- **Docs:** ADR 0117; `docs/adr/README.md` index row; `docs/configuration.md` (`uvk5.tot`);
  `docs/uvk5-setup.md` residual paragraph. **`uv run pytest`: 1487 passed, 5 skipped** (was 1471; +16).

**Out of scope (named in ADR 0117):** the out-of-process supervisor (the named follow-on for host-death
coverage), split/offset, EEPROM channel import, bench numbers, any arbiter-level TOT.

**Follow-ons:** **out-of-process supervisor / hardware watchdog** to cover host `SIGKILL`/panic/power-loss
(the one residual the in-process TOT can't reach); **split/offset** (a `CatRadio`-interface arc, from the
presets work). Bench: verify/tune `uvk5.tot` against real UV-K6 hardware (guardrail 1 — the 180 s value is
the classic default, never bench-validated on this radio).

---

## Channel presets, cycle 7: the web UI (ADR 0116) (2026-07-21)

**Branched fresh from `origin/master` (`channel-presets-web-ui`) off `5bf5741` after #173 merged — not
stacked.** The browser surface for the presets arc: a **Channels** card where tapping a named channel
tunes the radio. **No server change** (the API + status events already exist from cycle 6); **no new
deps**.

**What shipped (code, PR #<pending>):**
- **`web/src/components/PresetControl.jsx`** (new, patterned on `DStarPanel`): a standalone card, a
  tap-a-preset `btn-row`, fetches `GET /presets` on mount, applies via `POST /presets/apply`. Errors as
  `role="alert"`; a non-empty `skipped` from the apply response shown as a `notice` (non-silent).
- **The two hide gates (no third state):** the `SET_FREQUENCY` capability gate is a conditional mount in
  `ControlPanel.jsx` (`{hasCap("set_frequency") && <PresetControl/>}` — the same predicate as `showDial`,
  the ScanControl hide model); config-absence self-hides (`presets.length === 0 → return null`, the
  DStarPanel/LinkPanel/DvapPanel pattern).
- **Active-channel highlight — DERIVED, never stored:** `activePresetName(presets, state, hasCap)` matches
  a preset's honoured fields against live `state.frequency`/`tone`/`mode` (tone/mode compared only when
  `hasCap` advertises them), highlighting only on an **exactly-one** match (ambiguity → none;
  tune-away clears it). `aria-pressed` + a green `.preset-btn.active` CSS accent.
- **Reactive across browsers for free:** apply publishes a `status_event` server-side (ADR 0115), so every
  open tab's FreqLcd + highlight update over `/events` (ADR 0076/0077) — no polling, no client store.
- **`web/src/api.js`:** two one-liners — `presets()` (GET) and `applyPreset(name)` (POST). No new error
  class (404/409/422 → `ApiError`, 501 → `Unsupported("set_frequency")`).
- **Docs:** ADR 0116; `docs/using-it.md` a "Channels" paragraph; `docs/adr/README.md` index row.
- **Tests (Vitest — `cd web && npm test`, the local gate; separate from `uv run pytest`):**
  `PresetControl.test.jsx` (config-absence hide, apply-by-name, skipped-field notice, mid-TX 409 alert,
  501→onUnsupported, highlight derivation incl. the ambiguity + honoured-field-gating rules) + an extended
  `ControlPanel.test.jsx` gating test (mounts under CAT caps, hidden on audio-only). **`npm test`: 36
  passed (8 files); `npm run build` clean; `uv run pytest`: 1471 passed, 5 skipped (Python untouched).**

**Out of scope (named in ADR 0116):** preset editing in the UI (file stays source of truth), split/offset,
the watchdog/TOT arc, any server-side change (none), bench items.

**Follow-ons:** **split/offset** for TX-through-a-repeater (a `CatRadio`-interface arc); the presets arc
is otherwise complete end-to-end (config → API → UI).

---

## Channel presets, cycle 6: model + config + apply path + HTTP API (ADR 0115) (2026-07-21)

**Branched fresh from `origin/master` (`channel-presets`) off `6dc158c` after #172 merged — not
stacked.** Channels live server-side (ADR 0111; the `CatRadio` backends omit `SET_CHANNEL`), so a
"channel" is a host-side **preset** — a `{frequency, tone?, mode}` triple the operator names in
`radio.toml` and applies through the existing tuning surface. This cycle is the model, config, apply
path, and HTTP API; **the web UI is the next cycle** (curl is the acceptance interface). No hardware;
everything runs against `MockRadio`.

**What shipped (code, PR #<pending>):**
- **Model + config (ADR 0115 D1):** new `radio_server/presets.py` — a frozen `Preset`, a self-contained
  EIA 38-tone `CTCSS_TONES` set + `FM`/`NFM` modes, `resolve_presets` (fail-loud like
  `resolve_mumble_entries`: bad CTCSS tone / duplicate name (case-insensitive) / ≤0 or non-int frequency
  / unknown field / bad mode stop startup; empty → dormant `()`), the pure `split_preset_fields`
  (honoured/skipped in the `Capability` vocabulary), and `apply_preset(radio, preset)` (anchor-first,
  capability-gated per field). Config rides the `[[mumble.servers]]` recipe: `config.settings.load_presets`
  + a `PRESETS_TABLE` top-level peel-off in `_flatten` (the single load-bearing integration point — a
  top-level `[[presets]]` is a `list`, so without the skip it hits the unknown-key guard). `save_settings`
  leaves the hand-authored block untouched (source-of-truth is the file, v1); `render_example` ships a
  commented `_add_presets_table` example; `radio.toml.example` regenerated. **No `SettingSpec` added →
  settings-API canary unchanged.**
- **Apply path + API (ADR 0115 D2/D3, in `api/app.py`):** `resolve_presets(load_presets(config_path))`
  composed in `build_app`, threaded into `create_app(presets=…)`; the routes are closures reading the
  live `radio`/`arbiter`/`scan_runner`/`hub` locals (switch-safe, ADR 0076). `GET /presets` lists +
  per-backend honoured/unsupported split. `POST /presets/apply {name}` (case-insensitive) → **404**
  unknown, **501** on an audio-only backend (gated on `SET_FREQUENCY`, like `/frequency`), **409**
  refused mid-TX (arbiter, ADR 0017), stop-scan-first (ADR 0028), **422** on a backend `ValueError`
  (out-of-band for the active radio); on success `hub.publish(status_event(radio))` (no parallel store,
  ADR 0076/0077) and returns `{applied, skipped, status}`.
- **Docs:** ADR 0115; `docs/configuration.md` (a `[[presets]]` section + curl examples);
  `docs/api.md` (the two routes + the 404/409/501/422 rows); `docs/adr/README.md` index row.
- **Tests:** `tests/test_presets.py` (resolve fail-loud cases, `split_preset_fields`
  full/partial/audio-only, `apply_preset` incl. the tone-skip via a partial-cap stub, and the full API:
  list, apply-changes-state + `status` push, 404, mid-TX 409, mid-scan stop-first, audio-only 501,
  backend-ValueError 422); `tests/test_config.py` (`load_presets`, top-level-skip regression, commented
  example). **`uv run pytest`: 1471 passed, 5 skipped** (34 new; backend suites untouched).

**Out of scope (named in ADR 0115):** the web UI (next cycle), split/offset (a follow-on arc touching
the `CatRadio` interface — no backend has a split surface), preset editing via API (file is source of
truth v1), the watchdog/TOT arc, bench numbers.

**Follow-ons:** the presets **web UI** (the reactive `status`/`caps` path is already fed, so the control
just needs a card + apply button); **split/offset** for TX-through-a-repeater.

---

## UV-K5 (Quansheng Dock) backend, cycle 5: config + factory + doctor + setup docs (ADR 0114) (2026-07-21)

**Branched fresh from `origin/master` (`uvk5-config-factory-doctor`) off `4468ff9` after #171 merged —
not stacked.** Made the UV-K5 a first-class backend: selectable, diagnosable, documented. No baofeng/kv4p
behaviour change; no bench claims (fakes only; Kris keys).

**What shipped (code, PR #<pending>):**
- **Config + factory (ADR 0114 D1):** a `[uvk5]` block in `config/spec.py` (10 keys) with two
  **REQUIRED, fail-loud** fields — `uvk5.serial_port` (no safe default: the AIOC is an ambiguous
  `ttyACM*`) and `uvk5.frequency` (XVFO has no radio-side value to preserve; kv4p's NVS rationale does
  NOT transfer). New coercers `coerce_required_int` + `coerce_optional_float`. `factory.py` registers
  `Uvk5Radio`; `api/backend_config.py` threads every setting + a `cat`-with-0-threshold guard (uvk5 has
  a real RSSI busy line, so `cat` is valid, unlike baofeng). `radio.toml.example` regenerated; canary
  79 → 89.
- **Latent bug fixed (uvk5 was the first backend with REQUIRED keys):** `save_settings` and the
  `/radio/select` `base` patch both reconstructed config from every `is_set` key, **fabricating
  presence** for an unconfigured backend (its optional keys read as set) — harmless for baofeng/kv4p, but
  a fabricated-yet-incomplete uvk5 (unset REQUIRED `serial_port`) crashed backend enumeration. Fixed both
  sites (save skips a backend group with an unset REQUIRED key; select carries only configured backends'
  keys). Regression-tested.
- **Constructor (ADR 0114 D3):** `Uvk5Radio.__init__` gains `tone`/`mode` (applied at init) and
  `tx_allowed` — a software refuse-to-key (raises `Uvk5KeyingError` at the top of `_key_on`, before any
  stream open or register write; RF-safe, composes with the read-back confirm).
- **Doctor (ADR 0114 D2, all in `doctor.py`):** `--backend uvk5` routing; `_uvk5_config` (reads the real
  `radio.toml`); a connect probe (ReadRegisters(0x30) elicit = dock alive; best-effort HELLO version +
  wrong-version warn); register `--key-test` (read-back confirm, reused RF guards); rx-level/-capture/dtmf
  ride the shared canonical path with a real-sound-card true-rate print (`_format_soundcard_rx_rate`).
- **The stock-vs-dock tell — derived from the pinned firmware `app/uart.c` (`@4375c3e`), not memory:**
  `0x0514` HELLO/`0x0515` version is **unguarded** (stock answers); `0x0851` ReadRegisters + the other
  `0x08xx` are `#ifdef ENABLE_DOCK` (dock-only). So register-timeout **+** a HELLO answer = STOCK firmware
  (flash the Dock fw); neither = dead/wrong-port. **Obfuscation (also from `uart.c`):** the opcode is read
  *before* deobfuscation and `obf(0x0514)==0x6902`, so HELLO is **plaintext-out / plaintext-in** — run
  over a short-lived `Uvk5Transport(obfuscate=False)`, no change to `connect()`.
- **Docs:** new `docs/uvk5-setup.md` (flash recipe at the pinned `0.32.21q` per the repo's own
  instructions; AIOC/K1-jack wiring incl. the speaker/mic-mute note; the bench-gate bring-up checklist
  ending in THE acceptance gate; a prominent **stuck-key warning**). `configuration.md` / `install.md` /
  `troubleshooting.md` / `architecture.md` (backend table row) / `README.md` updated (kv4p entries
  untouched).
- **Tests:** `[uvk5]` config coercion + REQUIRED fail-loud + round-trip; factory wiring + cat guard;
  ctor tone/mode/tx_allowed; the full doctor set (routing, dock/stock/dead/wrong-version probe, the
  plaintext HELLO probe over a `dock`/`hello_version`/`withhold_tx_confirm` extension of
  `FirmwareFakeSerial`, key-test, rate helper). **`uv run pytest`: 1437 passed, 5 skipped.**

**⚠ Still the bench acceptance gate (carried from ADR 0112/0113):** whether register-keyed TX actually
carries the AIOC-injected K1 mic audio — nothing this cycle claims it. Also verify-on-hardware: the 0.5 s
lead, the AIOC device name/rate, and the real HELLO obfuscation/reset-on-open timing.

**Next (out of scope here, named in ADR 0114):** the server-side presets feature, the web UI, and the
stuck-key **watchdog/TOT** (the full-control loop has no time-out — SIGKILL mid-key leaves the radio keyed).

## UV-K5 (Quansheng Dock) backend, cycle 4: AIOC audio via a shared sound-card seam (ADR 0113) (2026-07-21)

**Branched fresh from `origin/master` (`uvk5-audio`) off `0daa772` after #170 merged — not stacked.**
Made `Uvk5Radio` audio-complete by **reusing** the AIOC sound-card machinery `AiocBaofeng` already
runs, not duplicating it. Keying stays the BK4819 register path from ADR 0112 (no serial-line PTT).

**Behaviour-preserving extraction (the STOP condition did not trigger).** All AIOC sound-card code was
inline in `aioc_baofeng.py`; moved the PTT-independent machinery to a new shared
`radio_server/backends/soundcard.py` — `SoundCardTxPacer` (the moved `_AiocTxPacer`),
`open_capture_stream`/`open_playout_stream`, `load_sounddevice(injected, extra_hint)`, `lead_in_bytes`
/`playout_buffer_bytes`, and the device/block/lead/buffer `DEFAULT_*`. `AiocBaofeng` delegates to it,
keeping every attribute + RF-safety invariant, and **re-exports** the moved names (`_AiocTxPacer =
SoundCardTxPacer`, the `DEFAULT_*`), so `tests/test_aioc_baofeng.py`, `doctor.py`, and `config/spec.py`
are **untouched — not even import-path edits**. Baofeng suite green unchanged (35 passed, 1 skip).

**What shipped (code, PR #<pending>):**
- `backends/soundcard.py`: new shared seam (above). `backends/aioc_baofeng.py`: delegates + re-exports.
- `uvk5/radio.py`: `receive()` (lazy capture → canonical `AudioFrame`); `transmit()` (fail-loud format;
  streaming enqueue vs one-shot key/enqueue/`wait_drained`/unkey). `_key_on()` opens the playout stream
  + pacer **before** the register TX-enable+confirm (a failed audio open never keys; any failure undoes
  the whole key-up); `_key_off()` restores RX **first**, then tears down audio (RF-safe, non-raising,
  also the pacer `on_error`). Ctor gains `input_device`/`output_device`/`blocksize`/`tx_lead_seconds`
  + `_audio` seam; `close()` closes capture.
- `pyproject.toml`: `uvk5 = [serial, soundcard]` (no opus). `uv lock`: **no version moved**;
  `hardware`/`kv4p`/`mumble` closures byte-identical (ADR 0067). Transport/audio lazy-import msgs name
  `radio-server[uvk5]`.
- `tests/test_uvk5_radio.py` (+6, reuse baofeng `FakeAudio`): receive round-trip, non-canonical reject,
  one-shot `[clip]`, lead-in `[lead, clip]`, streaming one-stream-across-frames, and the full
  tune→register-confirmed-key→transmit→unkey over `FirmwareFakeSerial`+fake sound card together.
  **`uv run pytest`: 1393 passed, 5 skipped.**

**⚠ Acceptance gate for bench day (carried from ADR 0112, unsettleable offline):** whether register-TX
in XVFO actually transmits the **AIOC-injected K1 mic audio** — the one gate the UV-K5 TX path must pass
before it is trusted; **nothing this cycle claims it**. Also verify-on-hardware: the `0.5 s` TX lead-in
(inherited from the AIOC/UV-5R bench — this radio earns its own number) and device/xrun robustness.

**Next (out of scope here, named in ADR 0113):** `[uvk5]` config block + factory registration + settings
canary; `doctor`; presets; web UI; the stuck-key watchdog/TOT.

## UV-K5 (Quansheng Dock) backend, cycle 3: the Uvk5Radio CatRadio class + register keying (ADR 0112) (2026-07-21)

**Branched fresh from `origin/master` (`uvk5-radio`) off `aff4d81` after #169 merged — not stacked.**
Built the `Radio` class — the last cycle buildable before hardware. Mirrors `Kv4pHt` (ADR 0063) but
holds a small tracked-register model (host is the brain in full-control mode) instead of kv4p's
desired-state reconciler. Same pins, re-read from the scratchpad clones: fw `4375c3e…`, client
`851efa9…` (`ExtendedVFO/BK4819.cs`, `XVFO.cs`, `Defines.cs`).

**Keying settled — no STOP triggered.** TX/PTT is a **BK4819 register sequence** (`XVFO.Ptt` →
`BK4819.Transmit` → `GoTransmit`) — there is **no RTS/DTR serial-line PTT anywhere in the client** —
and it works inside the `CMD_0870` full-control loop (`default: UART_HandleCommand`, uart.c:706-708).
`ptt(True)` writes the TX-enable sequence and **confirms** via `ReadRegisters(0x30) == 0xC1FE`, else
restores RX and raises `Uvk5KeyingError` (the kv4p no-silent-key rule). `ptt(False)` restores RX
unconditionally.

**What shipped (code, PR #<pending>):**
- `frames.py`: added `EnterHwMode`(0x0870)/`ExitHwMode`(0x0871) no-param command dataclasses + enum.
- `radio.py`: `Uvk5Radio(CatRadio)` — byte-exact register sequences from BK4819.cs: `set_frequency`
  (regs 0x38/0x39 low/high of `freq_hz/10`, 0x33 band bit at 28M·10Hz, 0x30 retune), `set_mode`
  (reg 0x43: FM=18856/NFM=18440), `set_tone` (CTCSS reg 0x51/0x07, `((round(hz*10)*206488)+50000)/1e5`),
  keying (PA 0x36, 0x50, tone, 0x30=0xC1FE + confirm), `busy` (reg 0x67 RSSI vs threshold). **Fail-loud
  units** (reject off-10Hz-raster / out-of-band / unknown-mode / out-of-range-tone — never round/snap).
  Lifecycle: connect→enter full-control→seed regs from readback; `close()` unkeys→exits(0x0871)→closes,
  idempotent + atexit. Caps `SHARED|{SET_FREQUENCY,SET_TONE,SET_MODE,SCAN}`; SET_CHANNEL omitted
  (presets host-side); `scan()` raises like kv4p (software ScanEngine gate).
- `tests/test_uvk5_radio.py` (17): reuse the cycle-2 `FirmwareFakeSerial` register file; byte-exact
  tune/mode/tone/key sequences; **key-up raises + leaves un-keyed when TX confirmation withheld**.
  **`uv run pytest`: 1388 passed, 5 skipped.**

**Decision: audio deferred.** `transmit`/`receive` (the AIOC sound-card path, a separate USB interface)
raise `NotImplementedError` — out of scope this cycle. The class is control + register-keying + status.

**⚠ Verify-on-hardware (marked, none fabricated):** (1) **post-crash stuck-key** — the full-control
loop has NO time-out, so a host crash mid-key (0x30=0xC1FE, atexit bypassed by SIGKILL) leaves the
radio KEYED; an app-level watchdog/TOT is a future concern (echoes ADR 0090-0093); (2) whether
register-keying in XVFO transmits the AIOC K1 audio vs wrong source (guardrail-2 tension); (3) physical
PTT likely inert in the loop; (4) squelch threshold / RX band range / full GoTransmit PA-GPIO sequence.

**⚠ Next (later cycles):** `[uvk5]` config + factory + settings canary; the **AIOC audio path** (and how
PTT-by-register coexists with AIOC audio on one cable); server-side **presets**; `doctor`; web UI. No
instruction issue existed (delivered as a prompt) → issue relabel N/A; PR is the deliverable.

## UV-K5 (Quansheng Dock) backend, cycle 2: serial transport + firmware-accurate fake (ADR 0111) (2026-07-21)

**Branched fresh from `origin/master` (`uvk5-transport`) off `9e1b824` after #168 merged — not
stacked.** Added the serial transport layer + the load-bearing firmware-accurate test fake, still
entirely pre-hardware, and recorded the control-path decision. Same pins as cycle 1 (re-read from the
surviving scratchpad clones): fw `4375c3e…`, client `851efa9…`.

**Control-path decision (ADR 0111 Decision 1): (b) BK4819 register-write tuning**, channels as
server-side presets. **Confirmed viable, no hard blocker** from the pinned source: `0x0870` "enter
hardware control mode" (uart.c:672-739) puts the firmware in a loop servicing only serial commands
(its own radio logic suspended) until `0x0871`, so our register writes aren't fought; tuning = regs
0x38/0x39 (low/high 16 bits of `freq_hz/10`), 0x33 band, 0x30 tuning; TX/PTT is the AIOC serial line,
not a dock command (Guardrail 2). Refined the cycle-1 `0x0872` finding: it IS handled inside the
full-control loop (uart.c:700-703), just not at top level — no reply in either case.

**What shipped (code, PR #<pending>):**
- `backends/uvk5/transport.py` — `Uvk5Transport`: lazy-pyserial serial seam (dtr/rts False before
  open — this AIOC line also carries PTT), daemon reader thread → `Uvk5Decoder(validate_crc=False)` →
  `parse_frame` → dispatch to blocked waiters; `request(msg, match, timeout)`/`send(msg)` primitives
  (`Uvk5Timeout`/`Uvk5Closed`); `connect()` that ELICITS via a `ReadRegisters`→`RegisterInfo` probe
  (the dock does NOT stream at top level — silence = timeout), retransmitting to tolerate a possible
  reset-on-open boot race; idempotent `close()` + atexit. Simpler than kv4p: no flow-control window,
  no sequence reconciler (the dock is plain request/reply).
- `tests/test_uvk5_transport.py` — a two-layer fake: `FakeSerial` (dumb pipe) + **`FirmwareFakeSerial`**
  running the exact firmware receive parser (framing/length/obfuscation/**command CRC**, drops what the
  firmware drops; `bIsEncrypted` toggle; SendReply dummy-CRC replies; register store; 0x0870 full-control
  state). The load-bearing regression: a transport wired plaintext against the encrypted fake gets every
  frame dropped → `connect` **times out** (not false-green). **`uv run pytest`: 1371 passed, 5 skipped.**

**⚠ Verify-on-hardware (marked, none fabricated):** (1) does opening the AIOC port reset the UV-K5;
(2) does the AIOC expose the K1 UART data AND a usable PTT control line on one serial device at once
(dock tuning + AIOC PTT sharing one handle); (3) 38400 baud + real by-id path; (4) idle-sleep/keepalive
in the full-control loop.

**⚠ Next (backend-class cycle, out of scope here):** the `Radio`/`CatRadio` class composing this
transport with tuning; the enter/exit-XVFO handshake (`0x0870`→setup→readback→…→`0x0871`); the
`freq_hz`→register arithmetic; whether to send `0x0514` (plaintext + remote-UI `0xB5` stream) or stay
obfuscated; `[uvk5]` config + factory + settings canary; the server-side **presets** feature; `doctor`;
audio (AIOC's existing path); web UI. No instruction issue existed (delivered as a prompt) — issue
relabel N/A; PR is the deliverable.

## UV-K5 (Quansheng Dock) backend, cycle 1: the wire codec (ADR 0110) (2026-07-21)

**Branched fresh from `origin/master` (`uvk5-wire-codec`) off `d81807a` after #167 merged — not stacked.**
Kicked off the third backend: a Quansheng UV-K6 on nicsure's "Quansheng Dock" custom firmware, wired via
AIOC (serial + audio through the K1 jack, same pattern as `baofeng`). Multi-cycle goal is browser-selectable
channel switching for repeater monitoring. **Radio ordered, not yet on the bench** — pure offline protocol
work, mirroring the kv4p arc (codec first, ADR 0061 precedent).

**Pinned spec-only** (cloned + read at the exact SHA, cited `file:line`, nothing copied/ported): firmware
`nicsure/quansheng-dock-fw` **0.32.21q** = `4375c3e9604ee4c14ec4bdae67af077879a96f34` (Apache-2.0); client
`nicsure/QuanshengDock` **0.32.21q** = `851efa955740db9251811cc90195e927b52ba68c` (GPL-2.0).

**What shipped (code, PR #168):** `radio_server/backends/uvk5/frames.py` — a pure, stdlib-only codec
(imports nothing from `radio_server.*`, no I/O). Framing `[AB CD][Size][obf(payload + CRC16)][DC BA]`,
`Size+8` total: `crc16` (CRC-16/XMODEM), `obfuscate` (self-inverse XOR), `build_frame` (mirrors the client
`SendCommand2` byte-for-byte), `Uvk5Decoder` (streaming deframer modelled on the client `ByteIn`:
drop-and-resync, never raises, optional CRC validation), frozen-dataclass struct codecs for every dock
command/reply (calcsize-asserted), and `parse_frame` dispatch. **Tests — `uv run pytest`: 1355 passed,
5 skipped** (26 new in `tests/test_uvk5_frames.py`).

**Two findings from reading the pin (both recorded in ADR 0110):**
- **Reply-CRC asymmetry** — host→radio *commands* carry a real CRC (firmware validates, uart.c:1037-1039);
  radio→host *replies* carry `obf(0xFF 0xFF)`, a dummy the client's own decoder ignores (Comms.cs:181-186).
  So `Uvk5Decoder` defaults to `validate_crc=False`; the transport cycle keeps that. **Verify live replies
  decode on the bench.**
- **Pin discrepancy** — the kickoff listed `0x0872` set-modulation, but at 0.32.21q `CMD_0872_t` is defined
  and **not in the dispatch switch** (uart.c:1098-1137 has `0x0870` instead). Codec keeps `SetModulation`
  with a "verify before use" note.

**⚠ Next / gated (all out of scope this cycle, named in the ADR):** serial transport + AIOC wiring (38400
baud — verify on hardware), the HELLO/session handshake (send `0x0514` → plaintext, or stay obfuscated like
the shipped client), the `Radio`/`CatRadio` class (PTT over the AIOC serial line, never CAT), `[uvk5]`
config + factory registration + settings-API canary + `radio.toml.example` + `doctor` + backend-select UI.
**Open control-path decision for the next ADR:** (a) keypress-sim driving the radio's own memory channels +
screen readback vs (b) XVFO register-write tuning with channels as presets — the codec covers both. BK4819
freq→register mapping (regs 0x38/0x39 = low/high 16 bits of `freq_hz/10`, 0x33 band, 0x30 tuning) recorded
in the ADR for that cycle. **No instruction issue existed for this cycle** (delivered as a prompt), so the
end-of-cycle issue relabel was N/A; PR #168 is the deliverable.

## DVAP autoheal: restart-until-the-dongle-opens, in user space (ADR 0100) (2026-07-20, overnight)

**Branched fresh from `origin/master` (`dvap-autoheal-usb-wedge`) after #154 merged — not stacked.** Kris's
DVAP (441.6, A602RQT5, `dstarrepeater`) kept going **deaf** (reflector dashboard never updates); he can't
always be home to power-cycle it. Root-caused on the live bench (dummy loads):

- **The DV Access Point Dongle open-wedges** — first open after any abrupt close fails (`The DVAP is not
  responding with its serial number` → `Cannot open the D-Star modem` → dummy controller, deaf). And it
  **ALTERNATES** good/bad on each successive open (bench: `WEDGED,OK,WEDGED,OK,WEDGED`). So the old
  `dvap-autoheal.sh`'s **single unverified `systemctl restart`** healed only ~50% — and could wedge a
  *healthy* dongle. (A manual "kick" this session wedged one that had been decoding fine.)
- **No USB reset available:** no passwordless sudo, USB node is root-only, so `usbreset`/unbind need root we
  don't have. A reboot recovers both dongles but isn't an auto-remedy.

**Fix (ADR 0100, PR #<pending>):** rewrote `scripts/dvap-autoheal.sh` to **restart-until-the-log-confirms-
the-dongle-opened** (retry ≤4, verify each open via the dstarrepeater log), detecting both deaf modes —
Mode 2 open-wedge (log shows `Cannot open`) and Mode 1 re-enum stale-fd (open ttyUSB ≠ by-id target) — plus
a 60 s backoff on a truly-dead dongle. Pure user space, no root. Versioned with its `--user` unit
(`scripts/dvap-autoheal.service`). **Proven on hardware:** a wedged A602RQT5 recovered to OK in ~5 s
(`healing … (open wedge): restart 1/4` → `healed … after 1 restart(s)`), 0 restarts of a healthy dongle
(no flapping), both dongles OK after. **Deployed live** to `/home/kb/applications/dvap-autoheal.sh` (old one
backed up `.pre-usbwedge.bak`); `dvap-autoheal.service` restarted onto it.

**NB the deaf DVAP was NOT a radio-server bug** — the crossband decode path (ADRs 0097-0099) is fine. The
2nd re-proof "nothing heard" was (a) the DVAP wedging and (b) a routing mismatch (module B was linked to
REF001 C while radio-server module A / Kris watched XLX999 A). Optional future upgrade: a udev event trigger
+ `sudoers` USB-reset (needs a one-time root install) for the silent same-ttyUSB re-enum case.

## Crossband fail-safe when the DV Dongle wedges (ADR 0099) + the 2nd dummy-load re-proof (2026-07-20)

**Branched fresh from `origin/master` (`dstar-vocoder-wedge-failsafe`) after #152 + #153 merged — not
stacked.** Deployed master to the live 8090 (it was stale at #151) and ran the joint re-proof with Kris:

- **Phase 1 PASSED (mock backend, zero RF):** linked module A, keyed the ID-51A, and the DV Dongle decoded
  **intelligible voice in the browser** → **ADR 0098 decode fix confirmed by ear.** (There is no real-backend
  listen-only mode; the guaranteed-no-PTT proof runs `backend="mock"` — MockRadio.ptt drives no line.)
- **Phase 2 FAILED (baofeng, dummy load):** dead air + the transmitter stayed keyed, and `systemctl stop`
  hung ~15 s before PTT dropped. Root cause: a **wedged DV Dongle** (left bad by the Phase 1→2 restart) and,
  worse, a crossband that **did not fail safe** around it. Radio confirmed safe/down; Kris chose "fix in
  code, no more RF tonight."

**Three defects (one concern — traced, sourced), fixed here:**
- **`_recover()` reader race** — reassigned `_serial`/`_stop`/`_reader` after a 1.5 s join, so a **zombie
  reader** read the closed port → `TypeError('NoneType'…integer)` → then every exchange raised. Fixed:
  each reader is **generation-tagged** and bound to its own `serial`/`stop`; `_dispatch`/`_fail` shed a
  superseded reader (`dvdongle._read_loop`/`_spawn_reader`/`_fail`).
- **Streaming decode never recovered a wedge** (unlike legacy `_exchange`) → 1 s write-timeout **every
  frame** = dead air + parked the drain. Fixed: `_DvDongleDecodeStream` **latches wedged** and fails the
  over FAST; the dongle is healed at the **next** `open_decode_stream` (not mid-over).
- **Teardown blocked the event loop** — `_teardown` called `vocoder.close()` **synchronously before**
  `_force_unkey()`; `close()` waited ~15 s for `_io_lock` held by a live `_recover`, starving the unkey.
  Fixed: `_force_unkey()` runs **FIRST**, then `close()` runs **off-loop, bounded** (`run_in_executor` +
  `wait_for`); `DVDongleVocoder.close()` no longer blocks on a contended lock (skips the courtesy REQ_STOP).

**What shipped:** `docs/adr/0099-*.md`; `dstar/bridge.py` (`_teardown` reorder + off-loop close);
`vocoder/dvdongle.py` (reader generations, non-blocking `close`, `open_decode_stream` recover-if-failed,
`_DvDongleDecodeStream` wedge latch). Tests (+6, `uv run pytest` **1291 passed**, 5 skipped): bridge —
wedged stream ends the over & unkeys via the watchdog, teardown drops PTT before a slow close; driver —
decode-stream fail-fast latch, stale-generation `_fail` ignored, `open_decode_stream` recovers a
prior-over wedge, `close()` non-blocking under a held lock.

**Still gated:** crossband stays **disabled on the live radios**. The re-proof is **not done** — re-run it
(Phase 1 mock listen → Phase 2 dummy-load TX, Kris watching) **from a COLD-BOOTED dongle** (unplug/replug
`ttyUSB1`; never reuse it across a restart). kv4p (8091) leg still deferred behind the same gate. Live 8090
left stopped, `backend="baofeng"`, `reflector=""` (a `radio.toml.reproof-bak` backup sits beside it).

## Fix the garbled crossband decode: ordered streaming decode over the pipelined AMBE2000 (ADR 0098) (2026-07-20)

**Branched fresh from `origin/master` (`dstar-decode-pipeline-align`) after PR #152 merged — not stacked.**
The correctness half of the crossband bring-up (the safety half was ADR 0097 / PR #152). The module-A
decode came out as **garbage**; a sourced G4KLX review proved the byte path is correct (DVAP firmware
already de-scrambles; the 9 AMBE bytes go to the AMBE2000 verbatim). The real cause: the **AMBE2000 decode
is pipelined** and the per-frame `DVDongleVocoder.decode` (single-value reply slots) **dropped/mis-ordered**
frames when keyed straight onto RF.

**Bench measurement (decode-only, no keying, free dongle — recorded in ADR 0098):** `NULL_AMBE` → silence
(interface correct); latency **L ≈ 5 frames (100 ms), range 4–6**; the **dominant fault is frame DROPOUTS**
(exact-zero holes mid-tone), not the lag. A `STOP/START` reset is fragile; `_recover` is clean.

**What shipped (code):**
- `vocoder/base.py` — new optional `DecodeStream` + `StreamingVocoder` protocols (feature-detected).
- `vocoder/dvdongle.py` — `open_decode_stream()` → a `_DvDongleDecodeStream` backed by an **ordered FIFO**
  (the reader appends decoded PCM in order instead of a single-value slot); fixed prime/flush of
  `DEFAULT_DECODE_LATENCY_FRAMES=8` (≥ observed max L; marked tunable); legacy `decode()`/`encode()` kept
  so `--vocoder-loopback`/`--dstar-echo` are unchanged. No fragile per-over session reset.
- `dstar/bridge.py` — opens a fresh decode stream per over (on HEADER), feeds each AMBE through it
  (`_play_ambe` → 0..n ordered frames → the existing `to_canonical`→hub→**rx-gate (ADR 0097 preserved)**→
  `session.feed` path), and drains `flush()` on the clean end-bit (`_flush_and_end_rx`); closes the stream
  on `_end_rx`/`_force_unkey`.
- Tests (+4, `uv run pytest` 1285 passed): pipelined `FakeDongle` proves the real driver FIFO returns
  frames in order with none dropped + tail flushed; `PipelinedFakeVocoder` bridge end-to-end proves every
  frame of an over is keyed in order (the regression that would have caught the incident).

**Fast-follow (documented, not in this PR):** fold the bench script into a versioned `doctor
--vocoder-latency` subcommand + pure `latency_metrics` helper (guardrail-1 re-measurement).

**Still gated:** crossband stays **disabled on the live radios**. RE-ENABLE needs BOTH ADR 0097 (merged) and
this to land, THEN a joint dummy-load re-proof (Kris watching) — now with an added no-keying step first:
confirm **intelligible audio through the decode→`dstar_rx_hub` browser listen path** before any TX. kv4p
leg still deferred behind the same gate. NB the DV Dongle wedges after an abrupt process kill — cold-open
retries (or unplug/replug) before re-testing.

## Module-A crossband bring-up: stuck-key on the AIOC → the content-liveness + over-cap fix (ADR 0097) (2026-07-20)

**Branched fresh from `origin/master` (`dstar-crossband-deadair-cap`) — not stacked.** First supervised
dummy-load bring-up of the module-A DV-Dongle crossband on the live AIOC. Two real defects + one operator
error surfaced (full detail in memory [[dstar-stuck-key-incident]]):

- **Bug A — garbage decode (STILL OPEN).** Real off-air D-STAR AMBE from an ID-51A (via DVAP-B → XLX999 A
  → module A) decoded to **noise, not voice**, in the browser AND the FM out → the fault is the decode. A
  sourced investigation (G4KLX DummyRepeater `DVDongleController.cpp` / DV3000 / DVAPController) proved our
  byte path is **correct** — the DVAP firmware already de-scrambles/de-interleaves, and the 9 AMBE bytes go
  to the AMBE2000 **verbatim** (adding a transform would be WRONG). Prime suspect is now the **per-frame
  decode driving** in `vocoder/dvdongle.py` (each `decode()` writes a decode-AMBE *and* a dummy encode-audio
  packet, then reads single-value reply slots → pipeline/reply mis-pairing that the whole-stream loopback
  self-test never exercised). NEXT (no keying, safe): a bench diagnostic on the free DV Dongle — decode the
  standard `NULL_AMBE` frame (should be silence) and decode captured ID-51A frames as one whole stream vs
  the per-frame path — to localise driving-vs-interface, then a targeted `dvdongle.py` fix. Its own ADR/PR.
- **Bug B — stuck key (FIXED here, ADR 0097).** The over held PTT on dead air past the 180 s TOT because
  reflector→RF liveness was measured by frame *arrival*, not decoded *content* — a continuous stream reset
  the idle deadline every frame. Fix: a content `rx_gate` (AudioLevelGate on the decoded audio — dead air
  no longer counts as activity, so the over idles out in ~`tx_hang`) + a hard per-over ceiling
  `dstar.max_over_seconds` (default 60 s, < TOT; content-independent backstop for loud garbage). `bridge.py`
  + `config/spec.py` + `app.py`; `tests/test_dstar_bridge.py` +3 (28 pass); `uv run pytest` green.
- **Operator/agent error (recorded so it never repeats):** probing the AIOC PTT serial port with bare
  `pyserial` to "inspect" it **re-keyed the radio** (pyserial asserts DTR on open = PTT). Never open a PTT
  line to diagnose a stuck key. Safe-stop = `systemctl --user stop radio-server.service` + unplug the AIOC.

**State: radio-server left STOPPED on 8090; DV Dongle free; gateway/DVAPs untouched; UV-5R safe.** The
crossband stays **disabled on the live radios** — this ADR only ensures a non-terminating over can't strand
the key. **RE-ENABLE gated on: Bug A fixed AND a fresh joint dummy-load re-proof (Kris watching).** The
kv4p leg (enable `[dstar]` on 8091) is deferred behind the same gate.

## DVAP support, PR 3: the DvapPanel web card (completes ADR 0096) (2026-07-19)

**Branched fresh from `origin/master` (`dvap-web-panel`) after PR #150 (ADR 0096 backend) merged — not
stacked.** The visible half of the DVAP tab: a card with one row per configured DVAP module (label +
frequency + **confirmed** link pill + a reflector picker + Connect/Disconnect), self-hiding when no DVAP
is configured (the `state.dvap == null` → render null pattern, same as `DStarPanel`).

**What shipped (web only, no server change).**
- `web/src/components/DvapPanel.jsx` — modelled on `DStarPanel.jsx`: mount-seed GET, 2s poll, folds the
  pushed `dvap` WS event; per-module rows keyed by letter with independent reflector inputs; module state
  pill = Linked·<reflector> / Not linked / **Unreachable** (from the confirmed `reachable`/`linked`/
  `reflector` fields). Placeholder `XLX999 A` (the private test reflector).
- `web/src/api.js` — `dvapStatus` / `dvapLink(module, reflector)` / `dvapUnlink(module)`.
- `web/src/useEvents.js` — a `dvap` case in `reduceStatus` folding `{configured, remote, modules}`.
- `web/src/components/ControlPanel.jsx` — import + slot next to `DStarPanel` (passes `state.dvap`).
- `web/src/styles.css` — `.dvap-module` row divider/spacing.

**Tests — vitest 25 passed (7 files); `npm run build` clean.** `DvapPanel.test.jsx` (5): hides when
unconfigured, renders a row per module with frequency + confirmed pill, marks an unreachable module,
Connect links the module by letter with the typed reflector, Disconnect unlinks a linked module. Added
`dvapStatus` to the `ControlPanel.test.jsx` mock client (DvapPanel now mounts inside it).

**⚠ NEXT = the operator deploy step (with Kris) — no more code until then.** All three DVAP PRs (#149,
#150, this) get the tab working end-to-end only once the gateway side is set up:
1. Enable gateway remote-control: stop `ircddbgateway.service` → add `remoteEnabled=1`, `remotePort=10022`,
   `remotePassword=<secret>` → start (stop-edit-start; one restart blips the live A/B links). Loopback → no ufw.
2. Stand up DVAP #2 as module C: new `dstarrepeater2` (localPort 20013, DVAP `A602RQXT`, `dvapFrequency=441000000`),
   gateway `repeaterCall3=AE9S/Band3=C/Port3=20013`. Add `XLX999`→104.168.125.41 to the gateway XLX/DExtra host list.
3. On 8090/8091: add `[dvap]` block + `[[dvap.modules]]` (B@441.6, C@441.0), `dvap_remote_password` in
   `radio-secrets.toml`, restart.
4. Bench-verify the ADR 0095 wire protocol against the live gateway (safe `login→GRP` read-back, no TX).
5. THE TEST: link DVAP-B/C **and** D-STAR module A to **`XLX999 A`** (private reflector, key-ups fine),
   key a D-STAR HT on 441.600 → verify it comes out DVAP-C (441.000) AND the module-A FM crossband (the DV
   Dongle decode — dummy load first per the stuck-key guardrails). See plan + [[hardware-bench]].

## DVAP support, PR 2: the control surface — config + a cached manager + `/dvap/*` (ADR 0096) (2026-07-19)

**Branched fresh from `origin/master` (`dvap-control-surface`) after PR #149 (ADR 0095) merged — not
stacked.** Wires the remote-control client into the app: radio-server now links/unlinks/monitors the DVAP
gateway modules (B = 441.600, C = 441.000). Pure control-plane — no vocoder, no bridge, no PTT — reading
**confirmed** link state over the gateway remote-control interface. Off by default; D-STAR module A untouched.

**What shipped (code).**
- **Config** — `[dvap]` scalars `host` (127.0.0.1) + `port` (10022), both advanced (`config/spec.py`,
  +2 → settings canary 75→77 in `test_settings_api.py`). Array-of-tables `[[dvap.modules]]`
  (`module`/`label`/`frequency_hz`) modelled on `[[mumble.servers]]`: `DVAP_MODULES_KEY` + `_flatten` skip
  + `load_dvap_modules` (`config/settings.py`), `resolve_dvap_modules` fail-loud validation
  (`dstar/dvap_manager.py`). Secret `dvap_remote_password` (`config/secrets.py`). `radio.toml.example`
  regenerated (commented `[dvap]` + `[[dvap.modules]]` demo; `_add_dvap_modules_example` in `save.py`).
- **`DvapManager`** (`dstar/dvap_manager.py`) — caches confirmed state: `status()` is I/O-free (so
  `/status` never blocks), `refresh()` does the bounded UDP round-trips and marks an unanswered module
  `reachable:false` (never fails the snapshot). Errors `DvapUnknownModule` / `DvapUnavailable`.
- **API** — `/dvap/{status,link,unlink}` beside `/dstar/*`, blocking client run off the loop via
  `asyncio.to_thread`; link/unlink publish the confirmed post-refresh block as a `dvap` WS event; 404 /
  422 / 503 mapping; `dvap` embedded in `/status`, self-null when unconfigured. `create_app` gains
  `dvap_*` params; `build_app` reads modules/host/port/secret and passes a lazy `UdpRemoteControlClient`
  factory gated on `dvap_modules`; client closed on lifespan shutdown. `dvap` added to `EVENT_TYPES`.

**Tests — `uv run pytest` 1278 passed, 5 skipped.** `tests/test_dstar_dvap.py` (manager + resolver) and
`tests/test_dvap_app.py` (routes: off-by-default null, status lists modules, link→unlink round-trip with
the `AE9S   B` callsign field, 404/422/503, `/status` embed from cache, graceful degrade when unreachable).

**⚠ Next (PR 3, branch fresh from master after this merges):** the web **DvapPanel** card (`web/src/`:
`DvapPanel.jsx` modelled on `DStarPanel.jsx`, `api.js` `dvapStatus/dvapLink/dvapUnlink`, a `dvap` case in
`useEvents.js`, slotted into `ControlPanel.jsx`; vitest). Then the **operator deploy step (with Kris)**:
enable gateway remote-control (`remoteEnabled=1`/`remotePort=10022`/`remotePassword` — stop-edit-start,
one restart), stand up DVAP #2 as module C (new `dstarrepeater2`, 20013, 441.000). Then bench-verify the
ADR 0095 wire protocol against the live gateway (safe `login→GRP` read-back = confirmed state, no TX).

**TEST REFLECTOR (Kris, 2026-07-19):** a **private XLX reflector** is up for testing — **`XLX999 A`** at
**104.168.125.41** (only module A, "Test"; nobody else on it, so key-ups / dead air are fine). Ports:
DExtra 30001, DPlus 20001, xlxcore (DSRP) 10001. This is the empty-reflector target: link DVAP-B/C **and**
D-STAR module A to `XLX999 A`, key a D-STAR HT, verify the crossband FM out (the DV Dongle decode). No code
change needed — `parse_reflector("XLX999 A")` → family XLX already works; gateway needs XLX999→VPS in its
XLX/DExtra host list (deploy step). See the plan `.claude/plans/cycle-1-dv-zippy-thacker.md` and [[hardware-bench]].

## DVAP support, PR 1 of 2: an ircDDBGateway remote-control client, isolated + unwired (ADR 0095) (2026-07-19)

**Branched fresh from `origin/master` (`dvap-gateway-remote-client`) after PR #148 merged — not stacked.**
First half of DVAP support (the "DVAP tab" roadmap item). Goal: let radio-server link/unlink/monitor the
DVAP gateway modules — which are separate `dstarrepeater` endpoints it can't see over DSRP — via the
gateway's **remote-control** UDP interface, and get *confirmed* link state (retiring module A's "believed
state" later). This PR lands only the protocol client, isolated and consuming nothing, exactly as the
vocoder (ADR 0086) and DSRP (ADR 0087) seams landed first.

**Verified server ground truth this cycle (read-only recon, `kb@192.168.1.62`):** BOTH DVAPs are present
(`A602RQT5` = module **B**, 441.600, **already a working standalone node** linked to REF001 C, passing
reflector traffic to RF right now; `A602RQXT` = **unconfigured**, becomes 441.000). The old
"DVAP not configured" note was **stale** — corrected in memory. The gateway binary has the remote-control
interface compiled in (`CRemoteHandler`, `KEY_REMOTE_ENABLED/PORT/PASSWORD`, `sendRandom`) but it is
**off** (no `remote*` keys, no listener on :10022). Module A is on 127.0.0.1:20012; DVAP-B on 20011.

**What shipped (code) — nothing wired, no config/route/startup change.**
- `radio_server/dstar/remote_codec.py` — pure wire codec (the `dsrp.py` analogue). 3-byte ASCII tags,
  all ints **little-endian** (wx `SWAP_ON_BE`). Build: `LIN` login, `SHA`+32-byte `SHA256(random_bytes ‖
  password)`, `LNK`/`UNL` (callsign8 + int32 + reflector8), `GCS`, `GRP`. Parse (never raises): `RND`,
  `ACK`, `NAK`, `CAL`, `RPT` (repeater + reconnect + reflector + N link records = confirmed state).
  `Reconnect`/`Protocol`/`Direction` enums reproduced from g4klx `Defs.h`/`DStarDefines.h` declared order.
- `radio_server/dstar/remote_client.py` — transport seam (the `client.py` analogue): `RemoteControlClient`
  Protocol; `MockRemoteControlClient` (models a tiny gateway — per-module link map so link→status→unlink
  reads back; `fail_auth` toggle); `UdpRemoteControlClient` (lazy `LIN→RND→SHA→ACK/NAK` login cached,
  bounded timeout + retries, one lock serialising the socket). Errors: `RemoteAuthError`, `RemoteTimeout`.
- ADR 0095. Source-of-truth = public g4klx `RemoteProtocolHandler.{h,cpp}` read as a spec (ADR 0086 stance).

**Tests — `uv run pytest` 1253 passed, 5 skipped.** `tests/test_dstar_remote.py` (19): codec byte-layouts
(link/unlink/hash/parse RPT/CAL/malformed), enum order, Mock link/status/unlink round-trip + fail-auth,
and the Udp client over a fake connected socket (login handshake asserts the exact SHA over the injected
random; auth caching; NAK→`RemoteAuthError`; silence→`RemoteTimeout` with the resend; close→`LOG`).

**Judge-on-the-chip, verify against the live gateway (guardrail 1):** the LE-int wire convention, the
exact `SHA256(random_bytes ‖ password)` input, and port 10022. First live check (hardware phase, after
remote-control is enabled) = a safe `login → GRP → LNK REF0xx → GRP → UNL` round-trip on **module A**
(no TX), the analog of the DSRP Echo-unit proof (corr 0.999).

**Next (PR 2, branch fresh from master after this merges):** the DVAP control surface — `[dvap]` config
(modules B@441.600 + C@441.000; remote password in `radio-secrets.toml`), a `DvapManager`, `/dvap/*`
routes + `dvap` WS event, and a `DvapPanel` web card. Then the **server deploy step (with Kris)**: enable
gateway remote-control (stop-edit-start, one restart), stand up DVAP #2 as module C (new `dstarrepeater2`,
20013, 441.000). See the plan at `.claude/plans/cycle-1-dv-zippy-thacker.md` and [[hardware-bench]].

## The AIOC transmitter can never be stranded keyed: drop the line first, unconditionally, atomically (ADR 0093) (2026-07-19)

**Branched fresh from `origin/master` (`aioc-panic-unkey`) after PR #146 (ADR 0092) merged — not
stacked.** ADR 0090/0091/0092 hardened the D-STAR *bridge*, but the crossband STILL stuck-keyed the AIOC
on real hardware (3rd time) during the dummy-load test. A **controlled carrier key-test** (bare
`pyserial` DTR toggle on the dummy load, Kris watching, no audio/dongle/bridge — `/tmp/keyd.py` stepper)
proved the decisive fact: **`dtr=False` with the serial port still open cleanly UNKEYS this AIOC.** So
the hardware is fine; the stuck-key is a SOFTWARE failure to reach `dtr=False`.

**Root causes (all in `backends/aioc_baofeng.py`, the stranding class):**
1. `ptt(False)` was guarded on `self._keyed` → a desynced flag made the watchdog's / teardown's / REST
   `/ptt off`'s safety lever **no-op**. Journal proof: `/ptt off`→200, `status.transmitting`=False, but
   the carrier stayed up until the port CLOSED (SIGKILL).
2. `_key_on` asserted the line THEN wrote the TX lead-in; a lead-in write that raised propagated with
   the line asserted but `_keyed` never set True → stranded under (1).
3. `_key_off` DRAINED the stream (`stream.stop()`) before dropping the line → a `stop()` that
   blocks/raises on an xrun'd/starved stream (the DV Dongle write-timeout wedge was failing every decode
   — `SerialTimeoutException: Write timeout`, the PR #145 `_WRITE_TIMEOUT`) kept the line asserted.

**What shipped (code) — no config/schema/canary change.**
- `_drop_line()` — a single unconditional un-key primitive: bare `setattr(line, False)` + `_transmitting
  = False`, never guarded, no drain/teardown, can't block or raise. Every un-key route ends here.
- `_key_off` drops the line FIRST via `_drop_line()`, THEN `stop()`/`close()`s the stream inside
  `contextlib.suppress` (the RF-safety inversion of ADR 0029's drain-then-drop; costs a few ms tail clip).
- `_key_on` atomic: once the line is up, the lead-in write is guarded — any failure drops the line
  (+ tears the stream down) before re-raising.
- `ptt(False)` unconditional: always calls `_key_off()` (the `if self._keyed` guard is gone).
- ADR 0093; README row.

**Tests (model the failure) — `uv run pytest` 1230 passed, 5 skipped.** In `test_aioc_baofeng.py`:
lead-in write raises → line dropped, not stranded, ptt(False) still safe; `stop()` raises → line still
dropped; the line is proven LOW before the stream is stopped (recording fake stop()); ptt(False) forces
the line low when `_keyed` is desynced. Verified the two `_key_off` tests FAIL on the old drain-then-drop.

**⚠ Does NOT re-enable D-STAR.** 8090/8091 stay `[dstar] callsign=""`. Crossband re-enable is gated on a
**JOINT dummy-load re-proof with Kris watching** — never an autonomous run (I stuck the TX 3× tonight;
`/ptt off` and `/dstar/unlink` did not physically unkey until this fix). After merge, deploy `master` to
`/home/kb/applications/radio-server{,-kv4p}` (stash local `dtmf.py`; `uv` rebuilds on start). Two open
follow-ups before crossband is trustworthy: (a) the **DV Dongle write-timeout wedge** (why FTDI writes
stall under sustained decode load — the trigger; consider not fail-hard, or pacing the feed); (b) decide
**crossband vs browser-only** posture (browser listen/talk `tx_to_rf=False` never keys the TX and already
proved out at corr 0.985). See `dstar-stuck-key-incident` memory + ADR 0090/0091/0092/0093.
## The DV Dongle recovers itself from the idle-sleep wedge (ADR 0094) (2026-07-19)

**Branched fresh from `origin/master` (`dvdongle-sleep-recover`) from `2fed662` — not stacked.** (PR #147
/ ADR 0093 — the AIOC panic-unkey — was open but not merged when this was cut; this branch is independent.
Both are needed; merge order doesn't matter as they touch different files.) The crossband stuck-keys were
*triggered* by the dongle wedging (`SerialTimeoutException: Write timeout`); ADR 0092/0093 closed the
SAFETY half, this closes the RELIABILITY half.

**No-RF bench characterisation (safe, dongle free) pinned the wedge:**
- **Sustained decode is rock-solid**: `voc_stress.py` — 3000 back-to-back decodes, **0 slow**, ~36/s.
- **The wedge is the AMBE2000 idle-sleep**: after ~2-3 s idle the chip sleeps, the next decode times out
  (`VocoderTimeout`), and it **does NOT self-wake** (all 10 post-idle gaps failed). As the bridge keeps
  feeding, the FTDI TX buffer fills → `Write timeout` (the incident signature).
- **`voc_recover.py`: close+reopen+re-handshake RECOVERS it** (idle 4s → decode fails → reopen → decode OK).

**What shipped (code) — no config/schema/canary change.** `radio_server/vocoder/dvdongle.py`:
- `_exchange` split into a recover-and-retry wrapper + the `_exchange_once` primitive. On `VocoderTimeout`
  it calls a new `_recover()` **once** and retries the frame; a second failure propagates.
- `_recover()` rebuilds the transport+session under the (now **`RLock`**) io lock: signals stop, closes
  the old port, **joins the old reader BEFORE reassigning** `self._serial`/`self._stop`/`self._reader`
  (the reader reads them by reference — a live reader would race the swap), then reopens + re-handshakes,
  retrying the flaky first open `_RECOVER_HANDSHAKE_ATTEMPTS` (3) times; a dead dongle → `VocoderUnavailable`.
- Saved `_serial_factory`/`_port`/`_baud` in `__init__` for the reopen.
- ADR 0094; README row.

**Tests — `uv run pytest` 1230 passed** (1226 master + 4 new in `test_vocoder_dvdongle.py`): a `FakeDongle`
that handshakes-but-never-answers, reopened healthy → recover-and-complete; a flaky reopen (start drops) →
handshake retry; a permanently wedged pair → timeout still propagates after one recover; an un-openable
dongle → `VocoderUnavailable`. Verified all 4 FAIL without the recover wrapper. The old
`test_exchange_times_out_when_no_reply` repointed at `_exchange_once` (so recovery doesn't consume its fake clock).

**⚠ Does NOT change the D-STAR posture.** Crossband stays disabled on 8090/8091. This only makes the
vocoder robust for whenever D-STAR runs (crossband OR browser-only). Remaining before crossband is
trustworthy: **the posture decision** (crossband-vs-browser-only — Kris's call) and a **JOINT dummy-load
re-proof** (never autonomous — the TX stuck 3× tonight). Bench scripts on the server: `/tmp/voc_stress.py`,
`/tmp/voc_recover.py`, `/tmp/aioc_keytest.py`, `/tmp/keyd.py`. See ADR 0090-0094 + `dstar-stuck-key-incident` memory.

## The parked decode can no longer hold PTT: independent watchdog + re-key guard + unconditional unkey (ADR 0092) (2026-07-19)

**Branched fresh from `origin/master` (`dstar-decode-park-fix`) after PR #145 (ADR 0091) merged — not
stacked.** ADR 0091 was verified on fakes and merged, but the **first dummy-load reflector→RF test on
the real DV Dongle stuck-keyed anyway** (`mode=rx`, `rx_frames` frozen, PTT held ~40 s to the TOT).
Two hardware realities the FakeVocoder/MockRadio can't reproduce were behind it; this PR fixes both.

**Root causes (found on the dummy load, 2026-07-19).**
1. **The idle watchdog was inline in a loop that parks.** ADR 0091 put the RX watchdog at the *top of
   the `_reflector_to_rf` loop, but that loop `await`s each decode in the single-worker executor and a
   **wedged DV Dongle decode parks the loop there** — so the loop-top check never runs and the over
   never closes. (`asyncio.wait_for` around `run_in_executor` does **not** help: on timeout it awaits
   the uncancellable executor thread to finish anyway.)
2. **`close()` could skip the unkey; a resumed decode could re-key.** `unlink` returned in ~1 s (0091's
   teardown fix held) but PTT stayed up: `TxSession.close()` transmits a Part-97 sign-off **ID before**
   `ptt(False)`, that transmit **raised** on the wedged backend, and `_force_unkey`'s `suppress`
   swallowed it — skipping the unkey. Separately a decode resuming after the watchdog closed the over
   fed a late frame and **re-keyed**.

**What shipped (code) — no config/schema/canary change.**
- `dstar/bridge.py` — new **`_rx_watchdog` task** (started beside `_reflector_to_rf` when `tx_to_rf`)
  that only ever `await`s `asyncio.sleep`, never the executor, so it drops PTT even while the decode
  loop is parked. `_play_ambe` now **drops its frame if `mode != "rx"`** (no re-key of a closed over).
  `_force_unkey` now calls `radio.ptt(False)` **directly** as a path-independent backstop.
- `tx/session.py` — `TxSession.close()` wraps the sign-off ID `transmit` in try/except so a raise can
  **never** skip the `ptt(False)` / arbiter release beneath it. Hardens **every** streaming keyer
  (browser TX + Mumble bridge too), not just D-STAR.
- ADR 0092; README row.

**Tests (now model the hardware failure) — `uv run pytest` 1226 passed, 5 skipped.**
- `test_parked_decode_still_drops_ptt_via_the_independent_watchdog` — a `_BlockingVocoder` parks the
  decode in the executor; the over closes **on its own** (no teardown) while it's still parked. Proven
  to FAIL if the watchdog task is removed.
- `test_late_decode_does_not_rekey_a_closed_over` — a decode released after the over closed does not
  re-key (rx_frames unchanged, mode stays idle).
- `test_txsession_close_drops_ptt_even_if_signoff_id_transmit_raises` — a spy radio that raises on the
  sign-off transmit; `ptt_log == [True, False]` (unkey still lands).

**⚠ Re-enable is STILL gated + still a post-merge deploy step.** D-STAR remains **disabled** on both
live radios (`[dstar] callsign=""`). After this merges: deploy `master` to `/home/kb/applications/
radio-server{,-kv4p}` (preserve local `dtmf.py` edits via `git stash`; `uv` rebuilds on start), then
re-run the **dummy-load reflector→RF** test on 8090 (power-cycle the DV Dongle first — it wedges after
abrupt kills) and watch the over close + PTT drop after each over before going back on the antenna.
The 8090 checkout was already moved to `cab1d89` (PR A+B) during the incident; it needs this PR's
commit too. See `dstar-stuck-key-incident` memory + ADR 0090/0091/0092.

## D-STAR folded into the real radios: shared DV Dongle, crossband + browser, activity log (ADR 0089) (2026-07-19)

**Branched fresh from `origin/master` (`dstar-folded-shared-dongle`) after PR #142 (ADR 0088) merged —
not stacked.** Moves the ADR 0088 browser reflector seam off its standalone MockRadio node (8092) onto
the two real instances (AIOC 8090, kv4p 8091). Link a reflector from an instance's own HTTPS web UI →
that instance crossbands it to **its** radio's frequency **and** the browser; talk onto the reflector
from **either** the PC mic **or** that FM radio. Both tabs show a live **activity log** of who's heard.

**What shipped (code).**
- `dstar/bridge.py` — a **TX-owner latch** (`_tx_source` `rf`/`op`) so the crossband RF pump and the
  browser mic coexist (one talker owns the over, the other drops; no interleave into one DSRP session)
  — supersedes ADR 0088's `rx_to_reflector = not operator_tx` exclusion. **Lazy exclusive acquire**:
  `start()` creates the vocoder from a **factory** (opens the DV Dongle with `pyserial exclusive=True`)
  *first*, then registers the gateway + launches tasks; `stop()` closes the vocoder + releases the port.
  Start/stop now follow the reflector link, not the app lifespan. New `on_activity` callback fires on
  each inbound header (parsed MYCALL) and each of our own overs.
- `dstar/manager.py` — `connect` calls `bridge.start()` (a busy dongle → `VocoderUnavailable` →
  `DStarUnavailable`/**503**), `disconnect` sends the unlink then `bridge.stop()` (releases the dongle).
- `vocoder/dvdongle.py` — `_default_serial_factory` opens `exclusive=True` (the cross-process arbiter).
- `api/app.py` — bridge built at boot but **not started**; `_on_dstar_activity` → an `activity` WS event
  + a 30-entry ring on `/dstar/status`; `rx_to_reflector=True` always; `dstar.operator_tx` removed.
- Web: `dstarMode` from `state.dstar.active` (link state, not a flag); new `DStarActivityLog` card;
  `useEvents` folds an `activity` case. `web/dist` rebuilt.
- Config: `dstar.operator_tx` removed (**canary 75→74**); `radio.toml.example` regenerated.
- ADR 0089; `doctor --dstar-browser-echo` updated for the factory constructor.

**No gateway change this cycle** — module `AE9S A` (127.0.0.1:20012) already exists from ADR 0088, so the
DVAP (module B) and the gateway are untouched.

**Hardware-proven on the LIVE gateway (2026-07-19), from a throwaway branch checkout, production
untouched.** `radio-server-dstar` (8092) was stopped only briefly to free the DV Dongle, then restarted
(NRestarts=0). Two objective proofs on `kb@192.168.1.62`:
- **Browser round trip (the refactored bridge):** `doctor --dstar-browser-echo` — `send_operator_audio`
  → DSRP → live gateway Echo → in-bridge decode → `dstar_rx_hub`, staircase **pitch correlation 0.985**
  (194 frames talked, 195 heard). Exercises the factory-created vocoder, the exclusive open, lazy
  `start()`, the `op` TX-owner path, the keepalive, and the decode→hub listen path on real hardware.
- **Shared-dongle arbiter (plan risk #1):** two exclusive `DVDongleVocoder` opens of the real dongle —
  first opens + handshakes, **second is REJECTED** (`VocoderUnavailable`, EBUSY), re-open after release
  works. This is exactly the 503 "in use by the other radio" the manager surfaces; the OS serial
  exclusive lock is a reliable cross-process arbiter on this FTDI.
- After the proofs: 8090 (PID 1851), 8091 (1850), gateway (1832), DVAP/`dstarrepeater` (1833) all at
  baseline PIDs, NRestarts=0; 8092 back up.

**⚠ Rollout to 8090/8091 is a POST-MERGE deploy step, deliberately NOT done headlessly.** Discovered:
**both production instances are on STALE master (`87407ae`, PR #139 — pre-vocoder/pre-D-STAR) with
uncommitted local edits** (8090: `dtmf.py`; 8091: `dtmf.py`, `entries.py`, `update-radio-server.sh`).
They have no `[dstar]` support at all yet. Folding D-STAR in means upgrading each checkout across the
merged 0086/0087/0088 PRs **plus this one**, and reconciling those local edits — I won't clobber
uncommitted production changes or run production on an unmerged branch. **After this PR merges:** for
each of `/home/kb/applications/radio-server{,-kv4p}` — preserve the local edits (`git stash`), update to
`master`, `npm --prefix web run build`, add a `[dstar]` block (`callsign=AE9S`, `module=A`,
`gateway_host=127.0.0.1`, `gateway_port=20010`, `local_port=20012`,
`vocoder_port=/dev/serial/by-id/usb-Internet_Labs_DV_Dongle_A602RQNI-if00-port0`), restart, then
**disable** `radio-server-dstar.service` (8092) so it stops holding the dongle + module A. HTTPS is
already configured on both (`/home/kb/applications/radio-server/tls/radio-{cert,key}.pem`), so the
browser mic works with no cert work. Only one instance holds the dongle at a time; a second connect
returns the clean 503.

**Next cycle:** the **DVAP tab** — its own gateway module (B), needs the ircDDBGateway **remote-control
interface** enabled (config + restart + a new protocol client) for a reflector picker + confirmed link
status (which also fixes ADR 0088's believed-state guess for module A).

**Unit tests green** (`uv run pytest`: 1207 passed; web vitest: 19 passed): TX-owner latch (no
interleave), lazy exclusive acquire/release + busy-dongle 503, the MYCALL activity path, `dstarMode`.

---

## D-STAR in the browser: reflector picker + talk/listen with mic/speakers (ADR 0088) (2026-07-19)

**Live-gateway-proven. Answer: YES — you can open a web page, pick a reflector, hit connect, and talk +
listen in the browser, no D-STAR radio.** Branched fresh from `origin/master` (`dstar-reflector-browser`),
after PR #141 (ADR 0087) merged — not stacked. Extends the ADR 0087 link to the usable goal (the
D-STAR analogue of ADR 0050's web-UI-as-Mumble-client).

**What shipped.**
- `dstar/bridge.py` — `send_link_command(urcall)` (synchronous, idle-gated URCALL burst; `NULL_AMBE`
  only, no vocoder) for reflector link/unlink; `send_operator_audio`/`end_operator_over` (the
  browser-mic TX seam, the `MumbleBridge.send_operator_audio` twin); a shared `_alloc_session_id`; and
  a **vocoder keepalive** (see the bug below).
- `dstar/manager.py` — `DStarLinkManager`: `"REF001 C"` → URCALL `REF001CL` (REF/XRF/DCS/XLX differ
  only by prefix; unlink = `"       U"`), **believed** link state (no gateway readback), a `dstar` WS
  event. One bridge, not a per-entry factory.
- `api/app.py` — `WS /audio/dstar/{rx,tx}` (`dstar_rx_hub` + a distinct `dstar_talk_slot`) and
  `POST /dstar/{link,unlink}` + `GET /dstar/status` + a `/status` `dstar` block; all behind the
  `dstar.callsign` gate; `rx_to_reflector = not dstar.operator_tx`.
- Config: one new key `dstar.operator_tx` (browser-operator posture); `dstar.reflector` is now the boot
  auto-link. **canary 74→75**; `radio.toml.example` regenerated.
- Web UI: `DStarPanel` reflector picker (free-text + presets) + a `dstarMode` pointing Monitor/Transmit
  at `/audio/dstar/*` (reuses the `path`-parameterized `useRxAudio`/`useTxAudio`).
- `doctor --dstar-browser-echo` — the browser acceptance (real `send_operator_audio` → gateway Echo →
  in-bridge decode → `dstar_rx_hub`, staircase pitch metric).

**The live-gateway proof (this cycle), nothing else disturbed.** The production ircDDBGateway got the
one approved change — a second homebrew module `AE9S A` on `127.0.0.1:20012` (config
`/home/kb/applications/dstar-gateway/ircddbgateway`, backed up to `*.pre-dstar-cycle3.bak`) — and a
single restart; the DVAP re-registered, `REF001 C` link returned. Throughout, `radio-server` (8090),
`radio-server-kv4p` (8091), and `dstarrepeater` (the DVAP) kept **baseline PIDs, `NRestarts=0`**.
- **Reflector link:** `POST /dstar/link {"reflector":"REF030 C"}` → gateway logged `Link command from
  AE9S A to REF030 C issued via UR Call` → `D-Plus ACK received` → `D-Plus link to REF030 C
  established`. A **header-only** URCALL links a live reflector (`command_frames` default 0 — no
  fallback ladder needed; the central verify-on-hardware unknown, resolved).
- **Browser talk+listen:** the round-trip doctor tracked the nine-tone staircase at **pitch
  correlation 0.88** through the real decode → `dstar_rx_hub` (a bare-AMBE-tap variant: 1.000).
- **WS endpoints** serve with `?token=` auth; the SPA serves the reflector picker.

**Bug found + fixed here — AMBE2000 idle sleep.** ADR 0087 wrongly assumed a live bridge never has the
record-then-replay idle gap. A browser *listener* sitting idle lets the chip go unresponsive after
~2–3 s (bench: OK at 2 s, timeout at 3 s), so the first inbound over's decode timed out and the whole
over was lost. Fix: an **idle-gated keepalive** decoding `NULL_AMBE` every ~1.2 s while IDLE (gated to
idle so it never interleaves with a live stream — the ADR 0086 hazard). Post-fix: no decode timeouts.

**The dedicated instance (how Kris uses it).** A new user service **`radio-server-dstar.service`** runs
a checkout at `/home/kb/applications/radio-server-dstar` (branch `dstar-reflector-browser`), MockRadio
backend, **port 8092**, `dstar.operator_tx=true`, DV Dongle vocoder on `ttyUSB1`. **Open
`http://192.168.1.62:8092`, log in with the token in that dir's `radio-secrets.toml`, use the "D-STAR
reflector" card to Connect a reflector, then Listen / Talk.** It owns the DV Dongle — stop it before
running `doctor --dstar-*` (both want the dongle). Left unlinked and idle for Kris.

**Follow-ons (noted in ADR 0088):** gateway-confirmed link state (wire the DSRP `TEXT`/`STATUS`
packets — the parser already decodes them), DTMF reflector control, D-STAR ↔ Mumble bridge. The
per-over staircase glitch on 2/9 steps (corr 0.88) is a minor pipeline/keepalive artifact on sharp
tone edges — benign for speech; worth a look if a cleaner metric is wanted.

## D-STAR link: radio-server ⇄ ircDDBGateway through the DV Dongle vocoder (ADR 0087) (2026-07-19)

**Hardware-proven. Answer: YES — radio-server talks and listens on D-STAR through the DV Dongle.**
PR #140 (ADR 0086 acceptance) is merged (`origin/master` tip `cd376f7`); branched fresh (`dstar-link`),
not stacked. The first live consumer of the vocoder seam.

**What shipped.** A new `radio_server/dstar/` package (sibling to the Mumble link):
- `dsrp.py` — pure, I/O-free DSRP repeater↔gateway wire codec (register/poll/header/data + parser).
- `header.py` — the 41-byte D-STAR radio header + CRC-16/X-25 (verified against the g4klx table).
- `client.py` — a `GatewayClient` seam: `MockGatewayClient` + a `UdpGatewayClient` (socket + daemon
  reader + register/poll timers, `_socket_factory`/`_clock` seams).
- `bridge.py` — a half-duplex reflector↔RF state machine. A **mode latch (IDLE/RX/TX)** drives the
  single AMBE2000 one direction at a time (D-STAR's one-talker reality + the ADR 0086 no-interleave
  rule); keys RF via the shared `TxSession`/`TxSlot`/`station_id`, pulls RF via the pump demand +
  `AudioHub`; resample lives at the bridge edge, never in the vocoder.
- Config `[dstar]` group **OFF by default** (inert until `dstar.callsign` is set); wired into
  `create_app`/`build_app` behind that gate. **canary 66→74**; `radio.toml.example` regenerated.
- `doctor --dstar-echo` — the hardware self-test (RF PCM → AMBE → DSRP → gateway Echo → AMBE → PCM),
  reusing the vocoder staircase pitch metric.

**The bench proof (this cycle).** On the real DV Dongle against a throwaway echo-only ircDDBGateway (a
named second instance, isolated on loopback so the production gateway/DVAP were never touched —
`NRestarts=0` throughout): 194 frames sent, 195 echoed AMBE frames decoded back, **pitch correlation
0.999** across the nine-tone staircase, aligned at +11-frame latency. WAV captured. Two facts the
hardware pinned (guardrail 1), both now encoded + tested:
1. **On-wire header order** is RPT2 (gateway) in slot 1 (offset 3), RPT1 (module) in slot 2 (offset
   11) — the gateway matches the incoming repeater by RPT1; the reversed order logs "Header received
   from unknown repeater". (`test_dstar_header::test_on_wire_field_order_*` pins the raw offsets.)
2. **The AMBE2000 stops responding after a short idle** — the gateway's record-then-replay leaves a
   gap between the last encode and first decode, so `doctor --dstar-echo` **reopens the vocoder** for
   the decode phase (fresh handshake wakes the chip). A live bridge never has this gap (RX/TX are
   separate live streams). The `--vocoder-loopback` (back-to-back) stays correct and green.

**Bench setup note (for reproducing the proof).** ircDDBGateway has a single-instance lock keyed by
its optional positional *name*; a throwaway runs as `ircddbgatewayd -confdir <dir> -logdir <dir> NAME`
reading an INI `[NAME]` section from `<dir>/ircddbgateway_NAME`, with `gatewayAddress` on a spare
loopback IP + all reflector/ircddb protocols disabled + `hbPort` on a free port, so it never collides
with (nor disturbs) the production gateway. The HB (homebrew) repeater protocol on port 20010 **is**
DSRP. The gateway's Echo unit is on by default; UR=`"       E"` triggers it. No registration needed.

**Full suite: 1175 passed, 5 skipped.** Next: wire radio-server to the **live** DVAP gateway (operator
step — add a second repeater module + restart ircDDBGateway, a brief DVAP blip), then DTMF/web
reflector control and a D-STAR↔Mumble bridge over this now-known-good seam.

---

## DV Dongle vocoder — hardware acceptance + loopback fix (ADR 0086) (2026-07-19)

**Verified on the real DV Dongle. Answer: YES — it encodes/decodes working audio.** PR #139 (the seam)
is merged (`origin/master` tip `87407ae`); branched fresh for this acceptance cycle, not stacked.

**What the bench proved.** On the deployment dongle
(`/dev/serial/by-id/usb-Internet_Labs_DV_Dongle_A602RQNI-if00-port0`, 230400 8N1): the handshake and the
AMBE2000 D-STAR config bytes are correct **as reimplemented — no protocol byte needed changing**. A
streaming loopback of a 9-tone staircase (300–1500 Hz) round-trips with **pitch correlation 1.00,
median error ~8 Hz**, reproducibly across runs. Pure ~300 Hz is the one weak tone (near AMBE's low
speech-model edge) but recovers within tolerance.

**The real find (a genuine bug the hardware exposed).** The AMBE2000 is a **pipelined, full-duplex**
chip: encode and decode must each be driven as a *continuous stream*. The shipped loopback interleaved
`decode(encode(frame))` per frame, which scrambles time-varying audio (pitch correlation ~0, gross
errors like 450 Hz → 3217 Hz). A single steady 600 Hz tone is invariant to that scrambling, which is
exactly why the first bring-up "passed." The round-trip also carries a **constant but session-varying
latency** (~0–18 frames).

**Fix (this cycle, PR against master):**
- **`doctor.py` `_vocoder_loopback`** rewritten: a **staircase-of-steady-tones** probe (+ flush frames),
  **encode the whole stream then decode the whole stream**, and a **lag-aligned per-step pitch-tracking**
  metric (`staircase_pitch_metrics` / `_synth_staircase_pcm` / `VocoderMetrics`, all pure) with a 0.8
  correlation pass threshold and a wide energy band. Replaces the old single-tone `vocoder_roundtrip_metrics`.
- **`vocoder/dvdongle.py`** — docstring hazard note: never interleave `encode`/`decode` per frame; the
  seam API is unchanged and correct (a real TX-encodes / RX-decodes path never interleaves).
- **Tests** (`tests/test_doctor.py`) rewritten for the new pure metric: identity tracks (corr 1.0, lag 0),
  a delayed round-trip is recovered by the lag search, a fixed buzz / silent output fail. Fake-serial
  `test_vocoder_*` suites untouched and green. Full suite: 1139 passed, 5 skipped.
- **No schema touched** — `radio.toml.example` byte-identical, canary unmoved (still unwired).

---

## The vocoder seam: PCM ⇄ AMBE over the DV Dongle, isolated + unwired (ADR 0086) (2026-07-18)

**No hardware, no keying; built/tested against fakes + a fake clock.** PR #138 (ADR 0085) is merged
(`origin/master` tip `56eed3f`); branched fresh (`vocoder-dvdongle-seam`), not stacked.

**Why now:** a future digital-voice/D-STAR path needs a vocoder (8 kHz speech PCM → compressed voice
frame and back). It's the one piece that can't be proven by inspection, so it lands **alone and
unwired**, before any framing/socket/bridge plumbing — the same posture the reverted Codec2 seam took
to open the old M17 arc.

**What shipped (new `radio_server/vocoder/` package):**
- **`vocoder/base.py`** — the seam: a `@runtime_checkable` `Vocoder` `Protocol` (`encode(frame)->bytes`
  / `decode(bytes)->AudioFrame`, one 20 ms frame; `close()`), the 8 kHz geometry constants, and
  `VocoderUnavailable` / `VocoderTimeout`. **The seam is native 8 kHz, not the app's 48 kHz canonical**
  — every real vocoder (AMBE2000/AMBE3000/Codec2/Griffin) is 8 kHz, so the 48k⇄8k resample belongs at
  the *future consuming backend's* edge (reuse `audio/resample.py`), not in the vocoder. Deliberate
  departure from the Codec2 seam (which resampled internally). Frame stays a fail-loud `AudioFrame`.
- **`vocoder/frames.py`** — pure, I/O-free DV Dongle wire codec (the kv4p `frames.py` split). Framing is
  a 2-byte LE header = 13-bit total length + 3-bit type (`length = word & 0x1FFF`, `type = word >> 13`),
  confirmed against every reference constant. The 48-byte AMBE payload = a 24-byte AMBE2000 **D-STAR
  full-rate config block** (verbatim from the reference) + the 9-byte voice frame at offset 24. Streaming
  length-prefixed deframer that resyncs past garbage.
- **`vocoder/dvdongle.py`** — `DVDongleVocoder`, kv4p-transport pattern: lazy pyserial behind `hardware`
  + `_serial_factory` seam; daemon reader thread (read→deframe→dispatch) bounded by a stop `Event`;
  fatal-read path wakes waiters; `Condition`-guarded reply hand-off; idempotent best-effort `close()`.
  Bring-up = `open()` (query name) → `start()`; AMBE config rides every AMBE packet. v1 = synchronous
  query/reply per frame. Port/baud (230400) are marked-verify module constants.
- **`doctor.py`** — new `--vocoder-loopback` (+ `--vocoder-port`), handled before the backend split
  (drives a separate FTDI device, not the radio; like `--analyze-wav`). Synthesize 8 kHz PCM → encode →
  decode → `to_canonical` → `_write_wav_mono16`; reports a pure `vocoder_roundtrip_metrics` (frame count,
  in/out RMS ratio, dominant-tone — **lossy, never sample equality**). Reuses `_Report` + the WAV writer.
- **Protocol source:** g4klx/DummyRepeater `Common/DVDongleController.cpp` (GPL-2) read as a **spec** and
  reimplemented clean — no ported code. AMBE vocoding stays on the licensed DVSI chip, so no codec and no
  patent/copyleft exposure in-tree.
- **Docs:** ADR 0086, README index row, this handoff. **`pyproject.toml`**: one-line note that the DV
  Dongle rides the existing `serial`/`hardware` extra (no new extra).

**Explicitly NOT done (unwired, per the issue):** no `[vocoder]` config group (so **no canary bump,
`radio.toml.example` byte-identical**), no factory registration, no `backend_config.py` kwargs, no
`Radio` backend, no reflector/D-STAR-header/bridge/DTMF work.

**Verified:** `uv run pytest` → **1137 passed, 5 skipped** (+37: `test_vocoder_frames.py`,
`test_vocoder_dvdongle.py` — a request/response `FakeDongle` proving handshake + per-frame codec +
fail-loud guards + missing-pyserial; extended `test_doctor.py` for the metric + argparse wiring).
`radio.toml.example` byte-identical, canary unmoved.

**Bench acceptance (operator, NOT headless — the risky unknown):** plug in the DV Dongle and run
`uv run python -m radio_server.doctor --vocoder-loopback --vocoder-port <by-id> --out vocoder-loopback.wav`.
Acceptance = the handshake + AMBE2000 config succeed and the written WAV is **intelligible** (DVTool's
"Audio Loopback Only" equivalent). Cross-check port/baud/config bytes and the metric threshold against
DVTool; correct any constant that differs and update its verify note.

## A post-transmit RX guard: keep the TX→RX turnaround transient off Mumble (ADR 0085) (2026-07-18)

**No hardware, no keying; built/tested against fakes + a fake clock.** PR #137 (ADR 0084) is merged
(`origin/master` tip `2ae1295`); branched fresh (`mumble-rx-guard`), not stacked.

**The bug (AIOC-only, understood):** talk from the phone (Mumble→RF), release PTT, the radio drops —
then the phone hears a ~0.25 s buzz. It's the UV-5R receiver recovering at the TX→RX turnaround (FM
hash before its squelch settles), captured by the AIOC sound card and relayed because
`audio.squelch="off"` passes everything. The duplex arbiter resumes RX the instant TX releases with no
guard — the exact timing its docstring (`arbiter/state.py:22-23`) says is "a bench fact, not modeled
here." kv4p is immune (SA818 hardware squelch keeps the transient off the wire).

**What shipped:**
- **`api/app.py`** — an app-scoped `rx_guard = DtmfMuteGate()` (the ADR 0049 timed latch reused a
  third time). The arbiter's `on_change` now tracks the prior mode and arms it (`mute_for`) when
  leaving `TRANSMITTING`. Keying off the **arbiter** (source-agnostic) means a **browser talker's**
  release arms it too, not just the Mumble bridge's own TX — both funnel through `release_tx()`.
  Threaded `mumble_rx_guard_seconds` from settings and passed `rx_guard=` into the bridge factory.
- **`link/bridge.py`** — new injected `rx_guard` param; `_rx_to_mumble` (both branches) drops the
  frame while `rx_guard.muted()`, counted as `rx_guarded` in `tx_stats()` (`/link/status`). Suppresses
  **only** the Mumble feed — browser Listen (a separate hub subscriber) and the recorder are untouched
  (recording never loses audio). `None` = no guard (unchanged relay).
- **`link/client.py`** — `DEFAULT_MUMBLE_RX_GUARD_SECONDS = 0.4` (marked verify-on-bench).
- **`config/spec.py`** — `mumble.rx_guard_seconds` (float, `coerce_nonneg_float` so **0 disables**),
  advanced tier beside `mumble.tx_hang`. canary 65→66; `radio.toml.example` regenerated.
- **Web (bundled UI ask):** all Settings-screen collapsibles now default **collapsed** — the basic
  GroupPanels (`SettingsView.jsx`, `open`→`open={false}`) and the Mumble-servers panel
  (`MumbleServersPanel.jsx`, dropped the bare `open`) joined the already-collapsed advanced tier.
- **Docs:** ADR 0085, index row, `troubleshooting.md` ("a short buzz on Mumble right after I stop
  talking (AIOC)" + the try-your-squelch-first note), this handoff.

**Verified:** `uv run pytest` → all green (see the run); `cd web && npm test` + `npm run build` green.
Tests (fakes + fake clock): suppress-then-resume across the guard window (the buzz regression), `0`
disables, the guard arms on a plain arbiter TX→RX release (the browser-talker path) and **not** on
RX→IDLE / IDLE→RX, and a `rx_guard=None` bridge relays exactly as before (Listen/recording unchanged).

**Bench acceptance (operator, not headless):** on the AIOC over Mumble, talk from the phone and
release — the post-release buzz is gone; a fast back-and-forth doesn't clip the reply's start (tune
`mumble.rx_guard_seconds`). kv4p unaffected. **Do not key headless.**

**Non-goals:** no kv4p change, no `mumble.tx_hang` change (that's the Mumble→RF quiet window; this is
the RX side after TX), no AGC/noise-gate, no new backends. Browser Listen could reuse the same latch
if it shows the same buzz — noted in the ADR, not broadened without cause.

## Make the kv4p RECEIVE path a continuous stream — RX mirror of the TX pacer (ADR 0084) (2026-07-18)

**No hardware, no keying; built/tested against fakes.** PR #136 (ADR 0083) is merged (`origin/master`
tip `0058d51`); branched fresh (`kv4p-rx-continuous-silence`), not stacked.

**The bug (mirror of ADR 0082, code-confirmed on both backends):** over Mumble via kv4p, the tail of a
*received* transmission loops ("Max Headroom") when a signal ends — not on the AIOC. The AIOC reads a
continuous sounddevice capture, so `receive()` always returns full-length audio (silence between
transmissions); the frame-push kv4p returned an **empty** frame (`AudioFrame(b"")`) on its idle-poll
timeout. The shared `RxPump` (`rx/pump.py:230` `if frame.samples:`) skips empty frames *before* the
activity gate, so the VAD gate's **hang** (`activity/gate.py`, which is what publishes the trailing
taper) never ran — the RF→Mumble feed (subscribed to the same `AudioHub`, `link/bridge.py:187`)
stopped abruptly and the far-end Mumble/phone client concealed the gap by looping the tail.

**What shipped:**
- **`backends/kv4p/radio.py`** — `receive()` returns a full-length canonical silence frame
  (`_RX_SILENCE`, 1920 zeros — the shape a decoded packet yields) on the idle-poll timeout instead of
  an empty frame. A real packet still returns immediately; only the idle path changed (empty →
  silence). This is a backend-level, gate-agnostic change: the RX stream is now continuous like the
  AIOC, so the pump/gate/recording/DTMF treat kv4p identically (no backend branching anywhere).
- **`doctor.py`** — `measure_rx_levels` now skips fully-silent (all-zero) frames, so the continuity
  fill and true inter-transmission silence can't dilute the avg RMS or inflate the ADR-0070
  `--rx-level` ADC-clock estimate. Measures real received audio only.
- **Tests:** `test_kv4p_radio.py` — idle queue → full-length silence (not empty; the regression),
  burst-then-idle → a continuous full-length stream tapering to silence, and the idle silence reads as
  zero-RMS so the `AudioLevelGate` holds open through its hang then **closes** (taper, not latch).
  `test_doctor.py` — `measure_rx_levels` skips all-zero frames. Existing kv4p RX / switch / TX-pacer
  (0082) tests stay green.

**Verified:** `uv run pytest` → **1100 passed, 5 skipped** (+3 net). No schema change
(`radio.toml.example` byte-identical, canary unmoved). A corrupt-packet decode still returns an empty
frame (a wire error the pump skips) — only the *idle* path fills silence.

**Bench acceptance (operator, not run headless):** over Mumble on the kv4p, someone keys the frequency
and stops — the tail no longer repeats in the Mumble app. AIOC behaviour unchanged.

**Cadence note for the bench:** the firmware sends nothing when idle, so the continuity silence is
produced at the `receive()` idle-timeout cadence (`DEFAULT_RECEIVE_TIMEOUT` = 0.1 s), not real-time —
enough to break the *sustained* loop. If the taper isn't smooth enough, lowering
`DEFAULT_RECEIVE_TIMEOUT` toward the 40 ms frame interval is a follow-up (kept at 0.1 s so a healthy
signal's inter-packet jitter never trips the timeout mid-signal).

**Non-goals:** no TX change (ADR 0082 owns kv4p→RF), no AIOC change, no `mumble.tx_hang` change, no new
backends.

## A fixed over-RF login code option + a collapsible Mumble panel (ADR 0083) (2026-07-18)

**Settings-screen cycle: a UI tidy + an opt-in auth mode. No hardware, no keying.** PR #135 (ADR
0082) is merged (`origin/master` tip `4e41b4b`); branched fresh (`settings-fixed-login-code`), not
stacked. Two operator asks on the Settings screen. (A third — "surface toml settings not on screen" —
was withdrawn by the operator as a mistake; all scalar settings already render, some behind the
Advanced fold.)

**1. Mumble servers panel now folds like the rest.** `web/src/components/MumbleServersPanel.jsx` — the
bespoke `<section className="card">` became the same `<details className="settings-group">` /
`<summary>Mumble servers <span className="settings-group-count">N servers</span></summary>` /
`.settings-group-body` shape the schema `GroupPanel` uses (CSS already existed). Native `<details>`,
no state added. `SecretsPanel` left as an open card (holds set-up actions; only Mumble was asked).

**2. A fixed 6-digit over-RF login code (opt-in, non-default, warned).** Auth is now a derived mode —
off / TOTP / fixed:
- **`auth.fixed_code`** (new bool setting, default false; `spec.py`, `auth` group, NOT advanced) beside
  the unchanged `auth.totp_enabled` gate. Description carries the security warning. Canary 64→65;
  `radio.toml.example` regenerated.
- **`radio_server/auth/fixed.py`** — `FixedCodeVerifier`: same `verify_and_burn(code, now)` surface
  `AuthGate` consumes, constant-time compare, **no burn** (a fixed code is reused → replayable: the
  documented downgrade). Exported from `radio_server/auth/__init__.py`.
- **Wiring:** `build_controller` gains `fixed_code=` and picks `FixedCodeVerifier` vs `TotpVerifier`
  by `load_fixed_code_enabled(settings)`; new `Controller.auth_method` ("fixed"/"totp"). `build_app`'s
  controller-build gate also builds in fixed mode when a code is set (byte-identical when off). The
  code is a **secret** (`fixed_code` / `RADIO_FIXED_CODE`, `config/secrets.py`), never in `radio.toml`.
- **API:** `POST /settings/secrets/fixed-code` (write-only, 6-digit-validated, `api/settings.py`);
  `_secrets_presence` reports `fixed_code` set/unset; `GET /auth/totp` returns `{enforced:true,
  fixed:true}` in fixed mode and **never** echoes the code (503 if selected-but-unset).
- **UI:** `SecretsPanel` gains a write-only 6-digit **Fixed login code** control + inline warning;
  `TotpCard` shows a locked "fixed code" chip (no rotating code); `api.js` `setFixedCode`.

**Verified:** `uv run pytest` → **1097 passed, 5 skipped** (+17: `test_fixed_code.py` verifier+build,
fixed-mode `/auth/totp`, the settings endpoint + presence, secret round-trip). `cd web && npm test` →
**19 passed** (+ `SecretsPanel`/`TotpCard`/`MumbleServersPanel` suites); `npm run build` green. Canary
64→65; `radio.toml.example` regenerated (golden green).

**Docs updated:** `configuration.md` (fixed-code how-to + warning), `using-it.md` (login-code note),
`operating.md` (security implication — no burn, replayable), `api.md` (the new endpoint + `/auth/totp`
`fixed` field), ADR 0083 + index row, this note.

**Non-goals:** no change to TOTP behavior when `auth.fixed_code` is off (existing configs unchanged),
no `auth.totp_enabled` rename/migration, no CLI enrollment for the fixed code (UI/secrets-file/env
only).

## Keep the kv4p transmitter fed while keyed-but-idle — a TX pacer (ADR 0082) (2026-07-18)

**No hardware, no headless keying; built/tested against the fake transport + a deterministic clock.**
PR #134 (ADR 0081) is merged (`origin/master` tip `3047715`); branched fresh
(`kv4p-keyed-idle-silence`), not stacked.

**The bug (code-confirmed on both backends):** over Mumble via kv4p, the last ~0.5 s of speech
repeats at the end of each over (the "Max Headroom" loop) — not on the AIOC. The AIOC opens a
continuous sounddevice output stream that clocks silence out whenever `transmit()` isn't writing
(`aioc_baofeng.py::_key_on`), so its TX buffer never starves. The kv4p is frame-push: `transmit()`
sends Opus frames only when called. When the Mumble bridge (`link/bridge.py::_mumble_to_rf` →
`tx/session.py::TxSession`) holds `ptt(True)` across a `mumble.tx_hang` quiet window
(`DEFAULT_MUMBLE_TX_HANG = 0.8 s`) but stops delivering audio, the kv4p sends nothing while still
keyed, the SA818's TX buffer underruns, and the firmware loops its last content. The browser
`/audio/tx` talker (same `TxSession`) had the identical latent bug.

**What shipped:**
- **`backends/kv4p/pacer.py`** (new) — `_TxPacer`: owns the per-keying `TxAudioEncoder` + a bounded
  drop-oldest PCM jitter buffer. `enqueue(pcm)` (non-blocking) feeds it; a **daemon thread** calls
  `tick()` every ~40 ms (`FRAME_MS`) and sends **exactly one** frame per slot — real audio if a whole
  frame is buffered, else one **encoded-silence** frame (reuses the key-up lead-in's
  silence-through-the-encoder path; zeros are `tx_gain`-invariant). `stop()` joins the thread;
  `flush_tail()` (caller thread, post-join) drains the remainder + flushes the encoder tail. It is a
  **daemon thread** (not an asyncio task) because the pacer must fire while the bridge's async task is
  parked in `wait_for(..., timeout=tx_hang)` — the transport-reader-thread / AIOC-output-stream shape.
- **`backends/kv4p/radio.py`** — `ptt(True)` starts the pacer **after** `_key_on()` (so the
  synchronous lead-in never overlaps it); `transmit()` while keyed `enqueue()`s instead of pushing to
  the encoder inline; `ptt(False)` → `_key_off_streaming()` (stop → flush_tail → drop PTT). The
  one-shot path (`_key_on`/`push`/`_key_off`) is **unchanged** — it never holds the key idle, so it
  never starves. New `_drop_ptt()` factored out of `_key_off` so streaming doesn't double-flush.
- **The single coherent sender is the crux:** during a held key the pacer thread is the *only* thing
  pushing to the encoder / calling `send_tx_audio` (thread-safe via the credit window), so there is no
  encoder race and no doubled frame — exactly one frame per slot.
- **Tests:** `test_kv4p_radio.py` — pacer policy driven by direct `tick()` calls (silence each idle
  slot; sparse audio one-per-slot with a multi-frame gap that decays past Opus prediction; `tx_gain`
  on real audio, gain-invariant silence; sub-frame held until complete; bounded drop-oldest;
  `tick()` swallows `Kv4pTimeout`, stops on `Kv4pClosed`) + lifecycle through `Kv4pHt` (keyed-idle
  emits silence end-to-end; key-down flush + drop-PTT-once; clean restart). Updated the existing
  streaming test (audio now ships on the pacer/flush, not inline). `test_kv4p_transport.py` — 200
  silence slots through the **real** transport + credit window with modeled `WINDOW_UPDATE` refunds
  stay within `[0, window]`, never block, never time out.
- **Docs:** ADR 0082 (cross-refs 0064/0065/0069/0080 + AIOC 0029); ADR index row; this note.

**Verified (no hardware):** `uv run pytest` → **1080 passed, 5 skipped** (+20 new). **No schema
change:** `radio.toml.example` byte-identical, settings-count canary unmoved (no config surface — the
frame interval is a fixed protocol constant).

**Bench acceptance (operator, not run headless):** over Mumble on the kv4p, speak and stop — the tail
no longer repeats; confirm a mid-speech pause is clean and that AIOC behaviour is unchanged.

**Next / open items unchanged:** kv4p DTMF bench acceptance; per-backend DTMF twist (ADR 0075); Opus
bitrate cap (ADR 0069); installer kv4p path; conditional Mumble-banner gate; the `Radio.close()`
protocol promotion / `ControllerRunner` removal (ADR 0073 deferrals); a JS-test CI step (ADR 0077).

## Removed the decorative frequency-dial scale from the control panel (ADR 0081) (2026-07-18)

**UI-only cycle, no hardware, no keying.** PR #133 (ADR 0080) is merged (`origin/master` tip
`f51f7f7`); branched fresh (`remove-frequency-dial`), not stacked.

**What & why:** the operator asked to drop the horizontal frequency-dial scale on the control
panel's "face" — the 144–148 MHz ruler + red needle (`DialScale`). It was decoration from the
ADR 0044 retro refresh: `aria-hidden="true"`, hard-coded to the 2 m band, and duplicating the
authoritative `FreqLcd` numeric readout right above it. Pure clutter, no function.

**What shipped:**
- **`web/src/components/ControlPanel.jsx`** — deleted the `DialScale` component and its
  `{showDial && <DialScale state={state} />}` mount. Kept `showDial` (`hasCap("set_frequency")`); it
  still gates the `FreqLcd`, which is unchanged.
- **`web/src/styles.css`** — deleted the `.dial*` block. Left `--tick`/`--ticksoft`/`--red` (used by
  other elements, e.g. the `.decor-dial` gate decoration).
- **Tests/build:** no test referenced the dial, so no test change; `npm test` (10 passing) and
  `npm run build` both green. No Python/server change.

**Non-goals:** no change to the numeric LCD, CAT tuning/scan cards, scanning, or any server-side
behaviour. Decoration removed only.

## The kv4p now has a TX audio-level control, `kv4p.tx_gain` (ADR 0080) (2026-07-18)

**Feature cycle, no hardware, no keying; RX-only-safe, built/tested against fakes.** PR #132 (ADR
0079) is merged (`origin/master` tip `ab797b8`); branched fresh (`kv4p-tx-gain`), not stacked.

**The symptom:** kv4p announcements/voice are **overmodulated**. The firmware TX path applies no
boost and the backend encodes near-full-scale TTS/CW to Opus with no attenuation, so it over-deviates
the SA818. The AIOC tames identical audio with `alsamixer`'s playback slider; the kv4p has no sound
card and no such stage — and no backend had a software TX-level knob.

**What shipped:**
- **`backends/kv4p/audio.py`** — `TxAudioEncoder` gains a `tx_gain` param and a pure
  `_apply_tx_gain(samples, gain)` helper applied in `push()` on the int16 samples **before** the Opus
  encoder (the one choke point every TX byte flows through). `gain == 1.0` is an exact int16 no-op;
  otherwise it multiplies and **clamps to ±32767** (so `>1.0` clamps, never wraps).
- **`backends/kv4p/radio.py`** — `DEFAULT_TX_GAIN = 1.0` (verify-on-bench, guardrail 1); a `tx_gain`
  constructor kwarg carried into the encoder built in `_key_on()`, so streaming and one-shot TX both
  inherit it.
- **`config/spec.py`** — `kv4p.tx_gain` (`coerce_positive_float`, matching `sample_rate_correction`;
  advanced tier). **`api/backend_config.py`** and **`doctor.py`** thread it through; both the initial
  build and the ADR 0076 live rebuild go via `build_radio → backend_kwargs`, so a switch honours it.
- **Tests:** `_apply_tx_gain` (0.5 halves, 1.0 exact no-op, 2.0 clamps not wraps); the encoder scales
  the pre-encode accumulator (sub-frame push, no libopus); the setting reaches the live encoder at
  key-up + defaults to unity; a one-shot transmit is attenuated **end-to-end** (decode the emitted
  Opus, energy halves); resolve/coerce + wiring. Canary 63 → 64; `radio.toml.example` regenerated.
- **Docs:** ADR 0080 (cross-refs 0076/0065/0070; notes AIOC needs no equivalent — OS mixer owns its
  TX level); ADR index row; configuration.md KV4P bullet ("overmodulated? lower it, ~0.5 start");
  this note.

**Non-goals:** no AIOC change, no limiter/AGC (plain gain), no RX change, no new backends.

**Note for the bench cycle:** the default is 1.0 (no change). The right level is a per-radio
deviation fact — set `kv4p.tx_gain` empirically until modulation is clean; ~0.5 is the documented
starting point.

## The over-RF auth session now persists across a backend switch (ADR 0079) (2026-07-18)

**Bug-fix cycle, no hardware, no keying; reproduced against fakes.** PR #131 (ADR 0078) is merged
(`origin/master` tip `9ef628e`); branched fresh (`persist-auth-session-across-switch`), not stacked.

**The bug:** a live backend switch (`POST /radio/select`, ADR 0076) logged out an authenticated
operator. `build_controller` minted a **fresh `Session()`** on every call
(`controller/engine.py`), and `holder.rebuild` → `controller_factory` → `build_controller` rebuilds
the controller on each switch — so a fresh, unauthenticated session replaced the live one. The auth
session belongs to the operator at the station, not the per-radio controller; it must outlive the
rebuild. (Same class as ADR 0078 — "a switch must preserve everything a fresh boot would set up" —
but for runtime session state, not config.)

**What shipped:**
- **`controller/engine.py`** — `build_controller` gains `session: Session | None = None`: use the
  passed one, else mint a fresh `Session()` (back-compat — direct callers/tests unchanged).
- **`api/app.py`** (`build_app`) — construct **one** `Session` and capture it in the
  `controller_factory` closure (alongside the stable service-bindings/mumble/plugins deps); pass it
  into every `build_controller` call. The same object flows into the initial build and every rebuild,
  so a switch injects the live session and its state + `last_activity` survive. `AuthGate` is still
  rebuilt fresh (stateless re: the session; re-wires to the new dispatcher/station ID).
- **Tests (`tests/test_backend_select.py`):** session survives a rebuild (same object, still
  authenticated, controller genuinely rebuilt); back-compat mints a fresh one; end-to-end
  (`POST /auth/session` → `POST /radio/select` → `GET /status` still `session_open: True`); the
  inactivity clock carries (near-timeout stays near-timeout, expires on schedule, not reset/extended);
  a mid-entry DTMF accumulation does not survive (per-controller framer). Verified the 4 behavioural
  tests FAIL without the engine fix and PASS with it.
- **Docs:** ADR 0079 (cross-refs 0076/0078); ADR index row; this note. No operator doc claimed the RF
  session lifecycle across a switch, so no correction was needed.

**Behaviour confirmed, not weakened:** Part 97 (guardrail 5) — the rebuilt `StationId` starts with
`_last_id=None`, so the **first** over on the new radio always carries the ID (errs toward ID-ing,
legal); the periodic-ID net doesn't fire until the new radio transmits. No `StationId` change.
Rationale for persisting across a LOCAL switch: auth over RF is "gated, not secure" (guardrail 4) —
same operator, same station, their own radio.

**Verified:** `uv run pytest` (full suite green: 1060 passed, +5 new tests). No schema change —
`radio.toml.example` byte-identical, settings-count canary unmoved at 63.

**No bench acceptance needed** — fully reproduced against a fake backend + wired controller. Operator
confirmation optional: authenticate over the air, switch AIOC↔kv4p in the browser, stay logged in.

**Next / open items unchanged:** kv4p DTMF bench acceptance; per-backend DTMF twist (ADR 0075 noted
it); Opus bitrate cap (ADR 0069); installer kv4p path; conditional Mumble-banner gate; the
`Radio.close()` protocol promotion / `ControllerRunner` removal (ADR 0073 deferrals); a JS-test CI
step (ADR 0077).

## A live backend switch dropped every local-plugin service — extra-channel loss (ADR 0078) (2026-07-18)

**Bug-fix cycle, no hardware, no keying; reproduced against fakes.** PR #130 (ADR 0077) is merged
(`origin/master` tip `507366c`); branched fresh (`fix-extra-channel-on-switch`), not stacked.

**The bug (confirmed on hardware + in code):** `POST /radio/select` dropped every local-plugin
(`[plugins.*]`) service from the live catalog until a restart. On the operator's box, switching
AIOC↔kv4p made weather/astronomy/quote/battery/bible vanish from the UI; a restart brought them back.

**Root cause:** the select handler rebuilt settings from **schema keys only** and called
`resolve_settings({**base, "server.backend": target})` with **no `extra=`** — so `new_settings` had an
empty extra channel (ADR 0051). `holder.rebuild` → `controller_factory(new_settings, …)` →
`build_controller(new_settings, …)` then gated every local plugin off (`enabled()` reads
`settings.extra("<name>.base_url")` → `""`), shrinking `controller.service_catalog`; `app.state.settings
= new_settings` propagated the stripped settings app-wide. Runtime-only — `save_settings` leaves the
on-disk `[plugins.*]` untouched, so a restart restores them, but every switch re-strips.

**Audit:** only the extra channel rides on `Settings`; `load_service_bindings`/`load_mumble_servers`/
`configured_backends`/`validate_configured_backends`/`backend_kwargs` are switch-safe (disk- or
schema-only). But the **identical idiom in `PATCH /settings`** had the same defect — any save stripped
the live plugins channel too. Both fixed.

**What shipped:**
- **`config/settings.py`** — new public `Settings.extras() -> dict` (the whole extra channel as a copy;
  `Settings` stays immutable). Was only reachable via private `_extra` / the per-key `extra(key)` getter.
- **`api/app.py`** (`POST /radio/select`) and **`api/settings.py`** (`PATCH /settings`) — both now pass
  `extra=current.extras()` through `resolve_settings`. `holder.rebuild`→`build_controller` already flow
  `new_settings` end-to-end, so the restored channel reaches the plugin gate and the catalog is whole.
- **Tests:** `test_config.py` (the `extras()` accessor + patch-idiom round-trip); `test_backend_select.py`
  (switch preserves the extra channel on `app.state.settings`; end-to-end — a controller wired to an
  extra-gated local plugin keeps its `GET /services` entry across a switch **and the switch back**);
  `test_settings_api.py` (PATCH preserves the channel). Verified the 3 endpoint tests FAIL without the
  fix and PASS with it.
- **Docs:** ADR 0078 (cross-refs 0076/0051); ADR index row; this note. `using-it.md`'s switch section
  never overclaimed service preservation, so no correction needed (the vanishing was a bug, not
  documented behavior).

**Verified:** `uv run pytest` (full suite green; +6 new tests). No schema change — `radio.toml.example`
byte-identical, settings-count canary unmoved at 63.

**No bench acceptance needed** — the failure and fix are fully reproduced against a fake backend +
MockRadio + a wired controller. Operator confirmation optional: switch AIOC↔kv4p in the browser and see
the local services stay in the panel without a restart.

**Next / open items unchanged:** kv4p DTMF bench acceptance; per-backend DTMF twist (ADR 0075 noted it);
Opus bitrate cap (ADR 0069); installer kv4p path; conditional Mumble-banner gate; the `Radio.close()`
protocol promotion / `ControllerRunner` removal (ADR 0073 deferrals); a JS-test CI step (ADR 0077).

## The backend selector in the web UI (ADR 0077) (2026-07-18)

**UI cycle, no server change, no keying.** PR #129 (ADR 0076) is merged (`origin/master` tip `cb51a4c`);
branched fresh (`backend-selector-ui`), not stacked. This is the web control panel consuming the ADR 0076
switch endpoints — the last user-facing piece of switching radios in the app.

**What shipped:**
- **Reactive capabilities (the crux)** — `web/src/useEvents.js`: `reduceStatus` gains
  `case "capabilities" → {...prev, caps: data.capabilities}` (and is now `export`ed for unit tests), so
  the ADR 0076 re-emit becomes reactive `state.caps` instead of being silently dropped.
  `web/src/components/ControlPanel.jsx`: `advertised = new Set(state.caps ?? caps)` — prefers the reactive
  set over the one-shot login prop, so the CAT tuning/scan cards mount/unmount live on a switch **without a
  reconnect**; the additive `disabledCaps` (501 greying) clears on `[state.caps]` so the new radio isn't
  greyed by the old radio's 501.
- **The selector** — new `web/src/components/BackendPanel.jsx`, a `.card` in the left column built from the
  `ModeControl` `<select>`/`useAction`/`.error` idiom. Fetches `GET /radio/backends` on mount, **self-hides
  when <2 backends are configured**. Tracks the **live** active backend (`state.backend`), so a 503 (switch
  failed, server already rolled back) snaps the dropdown back and the error names the radio you're still on;
  `pending` → "Switching…"/disabled; a caption warns switching drops PTT while transmitting.
- **`web/src/api.js`** — `backends()` + `selectBackend(backend)` beside the Mumble-link methods (no new
  error mapping needed). **`web/vite.config.js`** — dev proxy gains `/radio` (the ADR 0076 endpoints
  predate any UI caller).
- **Bootstrapped Vitest** — the frontend had **no JS test runner** (browser-verified, no CI). Added
  vitest + @testing-library/react + jsdom, a `test` block in `vite.config.js`, `src/test-setup.js`, and an
  `npm test` script. Three suites (10 tests): `BackendPanel.test.jsx` (renders list w/ active marked,
  selects POSTs the backend, in-flight disabled + "Switching…", 503 snaps back, mid-TX warning),
  `ControlPanel.test.jsx` (caps re-emit re-greys the CAT cards both directions), `useEvents.test.js`
  (`reduceStatus` capabilities fold).

**Verified:** `cd web && npm test` → **10 passed**; `npm run build` builds `web/dist/`; `uv run pytest` →
**1050 passed, 5 skipped** (no server change). `web/dist` is gitignored, so the rebuild isn't committed.

**Bench acceptance (operator, two-radio box — not run headless):** in the browser, pick the other radio;
confirm the tuning/scan controls appear for the kv4p and vanish for the AIOC **without a reconnect**, the
face label follows, a forced-failure leaves the selector on the previous radio, and the selection survives a
restart (ADR 0076 persists `server.backend`). Both backend blocks must be present in `radio.toml`.

**Next / open items unchanged:** kv4p DTMF bench acceptance; per-backend DTMF twist (one box now runs two
radios — ADR 0075 noted it); Opus bitrate cap (ADR 0069); installer kv4p path; conditional Mumble-banner
gate; the `Radio.close()` protocol promotion / `ControllerRunner` removal (ADR 0073 deferrals). A JS-test CI
step could now run `npm test` where before there was nothing to run.

## The live backend switch (ADR 0076) (2026-07-18)

**Endpoint + API cycle, no keying; tested against fakes.** PR #128 (ADR 0075) is merged (`origin/master`
tip `3790851`); branched fresh (`radio-backend-select-live`), not stacked. This wires the ADR 0073
holder seam + ADR 0074 `configured_backends()` into a live switch. **No UI** — the dropdown is next.

**What shipped:**
- **`api/holder.py`** — `RadioHolder.rebuild(new_settings)`: atomic under a new `asyncio.Lock`; runs
  `stop() → radio_factory(new_settings) → start()`. `start()` rebuilds the controller via a
  `controller_factory` (because `stop()` reaps it and it captures the radio). **Rollback** is the
  load-bearing case: if the target fails to construct/open, it reconstructs+restarts the *previous*
  backend and re-raises (the old radio was closed by `stop()`, so restore rebuilds fresh). Two injected
  factories added to `__init__` — `radio_factory` (default `build_radio`; fakes injectable) and
  `controller_factory` (default `None`) — both defaulted so the DI seam is unchanged.
- **`api/events.py`** — new `"capabilities"` event type + `capabilities_event(radio)` helper
  (`data.capabilities` = sorted cap strings, mirroring `GET /capabilities`).
- **`api/app.py`** — inside `create_app`: `POST /radio/select {backend}` (409 if not in
  `configured_backends`; `resolve_settings` patch; `holder.rebuild`; 503 + previous backend on failure;
  then `save_settings` write-back, `nonlocal`-rebind of `radio`/`rx_pump`/`scan_runner`/`controller` +
  the matching `app.state.*`, `if rx_demand>0: rx_pump.start()`, and re-emit `capabilities`+`status`)
  and `GET /radio/backends` (`{active, active_capabilities, backends:[{name,active,settings}]}`).
  `create_app` gained `controller_factory`/`radio_factory` kwargs; `build_app` builds the controller
  through the factory (same totp/secret gate) and forwards it.
- **The `nonlocal` rebind is the ADR 0073-deferred "routes read holder.radio live" step** — every
  late-binding closure (`_require_cat`, `get_capabilities`, `_acquire_rx`/`_release_rx`, the scan
  routes, the Mumble bridge's `rx_active`) then follows the new radio with no per-handler edits.
- **Tests** — `test_radio_holder.py` +4 (swap, rollback, lock-serializes, controller-rebuild, all
  against fakes keyed on `server.backend`); new `test_backend_select.py` +6 (select 200 + caps change,
  409 unconfigured untouched, 503 rollback + config unwritten, `capabilities` re-emit over `/events`,
  persistence round-trip preserving the rest of the file).

**Persistence decision (recorded in ADR):** a live switch **writes `server.backend` back** through the
schema on success only, so a restart lands on the last-selected radio; the rest of `radio.toml` is
preserved (tomlkit round-trip).

**Verified:** `uv run pytest` → **1050 passed, 5 skipped** (1040 prior + 10 new). **No schema change:**
`radio.toml.example` byte-identical (golden green, no regen), settings-count canary unmoved.

**Bench acceptance (operator, two-radio box — not run headless):** `POST /radio/select` to flip
AIOC→kv4p and back; confirm RX audio follows the newly-selected radio and the `capabilities` payload
changes; record switch latency both directions (kv4p reboots on open — a beat is expected, not a
failure). RX-only, no dummy load needed for the select itself.

**Next (the UI cycle):** the backend dropdown consuming `GET /radio/backends` + `POST /radio/select`;
frontend consumption of the `capabilities` event (a `reduceStatus` case + lift `caps` out of the
one-shot `session.caps` prop so controls re-grey live). Open items unchanged: kv4p DTMF bench
acceptance; per-backend DTMF twist (now that one box runs two radios — ADR 0075 noted it); Opus bitrate
cap (ADR 0069); installer kv4p path; conditional Mumble-banner gate; the `Radio.close()` protocol
promotion / `ControllerRunner` removal (ADR 0073 deferrals).

## Configurable DTMF reverse-twist tolerance (ADR 0075) (2026-07-18)

**Decoder + config cycle, no hardware, no keying.** PR #127 (ADR 0074) is merged (`origin/master` tip
`6e4e1e9`); branched fresh (`dtmf-reverse-twist-config`), not stacked.

**Why (bench-confirmed on real hardware):** same AIOC backend + same native decoder, a UV-5R decodes
DTMF fine but a **UV-5R Mini decodes nothing**. Replaying both captures through the real
`GoertzelStream`: the Mini's tones are on-frequency and above the energy floor, but its **low group
runs ~6.4 dB hotter than the high** (median reverse twist −6.4 dB), tripping the hardcoded −4 dB
`NATIVE_REVERSE_TWIST_DB` gate on 172/176 blocks; the UV-5R sits at −0.1 dB. The real `mini.wav`
decodes at a 10 dB limit, garbles at 8, nothing at 4 (UV-5R still fine at 10). Talk-off holds at 10 dB
— dominance + second-harmonic gates carry it, not twist — so widening reverse twist is talk-off-safe.

**What shipped (opt-in, default unchanged):**
- **`audio/dtmf.py`** — `GoertzelStream.__init__(reverse_twist_db=NATIVE_REVERSE_TWIST_DB)` computes
  `self._reverse_twist = 10.0 ** (reverse_twist_db / 10.0)` (power ratio — Goertzel `power` is
  magnitude-squared, so dB/10 not dB/20). `NATIVE_REVERSE_TWIST_DB` stays 4.0 as the fallback constant.
  New loader `load_dtmf_reverse_twist_db(settings)` beside the other DTMF loaders (re-exported from
  `audio/__init__.py`).
- **`config/spec.py`** — new `audio.dtmf_reverse_twist_db` (`RADIO_DTMF_REVERSE_TWIST_DB`,
  `coerce_positive_float`, default = imported `NATIVE_REVERSE_TWIST_DB`), in the `audio` group and
  `_ADVANCED_KEYS`. Settings-count canary 62 → **63**; `radio.toml.example` regenerated.
- **`controller/engine.py`** — native decode path builds
  `GoertzelStream(reverse_twist_db=load_dtmf_reverse_twist_db(settings))`.
- **`doctor.py`** — listen path threads the loaded value too (defaults to 4.0 if config read fails, in
  the existing `try/except`), so the diagnostic honors the override.
- **Tests (`tests/test_native_dtmf.py`)** — synthesized −6.4 dB Mini-profile tone **fails at 4.0,
  decodes at 10.0** (`1234#`); default-equals-constant preservation check; talk-off holds at the wide
  10.0 gate (12 white-noise seeds, a chirp sweep, off-grid/same-group tone pairs). The rest of the DTMF
  suite is unchanged — that's the proof the 4.0 default preserves every existing decode.
- **Docs** — ADR 0075; `configuration.md` + `troubleshooting.md` ("DTMF works on one radio but not
  another") entries; ADR index row.

**Deliberate scope:** reverse twist only (no forward-twist problem seen; could mirror this later);
**global**, not per-backend (revisit once the backend-switch arc lets one box run two radios). The
default stays 4.0 — the Mini is non-spec; compliant radios keep the tighter, talk-off-safe gate.

**Verify:** `uv run pytest` (full suite green), or focused
`uv run pytest tests/test_native_dtmf.py tests/test_config.py tests/test_settings_api.py -q`.

## radio.toml describes more than one backend (ADR 0074) (2026-07-18)

**Config-model cycle, no hardware, no keying.** PR #126 is merged (`origin/master` tip `0155068`);
branched fresh (`multi-backend-config`), not stacked. Builds on the ADR 0073 holder seam. **The user
chose the presence-based model** (over an explicit `server.backends` list) — lighter, no schema change.

**Why:** the holder can now be stopped/rebuilt (ADR 0073), but the config is still single-backend —
`server.backend` names one and the other `[<backend>]` block is inert. Per-key coercion already runs
for every block, but the *cross-field* validation (the two `audio.squelch=cat` guards; the kv4p
frequency band check) only ran for the *active* backend. So a config could carry a broken *other*
block that nothing notices until someone selects it live (the ADR 0051 "latent config surfaces on a
restart" lesson). This cycle moves that failure to load time. **No switching yet** — that's next.

**The model (presence-based):** a backend is *configured* if its `[<backend>]` block is present in
`radio.toml` (any `baofeng.*`/`kv4p.*` key), plus the active `server.backend` (always configured — it
boots from defaults). `server.backend` is the *initial* selection, not the only permitted one. A
single-block config is unchanged: only `[baofeng]` → only baofeng validated/enumerated.

**What shipped:**
- **`config/settings.py`** — presence captured during resolution (it can't be recovered later: every
  backend key has a default). `Settings.configured_backend_names() -> frozenset[str]`; derived
  `BACKEND_BLOCK_GROUPS = {spec groups} ∩ available_backends()` = `{baofeng, kv4p}`.
- **`api/backend_config.py`** (new, light — no pipeline imports, so `doctor` imports it cheaply):
  `backend_kwargs` (the settings→ctor mapping extracted verbatim from `build_radio`'s switch);
  `validate_backend_config(settings, backend, *, include_construction_checks)` — pure, no construction
  (constructing a hw backend opens serial / v71 raises); `validate_configured_backends` (validates
  every configured backend **except the active one**, which stays validated as before);
  `configured_backends() -> tuple[BackendChoice, ...]` enumeration (active first, each with resolved
  kwargs) for the next cycle's select endpoint + UI — **no caller yet**, shape defined per the task.
- **`api/holder.py`** — `build_radio` reuses the extracted helpers; behaviour byte-identical, and it
  still looks `create_radio` up locally so `test_backend_wiring`'s monkeypatch target is unchanged.
- **`api/app.py`** — `validate_configured_backends(settings)` right before `build_radio`.
- **`doctor.py`** — `_validate_doctor_backend_config` validates the selected backend loudly against
  the real `radio.toml` (`include_construction_checks=True`, so an out-of-band `kv4p.frequency`
  surfaces even with no hardware). Validation stays **out of** `resolve_settings`/`load_settings` on
  purpose: doctor wraps every settings read in `try/except`, so a raising loader would be swallowed and
  regress the ADR 0069 "read the real file" fix.
- **`backends/kv4p/radio.py`** — pure `default_freq_range_hz(band)` for the load-time band check.

**The validation split (the behaviour-preservation key):** the active backend is validated exactly as
before (squelch guard in `build_radio`; frequency at construction, HELLO-aware). Only the *inactive*
present blocks get the added pure checks (`include_construction_checks=True`) — they are never
constructed. This is why `test_kv4p_backend_passes_every_setting_through` (uhf + a VHF `146520000`)
stays green: the active backend skips the load-time band check.

**Deliberately stricter (called out in the PR):** a config that names both blocks AND sets
`audio.squelch=cat` while the *inactive* block is baofeng now fails at load (baofeng+cat is invalid) —
where before the stray block was ignored. Presence-scoped, so single-backend configs are unaffected.

**Verified:** `uv run pytest` → **1028 passed, 5 skipped** (1015 prior + 13 new `test_multi_backend.py`:
presence, invalid-inactive-fails-loud for both squelch and frequency, both-blocks-valid builds,
single-block back-compat, the active/construction validation split, the enumeration surface, and two
doctor validation tests). **No schema change:** `radio.toml.example` byte-identical (golden test green,
no regen), settings-count canary unmoved at 62. Behaviour of the active backend byte-identical
(`test_backend_wiring` green).

**Next (the swap cycle):** `RadioHolder.rebuild(new_settings)` + a `POST` select endpoint (consuming
`configured_backends()`) + the UI dropdown; make the routes read `holder.radio` live. Then per-backend
live capabilities (require construction — a note in ADR 0074). Other open items unchanged: kv4p DTMF
bench acceptance, Opus bitrate cap (ADR 0069), installer kv4p path, conditional Mumble-banner gate.

## A radio-holder seam for a swappable active radio (ADR 0073) (2026-07-18)

**Pure behaviour-preserving refactor, no hardware, no keying.** PR #125 is merged (`origin/master` tip
`dafb80c`); branched fresh (`radio-holder-seam`), not stacked. **This cycle's PR: #126.** (The authoring
machine crashed after the commit was pushed but before the PR was opened, corrupting the *local* git
object store; the commit itself was safe on `origin`. The local repo was repaired from the remote —
re-fetch of the pushed objects, `git fsck` clean — the suite re-run green (1015/5), and #126 opened.
No content change from the pushed commit.)

**Why:** the app was single-radio to the bone — `build_app` built one `radio` and threaded it into `RxPump`,
`TxSession`, the DTMF controller, `ScanEngine`, and the ID paths, with teardown scattered across the lifespan.
Live backend switching is impossible until one object owns the radio + its pipeline and can stop/restart them.
This cycle adds that seam and nothing else. "Make the change easy, then make the easy change."

**What shipped (new `radio_server/api/holder.py`):**
- **`build_radio(settings)`** — the `server.backend` switch + the two squelch fail-loud guards, extracted
  verbatim from `build_app` (which now just calls it). Lives at the composition root, not `backends/factory.py`
  (backends stay Settings-free). The one config→radio path the swap cycle reuses.
- **`class RadioHolder`** — owns the active radio (`.radio`) and the radio-bound pipeline lifecycle.
  `start()` (sync, idempotent) constructs `RxPump` + `ScanRunner` against the radio (with the two hub-publish
  adapters now owned by the holder); it starts **no** task (pump is demand-started, scan is plan-started).
  `stop()` (async, idempotent, fail-safe) tears down in the proven order: drop PTT **if `arbiter.transmitting`**
  → `scan_runner.stop()` → `rx_pump.stop()` → `controller.close()` (guarded) → `radio.close()` (getattr-guarded;
  V71 has none).
- **`create_app`** builds the holder, calls `holder.start()`, and **rebinds its locals** `rx_pump =
  holder.rx_pump` / `scan_runner = holder.scan_runner` (the demand-counter, the Mumble bridge's `rx_active`,
  and the scan routes close over those locals). `app.state.{holder,radio,rx_pump,scan_runner}` all point at the
  holder's instances. The lifespan's radio-bound teardown block is now `await app_.state.holder.stop()`, with
  `link.disconnect()` kept before it and recorder/event-log teardown after.

**Findings recorded in ADR 0073 (surfaced, not papered over):** (a) no single app-level "is keyed" flag — PTT
drop is fragmented across per-connection `TxSession`, the direct `POST /ptt` path, and the Mumble bridge, so
`stop()` keys down **conditionally on `arbiter.transmitting`** (an unconditional drop was tried and rejected:
it changed observable teardown keying and broke 5 keying-contract tests; the conditional form preserves
behaviour and still guarantees an arbiter-holding session can't latch TX across a swap); (b) `Radio` protocol
has no `close()` (V71 gap) → `getattr` guard; (c) the station-ID "scheduler" isn't a stoppable object (ID is
clock-driven inline); (d) the controller has no self-owned task (`ControllerRunner` vestigial).

**Verified:** `uv run pytest` → **1015 passed, 5 skipped** (prior 1008 + 7 new `test_radio_holder.py`). Behaviour
identical: `create_app`'s signature, the `app.state.*` names tests read, and every route's `radio` binding are
unchanged. `test_backend_wiring` now patches `create_radio` where `build_radio` looks it up (switch moved
modules; asserted wiring identical).

**Next (the swap cycle, explicitly deferred here):** `RadioHolder.rebuild(new_settings)` + a select endpoint +
multi-backend config + UI; make the routes read `holder.radio` live (so a swapped radio propagates); optionally
protocol-ize `close()` and remove the vestigial `ControllerRunner`. Other open items unchanged: kv4p DTMF bench
acceptance (`doctor --backend kv4p --dtmf`, operator RX-only); per-block DTMF normalization; Opus bitrate cap
(ADR 0069); installer kv4p path; conditional Mumble-banner gate (ADR 0067).

## kv4p DTMF found & fixed — the decoder's energy floor was ~10× too high for received audio (ADR 0072) (2026-07-18)

**RX-only cycle, no keying.** PR #124 is merged (`origin/master` tip `118ee17`); branched fresh
(`kv4p-dtmf-energy-floor`), not stacked.

**Why:** DTMF still didn't decode on kv4p after 0070 (sample rate) and 0071 (capture). Task: *stop
analysing, reproduce in a test, fix from what it shows.* The frame-size lead (kv4p 1882/1920 vs AIOC 960
against the 205-sample block grid) was **wrong**.

**What the reproduction showed** (feeding the operator's real `cap.wav` — the 0071 `--rx-capture` output
— through the live decode path):
- The analyzer reads `1234#` fine; the live `StreamingDtmfInput(GoertzelStream())` reads `''`.
- Not frame size: clean synth decodes at 960/1920/1882/705/441; the real capture decodes `''` at every
  size *and* as one whole-stream write (so not the per-frame `to_multimon` seams either).
- **Every block fails the energy floor.** 468/468 blocks below `NATIVE_ENERGY_FLOOR` (0.02); zero reach
  the dominance/twist/harmonic gates. Strongest tone block: low ≈937 Hz power **0.0123** (below floor),
  high ≈1483 Hz **0.0281**. Scaling the audio ×2 makes it decode `1234#` cleanly → **purely level**.

**Root cause:** `NATIVE_ENERGY_FLOOR = 0.02` is an *absolute* threshold tuned to the 0.4-amplitude synth
fixtures (power ~0.039). Real received DTMF lands ~10× quieter (~0.012). The same UV-5R decodes into
AIOC only because that cable is hotter. This is the level analogue of 0070's exact-frequency blind spot,
and closes the ADR 0060 / this-file "RX level vs `NATIVE_ENERGY_FLOOR`" item.

**What shipped (one constant + regressions, all hardware-free):**
- **`NATIVE_ENERGY_FLOOR` 0.02 → 0.002** (`radio_server/audio/dtmf.py`), with a marked comment recording
  the measured basis. Talk-off is preserved by the **scale-invariant ratio gates** (dominance 4×, twist,
  2nd-harmonic 4×), not the floor — full-scale white noise stays clean to 0.001 (12 seeds), so 0.002
  keeps a 2× guard. No kv4p-side gain: the decoder is the fix.
- Regressions in `tests/test_native_dtmf.py`: **received-level decode** (quiet clean `1234#` decodes;
  a monkeypatched-0.02 twin proves the old floor ate it), **frame-size invariance** (960/1920/1882/441/
  705 all decode), **12-seed talk-off** guard.
- **Realigned the 0070 offset regression** to a received level (`_RECEIVED_AMPLITUDE = 0.15`): at the
  loud 0.4 fixture level the scalloped off-bin tone clears the *new* floor for digits 1/4, so "offset
  never decodes" only holds at a realistic (quiet) level — where both fixes are genuinely load-bearing.

**Verified:** `uv run pytest` → **1008 passed, 5 skipped**.

**Bench acceptance (operator, RX only — the arbiter):** `doctor --backend kv4p --dtmf`, key `1234#` from
a handheld → digits decode. The offline reproduction over the real capture predicts this passes. This is
the last item before a working node. (`cap.wav` was a local artifact, not committed; its numbers live in
ADR 0072.)

**Deferred (not started without a new task):** per-block normalization / relative floor (fully
level-invariant detection; needs voice-corpus talk-off validation); Opus bitrate cap (ADR 0069);
installer kv4p path; conditional Mumble-banner gate (ADR 0067).

## kv4p DTMF still fails after 0070 — capture the RX audio and read the tones (ADR 0071) (2026-07-18)

**RX-only cycle, no keying.** PR #123 is merged (`origin/master` tip `953ce00`); branched fresh
(`kv4p-rx-capture`), not stacked.

**Why:** DTMF still doesn't decode on kv4p after the ADR-0070 sample-rate fix. Bench state (operator):
true ADC rate measured 48,759 Hz → `kv4p.sample_rate_correction = 1.0158`; signal strong (loudest block
17312); the same UV-5R that always decodes into the AIOC decodes nothing into kv4p. Analysis has been
wrong three times, so this cycle **stops analysing and instruments a direct capture**: read the DTMF
tones out of the actual received audio with an FFT, independent of GoertzelStream.

**What shipped (all hardware-free, tested against fakes):**
- **`doctor --backend kv4p --rx-capture`** — records N s of `receive()` (the corrected 48 kHz the
  decoder sees) to a WAV (`--out`, default `kv4p-rx-capture.wav`) while the operator keys `1234#`, then
  analyses it. `--analyze-wav PATH` re-runs the analysis on a saved WAV (no radio). Never keys.
- **`analyze_dtmf_windows` / `format_dtmf_analysis`** — per ~100 ms window: Hann-FFT, strongest low/high
  band peaks with parabolic sub-bin interpolation, snapped to the DTMF grid → digit, plus clip fraction
  and loudest peaks. Ends in a **verdict** (clipping checked first, since a clipped dual-tone still
  shows its fundamentals): (1) **CLIPPING** → the firmware's **16× RX gain** (`rxAudio.h` `Boost(16.0)`)
  saturates a strong dual-tone, breeding harmonics/intermod that trip the decoder's gates → upstream;
  (2) **off-frequency** → correction still wrong; (3) **on-frequency & clean** → decode-path wiring;
  (0) **absent** → mangled upstream (firmware filter / SA818 / RF).
- **`--rx-level` verdict tightened** to **0.2 %** (`_RATE_MATCH_TOL`) — the old 0.5 % gate wrongly
  called the bench's 1.0158-vs-1.02 (0.4 %) "dialed in"; now it flags the gap and prints the value to set.
- `MockRadio` gained a no-op `close()` (faithful double for the open-then-close diagnostics).

**Firmware RX chain read (`3f0e809` `rxAudio.h`, the leading suspect):** order is
`dcOffsetRemover → gain → afskTapEffect → mute` before Opus. `Boost gain(16.0)` = a 16× stage (the
clipping suspect); `DCOffsetRemover` is a one-pole HPF well below 697 Hz (harmless); the AFSK tap
returns its input unmodified (passive); `mute` is a squelch-gated `Boost(0.0)`. The 16× gain is the
concrete, testable hypothesis — hence the analyzer surfaces the clip fraction.

**Verified:** `uv run pytest` → **1000 passed, 5 skipped** (+ analyzer verdict tests for clean/clipping/
off-frequency/silence, WAV round-trip + bad-format reject, `--rx-capture` writes+analyses / no-audio
fail, and the tightened rate verdict).

**Bench acceptance still open (operator, RX only — the arbiter):** run
`doctor --backend kv4p --rx-capture --seconds 12 --out cap.wav` while keying `1234#`; paste the verdict
and the per-window dominant frequencies into the PR. Then apply the fix the verdict names (attenuate if
clipping; re-trim if off-frequency; decode path if clean). `--dtmf` decoding `1234#` is still done.

**Follow-up (not this cycle):** whatever the WAV names — an RX attenuation stage (if clipping), a
correction re-trim (if off-frequency), or a decode-path fix (if clean).

## kv4p RX sample-rate correction — the firmware `*1.02` offset that broke DTMF (ADR 0070) (2026-07-18)

**RX-only cycle, no keying.** PR #122 is merged (`origin/master` tip `72b0b00`); branched fresh
(`kv4p-rx-sample-rate`), not stacked.

**Root cause (verified in the pinned firmware `3f0e809`, guardrail 1):** `rxAudio.h` sets
`config.sample_rate = AUDIO_SAMPLE_RATE * 1.02` — the RX ADC runs ~2 % fast (≈48960 Hz) — while the
Opus encoder is told the unmultiplied 48000. So received audio arrives ~2 % off and mislabelled
48 kHz. That knocks every DTMF tone off its Goertzel bin (spacing ~39 Hz; 1633 Hz moves ~33 Hz), which
is why the codec, level, and clipping all tested clean while DTMF never decoded — every DTMF test used
*exact* frequencies. `globals.h` `SAMPLING_RATE_OFFSET 0` and `txAudio.h` confirm **TX is clean; the
offset is RX-only.** Wider than DTMF: it's a ~1.2 s/min clock drift for the hub, recorder, and Mumble
link too. (Irony: PR #118 deleted the kv4p soxr resamplers — true of the codec, false of the ADC.)

**The fix:**
- `RxAudioDecoder` (backends/kv4p/audio.py) resamples the true device rate → 48000 with a stateful
  **soxr `ResampleStream`, HQ** (the ADR-0054 `GoertzelStream` precedent, not the VHQ latency trap).
  `sample_rate_correction=1.0` is a byte-for-byte pass-through, so the generic decoder is unchanged.
- Config knob **`kv4p.sample_rate_correction`** (default **1.02**, marked verify-on-bench), threaded
  through `api/app.py` and `doctor._build_backend`; `radio.toml.example` regenerated (golden test green).
- doctor **`--rx-level` prints the measured true rate** = `fps × 1920` (invariant to the correction —
  the device emits one 1920-sample packet per 1920 ADC samples) and the implied correction; advise
  `--seconds 30` so USB jitter averages out.
- Also: **`DEFAULT_CONNECT_TIMEOUT` 2.0 → 10.0** (transport.py) — the reset-on-open board races its
  ~1 s boot and 2 s intermittently lost the elicit (ADR 0069's deferred first-connect item).

**Verified (no hardware for the suite):** `uv run pytest` → **989 passed, 5 skipped** (+ new tests: the
DTMF offset regression `1234#` fails-then-decodes, `RxAudioDecoder` pass-through / corrected-length /
empty-chunk, the correction reaches the decoder, `kv4p.sample_rate_correction` parse+reject, and
`_format_kv4p_rx_rate`). The rig was tested against fakes; no keying, no hardware.

**Bench-verify still open (operator, RX only — the last item before a working node):** run
`doctor --backend kv4p --rx-level --seconds 30` on 445.800 to read the measured rate and trim
`kv4p.sample_rate_correction`; then `doctor --backend kv4p --dtmf` and key `1234#` from a handheld — the
digits should decode. Record both in the PR.

**Follow-ups (unchanged):** Opus bitrate cap (ADR 0069); installer kv4p path + conditional
Mumble-banner gate (ADR 0067).

## kv4p TX bring-up — telemetry rig + first bench keying (ADR 0069) (2026-07-18)

The transmit side keyed hardware for the first time (dummy load, **445.800 MHz**, UHF, second
receiver). PR #121 is merged (`origin/master` tip `024021a`); branched fresh (`kv4p-tx-bringup`), not
stacked. Shape: **instrument (no keying, tested against fakes) → operator keys live → fold the real
numbers in** — the operator did the keying; the RF guards (non-interactive refusal, typed CONFIRM,
2 s/5 s caps) were kept verbatim.

**The rig (Phase 1, no hardware):**
- `transport.TxStats` — per-keying counters under the credit lock: encoded Opus bytes/frame
  (`send_tx_audio`), on-wire escaped bytes, `blocked_frames`, `min_credits` (`_write_frame`). Exposed
  as `Kv4pTransport.tx_stats`/`window_size`; `reset_tx_stats()` at each `_key_on`. Surfaced on `Kv4pHt`.
- doctor: key-up latency in `_kv4p_keying_core`; pure `_format_tx_stats()` printed by `--tx-tone`;
  kv4p-specific no-tone hint (dropped the stale AIOC "alsamixer" text); `--tx-lead SECONDS` sweep knob.
- New runbook `docs/kv4p-tx-bringup.md` (linked from `kv4p-setup.md` + `hardware-bringup.md`).

**Two bugs the bench surfaced (fixed this cycle):**
1. **Doctor never read `radio.toml`.** Every `load_settings()` in `doctor.py` passed no path →
   pure defaults. So a keying test used the **default** serial port/band — and on this bench
   `/dev/ttyUSB0` is a *DV Dongle*, the kv4p is on `ttyUSB1`, and `kv4p.frequency` has **no CLI flag**,
   so 445.800 was unreachable. Fix: `_doctor_settings()` reads `DEFAULT_CONFIG_PATH` (all backends).
2. **Keying modes gave no next step on a connect failure.** The first `--key-test` lost the elicit
   handshake (first-connect/reset-on-open race, ADR 0066) and printed only the raw error. Fix: point
   at the non-keying connect probe (`_print_kv4p_open_hint`).

**Bench numbers (2026-07-18, guardrail-1 facts now measured):**
- **It keys** — `TX_ACTIVE` confirmed, clean unkey; `TX_ALLOWED` gate works. **Key-up ≈ 103 ms.**
- **Clean 1000 Hz tone on a monitoring receiver.**
- **Encoded ≈ 230 B/frame** (min 5, max 245), ~25 fps ≈ 46 kbps.
- **Window 2048 B (HELLO-confirmed)**, ~8.6 frames; a one-shot 3 s clip **blocked 28/80** (min credits
  15) and recovered — **healthy backpressure** (host produces faster than the device drains at real
  time), never neared the 2 s write timeout, audio clean.
- **`tx_lead_seconds`: 0.2 clipped, 0.5 clean** → `DEFAULT_TX_LEAD_SECONDS = 0.5` (spec +
  `radio.toml.example` regenerated; the golden test enforces the match).

**Verified (no hardware for the suite):** `uv run pytest` → **972 passed, 5 skipped** (+11 new tests:
transport `tx_stats`/blocked/reset, `_format_tx_stats`, key-up latency, `--tx-tone` hint, `--tx-lead`
override, the `_doctor_settings` regression, and the connect-hint). Live keying done by the operator.

**Follow-ups (not this cycle):** first-connect reliability (lengthen connect timeout / boot-settle
retry — ADR 0066 territory); an Opus **bitrate cap** to ease window pressure (~230 B/frame is high for
a tone — trades fidelity, needs its own analysis); DTMF-over-kv4p-RF measurement + installer kv4p path
+ conditional Mumble-banner gate (all still open from ADR 0067/0068).

---

## kv4p bring-up — two silent-failure detections + the user docs (ADR 0068) (2026-07-18)

Ships the kv4p user docs **and** the two doctor detections that make them mostly unnecessary — the
detection and the prose are the same fact. PR #120 is merged (`origin/master` tip `b62ff53`); branched
fresh (`kv4p-bringup-detections-docs`), not stacked. **No hardware, no keying** — both detections are
pure logic over injected wire data, verified through test seams.

**The two detections (`doctor.py::_kv4p_connect_probe`):**
- **Pre-KISS firmware.** On a failed handshake, `_sniff_pre_kiss_firmware(port)` re-opens the port
  (DTR/RTS low — the reset-on-open dumps the board's boot frames) and reports pre-KISS **only** on a
  positive tell: the `de ad be ef` delimiter present AND no KISS `FEND` (`0xC0`) AND no `KV4P` prefix.
  → *"this board is running pre-KISS firmware — flash v17"* pointing at `docs/kv4p-setup.md`. The
  delimiter is a **new marked constant** (`_PRE_KISS_DELIMITER`) — it was nowhere in the tree. Boot
  banner deliberately not used (exists in both firmwares).
- **Band mismatch.** When a HELLO is present, compare `RfModuleType(v.rf_module_type)` against the
  configured `kv4p.module_type` (via `module_type_from_band()`). On disagreement → a **WARN** (not a
  FAIL): *"band mismatch: board reports VHF, you configured UHF — the hwconfig NVS is probably
  missing/wrong; reflash the board-config"*. Catches a wiped/never-written board-config, invisible on
  the protocol otherwise.
- Also: fixed a stale "16 kHz ADPCM" phrase in the doctor module docstring (it's Opus now, ADR 0064/65).

**The docs (integrate, not bolt on):**
- **NEW `docs/kv4p-setup.md`** — flashing/first-run guide: two-writes-in-order (firmware `0x0` then
  board-config `0x9000`; the merged `0x0–0xeafff` image **wipes** the NVS, so firmware-only is the
  trap); six board-config images + reading the PCB silkscreen (v2.0e→v2.0d config); web-flasher
  port-lock (fully quit Chrome) with the `esptool` terminal escape; by-id path not `/dev/ttyUSB0`; run
  doctor first; set `kv4p.frequency` (no knob, no invented default); reset-on-open, ADR-0066 flag loss,
  and DTMF-is-an-open-bench-item all stated plainly.
- **`install.md`** forks by radio — kv4p path is `uv sync --extra kv4p` with **no** PortAudio/sound-card
  steps (the easier radio). **`troubleshooting.md`** early fork (kv4p has no volume knob, no capture
  level). **`configuration.md`** gains the `[kv4p]` section and owns the `kv4p.squelch` (SA818 level
  0–8) vs `audio.squelch` (gate mode) collision; notes `audio.squelch="cat"` valid on kv4p, rejected on
  baofeng. **`README.md`/`architecture.md`/`deployment.md`/`hardware-bringup.md`** move kv4p from
  "planned" to supported; `hardware-bringup.md` stays the AIOC bench reference (not merged).
- Stale "TM-V71A only" on `audio.squelch` → "TM-V71A and kv4p" (spec.py + radio.toml.example).
- ADR **0068** (new); ADR index backfilled with the missing 0064–0068 rows.

**Verified (no hardware, no keying):** `uv run pytest` → **961 passed, 5 skipped** (env with opus
installed; +10 new doctor tests). New tests: pre-KISS sniff (delimiter→True; FEND/KV4P/no-delimiter→
False; can't-open→False; probe-level pre-KISS line + generic-when-inconclusive) and band-mismatch
(WARN on disagree, none on agree), all via the existing `_ProbeTransport` seam + a stubbed `_open`.
Grep gate: no stale "kv4p planned / no backend" statements remain (the surviving "planned" lines are
the Kenwood TM-V71A).

**Facts recorded from the bench brief (not repo-derived, marked as such):** the `de ad be ef`
delimiter, flash offsets `0x0`/`0x9000`, the `0x0–0xeafff` blob span, the six `board-config-*.bin`
images, PCB revs (v1x/v2abc/v2de; v2.0e→v2.0d), and the 435.000/400.000 NVS-default frequencies. The
repo had only observed 400.000 (HANDOFF, ADR 0066).

**Live follow-ups (next cycles, NOT this one — deferred in ADR 0067/0068):**
1. **Installer kv4p path.** `scripts/install.sh`/`.ps1` have no kv4p option; `--with-hardware` is the
   AIOC path (drags a sound card). Add a kv4p install path.
2. **Conditional banner gate.** When (1) lands, the installers' `check_mumble_importable()` "earn the
   banner" gate is wrong for a kv4p install with no Mumble — make it conditional on the mumble extra.
3. **DTMF-over-kv4p RF measurement.** Survives Opus in software; RX level vs `NATIVE_ENERGY_FLOOR`
   unmeasured on real RF — fails silently just under the floor. A bench measurement, not a code change.

---

## Extras taxonomy — a node installs what it needs and nothing else (ADR 0067) (2026-07-18)

Factors the optional-dependency leaves and composes the backends from them, so a **kv4p node installs
pyserial + the Opus stack and no system library at all** — no sound card, no PortAudio, no Mumble. PR
#119 is merged (`origin/master` tip `5d9f613`); branched fresh (`extras-taxonomy-kv4p`), not stacked.

**The problem.** `hardware = [pyserial, sounddevice]` and the Opus stack rode the `mumble` extra, and
`opuslib` (the binding both the kv4p codec and pymumble import) was named nowhere — it arrived only
transitively via pymumble. So a kv4p node had to run `--extra hardware --extra mumble` to get pyserial +
libopus, dragging in sounddevice/PortAudio/pymumble it never calls. `uv sync` is exact, so naming the
wrong set silently uninstalls the right one.

**The taxonomy (ADR 0067) — the table of facts for the docs cycle:**

| extra | installs | for |
| --- | --- | --- |
| `serial` | pyserial>=3.5 | leaf: serial line (AIOC PTT / kv4p transport) |
| `soundcard` | sounddevice>=0.4 (+ system libportaudio2) | leaf: AIOC sound card |
| `opus` | opuslib>=3.0.1 + opuslib-next-bundled (env-marked carrier) | leaf: Opus codec stack |
| `tts` | piper-tts, onnxruntime | leaf: Piper TTS (unchanged) |
| `hardware` | serial + soundcard | AIOC/Baofeng backend (**same closure as before**) |
| `kv4p` | serial + opus | kv4p HT backend (**new**) |
| `mumble` | opus + pymumble (git tarball) | Mumble link (**same closure as before**) |

**Changed this cycle:**
- **`pyproject.toml`:** the leaves + composites above (PEP 621 self-referencing extras). `opuslib>=3.0.1`
  now named **explicitly** in `opus` (was transitive via pymumble; pymumble still pins ==3.0.1 so no
  drift). ADR 0057's `opuslib-next-bundled` env-marker gating moved into `opus` **intact**. `uv.lock`
  regenerated — **no version drift**, only extras regrouped.
- **`link/_opus.py`:** `opus_install_hint(*, extra="mumble", …)` — the hint now names the caller's extra.
- **`backends/kv4p/audio.py`:** `_load_opus()` passes `extra="kv4p"`, so a kv4p node with a missing
  libopus is told `uv sync --extra kv4p`, not `--extra mumble`. Docstrings updated (ADR 0067).
- **`AGENTS.md`** setup block: new leaves/composites documented; stale `hardware`→qrcode comment fixed.
- **`test_opus_loader.py`:** new case — `opus_install_hint(extra="kv4p")` yields `--extra kv4p` (and not
  `--extra mumble`), per-platform tails preserved.
- **ADR 0067** (new).

**Verified (no hardware, no keying):**
- Clean `uv sync --extra kv4p` → **pyserial 3.5 + opuslib 3.0.1 + opuslib-next-bundled 0.1.1**, and
  `sounddevice`/`pymumble_py3` confirmed **absent**. `audio._load_opus()` loaded libopus from the carrier
  (`opuslib_next/_native/libopus.so`, **not** a system lib); Opus encode→decode round-tripped
  (3 B packet → 1920 B / 960-sample PCM).
- Clean `uv sync --extra hardware` → pyserial + sounddevice (+ cffi), no opus/pymumble — closure
  identical to before the split.
- `uv run pytest`: bare **941 passed, 14 skipped**; with `--extra mumble` **951 passed, 5 skipped**.

**Compatibility (stated loudly):** `update-radio-server.sh`, `scripts/install.sh`, `scripts/install.ps1`
are **untouched** — their `--extra hardware --extra tts --extra mumble` / `--extra mumble` lines resolve
to the identical package set. Kris's deployed box keeps working exactly.

**Live follow-ups (next — the docs cycle, NOT this one):**
1. **User docs** still say `--extra hardware --extra mumble`: `docs/install.md`, `getting-started.md`,
   `deployment.md`, `configuration.md`, `hardware-bringup.md`. Update them from the table above; add a
   kv4p install path (`--extra kv4p`).
2. **Installer kv4p path.** `--with-hardware` is the AIOC path (would drag a sound card onto a kv4p node);
   no kv4p install path exists yet. When one is added, the installers' "earn the banner" gate
   (`check_mumble_importable()`, ADR 0057) is **wrong for a kv4p install with no Mumble** — make it
   conditional on whether the mumble extra was requested. It stays correct for today's mumble installer.

---

## kv4p HT — connect on a running board, re-founded on shipped firmware (ADR 0066) (2026-07-18)

Makes `Kv4pTransport.connect()` work on a **not-just-reset** board and fixes a confirmed **NVS data-loss
bug**. PR #118 is merged (`origin/master` tip `bb66396`); branched fresh (`kv4p-connect-running-board`),
not stacked.

**The re-derivation (shipped v2.0.0.1 `3f0e809`, read verbatim this cycle).** The last two diagnoses —
"sequence gate" (ADR 0062) then "edge-triggered reports" (ADR 0064) — were inherited from the wrong pin
(`e9935bd`). Shipped firmware: `handleCommands` accepts a `HostDesiredState` iff `param_len == 22`, then
**whole-struct memcpy** (no session, no sequence gate, no mask); `reconcileDesiredState` persists it to
NVS **unconditionally**; `deviceStateFlags()` echoes the **whole** `desiredState.flags` word; reports
fire **on-dirty AND periodically**. So a probe that lands *is* answered — the real failure is a
**silently dropped probe** (the `param_len` gate gives no error), and the old **neutral-zeros probe
permanently wrote freq 0.0 + `tx_allowed=false` to NVS** on every connect/close.

**Changed this cycle:**
- **`transport.py connect()` re-founded (ADR 0066): passive-first → elicit-with-retransmit → restore.**
  (1) Listen first — a board already streaming reports is read with **zero writes**. (2) Else send an
  elicit (`ENABLE_STATUS_REPORTS` on, `RADIO_CONFIG_VALID` off — no retune), **retransmitting** until the
  flag is echoed. (3) **Restore** the tuning read back (freq/CTCSS/bw/memory) with safe flag defaults
  (`RADIO_CONFIG_VALID|HIGH_POWER|RSSI_ENABLED`; **TX_ALLOWED left cleared**), undoing the elicit's
  zero-clobber of the stored frequency.
- **`close()` de-clobbered:** the PTT-off reconcile now echoes the last known state (PTT cleared), not
  zeros — no NVS write. **`_session_flags` → `_link_flags`** (kept asserted for the connection's life; the
  "session mask" model was `e9935bd` fiction). Docstrings/comments re-founded on shipped behaviour.
- **`radio.py` backend also de-clobbered** (surfaced at the bench): `Kv4pHt`'s initial reconcile sent
  `freq_rx=0.0` (RADIO_CONFIG_VALID off), which shipped still persists — so it now **seeds its tuning from
  the `DeviceState` `connect()` returns**. This is what makes `doctor --rx-level` (which builds the
  backend) non-destructive, not just the bare connect probe.
- **`doctor.py` wording made true:** the kv4p connect probe "does not key" but is **not read-only** —
  it preserves the board's tuned frequency/CTCSS and resets TX-allow/filter flags to safe defaults. The
  TX_ALLOWED / RADIO_CONFIG_VALID report lines updated. `--rx-level` stays genuinely read-only.
- **ADR 0066** (new); **ADR 0062 Decision 1 amended** (marked historical); `frames.py`'s
  `HOST_STATE_*_MASK` re-labelled a host-side grouping, not a firmware mask.
- **Tests:** `FirmwareFakeSerial` re-founded on shipped acceptance (whole-struct memcpy, whole-flags echo,
  conditional retune, **unconditional persist** with a modeled `persisted` view). New regressions: passive
  zero-write path; elicit-then-restore preserves the stored frequency and leaves TX_ALLOWED safely off;
  `close()` doesn't clobber; the backend seeds tuning from connect's state.
- **Bench (kv4p HT, SA818_UHF, `/dev/ttyUSB1`, RX-only, never keyed):** `connect()` **succeeded** on the
  running board. Wire capture (hand-decoded) proved the real cause: the board **resets on open** (a HELLO
  arrives despite DTR/RTS held low), so a single probe races the boot and is lost — `connect()` sent
  **3–4 elicit retransmits** (seq 1→4, flags `0x1000`) before the board echoed, then a **restore** (flags
  `0x1019`, freq `0x43c80000`=400.0 — the board's real appliedState freq). **NOT** edge-triggering or a
  sequence gate. With RX audio opened (RX-only): **75 Opus frames in 3 s (25/s), 144000 samples, rms ≈
  6979, 0 drops** — live RX across the backend for the first time.

**Firmware limitation (recorded, ADR 0066):** shipped exposes no read-before-write, so on a reports-off
board the operator's *flag* bits (TX_ALLOWED/power/filters) are unrecoverable — only the *tuning* is
preserved; TX is left safely off. There is also a sub-second window during the elicit where NVS holds
zeros before the restore lands.

**Live follow-ups (next cycles, NOT this one):**
1. **Extras taxonomy.** A kv4p-only extra so libopus arrives without `--extra mumble`.
2. **User docs.**

---

## kv4p HT — the Opus audio codec: replace the dead ADPCM edge (ADR 0065) (2026-07-17)

Implements what ADR 0064 pinned. **Audio now actually crosses the kv4p backend.** PR #117 is merged
(`origin/master` tip `241c547`); branched fresh (`kv4p-opus-codec`), not stacked.

**Changed this cycle:**
- **`backends/kv4p/audio.py` rewritten ADPCM → Opus.** Deleted the IMA-ADPCM codec, the `soxr` 16k↔48k
  resamplers, the 249↔747 re-blocker, and the step/index tables. New surface (same class names):
  - `RxAudioDecoder.push(packet) -> AudioFrame` — `opuslib.Decoder(48000, 1)`, one Opus packet → one
    canonical 1920-sample frame. **No re-block, no resample** (Opus is native 48 kHz; `AudioFrame` has no
    length contract). A corrupt/truncated packet (`opuslib.OpusError`) is **dropped** (empty frame, no
    raise) so a bad wire byte can't kill the RX pump.
  - `TxAudioEncoder.push/.flush` — `opuslib.Encoder(48000, 1, APPLICATION_AUDIO)`, `vbr=1`,
    `max_bandwidth=NARROWBAND` (**mirrors the firmware's own RX encoder** — ADR 0064; a wrong setting
    decodes fine and just sounds wrong on the air). Re-blocks arbitrary 48 k input to exact 1920-sample
    frames (the only re-blocker left); `flush` zero-pads the tail (padding, never sample loss).
- **libopus loads lazily** (first encode/decode, not at import / not at `Kv4pHt.__init__`, so the ~30
  codec-free backend tests need no libopus) via the shared `link/_opus.py ensure_opus_loadable()` shim
  (ADR 0056/0057). Missing libopus → **`Kv4pOpusUnavailable`** with an actionable hint, not an
  `ImportError` three frames down.
- **`radio.py`:** `receive()` decodes each queued packet straight to a frame — **removed #117's
  non-128-byte scaffolding drop** (its job is done). Docstrings/wiring updated (audio path is live Opus).
- **`transport.py`:** comment-only — "Opus packet" wording; flow-control headroom note (narrowband VBR
  Opus ≪ ADPCM's ~89 kbit/s, so the RX deque depth `256` has ample headroom — revisit on real numbers).
- **ADR 0065** (new): frame geometry (1920/3840), TX-settings-and-source, ADPCM deletion, lazy-load +
  `Kv4pOpusUnavailable`, the packaging gap, flow-control headroom.
- **Tests:** `test_kv4p_audio.py` rewritten as the Opus suite (round-trip, corrupt-drop, TX re-block
  loses-no-samples, missing-opus→actionable-error, frame geometry). `FirmwareFakeSerial` grew an
  `emit_rx_audio` Opus path + two end-to-end RX tests. `test_kv4p_radio.py` RX/TX tests updated to Opus.
  Codec-behaviour tests `pytest.importorskip` opus (skip green bare); the missing-opus test always runs.
  **Bare `uv run pytest`: 941 passed, 14 skipped. With `--extra mumble`: 950 passed, 5 skipped.**

**Packaging gap (recorded, NOT fixed — it's the next-but-one cycle):** `opuslib` rides the **`mumble`
extra** today, so a kv4p node currently needs `uv sync --extra mumble` for libopus even though it has
nothing to do with Mumble. The clean kv4p-only extra is the extras-taxonomy cycle.

**Live follow-ups (next cycles, NOT this one):**
1. **Running-board handshake.** `connect()` completes only right after a boot (shipped status reports are
   edge-triggered — ADR 0064). Read shipped `reconcileDesiredState` for the exact dirty-trigger, then make
   `connect()` robust against a no-change probe. (Bench: RTS-pulse reset the board before each run.)
2. **Extras taxonomy.** Give kv4p its own extra so libopus arrives without `--extra mumble` (no sound
   card / PortAudio / pymumble needed for a kv4p node).

---

## kv4p HT — re-pin the spec to shipped firmware, write ADR 0064, fix the audio command IDs (2026-07-17)

Follow-up to the bench-debug cycle below. That cycle *found* the firmware drift; this one **re-pins the
spec to what actually ships** and records the real protocol in **ADR 0064** — from source, not the wire
(the discipline that isolated the bug). **No codec work** (deferred, see follow-ups). PR #116 is merged
(`origin/master` tip `8b2dcf6`); branched fresh, not stacked.

**The re-pin (authoritative, read from GitHub source this cycle):**
- Shipped firmware is **v2.0.0.1** (`3f0e809baa02a946c3f0602681303f600c321d31`, released 2026-06-01;
  v2.0.0.0 `6a3b3e30…` matches it). Our old pin **`e9935bd…` is unreleased — `FIRMWARE_VER 17`, exactly
  **+44 commits ahead** of v2.0.0.1 (which is *also* `FIRMWARE_VER 17`). The version number cannot
  discriminate the two protocols; that is the whole trap.
- **Audio (both directions) moved `0x0C` → `0x07`, and the codec is Opus, not ADPCM.** Shipped RX encoder:
  Opus, 48 kHz mono s16, `OPUS_APPLICATION_AUDIO`, `OPUS_FRAMESIZE_40_MS` (40 ms), `vbr=1`,
  `OPUS_BANDWIDTH_NARROWBAND`, **no length prefix** — one Opus packet per `RX_AUDIO` KISS frame, bounded by
  `PROTO_MTU=2048`. This replaces the 128-byte/249-sample block contract; no resample, no re-block (Opus is
  native 48 kHz). Our old pin `e9935bd` genuinely *is* `0x0C`+IMA-ADPCM — our code was a correct read of an
  unreleased commit.
- **What else moved — checked, not assumed** (full `protocol.h`/`globals.h` diff read): `e9935bd` adds the
  BT/BLE `ProtocolSession` plumbing, `HOST_STATE_SESSION_FLAG_MASK`/`GLOBAL_FLAG_MASK`, and the ADPCM
  `globals.h` constants — none exist in shipped. Everything else is **byte-identical**: KISS framing/vendor
  envelope, `HostDesiredState` (22 B), `DeviceState` (26 B), `Version` (17 B), all flag bits, and
  `HOST_DESIRED_STATE=0x0D`/`HELLO=0x06`/`WINDOW_UPDATE=0x09`/`DEVICE_STATE=0x0B`.
- **Correction to last cycle's deferred item 2:** shipped has **no sequence gate** — `handleCommands`
  applies `HOST_DESIRED_STATE` unconditionally on `param_len==22` via a whole-struct `memcpy` (no flag
  mask). `connect()` times out on a *running* board because status reports are **edge-triggered**
  (`sendCurrentDeviceState` emits only when `deviceStateDirty` AND `ENABLE_STATUS_REPORTS`) — a no-op probe
  changes nothing and draws no echo. The `appliedSequence` sync stays correct. ADR 0062's sequence-gate
  rationale was `e9935bd`-only.

**Changed this cycle:**
- **ADR 0064** (new): the shipped protocol, the Opus params, the "version can't discriminate" point, and
  three decisions — (1) **support shipped only**, explicitly rejecting command-ID sniffing to dual-support
  the unreleased line; (2) **PR #111's IMA-ADPCM codec (`audio.py` + `tests/test_kv4p_audio.py`) is dead**,
  marked now, *deleted by the Opus cycle* (deletion belongs with the replacement so the tree is never
  without a decoder); (3) the Opus reuse path (ADR 0056/0057 `_opus.py`) + a packaging note (opuslib rides
  the `mumble` extra today — a kv4p codec is a second consumer).
- **ADRs 0061/0062/0063 amended:** citation `e9935bd…` → shipped `3f0e809…`, each with a caveat block; 0062
  corrects the sequence-gate rationale; 0063 corrects the `flags &= …_GLOBAL_FLAG_MASK` firmware quote
  (shipped keeps the whole flags word — our "ride every flag every frame" discipline stays correct).
- **`frames.py`:** `RcvCommand.HOST_TX_AUDIO` and `SndCommand.RX_AUDIO` `0x0C → 0x07`; source-of-truth SHA
  updated. **`radio.py`:** `receive()` now **drops a non-128-byte block** (returns an empty frame) instead
  of raising — a live shipped board sends variable-length Opus, and the `receive()` call sites (`rx/pump`,
  `controller/engine`, `doctor`) are unguarded, so the old ADPCM `ValueError` would have killed the RX
  capture task. **`audio.py`/`transport.py`:** comment-only (SHA + dead-code banner + shipped-handshake NB).
- **Tests:** `test_kv4p_frames` command asserts → `0x07`; new `test_receive_drops_a_non_adpcm_block_without_raising`.
  Full suite green.

**Live follow-ups (next cycles, NOT this one):**
1. **Opus codec.** Delete `audio.py`'s ADPCM/resampler/re-blocker + `tests/test_kv4p_audio.py`; add an Opus
   RX decoder (`opuslib.Decoder(48000, 1)`, one packet per `RX_AUDIO` frame) and TX encoder via the ADR
   0056/0057 infra (`ensure_opus_loadable`). This is what actually makes audio flow.
2. **Handshake bootstrap on a running board.** Now correctly understood as edge-triggered status reports,
   not a sequence gate — read shipped `reconcileDesiredState` to pin the exact dirty-trigger, then make
   `connect()` robust against a no-change probe.
3. **Packaging.** opuslib must be available to a kv4p node without the `mumble` extra (compounds the
   kv4p-only-extra question the docs cycle flagged).

**No GitHub instruction issue this cycle** — recorded in the PR. RX-only, board not keyed.

## kv4p HT bench debug — the boot-HELLO latch, the module band, and the v17 firmware drift (2026-07-17)

First cycle to drive the real board (kv4p HT, PCB v2.0e, SA818_UHF, firmware **v17**, on the CP2102N
by-id path — RX-only, **no keying**). Chased "`doctor --backend kv4p --rx-level` captures zero audio."
The stated hypothesis — *nothing we send is landing* — was **disproven on the bench**; the truth is a
device→host firmware drift. This cycle lands the three fixes that the bench **confirmed** and pins the
authoritative firmware source for the (larger) audio fix, which is its own next cycle.

**What the bench proved (RX-only, never keyed):**
- **Frames land.** A 22-byte `HostDesiredState` was accepted and echoed (`ENABLE_STATUS_REPORTS`
  came back in `DeviceState.flags`). The host→device codec is correct; `HOST_DESIRED_STATE` is `0x0D`,
  unchanged across the drift, so tuning/PTT/flags all reach the device.
- **The zero-RX-audio root cause is a firmware-version drift, authoritatively pinned.** The board
  streams RX audio on **vendor command `0x07`** as **Opus** (variable-length frames), but our code
  expects `SndCommand.RX_AUDIO = 0x0C` + 128-byte IMA-ADPCM. Commit `3012612f` ("BLE+move adpcm to a
  different ID") moved both audio commands `0x07 → 0x0C` on the unreleased BLE/main line; **every
  shipped release (v2.0.0.0, v2.0.0.1) — both `FIRMWARE_VER = 17` — still uses `0x07` + Opus**. Our repo
  pins `e9935bd`, which is **44 commits ahead of that move** — an **unreleased** commit no shipped
  firmware matches. We pinned repo-tip and assumed the shipped v17 matched it; it does not.
- **The board is UHF** (HELLO: SA818_UHF, 400–480 MHz) and HELLO is boot-only, so the VHF-default band
  bug is real.

**Fixes landed this cycle (the three the bench confirmed — audio is NOT touched here):**
- **`transport.connect()` now proves a round trip** instead of latching the boot HELLO. It waits for a
  `DeviceState` that echoes the session flag we sent (`ENABLE_STATUS_REPORTS`) — the firmware ORs
  session flags into `DeviceState.flags`, and a boot HELLO's embedded state has session flags `0`, so it
  can no longer be mistaken for an ack. A dropped/ignored frame is now a **loud timeout**, not a
  false-green. Bench-confirmed: proves the round trip on a freshly-booted board; fails loud on a running
  one. New helper `_session_acknowledged()`; `DeviceStateFlag` imported. The 3 existing connect tests
  encoded the old "boot data is enough" behaviour and were updated; **new firmware-accurate fake**
  (`FirmwareFakeSerial`) drops a wrong-length `HOST_DESIRED_STATE` and only echoes session flags once a
  frame is accepted — the regression the old accept-anything fakes could never catch.
- **`kv4p.module_type` config (`vhf`/`uhf`, default `vhf`, verify-on-bench)** decides the frequency band
  when no HELLO arrives (the normal case on a server restart against a running board — ADR 0062). Threads
  `radio.py` (new `Kv4pBand` StrEnum + `module_type_from_band`) → `spec.py` → `app.py`/`doctor` →
  `create_radio`; `--module-type` CLI override; `radio.toml.example` regenerated. Without it a UHF board
  rejects every UHF frequency as out-of-band.
- **doctor honesty:** the probe's `TX_ALLOWED`/`RADIO_CONFIG_VALID` lines were WARNs advising
  `set kv4p.tx_allowed = true`; but the probe is read-only (neutral state) so those flags reading clear
  is **expected** — now reported, not warned. `--rx-level`'s open-fail and zero-frame messages are
  backend-aware (no more "is the AIOC capture device correct?" on a kv4p run; kv4p points at the connect
  probe). Full suite **955 passed, 5 skipped**.

**Surfaced, deferred to the follow-up (firmware drift consequences, NOT fixed here):**
1. **RX audio (the actual symptom).** To make audio flow, re-pin the spec to a **shipped** release
   (v2.0.0.1), change the audio command to `0x07`, and **replace the ADPCM codec with Opus** (both
   directions — TX audio moved too). Alternatively decide the deployment runs a firmware matching our
   current `e9935bd` pin — but that is an unreleased build, so re-pinning to shipped is the safer call.
   This is a real `frames.py`/`audio.py` change with its own bench pass; it is the next cycle.
2. **Handshake bootstrap on a *running* board.** Shipped v17 appears to gate session-flag application
   behind the sequence check, so `connect()` only completes right after a device boot (a stale-sequence
   probe gets no echo). Today `connect()` fails loud on a running board; the follow-up should make it
   robust (e.g. seed the sequence high, or a scoped reset) once the firmware is re-pinned and understood.

**No new ADR** (this repairs ADR 0061/0062/0063 tooling against the real device). **No GitHub instruction
issue** — recorded in the PR. Bench scripts were scratch-only (session scratchpad), not committed.

## kv4p HT backend — `doctor` bench diagnostic learns the kv4p (2026-07-17)

Teaches `python -m radio_server.doctor` the kv4p backend (the bench tool an operator runs *first* when
the board is plugged in). Previously it was AIOC/Baofeng-shaped throughout: it never read
`server.backend`, `_build_backend()` hardcoded `create_radio("baofeng", …)`, the default check probed a
PortAudio sound card, and `--key-test` bisected a DTR/RTS line. **No new ADR** (implements the bench
tooling ADR 0061/0062/0063 already specified). **Non-goal (next cycle):** user docs + the packaging
question — see the note at the end.

- **Dispatch (behaviour-preserving).** New `_resolve_doctor_backend(args)`: the `--backend
  {baofeng,kv4p}` override if given, else `server.backend` **iff it is `kv4p`**, else `baofeng`.
  Rationale: `server.backend` defaults to `mock` and the AIOC bring-up runs doctor *before* flipping it
  to `baofeng`, so every non-kv4p value resolves to the AIOC checks — **today's behaviour, unchanged**.
  The baofeng paths are kept intact and routed to for baofeng (byte-for-byte). `--link` is
  backend-independent and handled before the split.
- **`--rx-level` / `--tx-tone` / `--dtmf` needed no rewrite** — their measurement/decode primitives
  already drive only the `Radio` surface. The sole coupling was `_build_backend` hardcoding `baofeng`;
  it now dispatches on `cfg["backend"]` (kv4p → `create_radio("kv4p", serial_port/squelch/
  tx_lead_seconds/high_power/tx_allowed/frequency)`). The three "AIOC backend" error strings name the
  resolved backend (baofeng wording preserved). `--tx-tone`'s PTT-line banner is now conditional
  (kv4p has no line). `--rx-level`'s *silent* hint is kv4p-specific: no OS capture level / no volume
  knob — the SA818 volume is a firmware constant (kv4p-ht `globals.h DEFAULT_VOLUME 8 → hw.volume`,
  **verify against pinned firmware / on bench**) and **not** in `HostDesiredState` (confirmed in-repo:
  `frames.py` has no volume field), so the only host levers are `kv4p.squelch` + `audio.vad_on_rms`.
- **The star — kv4p connect probe** (`_kv4p_connect_probe`, replaces the sound-card check as the
  default). Read-only, **never keys** (`Kv4pTransport.connect()` sends only the neutral state +
  `ENABLE_STATUS_REPORTS`). Uses `Kv4pTransport` directly, **not** `Kv4pHt` (whose ctor eagerly
  reconciles/configures NVS — a probe must observe, not mutate). Prints: HELLO (fw/module band/
  windowSize/features — absent is a WARN, it only fires at ESP32 boot, ADR 0062), the DeviceState
  (applied freqs/bw/ctcss/squelch/mode/rssi + flags **decoded into words**), a non-`NONE` `lastError`
  as a FAIL (never a silent pass), and whether `TX_ALLOWED`/`RADIO_CONFIG_VALID` survived the reconcile.
  Degrades to a clear FAIL when the `hardware` extra / device is absent (still runs in CI). Plus
  `_check_kv4p_serial` (by-id CP210x/CH340, `/dev/ttyUSB*` not the AIOC's `ttyACM*`, dialout via a
  lines-low open). **This one command settles a pile of guardrail-1 items on bench day** (windowSize
  2048, whether pyserial's open resets the board, the real module band, flag survival) — the docstring
  says to run it first.
- **`--key-test` for kv4p = a KEYING test** (`_kv4p_key_test` + testable `_kv4p_keying_core`). No line
  to bisect; instead reconcile `PTT_REQUESTED` on, assert `TX_ACTIVE` came back (a withheld key raises
  `Kv4pKeyingError` → **loud FAIL**, never reported as success — exercises the `TX_ALLOWED` gate, ADR
  0063), hold the hard cap, drop, assert it cleared. **Every RF guard reused unchanged** — refuses
  non-interactive/CI, dummy-load warning, typed CONFIRM, `_KEY_TEST_SECONDS` cap.
- **`--dtmf` on kv4p** is unchanged code, but the module docstring + this handoff record that running it
  is the bench measurement that settles the arc's oldest open question — DTMF through the lossy 16 kHz
  ADPCM path against the native Goertzel decoder (open since cycle 1). A measurement, not a code change.
- **Tests (`tests/test_doctor.py`, +14):** backend dispatch (kv4p threads every setting, baofeng
  unchanged, unknown → `ValueError`; `_resolve_doctor_backend` flag/`server.backend`/default) via a
  `create_radio` stub + `conftest.make_settings`; the connect probe against a fake transport (with/
  without HELLO, `lastError` surfaced, flags decoded, missing-extra degrade, missing device); the
  keying core (`FakeTransport(grant_tx=True)` → pass, `grant_tx=False` → loud FAIL) reusing
  `test_kv4p_radio`'s `FakeTransport`/`make_radio`; and the RF guard refusing non-interactive on kv4p.
  All existing baofeng doctor tests pass untouched. Full suite **950 passed, 5 skipped** (936 + 14).

**Decisions noted:** dispatch falls back to baofeng for any non-kv4p `server.backend` (preserves the
documented AIOC-before-flip workflow); the connect probe drives `Kv4pTransport` not `Kv4pHt` (read-only,
no NVS mutation); `_check_kv4p_serial`/`--tx-tone` reuse the baofeng shape minus the PTT-line concept.

**NEXT CYCLE — user docs + the packaging question (flagged, NOT built this cycle):** leave `install.md`,
`configuration.md`, `troubleshooting.md`, `hardware-bringup.md` alone until then. The `hardware` extra is
pyserial + sounddevice, but **a kv4p node needs no sound card at all** (no sounddevice, no
`libportaudio2`). A pyserial-only `kv4p` extra would delete install.md's PortAudio step and
troubleshooting.md's whole premise for kv4p users — a pyproject + installer + docs change with real
blast radius. Then the empirical **hardware bring-up** phase ("plug it in, it keys up clean").

**No GitHub instruction issue this cycle** — `gh issue list` has no target; recorded in the PR instead
of an issue comment/label.

## kv4p HT backend — wiring: `server.backend="kv4p"` selectable/configurable/startable (2026-07-17)

Makes the `Kv4pHt` class (ADR 0063, prior cycle) reachable: factory registration, a `[kv4p]` config
section, and the `api/app.py` composition branch. **No new ADR** — follows ADR 0063's complete-state
model and the frequency recommendation. Still no hardware touched. **Non-goals (next cycle):**
`doctor` bring-up and the user docs (install/configuration/hardware-bringup prose).

- **`radio_server/backends/kv4p/radio.py`** — `Kv4pHt.__init__` gained four config params that ride
  the **initial** desired state (before the first reconcile), plus module `DEFAULT_*` constants that
  `config/spec.py` imports (source-of-truth, like `aioc_baofeng.py`): `squelch` (SA818 level 0..8,
  default `4`), `high_power` (HIGH_POWER flag, default True), `tx_allowed` (TX_ALLOWED NVS gate,
  default True), `frequency` (optional Hz — when set, `set_frequency` at construction reusing the
  existing out-of-band validation; unset leaves the device on its NVS frequency, **no invented
  default on the air**). `DEFAULT_SERIAL_PORT = /dev/ttyUSB0` (CP210x/CH340, **not** the AIOC's
  `ttyACM0`).
- **`radio_server/config/spec.py`** — `[kv4p]` block (serial_port/squelch/tx_lead_seconds/
  high_power/tx_allowed/frequency), a new `coerce_optional_int` (for the `None`-default frequency),
  the six keys added to `_ADVANCED_KEYS`, and `server.backend`'s description now names `kv4p`. The
  `kv4p.squelch` description owns the collision with `audio.squelch` and the level-0 caveat.
- **`radio_server/config/save.py`** — `kv4p` group banner (between baofeng and mumble),
  `kv4p.frequency` in `_COMMENTED_DEFAULTS` (renders commented, no invented value); `save_settings`
  now skips an optional `None` (would be unwritable TOML). `radio.toml.example` regenerated (the
  byte-exact contract test guards it).
- **`radio_server/backends/factory.py`** — `Kv4pHt` registered; `available_backends()` →
  `(mock, v71, baofeng, kv4p)`.
- **`radio_server/api/app.py`** — the `elif backend == "kv4p"` branch passes the `[kv4p]` settings
  through, same shape as baofeng. It **relaxes the `audio.squelch="cat"` rejection**: cat is valid
  here (real busy line), but `cat` + `kv4p.squelch=0` raises a `RuntimeError` naming **both**
  settings (at level 0 the SQ pin never asserts → busy latches True → a cat scan dwells forever).
  Baofeng + cat still raises exactly as before.
- **Tests:** `tests/test_backend_wiring.py` (new, 5 — build_app passthrough + the squelch-gate
  combinations, monkeypatching `create_radio` so no serial is opened); `tests/test_config.py` (+7:
  kv4p resolve/coerce/round-trip, frequency optional/reject, the `_ADVANCED_KEYS` known-keys guard);
  `tests/test_kv4p_radio.py` (+6: config flags/squelch on the first frame, tx_allowed/high_power
  withheld, frequency tune-once/no-tune/out-of-band). Count fixes in `test_factory.py` (+kv4p) and
  `test_settings_api.py` (54→60 keys, kv4p render check). Full suite **936 passed, 5 skipped** (918
  baseline + 18), no regressions.

**Decisions noted:** `high_power`/`tx_allowed` default True (a node exists to transmit;
operator-overridable, and `tx_allowed=false` is a real receive-only gate). `kv4p.squelch=4` is a
marked verify-on-bench default. **`module_type` intentionally NOT a config key** — it only picks the
*fallback* band when no HELLO arrives, and a HELLO overrides it; a follow-up if a UHF board with no
HELLO ever needs it. `kv4p.frequency` renders in the settings API as an untyped (string) field
because its default is `None` — cosmetic, coerces fine.

**NEXT CYCLE:** `doctor` bring-up for kv4p (the bench diagnostic — its own reviewable unit) **and**
the user docs together (install/configuration/hardware-bringup). Then the empirical **hardware
bring-up** phase ("plug it in, it keys up clean").

## kv4p HT backend — the `Kv4pHt` class (Radio/CatRadio over transport + audio, ADR 0063, 2026-07-17)

Composes `transport.py` + `audio.py` + `frames.py` into the backend implementing the
`Radio`/`CatRadio` surface — the first real `CatRadio`, the first backend with a genuine `busy`
line, the first where the software `ScanEngine` runs on hardware. Still **fake-transport tested**
(guardrail 6). **Not** factory/config/`app.py` wiring, `doctor`, or the `squelch="cat"` relax —
that is the wiring cycle.

- **`radio_server/backends/kv4p/radio.py`** (`Kv4pHt`):
  - **Complete-state reconcile (the load-bearing rule).** `HostDesiredState` is not a partial update
    — the firmware replaces the whole struct + the whole global-flag word each frame. So the class
    owns a full desired-state model and every mutation is read-modify-write-the-whole-thing then
    `send_desired_state` + `await_applied`. `RADIO_CONFIG_VALID` (gates the `sa818.group` apply),
    `TX_ALLOWED` (hard-gates PTT, NVS-persisted, defaults false), and `RX_AUDIO_OPEN` (session, opens
    RX audio) ride **every** frame. On key-up we set `PTT_REQUESTED` **and assert `TX_ACTIVE` came
    back**, else raise `Kv4pKeyingError` — a silent no-key never becomes dead air.
  - **Keying** mirrors `AiocBaofeng`'s `_keyed` one-shot-vs-streaming discipline (reconciled PTT
    flag, not a line). TX audio: `audio.py`'s re-blocker → `HOST_TX_AUDIO` blocks through the
    transport window. `tx_lead_seconds` knob (value **unknown** — marked default, not AIOC's 0.5 by
    analogy). `receive()` polls the transport RX queue (~one block) → one canonical frame per block.
  - **Units (fail loud, ADR 0063):** freq int Hz ↔ float MHz (simplex — both legs; out-of-band
    **raises**, no silent clamp; quantized to a marked raster); tone Hz ↔ CTCSS **index** (0..38,
    unmapped **raises**, TX tone only); mode ↔ `bw` (FM↔25 kHz / NFM↔12.5 kHz, else **raises**).
  - **`status()`:** `busy = not SQUELCHED` (real SQ-pin carrier detect), `transmitting = TX_ACTIVE`
    (also catches the firmware's ~200 s `RUNAWAY_TX_SEC` auto-drop), `frequency` from `freq_rx`,
    tone/mode inverted.
  - **`capabilities()`** = `SHARED_CAPS | {SET_FREQUENCY, SET_TONE, SET_MODE, SCAN}`. **`SCAN` is in**
    (gates the software sweep, which kv4p can run — a first) but `radio.scan(on)` raises (no native
    toggle; `radio.scan()` is tree-wide dead code — possible tidy). **`SET_CHANNEL` omitted**
    (`memory_id` is an opaque echo, no device memory table) → `UnsupportedCapability`.
- **`radio_server/backends/kv4p/transport.py`** gained one public method: **`send_tx_audio(block)`**
  — TX audio must ride the same encoded-byte credit window, but the transport cycle exposed only
  `send_desired_state`. Reuses the existing private `_write_frame`.
- **ADR 0063** (`docs/adr/0063-kv4p-backend-capabilities-and-units.md`, index row added): the two
  decisions — capabilities/the SCAN reversal, and unit mapping — plus the complete-state rule.
- **Tests:** `tests/test_kv4p_radio.py` — 17 fake-transport tests (a `FakeTransport` echoing the last
  desired state as a synthesized `DeviceState`): the whole-word flag regression, the withheld-key
  raise, unit conversions (all raising before send where invalid), capabilities/`set_channel`/`scan`,
  `status()` busy/tx/freq, one-shot-vs-streaming keying, `receive()` decode + clean timeout. Full
  suite **918 passed, 5 skipped** (901 baseline + 17), no regressions.

**Verify-on-bench (guardrail 1):** DRA818 bandwidth code integers; CTCSS index↔Hz mapping; SA818
tuning raster; per-module default freq bands; `tx_lead_seconds`; and (config cycle) the `squelch`
level default (level 0 → SQ never asserts → `busy` reads True forever).

**NEXT CYCLE:** the **wiring** — factory registration + `config/spec.py` (`server.backend="kv4p"`
with `serial_port`/`baud`/`module_type`/`squelch`/`tx_lead_seconds`), the `app.py` backend branch,
`radio.toml.example`, `doctor` bring-up, and relaxing the `audio.squelch="cat"` rejection
(`api/app.py`) now that this backend reports a real `busy`. Then the empirical **hardware bring-up**
phase ("plug it in, it keys up clean").

## kv4p HT backend — the serial transport (reader thread + window + reconciler, ADR 0062, 2026-07-17)

The I/O layer under `frames.py` — the first kv4p cycle that touches a wire. Still **fake-serial
tested** (guardrail 6; hardware exists but bring-up is its own phase). **Not** the `Kv4pHt` backend
class, `capabilities()`, or factory/config/`app.py` wiring — those compose transport + `audio.py` +
`frames.py` later. Uses the `_serial_factory` DI seam from `aioc_baofeng.py`.

- **`radio_server/backends/kv4p/transport.py`** (`Kv4pTransport`; stdlib + lazy pyserial, the
  `hardware` extra — import stays hardware-free):
  - **Reader thread** (`kv4p-reader`, daemon; the `MultimonStream` idiom): `read` → `KissDecoder.feed`
    → `parse_frame` → dispatch. `RX_AUDIO` → bounded drop-oldest `deque` (drops counted,
    `rx_audio_drops`); `DEVICE_STATE` → latest + `applied_sequence`; `HELLO` → adopt
    windowSize/module/freq range; `WINDOW_UPDATE` → credits; `DEBUG_*` → `logging` at the matching
    level (TRACE→debug); a KISS **DATA** frame → inert `Ax25Frame`, **separate path, never a vendor
    sink**. A read error (SerialException et al.) is **surfaced** (stored + re-raised to blocked
    writers/waiters), not wedged; a malformed frame is logged and skipped without killing the reader.
  - **Flow control counts ENCODED bytes** (the cycle-1 gotcha): `build_vendor_frame` returns the
    escaped/FEND-delimited on-wire bytes, so `len(frame)` *is* the ack unit (`_encodedFrameLen`). A
    write blocks until the window has room and raises `Kv4pTimeout` rather than hanging TX; a
    `WINDOW_UPDATE` refunds the same encoded count.
  - **Reconciler:** `send_desired_state(state)` assigns the next sequence + ORs in the session flags
    (which ride every frame — the `HOST_STATE_SESSION/GLOBAL_FLAG_MASK` split); `await_applied(seq,
    timeout)` blocks on `DeviceState.appliedSequence`.
  - **Lifecycle:** `close()` idempotent + atexit; safe shutdown is a **reconciled PTT-off flag, not a
    dropped line** (there is none), bounded by a short `_CLOSE_ACK_TIMEOUT` (0.5 s) so shutdown never
    hangs on a silent device; fail-safe if the port is already gone.
- **ADR 0062** (`docs/adr/0062-kv4p-transport-handshake.md`, index row added) records the two real
  decisions, both firmware facts read from `kv4p_ht_esp32_wroom_32.ino` (not memory):
  - **Decision 1 — connect by syncing `DeviceState.appliedSequence`, never by waiting for a HELLO.**
    USB HELLO fires once at boot (no connect event; `connected` hardcoded true), and `sequence` is
    RAM-only/monotonic-within-a-boot — so a restarted host counting from 1 is **silently ignored**.
    `connect()` sends a probe with `ENABLE_STATUS_REPORTS` (firmware applies session flags + pushes
    DeviceState *before* the sequence check), reads `appliedSequence`, sets the counter to
    `applied + 1`. HELLO is a bonus, never a precondition; else windowSize defaults to
    `USB_BUFFER_SIZE = 2048` (**verify-on-bench**).
  - **Decision 2 — hold DTR/RTS low before `open()`** (ESP32 auto-reset footgun; the aioc shape, for
    a different reason). Deliberately **do not** reset-to-get-a-HELLO (would reboot the radio every
    restart; the appliedSequence sync makes it needless). Whether pyserial's default resets this
    board is **verify-on-bench**.
- **Tests:** `tests/test_kv4p_transport.py` — 15 fake-serial tests (a `FakeSerial` feed/writes pipe +
  background threads for the blocking calls): appliedSequence sync with/without HELLO, sequence never
  regressing below applied, encoded-byte accounting (block-at-zero / resume-on-`WINDOW_UPDATE` /
  timeout, driven with a FEND-heavy payload so encoded >> decoded), dispatch routing, DATA-frame
  inertness, reader survival across a chunk boundary / `b""` read / a surfaced serial error, and the
  reset-safe factory (lines low before open). Suite **901 passed, 5 skipped** (886 baseline + 15).

**Verify-on-bench (guardrail 1, recorded not asserted):** windowSize 2048; whether pyserial's default
open resets this board; the real serial path/name (`/dev/ttyUSB*` for CP210x/CH340, not the AIOC's
`/dev/ttyACM*`). **Throughput budget (open measurement, not a problem):** ~64 ADPCM blocks/sec through
cycle 2's pure-Python codec on the reader thread, ~89 kbit/s ≈ 77% of the 115200 line — the reader
must not stall; measured in the composed backend, not here.

**NEXT CYCLE:** the `Kv4pHt` backend class — implement the `Radio`/`CatRadio` surface on top of
transport + `audio.py` (`transmit`/`receive`/`ptt`/`status` + `set_frequency`/`set_channel`/`set_tone`
via a pending `HostDesiredState`), then factory/config/`app.py` wiring. The `Capability.SCAN`
advertise-or-omit question and the `audio.squelch="cat"` relax (`app.py:1276-1286`) land there (ADR 0061).

**No GitHub instruction issue this cycle** — `gh issue list` has no target; recorded in the PR instead
of an issue comment/label.

## kv4p HT backend — the audio edge (ADPCM codec + resamplers + TX re-blocking, 2026-07-17)

Second frame-layer cycle for the kv4p backend (ADR 0061; cycle 1 = `frames.py`, PR #110 merged).
This is the **audio edge**, still pure and hardware-free: no serial, no flow control, no `Kv4pHt`
class, no wiring. **No new ADR** — nothing here decides anything 0061 didn't cover.

- **`radio_server/backends/kv4p/audio.py`** (stdlib + numpy + soxr, all core deps — safe with no
  extras):
  - **IMA ADPCM WAV-block codec both directions.** `decode_adpcm_block(128B) -> 249 int16`
    (self-contained: header seeds predictor+index), `encode_adpcm_block(249, index) -> (128B,
    next_index)`, and `AdpcmEncoder` carrying the step index across blocks. Block = 4-byte header
    (`int16 LE predictor` = sample 0 verbatim, `uint8 index`, `uint8 reserved=0`) + 124 data bytes
    = 248 nibbles **low-nibble-first**; 1+248=249. Per-sample loop is **pure Python ints** (the
    predictor feedback is sequential, not vectorizable; int16 numpy would wrap). **Codec choice
    (documented):** predictor re-anchored to the true first sample each block (bounds drift, exact
    sample-0) while the index is carried (avoids per-block reset artifacts); decode stays
    self-contained because the header carries both.
  - **Streaming 16k↔48k resamplers** (`StreamResampler`) over `soxr.ResampleStream(..., dtype=
    "float32", quality="HQ")` + `resample_chunk` — the `GoertzelStream` precedent (`audio/dtmf.py:
    682`), **not** `audio/resample.py`'s VHQ one-shot (its ~150 ms buffering is the latency trap
    ADR 0054 caught; this is a live full-duplex path). `resample.py` untouched. `flush()` (soxr
    `last=True`) drains the filter tail.
  - **TX re-blocking** (`TxAudioEncoder.push(frame) -> list[128B blocks]`): 48k → 48k→16k resample
    → accumulate → emit whole 249-sample-at-16k blocks, **hold the remainder** (`pending_samples`).
    **RX** (`RxAudioDecoder.push(128B) -> AudioFrame@48k`): decode → 16k→48k resample → one
    canonical frame per block, **no re-blocker** (AudioFrame is format-identity-only, no length
    contract).
- **Empirical (guardrail 1, measured this cycle):** soxr HQ streaming has real filter latency — a
  single 249-chunk emits 0 samples; cumulative output converges to exactly the rate ratio only
  after `flush(last=True)` (16→48 == 3×, 48→16 == ÷3, both exact when flushed). Chunked feeding ==
  one big call (bit-identical). ADPCM round-trip SNR on a 440 Hz sine ≈ **30.5 dB**, step index
  stayed in [56,67] (never runs away). Tests assert an SNR floor of 24 dB and cumulative ratios.
- **Tests:** `tests/test_kv4p_audio.py` — 13 pure tests incl. a hand-worked decode fixture
  (nibbles `[4,4,8,0]` → samples `[0,7,17,16,17]`, derivation in a comment). Suite **886 passed, 5
  skipped** (873 baseline + 13).

**Verify-on-hardware (bench, recorded not asserted):** real ADPCM fidelity against the device's own
pschatzmann-based codec — byte-for-byte block compatibility and audible quality. Our codec follows
the standard IMA WAV spec; the firmware tests only expose the 128/249/747 sizing, not the nibble
tables. **Open from cycle 1, still open:** DTMF through lossy 16k ADPCM has never met the native
Goertzel gauntlet (talk-off / weak-signal).

**NEXT CYCLE:** the reader/writer over pyserial + the reconciler state machine, wiring `frames.py`
+ `audio.py` into a `Kv4pHt` backend (flow control counts *encoded* bytes; the `Capability.SCAN`
question and the `audio.squelch="cat"` relax from ADR 0061 land there).

**No GitHub instruction issue this cycle** — `gh issue list` has no target; recorded in the PR
instead of an issue comment/label.

## kv4p HT backend — ADR 0061 + the pure wire codec (frame layer only, 2026-07-17)

New backend *shape* recorded and its I/O-free wire codec landed. The kv4p HT is not a sound card
+ serial PTT like the AIOC: it is a CP210x/CH340 **UART at 115200 8N1** over which everything
rides — RX/TX audio, tuning, PTT, squelch — in **KISS frames**. No sounddevice, no Hamlib. This
cycle is the frame/struct layer ONLY (no serial I/O, no audio codec, no backend class, no wiring).

- **ADR 0061** (`docs/adr/0061-kv4p-uart-backend.md`, index row added) records three things that
  make it a new shape: (1) it's a **state reconciler** — the host sends a whole `HostDesiredState`
  with a monotonic `sequence`, the firmware echoes `DeviceState.appliedSequence`; **PTT is a flag
  inside that struct** (`HOST_STATE_PTT_REQUESTED`), so guardrail 2 holds trivially (no command to
  misuse). (2) It'd be our **first real `CatRadio`** (only `MockRadio` implements CAT today;
  `SignaLinkV71` is a `NotImplementedError` stub). (3) It has a **real busy line**
  (`DEVICE_STATE_SQUELCHED` + RSSI), so `audio.squelch = "cat"` — which `api/app.py:1276-1286`
  rejects for `baofeng` — becomes valid for this backend.
- **`radio_server/backends/kv4p/frames.py`** (stdlib only, no I/O): streaming `KissDecoder`
  (mirrors the firmware parser `protocol.h:392-515` — boot-banner discard, unknown-escape resync,
  oversize-drop-not-truncate); vendor envelope `FEND|0x06|"KV4P"|0x01|<cmd>|payload|FEND` with
  port-nibble drop and bad-prefix/version ignore; KISS DATA (`0x00`) parsed as a SEPARATE
  `Ax25Frame` path (future text-over-RF, inert this cycle); frozen-dataclass struct codecs for
  `HostDesiredState`(22)/`DeviceState`(26)/`Version`(17)/`Hello`(43)/`WindowUpdate`(4) using
  `struct` `<` (`calcsize` asserted in tests); `HostStateFlag`/`DeviceStateFlag` IntFlags,
  `RcvCommand`/`SndCommand`/`DeviceMode`/`DeviceStateError`/`RfModuleType`/`FeatureFlag` enums, and
  the `HOST_STATE_SESSION/GLOBAL_FLAG_MASK` split carried for the next cycle.
- **Source of truth:** kv4p-ht pinned at `e9935bd37e7505f70ae7023c78fe6a714be90be9`
  (`protocol.h` + `globals.h`), read as a spec — **not ported** (kv4p-ht is GPL-3.0; independent
  impl, cited in the ADR, no firmware source pasted). `RfModuleType` is `uint8_t` (fixes `Version`
  at 17 bytes); ESP32 Xtensa `char` is signed (signed-byte codes for `radioModuleStatus`).
- **Tests:** `tests/test_kv4p_frames.py` — 28 pure tests. Suite **873 passed, 5 skipped**
  (`uv run pytest`), 845 baseline + 28.

**NEXT CYCLE (the backend, recorded in the ADR — none built here):** the reader/writer over
pyserial + a reconciler state machine; flow control counts **encoded** bytes (the firmware acks
each frame with its escaped/FEND-inclusive length, `protocol.h:421-431`), not decoded payload;
audio is 16 kHz 4-bit IMA ADPCM, 128-byte block → **249 samples**, and 249 does not divide our
960-sample canonical blocks (ADPCM + resampling live in that cycle); ≈89 kbit/s ≈ 77% wire use at
115200. **Open question:** whether to advertise `Capability.SCAN` — the kv4p has no hardware scan,
but `ScanEngine.__init__` (`scan/engine.py:199-200`) requires `SCAN` to run its software sweep.
Also relax the `audio.squelch="cat"` rejection (`app.py:1276-1286`) for this backend.

**No GitHub instruction issue this cycle** — `gh issue list` has no target, mirroring the
precedent below; recorded in the PR instead of an issue comment/label.

## Fix four beginner-facing doc bugs (docs-only, no ADR, 2026-07-17)

Four verified defects a beginner hits following the docs to bring up a real radio. No behaviour change,
no code — docs only.

- **BUG 1 — the required Piper voice had no source anywhere in the repo.** `tts.voice` is required with
  no default, yet nothing said where to get a voice or that it's *two* files. Added a **Getting a
  voice** section to `docs/install.md` (voices page + samples + VOICES.md, `en_US-amy-medium` as the
  default, both `.onnx` and `.onnx.json` download links, the sidecar-must-sit-beside-it warning, and
  why medium over high on ~3 kHz FM) and expanded the "Voice file" bullet in `docs/configuration.md`
  with the same essentials + a cross-link. All five URLs verified HTTP 200. Sidecar claim verified
  against `radio_server/services/tts.py:99-103,142-159` (fails loud without `<voice>.onnx.json`, reads
  `audio.sample_rate` from it).
- **BUG 2 — stale "pause between repeated digits" advice removed.** Bench-confirmed false since 0060
  (native does its own onset/gap detection). Dropped the pause clause in `docs/using-it.md` (kept the
  "hold each tone ~1s" tip) and removed the "Held keys count once …" blockquote + a dangling
  held-vs-repeated sentence in `docs/hardware-bringup.md`. The `pause`/`repeated` grep hits that remain
  are all in **historical ADRs (0030/0038)** — left intact on purpose; they accurately record the old
  buffered behaviour, and rewriting them would falsify the record.
- **BUG 3 — DTMF spelling normalized to unspaced** (`10#`/`01#`/`02#`/`98#`/`99#`, matching the Services
  card's `{digit}#` and `radio.toml`) across README.md and docs/. Also unspaced the 6-digit TOTP
  examples (`1 2 3 4 5 6 #` → `123456#`) for one consistent spelling. `grep -rnE '[0-9]( +[0-9#])+ *#'`
  over README + docs is now empty.
- **BUG 4 — Homebrew introduced before first use** in `docs/install.md`'s macOS section (what it is +
  brew.sh + the Xcode CLT it pulls in), so `brew install portaudio` no longer appears from nowhere.
- **Suite: 845 pass, 5 skipped** (`uv run pytest`, unchanged — docs-only).

## Flip `auto` to `native`; multimon-ng becomes optional (ADR 0060, 2026-07-17)

The bench A/B ADR 0055 deferred is settled: on the reference station (AIOC + UV-5R) `native` decodes
better than multimon-ng on real RF. So this cycle makes the one-line flip 0055 named and drops
multimon-ng as a dependency.

- **The flip — `resolve_decode_mode` (`radio_server/audio/dtmf.py`)** loses its `shutil.which` branch:
  `auto` → `(native, "bench-verified, ADR 0060")` unconditionally, binary present or absent. `multimon_bin`
  stays in the signature (call-site stability + explicit modes still read it). Four description sites
  updated in lockstep so nothing lies: the `DECODE_MODES` and `DEFAULT_DTMF_DECODE_MODE` comments in
  `dtmf.py`, the `build_controller` comment (`controller/engine.py:738-748`), the `dtmf.decode_mode`
  help in `config/spec.py`.
- **`streaming`/`buffered` unchanged and still raise.** An explicit mode is a contract — the
  raise-on-missing-binary in `MultimonStream`/`MultimonDtmfDecoder` is untouched. The flip is confined
  to `auto` (the only mode whose job was to choose). `doctor` now prints `decode mode: auto -> native
  (bench-verified, ADR 0060)`.
- **Tests — `tests/test_auto_decode.py` rewritten** to the flipped contract: `auto` → native with the
  binary present AND absent (parametrized), auto wires `GoertzelStream` regardless, auto never raises
  with no binary. Kept verbatim: explicit-mode pass-through and `test_explicit_streaming_without_binary_still_raises`.
  Doctor test asserts the new reason for both present/absent. No `skipif`. Grep confirmed the old
  reason strings (`multimon-ng found` / `no multimon-ng on PATH`) lived only in this file.
- **Docs — multimon-ng optional + Opus collapse (user-approved).** `docs/install.md` extras table is
  now exactly **PortAudio + a voice**; apt → `libportaudio2`, brew → `portaudio` (dropped multimon-ng,
  libopus0, opus); Windows section drops the "no Windows build → WSL2 for DTMF" story (native decodes
  in-process on Windows). `docs/hardware-bringup.md` reframes the DTMF-test section native-first
  (multimon only for the streaming/buffered escape hatches). `scripts/install.ps1:11` softened.
  `radio.toml.example` keeps `multimon_bin`, rewords `decode_mode`/`buffer_seconds`.
  `docs/configuration.md:209` dropped the stale "system libopus0" clause (opus rides the `mumble` wheel).
- **Open item, recorded in the ADR, NOT acted on:** the bench proved *decode*, not *talk-off*. The lever
  is `NATIVE_ONSET_BLOCKS = 1` (Q.24 wants ≥2 blocks / ≥40 ms; pinned by ADR 0038's "two 9s @ 30 ms gap
  → 99" row). Quiet failure mode: a spurious combo fires, and since `98#` is ungated (ADR 0043) the
  visible symptom is a Mumble link dropping on its own. Left pinned this cycle.
- **Suite: 845 pass, 5 skipped** (`uv run pytest`).

## Removed services get a home + the two migrations that took the station down now say what they are (ADR 0059, 2026-07-17)

ADR 0051/0052 made three breaking `radio.toml` changes and shipped a migration error for none of them;
a deployment hit all three, in sequence, on the first restart after an upgrade. This cycle gives the
removed features a home and names the errors — the `_LEGACY_MUMBLE_KEYS` habit, extended.

- **Part 1 — the five services ship as `examples/local_services/`** (weather, astronomy, quote, battery,
  bible), already ported (absolute imports, `settings.extra(...)`, astro's bare `from weather_service
  import …`). **Not** registered in `PLUGINS`, **not** imported by the app — copy-source only. Upgrade
  path is now `cp examples/local_services/*.py local_services/`. `.gitignore`'s `/local_services/` is
  anchored, so the examples commit while the operator's folder stays ignored. Fixed the one residual
  dangling `from .plugin import PluginBuildContext` → absolute in each.
- **Part 1 test — `tests/test_examples_local_services.py`** imports every example through the real
  `discover_local_plugins` and asserts a valid `PLUGIN`. This is the load-bearing unit that catches
  `Fetcher`/`ServiceContext`/`Service`/`ServicePlugin` drift in CI. The deleted per-service tests were
  **not** restored (deliberate — one import test carries it). Note the bare-stem `sys.modules` cache
  gotcha: the test pops/restores the five stems + puts the examples dir first on `sys.path`, so it's
  deterministic even though the dev box's gitignored root `local_services/` shares those stems.
- **Part 2a — `resolve_settings` (settings.py:127)** now splits unknown keys by namespace: a table
  that isn't a schema group (`weather.base_url`) → `"unknown config table(s): [weather] (weather.base_url)
  -> [plugins.weather] … only the TOML nesting moves. See examples/local_services/."`; a real typo whose
  namespace IS a group (`server.prot`) keeps the generic `"not in the config schema"`. Namespaces derived
  from `{s.key.split(".",1)[0] for s in SETTINGS}` — no constant. `_LEGACY_MUMBLE_KEYS` (raised earlier in
  `_flatten`) is untouched; `mumble.enabled` never reaches the new check.
- **Part 2b — `resolve_bindings` (plugin.py:152)** keeps the `"unknown service or command; known ids
  are […]"` prefix (tests match it) and appends: ids come from `./local_services/`; if the id is one of
  the five 0051 removals (`_REMOVED_IN_0051`), names the example file to copy; if the folder is absent,
  says so. `DEFAULT_LOCAL_SERVICES_DIR` is lazy-imported from `.local` inside the function (avoids the
  `local`↔`plugin` cycle).
- **Part 3 — docs/configuration.md** "Add your own services": wrong-vs-right TOML (`[weather]` fails loud
  vs `[plugins.weather]`), notes the plugin code is unchanged, points at `examples/local_services/`.
- **Scope held:** no per-service tests restored, no new plugin features, no digit remap, no `[services]`
  default change. Examples not registered, app doesn't import `examples/`. `PLUGINS` still `("time",)`.

Suite 846 pass, 5 skipped. PR against master; human merges.

## Copy-pasteable commands actually run + a docs↔script contract test (ADR 0058, 2026-07-17)

Narrow, unblocked slice (NOT the hardware-gated install.md/WSL2 rewrite): commands the docs tell people
to type that failed when typed. Three bugs + the reason they kept regressing (nothing tested it).

- **Bug 1 — `curl … | sh` died on Debian/Ubuntu.** install.sh was `#!/usr/bin/env bash` + `set -euo
  pipefail`, but README/getting-started pipe to `sh` = dash, which lacks `pipefail` (fatal, instant).
  **Decision (ADR 0058): POSIX-clean the script**, not `| sh`→`| bash` — the audit found it ~99% POSIX
  already (only `pipefail`, the `curl|tar` pipe, and two dash-supported `local`s). shebang→`#!/bin/sh`,
  `set -euo pipefail`→`set -eu`, and the line-93 `curl | tar` pipe → temp-file download with an explicit
  curl status check (more robust than the masked pipe). README/getting-started keep `| sh` (now correct).
  **Honest repro note:** couldn't reproduce locally — this box's dash is 0.5.12, which *added* pipefail
  (2023); the bug bites dash ≤0.5.11 = Ubuntu 22.04 LTS / Debian 11 (huge base, supported to 2027).
- **Bug 2 — `--with-hardware` advice couldn't work.** install.md:70's "add the flag to the `curl … | sh`
  line" → `sh --with-hardware` = "Illegal option". Fixed to `curl … | sh -s -- --with-hardware` and
  `./scripts/install.sh --with-hardware`.
- **Bug 3 — bare `python -m radio_server`.** Swept to `uv run python` across all user-facing guides
  (the 8 flagged copy-paste blocks + inline mentions in hardware-bringup/deployment/architecture/
  operating/configuration). ADRs left as frozen history (excluded by decision).
- **THE anti-regression: `tests/test_docs_install_command.py`** (4 tests, no skipif) — parses README's
  pipe target, asserts it agrees with install.sh's shebang, **executes** `sh scripts/install.sh --help`
  to prove it starts (not just `-n`), and statically forbids `pipefail` (this box's dash tolerates it,
  so execution alone can't guard reintroduction). Proven to fail on a reverted shebang/pipefail. Suite
  838 pass.
- **Still the docs cycle (out of scope, unchanged):** install.md WSL2 rewrite, prose, Piper voice-link,
  hardware-bringup split.

## Installer ships the Mumble link on all three platforms (ADR 0057, 2026-07-17)

Made the README headline command actually deliver: install on a clean box, open the panel, click
Connect, talk. Both installers ran a bare `uv sync` (no `pymumble`) then printed "All set." — a lie.
Fixed this cycle (branches ADR 0056 → 0057):

- **libopus is now a dependency via a bundled-wheel carrier, all platforms.** Re-asked 0056's gate the
  right way: not "is opuslib-next-bundled a drop-in for `opuslib`?" (no) but "does it carry a libopus
  binary we can point the shim at?" — **yes**, verified end-to-end on Linux. Full wheel tag matrix
  confirmed: win_amd64, macOS x86_64+arm64, manylinux2014 x86_64+aarch64 (so **Pi and Apple Silicon are
  covered**). `radio_server/link/_opus.py` `ensure_opus_loadable()` is now one code path: locate
  `opuslib_next/_native/libopus.*` via `find_spec` (no bindings import) and **patch
  `ctypes.util.find_library('opus')`** to return it (delegating every other name). The vendored
  `radio_server/_vendor/` DLL is **retired** — the win wheel's opus.dll is byte-identical (both sha256
  `d553adca…`, proven in the ADR).
- **Carrier gated by a PEP 508 env marker** to exactly the five wheel tags, so no-wheel tags (win-arm64,
  32-bit) omit it and hit the system-lib hint instead of hard-failing `uv sync` on an sdist build.
  Residual edge: musl/Alpine can't be marker-excluded (non-target; Pi OS is glibc).
- **`--extra mumble` is the default sync** in both installers (browser voice link needs no radio =
  headline). **"All set." is earned:** each installer runs a `python -c "…check_mumble_importable()…"`
  that imports pymumble + libopus and won't claim the link works if it doesn't. `getting-started.md`
  Step 2 gained `--extra mumble` so the hand path matches the one-liner.
- **VERIFY ON HARDWARE:** real Windows amd64 box (git-less `uv sync --extra mumble` → the `python -c`
  check exits 0 → `doctor --link` passes) and **macOS arm64** (CI can't run it; mechanism identical to
  the verified Linux path). `install.ps1` wasn't pwsh-parse-checked here (no pwsh) — eyeball on Windows.
- **NEXT CYCLE (unblocked, out of scope here):** the `docs/install.md` rewrite — its extras table can
  collapse to PortAudio + a voice (multimon optional since 0055, opus now a dep) and drop the
  Windows→WSL2 framing for the browser link. Gated on hardware verification. Tests:
  `tests/test_opus_loader.py` rewritten for the carrier (15, no skipif). Suite 834 pass.

## Link audio fixes + web session-open + restart button (ADR 0045/0046/0047, 2026-07-16)

Two field bugs and two features in one cycle:

- **Mumble→RF never keyed (ADR 0045).** Root cause: the bridge defers to `rx_pump.active`, and
  under the deployment's `squelch = "off"` the pass-through gate never rejects a frame, so
  `active` latched `True` at the first hardware frame — every Mumble frame silently dropped.
  Gates now carry `detects_signal` (`False` on pass-through) and the pump never asserts `active`
  off a signal-blind gate. **Field-verify on the box**: watch `GET /link/status` → the active
  entry's new `tx` counter block (`frames_in` / `dropped_rx_active` / `dropped_slot_busy` /
  `overs_keyed`) while a Mumble peer talks — `overs_keyed` should climb and the radio key. If
  `dropped_rx_active` climbs instead, the deployment is on a VAD gate whose thresholds hold the
  channel busy.
- **DTMF tones leaked into Mumble (ADR 0045).** The bridge's RF→Mumble feed now runs a 0.3 s
  delay line (`DEFAULT_DTMF_MUTE_DELAY`, marked verify-against-hardware) and a decoded digit —
  surfaced via the new `Controller.on_digit` → shared `DtmfMuteGate` — retroactively condemns
  the buffered tone, then holds mute `mumble.dtmf_mute_hold` (1.0 s, re-armed per digit). New
  settings: `mumble.dtmf_mute` (default on), `mumble.dtmf_mute_hold` (advanced). Browser
  listeners/recordings still carry tones (deliberate; possible follow-up). **Field-verify**:
  dial digits from an HT with a Mumble listener attached — a leading blip means bump the delay
  constant to 0.4.
- **The OTA-code chip is now a button (ADR 0046).** `POST /auth/session` →
  `Controller.open_session()`: same on-air effect as a DTMF login (welcome over, ID armed,
  `session` events) but NO TOTP burn — the LAN token is the credential (the `trigger()` posture),
  so an RF caller's code stays valid. Repeat click = keep-alive. The chip lights green while the
  session is open.
- **Restart from the settings screen (ADR 0047).** `POST /server/restart` runs
  `server.restart_command` (default `systemctl --user --no-block restart radio-server`, matching
  `restart-radio-server.sh`; empty disables), spawn delayed 0.3 s so the reply beats the stop.
  `GET /settings` gained `restart_available`; the UI shows a two-step-confirm Restart button in
  the intro card and the post-save banner. Bench servers (no unit): set the command empty or
  ignore the button's 503. Dev proxy: `/server` added to vite's REST_PATHS.

## Retro-ham visual refresh of the web UI, Day/Night themes (ADR 0044, 2026-07-16)

The operator delivered a design handoff (`design_handoff_visual_refresh/`, local-only — not
committed) and the whole `web/` UI was re-skinned to the banner's warm retro-ham brand:
CSS-custom-property token set (Day on `body`, Night on `body[data-theme="night"]`, toggled from
the masthead and persisted in `localStorage["radio.theme"]`), masthead with a segmented
Control/Settings pill + LCD-style OTA-code chip (countdown bar), a "radio face" hero (state lamp,
frequency LCD + live dial scale on CAT radios, Monitor/Transmit sub-panels with LED-segment
meters), typed badges in the operating log, settings groups as collapsible cards with a floating
save bar, and a redesigned login gate reusing the banner radio SVG. IBM Plex Mono is vendored via
`@fontsource/ibm-plex-mono` (no CDN — LAN may be offline). **Zero functional changes**: all
handlers, hooks, capability gates, polling, and dirty-tracking are untouched (verified by diff
audit; PTT pointer-capture block is byte-identical). Layout moves: the state pill left the Status
card for the face; frequency/mode read out on the face LCD instead of status rows. New rule for
future UI work: text on amber gradients is literal `#3a1d0b`, never `var(--ink)`. Screenshots
under `docs/screenshots/`. Dev nicety: `/services` joined the vite dev proxy (it was missing —
the Services card 500'd only under `npm run dev`). Known dev-only quirk (pre-existing): under
React StrictMode's double-mount the TOTP chip's first fetch is discarded and the chip stays
hidden in `npm run dev`; production builds are unaffected.

## `update-radio-server.sh`: updates no longer strip the extras (2026-07-16)

Second field report: after updating the LAN box the Mumble link failed again with the
"needs the 'mumble' extra" 503 — **not a regression** (the new PR #82 message wording on screen
proved the new code was running). Root cause: `uv sync` is exact, so an update flow of
`git pull && uv sync && restart` *uninstalls* the extras installed at setup; the link worked
until the very next update. Bench-verified nuance (uv 0.11): `uv run` — the systemd launcher —
does an **inexact** implicit sync (`--exact` is opt-in), so service restarts never strip
anything; only an explicit bare `uv sync` does. Fix: checked-in `update-radio-server.sh`
(pull → sync naming all three extras → web build → restart) + a "Updating the server" section in
docs/deployment.md. If another extra is ever adopted on the box, it must be added to the script's
sync line.

## Link-off is un-gated over RF (ADR 0043); OTA login code moved into the header (2026-07-16)

Operator request after living with the link: the session times out while listening to a net, and
dropping the link then required a full re-login. **The disconnect combo (`73#`) now bypasses the
TOTP gate** — `Controller.step` intercepts `_link_off_digits` entries *before* `AuthGate.on_dtmf`
and runs the existing `_run_command` link-off branch (on_link(None) + spoken confirmation,
ID-prepended when due + `link` event), appending a plain `COMMAND` outcome. Deliberate
consequences (all in ADR 0043): connect combos stay gated (they enable TX); anyone on frequency
can key 73# (accepted — de-escalation only); the session is untouched (no activity stamp, no
TOTP burn — a disconnect never extends a session); empty `_link_off_digits` (no entries) means
no carve-out. `AuthGate` itself is unchanged. Web change: **TotpCard is now a compact chip in
the topbar** (visible on both Control and Settings views) instead of a card at the bottom of the
control column; fetch/countdown logic untouched.

## Install docs cover the mumble extra; extra hints say `uv sync`, not pip (2026-07-16)

Field report from the operator's LAN deployment: Connect on the Mumble Link card returned the
PR #79 503 ("needs the 'mumble' extra") because the box never had pymumble installed — and
`docs/install.md` never mentioned the `mumble` extra at all. Worse, it prescribed `uv sync
--extra hardware` **then** `uv sync --extra tts` as two commands; `uv sync` is exact by default,
so the second silently uninstalls the first extra. Fixed: install.md now shows one combined
`uv sync --extra hardware --extra tts --extra mumble` with the exactness caveat spelled out,
`libopus0` joined the apt line (`opus` on the brew line), hardware-bringup.md's lone
`--extra hardware` step carries the same caveat, and configuration.md's link-install hint
switched from `pip install '.[mumble]'` to the uv phrasing. The in-app hints
(`link/pymumble_client.py::_EXTRA_MSG`, `doctor.py` `--link` fail) now say `uv sync --extra
mumble` too — the deployment is a uv-managed source checkout, so the old
`pip install 'radio-server[mumble]'` hint didn't work as written. **Known leftover:** the
hardware/tts/qrcode hints (`backends/aioc_baofeng.py`, `services/tts.py`, `enroll.py`,
`doctor.py` audio/serial checks) still use the pip phrasing — same mechanical fix if it bites.

## Mumble nick is now `<callsign> (radio-server)` — per-entry `username` removed (2026-07-16)

Operator request: the station should identify as the licensee on every Murmur, not carry a
per-entry nick. `link/entries.py::link_username(callsign)` is the single source of truth
(`"AE9S (radio-server)"`; callsign-less bench/mock deployments fall back to the bare
`"radio-server"`). `build_app` threads it into `_pymumble_client_factory` (guarded
`settings.is_set("station.callsign")`); `doctor --link` computes the same nick. The
`MumbleEntry.username` field, the settings-API serialization, the web editor's Username input,
and the example prose are all gone. A config still carrying `username =` in an entry **fails loud
with a tailored message** ("delete the line…"), not the generic unknown-field error. No
SettingSpec change — canary stays 56; `/link` entry payloads simply lose the `username` key.

**Verified:** full suite green; `npm run build` clean; live Docker Murmur
(`mumblevoip/mumble-server`, default config): the nick **`AE9S (radio-server)` — space and
parens — was accepted** by the stock server (doctor `--link` pass + connected client), so no
fallback nick was needed.

## Link announcements configurable, combos on the keypad card, TOTP code in the UI (2026-07-16)

Operator follow-ups after first live use of ADR 0042:

- **`mumble.link_announcement`** (a `{name}` template — the entry name, underscores spoken as
  spaces; validated at load by `coerce_link_announcement`) and **`mumble.link_off_announcement`**
  replace the hardcoded "Linked to <name>." / "Link off." in `build_controller`. Blank = silent
  (the `coerce_optional_str` announcement convention). **Canary 54 → 56**, example regenerated.
  (`mumble.disconnect_dtmf` already existed — the operator asked for it, nothing new needed.)
- **Link combos join the `/services` catalog** (`link:<entry>` per combo + `link-off` for the
  disconnect combo), so the web Services card lists them with the keypad and their Transmit
  buttons fire them via the trigger seam (which already ran link built-ins).
- **`GET /auth/totp`** (token-gated) returns `{code, seconds_remaining, interval}` — the current
  authenticator code, NEVER the secret; new `TotpVerifier.current_code()/seconds_remaining()/
  interval` accessors (read-only, burn intact). New **TotpCard** on the Control screen (local 1 s
  countdown, refetch per step, hidden when unenrolled). Posture note added to docs/operating.md:
  the LAN token already transmits directly, so the code display grants no new capability.
- `restart-radio-server.sh` (operator's systemd-user restart helper) checked in.

**Verified:** full suite green; `npm run build` clean; live smoke on a mock-backend scratch server
(catalog rows, custom announcement in tx_log via trigger, /auth/totp matches pyotp across a step
boundary). Vite proxy gained `/auth`.

## Multiple Mumble servers, DTMF-selectable — ADR 0042 (2026-07-16)

The single hardcoded ADR 0041 link became **N named destinations with one active link** (switch
semantics — one radio, one talker slot). New `docs/adr/0042-multi-mumble-servers.md`; one PR, six
implementation commits (config → manager → controller → API → web UI → docs).

**Config**: `[[mumble.servers]]` array-of-tables (per-entry `name` slug / `host` / `port` /
`username` / `channel` / `dtmf` / `tx_to_rf` / `autoconnect`), a separate channel outside the
SettingSpec schema exactly like `[services]` — `load_mumble_servers()` (raw) +
`link/entries.py::resolve_mumble_entries()` (validated frozen `MumbleEntry`s, fail-loud). The six
flat `mumble.*` connection specs are **removed** (a leftover block fails loud with the migration
snippet); `mumble.tx_hang` stays; new `mumble.disconnect_dtmf` (default `"73"`). **Settings canary
59 → 54.** Per-entry passwords are **dynamic secrets** `mumble_password_<name>` (file or
`RADIO_MUMBLE_PASSWORD_<NAME>` env; the legacy `mumble_password` name is gone) — `secrets.py` gained
a prefix predicate and preserves dynamic keys on rewrite.

**Server**: `link/manager.py::LinkManager` — entries + at most one live `MumbleBridge` (bridge
reused unchanged), **fresh client + bridge per connect** via injected factories, `on_change`
transition callback. `create_app` takes `mumble_entries` + `mumble_client_factory`;
`POST /link {entry?, on}` (404 unknown, 422 ambiguous bare `on:true`, still accepted with a sole
entry, 503 unconfigured — **breaking**: old body was `{on}`); `GET /link/status` → `{active,
entries: [...]}`; the `autoconnect` entry starts in the lifespan. DTMF: link combos are controller
built-ins resolved from the entry list, validated against the `[services]` keypad at build
(exact-string only), auth-gated, spoken confirmations ("linked to <name>" / "link off"), crossing
to the manager via the rebindable `controller.on_link` (task-scheduled, failure-isolated).

**Found + fixed while wiring the UI**: WS `status` frames are RadioStatus-only — **`state.link`
was never populated**, so the Cycle D card only ever rendered from its own poll. Now every manager
transition publishes `Event(type="link", data={entry, state, active, entries})` (the full block),
`useEvents` folds it, and the card seeds itself with one `GET /link/status` on mount. The card
lists every entry (state pill, host/channel/combo/peers, per-entry Connect/Disconnect); the
Settings tab gained **MumbleServersPanel** (add/remove/edit rows, whole-list PUT with atomic 400
handling, write-only per-entry password + set-indicator) over the new
`GET/PUT /settings/mumble-servers` + `POST /settings/mumble-servers/{name}/password`.
`doctor --link` takes an optional entry name (defaults to the sole/autoconnect entry).

**Verified**: `uv run pytest` — **718 passed, 5 skipped** (57 new tests across
config/entries/manager/controller/API); `npm run build` clean. **Live Docker-Murmur rig**
(mumblevoip/mumble-server): the 17 `RADIO_TEST_MURMUR`-gated pymumble tests pass; real server with
two entries — connect → switch (old link fully dropped) → rapid A→B→A stable → disconnect; 404
unknown / 422 bare `on:true` / `link` WS event carries the full block; `PUT /settings/mumble-servers`
persists (collision → atomic 400), the password endpoint lands `mumble_password_backup` in the 0600
secrets file (presence-only in GET); `doctor --link home` PASSes and the no-name ambiguous case
lists the entry names; `autoconnect = true` connects on boot; the served bundle contains the new
panels. Browser look/feel is the operator's check. Follow-ups unchanged: `mumble.bandwidth` spec,
client-cert auth; the dedicated `link` WS event follow-up is DONE (this cycle).

## Mumble link — web UI link card; ADR 0041 roadmap complete (Cycle D, 2026-07-16)

The final ADR 0041 roadmap item: a **Mumble link card** on the Control screen. New
`web/src/components/LinkPanel.jsx` (the StatusPanel + ServiceRow idioms): a state pill
(**Linked** green / **Connecting…** amber, new `.state-pill.state-warn` variant / **Off**), rows for
server/channel/peers, a muted receive-only note when `mumble.tx_to_rf` is off, and a
Connect/Disconnect toggle via `useAction` → the new `client.setLink(on)`. **Hidden entirely when the
link isn't configured** (`state.link` null — the TuneControls hide-don't-grey pattern, ADR 0037).
No new ADR: this executes ADR 0041's roadmap inside the ADR 0022/0037 UI conventions.

Plumbing: `api.js` gained `linkStatus()`/`setLink(on)` (the `POST /link` 503 maps onto the existing
`ControllerUnavailable` typed error); `LinkPanel` renders from the WS-folded `state.link` (the
`/events` `status` frames already carry the `link` block) wired in `ControlPanel` after
`StatusPanel`; `web/vite.config.js` proxies `/link` (was missing → dev-server 404).

**Deliberate trade:** there is no dedicated `link` WS event, and link connect is non-blocking — the
status snapshot published by `POST /link` usually still says `connected:false`. So while the link is
running the card **polls `GET /link/status` every 5 s** (plus once immediately, and it applies the
`POST /link` response body), preferring the fresher local snapshot until the next WS status frame.
Follow-up if the poll ever bothers anyone: emit a `link` event from the bridge on connect/disconnect
(needs a thread-safe hop — the pymumble connected callback fires on the library thread).

**Verified live** (Docker Murmur + real server + built bundle): served JS contains the card;
autostart → `connected:true`; `POST /link {on:false}` → `running:false`, `{on:true}` → reconnected;
`/status` carries the block the WS fold feeds. `npm run build` clean; `uv run pytest` unchanged
(**653 passed, 5 skipped** — no Python changes). No UI test framework exists (none added).

**ADR 0041 is now fully delivered** (A design #73, B bridge+streaming-ID #74, C pymumble client #75,
D this cycle). Remaining nice-to-haves: dedicated `link` WS event, `mumble.bandwidth` as a settings
spec, client-cert auth for registered Murmur identities.

## Mumble link — real pymumble client, live-Murmur verified (ADR 0041 Cycle C, 2026-07-16)

Implements ADR 0041's roadmap **Cycle C**: the real network client. `_build_mumble_client` no longer
raises `NotImplementedError` — `mumble.enabled=true` now builds a working `PyMumbleClient`
(`radio_server/link/pymumble_client.py`), and the whole link was **verified against a live Murmur**
(Docker `mumblevoip/mumble-server`, both 1.5.901 and 1.4.230).

**Empirical facts locked this cycle (guardrail 1 — each bench-confirmed):**
- **PyPI pymumble 1.6.1 cannot connect on Python 3.12** (`ssl.wrap_socket` removed). The azlux
  `pymumble_py3` branch fixed SSL in Nov 2023 but never released; the `mumble` extra is now **pinned
  to the branch-head git SHA `a560e60`** (needed `[tool.hatch.metadata] allow-direct-references`).
  Revisit when a >1.6.1 release lands.
- **Uncapped bandwidth = silent audio loss.** pymumble adopts the *server's* max bandwidth (Murmur
  default 558 kbps) as its Opus target → ~1.3 KB voice frames exceed Mumble's ~1 KB voice-packet
  limit → the server drops every frame with no error (confirmed on 1.4 AND 1.5: zero audio uncapped,
  clean audio capped). Fix: `set_bandwidth(96000)` **re-applied on every (re)connect** (the library
  resets it per connection) — `DEFAULT_MUMBLE_BANDWIDTH` in `pymumble_client.py`, a constructor
  param (not a settings spec yet; add one if operators need to tune it).
- **`is_ready()` blocks forever on an unreachable server** → `connect()` never calls it (the bridge
  connects on the event loop). Non-blocking connect + the `connected` callback (bandwidth cap +
  channel join, so both re-apply after auto-reconnect); `status()` polls readiness.
- **Branch-head quirk:** `sound_output` only exists when `set_receive_sound(True)` was called (and
  only after connection init) — the adapter always enables receive and guards every access.
- **The library thread is non-daemon with an uninterruptible retry sleep** — it held the process
  open at exit and raised into a dying interpreter. The adapter daemonizes it before `start()`.

**Shipped:** `PyMumbleClient` (lazy-import `_pm()` seam + injected `_pymumble` fake, the AiocBaofeng
pattern; sound-received → `on_audio` forward; connected → cap + join, missing channel survived;
guarded `send_audio`; peers = channel users minus self), `_build_mumble_client` real construction,
`doctor --link` (read-only connect check, `--host`/`--port` overrides, exit 0/1 verified both ways).

**Live verification (the "plug it in" bar, all passed):** two-client audio loop through Murmur 1.5
and 1.4 (`RADIO_TEST_MURMUR=host:port` gates the pytest version, skipped otherwise); full composed
app (`build_app`, mumble.enabled) autostarted the bridge, `GET /link/status` showed connected+peers,
mock-radio RX audio was heard by an independent pymumble listener in the channel, and a real Mumble
talker keyed the mock radio with the **byte-exact 2.22 s CW station ID leading the over** (Part 97).

**Tests:** `uv run pytest` → **653 passed, 5 skipped** (637 baseline + 16 fake-module client tests;
the 5th skip is the gated live test). `test_link_api.py`'s NotImplementedError test replaced with a
composes-`PyMumbleClient` assertion (construction is import-free, runs without the extra).

**Next (ADR 0041 roadmap):** Cycle D = web UI link card. Possible follow-ups: `mumble.bandwidth` as
a settings spec; certfile/keyfile support for registered-identity servers.

## Mumble link — bridge core + shared streaming station ID (ADR 0041 Cycle B, 2026-07-16)

Implements ADR 0041's roadmap **Cycle B**: the RF↔Mumble bridge against a mock client (no network,
no `pymumble`), plus the streaming station-ID seam it needs — which also **closes a pre-existing gap:
the browser `/audio/tx` talker transmitted un-ID'd** (only the DTMF/dispatcher path went through
`StationId`). The operator chose the full-bridge + shared-ID-fix scope.

**Streaming station ID (Part 97, guardrail 5).** New `StreamingId` in `services/station_id.py` — a
**radio-free** ID scheduler (reuses `IdEncoder`/`load_callsign`/`load_id_interval`/`load_id_mode` +
the `_due` interval logic) that *renders* ID audio on demand instead of owning a radio like
`StationId`. `TxSession` gains an optional `station_id` (a new `TxIdentifier` protocol, Protocol-here
/ concrete-elsewhere like `TxRecorder`, so the `tx -> {audio,backends}` arrow is intact): it transmits
ID into the **same keyed over** at key-up (when due), across the ≤10-min boundary, and at key-down
(due-gated so rapid short overs aren't ID'd every time). Default `station_id=None` → historical
un-ID'd behaviour, so every existing tx test is unchanged. `build_app` builds **one shared**
`StreamingId` (gated on `station.callsign` being set; CW mode needs no TTS) and passes it to BOTH the
`/audio/tx` `TxSession` and the bridge.

**The bridge is a peer, not a backend.** New `radio_server/link/`: `client.py` (`MumbleClient`
Protocol + `MockMumbleClient` + `DEFAULT_MUMBLE_*`), `bridge.py` (`MumbleBridge`). RF→Mumble = an
`AudioHub` subscriber holding a pump demand; Mumble→RF = `on_audio` (client thread) →
`loop.call_soon_threadsafe` → bounded drop-oldest queue → drain task that keys a `TxSession` sharing
the single `TxSlot` + arbiter + the shared `StreamingId`, with a hang timeout to unkey. Defers to a
live RF signal via a new `RxPump.active` property. `tx_to_rf=False` runs receive-only.

**Config/API.** New `[mumble]` group (`enabled`/`host`/`port`=64738/`username`/`channel`/`tx_to_rf`
=true/`tx_hang`) in `config/spec.py`; `mumble_password` secret in `config/secrets.py`. Token-gated
`GET /link/status` + `POST /link` (503 when unconfigured), plus a `link` block in `GET /status`.
`create_app` gained `station_id`/`mumble_client`/`mumble_tx_to_rf`/`mumble_tx_hang`/`mumble_autostart`
kwargs; the lifespan autostarts/stops the link. **Real client deferred:** `_build_mumble_client`
raises `NotImplementedError` (the SignaLinkV71 stub posture) — enabling the link fails loud until the
`pymumble` bring-up. New optional extra `mumble = ["pymumble>=1.6"]` (needs system `libopus0`).

**Tests:** `uv run pytest` → **637 passed, 4 skipped** (608 baseline + 29). New: `test_streaming_id.py`,
`test_link_bridge.py` (asyncio.run, mock client + MockRadio), `test_link_api.py`; `test_tx_audio.py`
extended (key-up/periodic/sign-off ID + a WS-level "browser talker is now ID'd" proof + un-ID'd
regression guard). Settings canary **52 → 59**; `radio.toml.example` regenerated. `make_secrets` gained
`mumble_password`. Note: `/link` routes are inline in `app.py` (next to `/controller`), not a separate
`register_link_routes` module — 2 small routes, lower surface.

**Next (ADR 0041 roadmap):** Cycle C = real `PyMumbleClient` behind the `mumble` extra (implement
`_build_mumble_client`, live-Murmur talk-through, a `doctor` link check); Cycle D = web UI link card.

## Mumble/Murmur link — design ADR only (ADR 0041, 2026-07-15)

**Ask:** the operator wants to bridge radio-server to a self-hosted **Murmur** (Mumble server) so an
RF radio and a Mumble channel share audio — impromptu-ham-net RF↔VoIP linking. This is the leaner
successor to the **reverted M17 arc** (cycles 41–58: hand-rolled Link protocol + M17 backend + mrefd
reflector + Codec2 + `/link*` routes, all rolled back to Cycle 40): Mumble reuses a mature TLS+Opus
VoIP stack with a maintained Python client instead of a bespoke protocol/reflector/vocoder.

**This cycle is design-only** (operator's choice): a single new ADR, no code. `docs/adr/0041-mumble-link.md`.

**Feasibility: high.** The seams already exist and the audio format matches exactly, so the bridge is
mostly glue:
- Canonical audio = 48 kHz/s16le/mono/20 ms (ADR 0006) == Mumble/Opus 48 k mono → **no resampling on
  the Mumble seam** (unlike the reverted Codec2 path).
- RF→Mumble: the bridge is one more `AudioHub` subscriber (`rx/hub.py`, bounded-queue drop-oldest),
  like a browser `/audio/rx` listener.
- Mumble→RF: the bridge is a TX client through `TxSession`/`TxSlot` + `RadioArbiter` (half-duplex,
  TX-priority) + the RMS activity gate — the existing key-from-external-source primitives.
- pymumble's threads bridge into asyncio via bounded thread-safe queues with drop-oldest — the
  `MultimonStream` reader/writer pattern (ADR 0038/0040); a stuck network drops audio, never blocks
  the loop.

**Key decisions in the ADR:** the bridge is a **peer, not a `Radio` backend** (new `radio_server/link/`,
not in `backends/factory.py`); a `MumbleClient` **Protocol + `MockMumbleClient`** so the whole bridge
is testable with no server (real `PyMumbleClient` is a later hardware-like bring-up cycle);
**Mumble→RF default on when linked** (operator's choice) but a `mumble.tx_to_rf=false` switch drops to
receive-only; **auto station ID (ADR 0005) must cover bridge-originated TX** (Part 97, guardrail 5);
`[mumble]` config group (ADR 0025) with the server password/cert on the **separate 0600 secrets
channel**; token-gated `GET /link/status` + `POST /link` (ADR 0011), independent of `capabilities()`;
new lazily-imported optional extra `mumble = ["pymumble_py3", ...]` needing system `libopus0`.

**Roadmap (in the ADR):** A = this design cycle; B = bridge core vs `MockMumbleClient`+`MockRadio`
(protocol, state machine, arbiter/gate/station-ID wiring, `[mumble]` config, `/link` routes, tests);
C = real `PyMumbleClient` bring-up behind the extra + `doctor` link check; D = web UI link card.

Docs-only cycle — no code touched, `uv run pytest` baseline (602 passed, 3 skipped) unchanged. Next
cycle to implement should start at roadmap Cycle B.

## Streaming DTMF decode — fixes dropped repeated digits like `99#` (ADR 0038, 2026-07-15)

**Problem:** over-the-air DTMF codes with a repeated adjacent digit (notably `99#`, logout) dropped a
digit and failed, while all-distinct codes (`01#`) never missed. Root cause was the ADR 0030
fixed-window path: it ran a fresh `multimon-ng` per ~0.5 s window and papered over window-boundary
double-counts with a lossy held-tone de-dup that also ate genuine repeats unless a fully-silent
window fell between the two presses.

**Fix (ADR 0038):** realize ADR 0030's deferred "persistent streaming multimon process". New in
`radio_server/audio/dtmf.py`: `DtmfStream` protocol, `MultimonStream` (one long-lived
`multimon-ng -a DTMF -t raw -` with a daemon reader thread → thread-safe queue, restart-on-death,
`atexit`/`close()` reaping), and `StreamingDtmfInput` (same `pump`/`flush` surface as
`BufferedDtmfInput`, **no de-dup** — multimon does its own onset/gap detection). Empirically verified
against multimon-ng 1.3.1: a held tone emits once, two presses emit twice even at a 30 ms gap.

**Toggle:** `dtmf.decode_mode` (`streaming` default | `buffered` fallback, env
`RADIO_DTMF_DECODE_MODE`, Advanced tier). `buffered` keeps the ADR 0030 path verbatim as a one-line
in-field revert (guardrail 1). Wired in `build_controller` (an injected `decoder` still forces the
buffered path, so all existing controller tests are unchanged), `Controller.close()` reaps the
process, called from the API lifespan shutdown. `doctor --dtmf` uses the same streaming path via a
shared `_drive_dtmf` loop.

Settings canary **49 → 50**; `radio.toml.example` regenerated with `dtmf.decode_mode`. `uv run pytest`
reports **602 passed, 3 skipped** (592 baseline + 9 streaming tests incl. a `skipif`-guarded
real-multimon `99#`→`"99"` regression, + 1 config case). Buffered-vs-streaming A/B confirmed: same
`99#` input yields `['9']` (old) vs `['99']` (new).

## Restore — PR #50 (web-UI simplification, ADR 0037) reinstated (2026-07-15)

After the cycles-41-58 revert (below), **PR #50 was restored on its own** — it was authored outside
the reverted arc and is wanted back. Master is now **pre-#48 plus #50, and nothing else**. No other
reverted PR was reinstated.

#50 was cherry-picked as its single work commit `41993bf` onto the reverted master; it applied cleanly
with no conflicts (it touches `web/` plus two config keys and does **not** depend on the #48/#49 ledger
work — verified: no `rx_open`/`rx_close`/`activity`/reader references). It brings the status pill
(collapsing Transmitting/Busy/Arbiter and capability-gating the CAT rows), removal of the PTT and
Controller cards, `controller.autostart` + `web.auto_listen` (both default on), hold-to-talk vs
click-to-toggle, opt-in token persistence + Log out, card reordering (Listen + Talk lead), Basic vs
Advanced settings tiers (`SettingSpec.advanced`), a `styles.css` pass, and ADR 0037. The settings
canary went **47 → 49** and `radio.toml.example` regained `controller.autostart` / `web.auto_listen`.
`uv run pytest` reports **592 passed, 3 skipped** (589 baseline + #50's 3 controller-autostart tests).
`web/dist` (gitignored) was rebuilt so the served UI matches.

**Operator note:** #50's two keys are back in `radio.toml.example`. A live `radio.toml` that predates
#50 will fall back to the defaults (`controller.autostart = true`, `web.auto_listen = true`); add them
explicitly only to override.

## Revert — cycles 41-58 rolled back (2026-07-15)

Cycles 41-58 (PRs #48–#66) were reverted wholesale, rolling the tree back to `703177e` — the commit
immediately before PR #48 (cycle 41, "persist RX activity to the event ledger") merged. The revert is
a single new commit on top of master, so it is itself undoable and rewrote no history; the reverted
work stays on GitHub, cherry-pickable, on its cycle branches. After the revert `uv run pytest` reports
**589 passed, 3 skipped**, and every tracked file is byte-identical to `703177e`.

**The real code state is Cycle 40**, described under "Current state" below. Everything the reverted
cycles built is gone from the code: the RX-activity ledger + channel-activity summary + `/activity`
panel (41-45), the whole Link/M17 arc — Link protocol, mock + M17 backends, mrefd UDP client, Codec2
seam, wire codec, inbound/outbound link audio, TX limiter, `/link*` routes, `doctor --link`
(46-58), optional over-RF TOTP, and the web-UI simplification's link/activity surfaces. The next
cycle continues from Cycle 40, not Cycle 59.

Two gitignored files git does not own were NOT reverted and may need hand-cleanup (see the revert PR
for specifics): `radio.toml` (delete any `[link]`/`[activity]` sections and `controller.require_auth`
/`controller.autostart`/`web.auto_listen` keys if present — the bench copy here had none) and
`radio-server.jsonl` (holds inert `rx_open`/`rx_close` records from cycle 41; nothing pre-#48 reads
the ledger, so removal is optional). `web/dist` (also gitignored) was rebuilt from the reverted source.

## Current state

Cycle 40 follow-up: **the built-ins (`station-id`/`logout`) are operator-assignable too.** Per review
feedback ("#4 and #99 need to be configurable too"), the two controller built-ins are no longer
reserved-digit special cases — they are ordinary entries in the same `[services]` keypad map, keyed by
stable ids `station-id` / `logout` (`BUILTIN_IDS` in `services/plugin.py`). `RESERVED_DIGITS` is gone;
`resolve_bindings` now accepts service **and** built-in ids and no longer rejects `4`/`99` (there are no
reserved digits); `build_registry` skips built-in ids (no `Service` to build). New
`builtin_digits(bindings, id)` reports which digit(s) a built-in sits on; `build_controller` derives
`id_digits`/`logout_digits` from the bindings and passes them to the `Controller`, which matches
incoming digits against those frozensets in `_run_command` (was `== PLAY_ID_DIGIT`/`LOGOUT_DIGITS`
module constants, now **removed**). The catalog's built-in entries are derived from the bindings, not
hard-appended. `DEFAULT_BINDINGS` now includes `"4":"station-id","99":"logout"` (default keypad
unchanged). A `[services]` table is the **complete** keypad: an omitted built-in is off the keypad
(auto-ID + idle timeout still run) — documented in README, ADR 0034 (amended, not superseded), and the
regenerated `radio.toml.example`. Folding both into one TOML table makes service/built-in digit
collisions impossible by construction. New tests cover remapping built-ins over the air, the old digits
going inert after a remap, omission, and `builtin_digits`. `uv run pytest` → **589 passed, 3 skipped**.
Same branch/PR (`cycle-40-pluggable-voice-services` → #44).

Cycle 40: **pluggable voice-service architecture** (ADR 0034). Formalized the existing
`ServiceRegistry`/`Service`/`ServiceContext` seam into a `ServicePlugin` contract and retrofitted all
six services (time/weather/astro/quote/battery/bible) onto it — **behavior-preserving** (every
per-service formatter/factory test unchanged; the settings canary stays 47). New
`radio_server/services/plugin.py`: `ServicePlugin` Protocol (`id`, `description`, `enabled(settings)`,
`build(ctx) -> Service`), `PluginBuildContext` (carries `Settings` + a **lazily-built, memoized shared
`Fetcher`** — reproduces ADR 0033's "one fetcher on first enabled fetch service"), the in-tree
`PLUGINS` tuple, `DEFAULT_BINDINGS`, `RESERVED_DIGITS` (`{"4","99"}`), `resolve_bindings` (fails loud on
reserved/unknown/non-DTMF), and `build_registry`. Each `*_service.py` gained a small `PLUGIN` singleton
wrapping its **unchanged** factory; the `register()` free functions were **removed** (5 helper tests +
engine updated to register via the factory / plugins). **Operator-assigned digits:** a new `[services]`
TOML table maps digit→service id — a **separate config channel** (like secrets; arbitrary digit keys
don't fit the `SettingSpec` schema). `config/settings.py` peels `[services]` off before schema
resolution (`_flatten`) and reads it via new `load_service_bindings`; `save_settings` leaves the table
intact (only rewrites schema keys); `render_example` emits a documented `[services]` block
(`radio.toml.example` regenerated). `build_controller` gained `service_bindings=None` (defaults to
`DEFAULT_BINDINGS`) and **replaced the imperative registration block** with
`build_registry(PLUGINS, resolve_bindings(...), PluginBuildContext(settings, fetcher))`; `build_app`
loads bindings via `load_service_bindings(config_path)`. New tests: `test_service_plugin.py`,
`test_service_bindings.py`; `test_services_catalog.py` gained remap/reserved/unknown cases.
`uv run pytest` → **582 passed, 3 skipped**. Verified end-to-end through the real composition root: a
remapped keypad (time→8#, weather→9#) transmits correctly; an unbound digit is a graceful miss; `4`/`99`
stay controller built-ins. **Adding an in-tree service** is now: write the module + plugin, append to
`PLUGINS`, add its default digit + scalar settings — `build_controller` is untouched. Scope is in-tree
(no pip/entry-point discovery — a Part-97/guardrail-4 trust decision left for later behind an explicit
opt-in). Branch `cycle-40-pluggable-voice-services` from freshly-pulled `origin/master` (`08143f2`), PR
against `master`.

Cycle 34: **weather (2#) + astronomy (3#) DTMF voice services** reading a LAN weather station, plus a
`/services` catalog. New **HTTP fetch seam** `radio_server/services/fetch.py`: a `Fetcher` protocol
(`fetch_json(url) -> Mapping`, mirrors `TtsEngine`), a real `UrllibFetcher(timeout)` over stdlib urllib
(**no new dependency**; the single network-dependent path, wraps every failure as `FetchError`), and a
`StubFetcher` (canned JSON) for tests. Two services mirroring `time_service`
(`radio_server/services/weather_service.py` `2#`, `astro_service.py` `3#`): pure formatters
`format_spoken_weather` (`sensors.outdoor.derived.{temperature_f, feels_like_f,
absolute_humidity_g_m3}` → *"Outdoor temperature 78 degrees. Feels like 78. Absolute humidity 8.1 grams
per cubic meter."*) and `format_spoken_astro` (`astronomy.sun.{sunrise,sunset}` +
`astronomy.moon.{phase_name,moonrise,moonset}`, ISO→local 12-hour, null moon → "not available" →
*"Sunrise 5:43 AM, sunset 8:26 PM. Moon phase New Moon. Moonrise 7:03 AM, moonset 9:10 PM."*). Each
service factory (`weather_service(base_url, fetcher)`) binds URL+fetcher at construction and catches
`FetchError`/`KeyError` → speaks an "unavailable" line (a dead station never crashes the loop; the GET
runs in the controller loop so `weather.timeout` defaults to a short **3 s**). **Config:** new `weather`
group — `weather.base_url` (`RADIO_WEATHER_URL`, optional, default `""`) and `weather.timeout` (default
3.0); settings-API canary 37→39; `radio.toml.example` regenerated; `save.py` banner. **Registration:**
`build_controller` gains an injectable `fetcher=None` and registers weather+astro **only when
`weather.base_url` is set** (else the digits are graceful misses). **`ServiceRegistry.register` gained a
`description`; `catalog() -> [{digit,name,description}]`** surfaced on `Controller.service_catalog` and
a new **`GET /services`** endpoint (token-gated; `[]` when no controller) — drives the web UI panel
(PR B) + the README table. **`uv run pytest` → 503 passed, 3 skipped** (+ test_fetch, test_weather_service,
test_astro_service, test_services_catalog). **Verified live against the real station** (192.168.1.62):
both formatters + the real `UrllibFetcher` produce the natural-language lines above. Docs: README "DTMF
voice services" table. Also set the operator's local `radio.toml`: `[time] tz="America/Denver"` (1# now
speaks 24-hour local time, was UTC) + `[weather] base_url`. Cut from freshly-pulled `origin/master`
(cycle 33 / PR #35 merged, `63cdc6d`); branch `cycle-34-weather-astro-services`, PR against `master`.
**Follow-ups queued:** PR A2 — announce on successful auth + on session timeout/de-auth (controller
voice); PR B — web UI hide-unsupported-controls + services panel; PR C — RX activity in the event log.

Cycle 33: **single capture reader — one `receive()` feeds both the browser and the DTMF controller**
(ADR 0031). Root-causes why over-RF DTMF login did **nothing** on the bench even after cycle 31: (1)
`ControllerRunner` read one ~20 ms AIOC block then slept `controller.poll` (**0.5 s**), sampling ~4% of
the audio into non-contiguous slivers that multimon can never lock; (2) `RxPump` (browser Listen) and
`ControllerRunner` were **two independent `receive()` loops on one single-open capture**, stealing each
other's blocks — Listen made it strictly worse. Both files had literally deferred "one `receive()`
feeding both `controller.step` and this pump" as a hardware decision. **Fix:** `RxPump` is now the
single reader — it reads back-to-back and, when a `controller` is set, calls `controller.step(now,
frame)` on the **raw** frame FIRST (guarded), then the gate→hub→recorder path (`radio_server/rx/pump.py`).
`build_app` no longer creates a `ControllerRunner` (class kept, retired from the live path);
`build_controller` still builds the controller. Lifecycle is **reference-counted demand** in
`create_app`: the reader runs while a `/audio/rx` listener is connected OR the controller is active —
`POST /controller {on}` and `/audio/rx` connect/disconnect each `_acquire_rx`/`_release_rx`
(`radio_server/api/app.py`); `_controller_state.running` now reports `controller_active`. `controller.poll`
is vestigial for DTMF. **Why the cycle-31 test missed it (operator's point — this WAS mockable):** it
fed a `FakeDtmfDecoder` returning whole pre-formed entries, never exercising `receive()` cadence /
real accumulation / real multimon / contention. **New `tests/test_controller_rx_e2e.py`** is the test
that would have caught it: a TOTP code rendered as **real `synth_dtmf` sliced into 20 ms blocks**
(0.5 s tone + 0.5 s silence per key) decoded by **REAL multimon** through the real `BufferedDtmfInput`
→ `session.authenticated` (fails on the old design); plus a proof that ONE `RxPump` feeds both a
`controller` and a hub subscriber from one `receive()`. **`uv run pytest` → 483 passed, 3 skipped.**
**Verified live:** the fixed server starts against the real AIOC, `POST /controller {on:true}` →
`running:true`, the reader pumps the card continuously with no errors (browser `/events` connected).
Docs: ADR 0031, `docs/hardware-bringup.md` DTMF note updated. **The last inch — a human keying a
DTMF code over RF — cannot be automated (no self-loopback on a half-duplex radio); the live decode path
is now byte-identical to `doctor --dtmf`, which already decodes real keyed tones on this hardware.**
Cut from freshly-pulled `origin/master` (cycle 32 / PR #34 merged, `0e62dfc`); branch
`cycle-33-single-rx-reader`, PR against `master`.

Cycle 32: **TOTP enroll CLI for Google Authenticator** (`python -m radio_server.enroll`) — the
companion to cycle 31: now that the live controller decodes over-RF DTMF, the operator needs an easy
way to get the TOTP secret onto their phone. Before this there was no CLI — minting meant the
authenticated REST endpoint `POST /settings/secrets/totp/enroll` or hand-running `pyotp.random_base32()`.
New **`radio_server/enroll.py`**: `enroll(secrets_path, account, *, force, out, env)` mints a fresh
secret via `rotate(path, "totp_secret")` (writes `radio-secrets.toml` `0600`), builds the `otpauth://`
URI via `TotpVerifier.provisioning_uri`, **always prints the base32 secret + URI**, and **renders a
scannable terminal QR** when the optional **`qrcode`** package is importable (soft import; falls back
to a "install the hardware extra" hint + the URI otherwise). Re-enrolling mints a NEW secret and
invalidates the phone's current one, so an existing secret is **refused without `--force`**. `env`
defaults to `os.environ` (respects an ambient `RADIO_TOTP_SECRET`); tests pass `env={}` to isolate.
`main([...])` wires argparse (`--secrets`/`--account`/`--force`). Nothing transmits or touches the
radio. **`qrcode>=7` added to the `hardware` optional extra** (kept optional — the CLI degrades
gracefully; consistent with ADR 0003's no-required-image-dep stance). Docs: `docs/hardware-bringup.md`
gained an "Enrolling Google Authenticator (DTMF login)" section (run enroll → scan QR → set callsign +
voice → restart → key `<code>#` then `1#`), README secrets section points to it. **`uv run pytest` →
480 passed, 4 skipped** (+5 `tests/test_enroll.py`: mints+persists a base32 secret at `0600` with the
URI+account shown, refuses-without-force, `--force` replaces, qrcode-absent URI fallback, qrcode-present
QR render via `importorskip`, `main` writes the named file; the 4th skip is the qrcode QR test when the
dep is absent). **Verified live:** `uv run --with qrcode python -m radio_server.enroll` mints, writes
`0600`, and renders a clean scannable QR + secret + URI. Cut from freshly-pulled `origin/master` **after
PR #33 (cycle 31) merged** (`cd788b5`); branch `cycle-32-totp-enroll-cli`, PR against `master`. NOT
stacked on cycle 31 — it was rebased onto the merged master. **Deferred:** QR is best-effort terminal
rendering (invert=True for dark terminals; the URI/secret always print as the reliable fallback).

Cycle 31: **buffer DTMF audio in the live controller** (ADR 0030) — closes the cycle-30 flagged
limitation so **over-RF TOTP auth actually decodes**. Root cause: `Controller.step` decoded one
`receive()` frame at a time (~20 ms on the AIOC), far too short for multimon to lock a tone (~40–200
ms), so keyed codes never decoded on the live server even with a secret + callsign configured. The
fix promotes the accumulate-and-dedup logic the operator already bench-proved in `doctor --dtmf` into
a shared **`BufferedDtmfInput`** (`radio_server/audio/dtmf.py`, same `pump(frame, now) -> list[str]`
surface as `DtmfInput`): it buffers frame bytes until a **`dtmf.buffer_seconds` window** (default 0.5
s, `dtmf_window_bytes`) then decodes the chunk, **de-dups held tones** (consecutive identical digits
collapsed; a **silent window resets** the run, so a genuinely-repeated key needs a brief pause),
feeds the framer, and returns completed entries; `flush(now)` drains the tail; an optional `on_digit`
hook surfaces each key for live display. **`doctor.py`'s `collect_dtmf` is refactored onto the same
core** (behavior identical — the existing collect_dtmf tests, incl. the real-multimon round-trip,
pass unchanged), so the tool and the live controller share ONE decode path. `build_controller` now
wires `BufferedDtmfInput` (window from `load_dtmf_buffer_seconds`); `Controller.step`'s
`self._dtmf.pump(...)` call is unchanged, and the station-ID/idle checks still tick every poll (only
the *decode* buffers). **`dedup` is a `build_controller` test seam** (default True for production):
the existing controller tests feed a `FakeDtmfDecoder` that returns whole pre-formed entries per
call, which would fold a code's repeated digits, so `build_ctrl` (and the event-log-wiring test) pass
`dedup=False` + a tiny `dtmf.buffer_seconds=0.02` window to keep the per-over cadence. New config
`dtmf.buffer_seconds` (spec.py, `RADIO_DTMF_BUFFER_SECONDS`, positive-float, verify-on-hardware) →
settings-API canary 36→37, `radio.toml.example` regenerated. **`uv run pytest` → 475 passed, 3
skipped** (+ new `tests/test_buffered_dtmf.py`: accumulate-until-window, cross-window framing,
held-tone dedup + silent reset, dedup-off, flush tail, real-multimon round-trip, window-bytes math;
+ `test_controller.py::test_login_accumulates_from_short_frames_over_the_buffered_loop` proving a
code arriving in ~20 ms frames authenticates via the buffered loop with dedup on). Docs:
`docs/hardware-bringup.md` "Testing DTMF decode" note rewritten (over-RF auth now decodes; pause
between repeated code digits; `dtmf.buffer_seconds` knob), ADR 0030. Cut from freshly-pulled
`origin/master` (cycle 30 / PR #32 merged, `e1e11ab`); branch `cycle-31-controller-dtmf-buffering`,
PR against `master`. **FOLLOW-UP (separate branch, not stacked):** PR B — a `python -m
radio_server.enroll` CLI to mint the TOTP secret + print the `otpauth://` URI + a terminal QR (soft
`qrcode` dep) so the operator can load Google Authenticator. **Deferred (noted in ADR 0030):** a
persistent streaming multimon process (more robust to tones split across a window boundary; the
fixed-window accumulator was chosen for simplicity and is already bench-proven — a boundary split
fails *safe*: a corrupted digit just rejects the code, never a false accept).

Cycle 30: **DTMF decode test tool** (`doctor --dtmf`) — the operator wanted to test DTMF decode on
the AIOC. Findings: `multimon-ng` (the decoder the server shells out to) wasn't installed (now is,
1.3.1), and there was no way to watch DTMF decode from the radio — the live DTMF path is gated on a
TOTP secret AND `controller.step()` decodes **one ~20 ms `receive()` frame at a time**, far too short
for multimon to lock onto a tone (needs ~40–200 ms). New **`radio_server/doctor.py --dtmf`** (read-
only, no keying): builds the AIOC backend and runs **`collect_dtmf(radio, decoder, framer, *, seconds,
chunk_bytes, clock, on_event)`** — a pure helper that **accumulates received audio into ~0.5 s chunks**
(`DEFAULT_DTMF_CHUNK_BYTES`) before each `MultimonDtmfDecoder.decode`, feeds digits to `DtmfFramer`
(`#` submits, `*` clears), and prints each digit + completed entry live. Reuses the existing
decoder/framer + the `--rx-level` scaffolding; handles multimon-missing and capture-busy with clean
messages. **Verified live (no hardware needed):** `collect_dtmf` over a `MockRadio` serving
`synth_dtmf("123#")` through REAL multimon decodes entry `123`. **`uv run pytest` → 465 passed, 3
skipped** (+4: `collect_dtmf` accumulate/frame with a fake decoder, silence, and a multimon round-trip
gated on the binary; the pre-existing `test_dtmf` real-decode test now RUNS since multimon is
installed — 4→3 skips). Docs: `docs/hardware-bringup.md` gained a "Testing DTMF decode" section
(install multimon → pytest self-test → `--dtmf` from the radio). **FLAGGED FOLLOW-UP (next cycle,
own ADR):** the live controller's per-frame DTMF decode almost certainly won't decode real over-RF
tones — fix is to buffer received audio into ~0.3–0.5 s windows (or stream a persistent multimon
process) in the controller; `--dtmf` is the tool that confirms the need. Cut from `origin/master`
(cycle 29 merged, `407958a`); branch `cycle-30-dtmf-diagnostic`, PR against `master`.

Cycle 29 (cont.): **AIOC audio-level diagnostics** — added to the same PR #31 after bench testing
showed keying works but audio doesn't audibly flow (unverified levels, guardrail 1). Root causes
confirmed in code: RX "Listen" is silent because `audio.squelch=audio` gates on a software VAD
(`vad_on_rms=500`) and the AIOC's received level (which follows the UV-5R volume knob + the card's
ALSA capture level) sits under it; TX "Talk" transmits the **computer mic** (not the radio) and the
local monitor mutes while keyed. Deliverables: **`python -m radio_server.doctor --rx-level`**
(read-only — reads `receive()` for N s, reports RMS/peak in int16+dBFS vs the VAD thresholds and
recommends `vad_on/off` values or flags "no audio arriving"; pure `measure_rx_levels(radio, seconds,
clock)` reused-`frame_rms` helper, MockRadio-testable) and **`--tx-tone`** (RF, same dummy-load
CONFIRM guard as `--key-test` — one-shot `transmit(synth_tone(...))` into a dummy load to prove TX
audio without the browser mic). Also: `web/src/useTxAudio.js` now requests the mic with
`echoCancellation/noiseSuppression/autoGainControl:false` (raw mic for radio, not call-DSP);
`docs/hardware-bringup.md` gained an "Audio levels & squelch" bring-up flow (squelch=off → alsamixer
+ UV-5R volume → `--rx-level` → set VAD → squelch=audio → `--tx-tone`). **Verified live on the
bench:** `--rx-level` reads real audio and correctly reports it as arriving-but-gated (~112 RMS vs
threshold 500); `--tx-tone`/`--key-test` refuse non-interactively (RF safety). **`uv run pytest` →
461 passed, 4 skipped** (+7: new `tests/test_doctor.py` — level summary, silence, the classify
branches, RF-refusal). Web build clean (51 modules). **AIOC bring-up COMPLETE — full talk-through
confirmed on the bench:** operator raised `alsamixer` + UV-5R volume (received signal then measured
~5675 RMS avg / 25837 peak-block via `--rx-level`), set `audio.vad_on_rms=1000`/`vad_off_rms=500`
(squelch=audio); browser **Listen** gates on real audio, `--tx-tone` was heard on a second radio, and
**Talk** (computer mic → radio) works. Also added a graceful "capture busy — stop the server" message
to `--rx-level` (the AIOC sound card is single-open; the doctor and server can't share it). The
tuned VAD values live in the operator's gitignored `radio.toml`. **AIOC/Baofeng is production-ready.**

Cycle 29 complete: **AIOC/Baofeng hardware backend bring-up** (ADR 0029) — the real `AiocBaofeng`
is implemented; it was a `NotImplementedError` stub. The AIOC cable is physically plugged in and was
**empirically confirmed** (guardrail 1): USB `1209:7388`, PTT serial `/dev/ttyACM0` (stable by-id
`usb-AIOC_All-In-One-Cable_da3441ac-if04`, group `dialout`, operator in `dialout`), ALSA card
`hw:CARD=AllInOneCable` (48 kHz-native capture+playback). **The backend** (`radio_server/backends/
aioc_baofeng.py`) is a pure DI object (Settings-free, like `MockRadio`): `sounddevice` `RawInput/
OutputStream` for TX/RX (48 kHz, no resample — bytes straight through), `pyserial` control line for
PTT. **Keying model:** `transmit()` self-keys only when the line isn't already held — a one-shot clip
(station ID / service TTS / REST `/transmit`, each one `transmit(whole_clip)` call) asserts→plays→
drains→drops; a stream (`TxSession`: `ptt(True)`…N×`transmit`…`ptt(False)`) holds the line across
frames and `transmit()` only plays (state `_keyed`). **PTT line is configurable** (`baofeng.ptt_line`,
`rts`/`dtr` enum, **default RTS — marked verify-on-hardware**). **RF-safety:** the port opens with
both lines pre-set **low** (kills the pulse-on-open footgun), `close()`+`atexit` always drop the line
(never exit keyed), and playback is `stop()`-drained before the line drops (no clipped tail).
`capabilities()`=`SHARED_CAPS` only (API 501 on CAT — guardrail 3); `status().busy` always False (no
COS line — ADR 0015 → use `audio.squelch=audio`; `build_app` **rejects** `squelch=cat` for baofeng).
**Config:** new `[baofeng]` group in `config/spec.py` (serial_port/ptt_line/input_device/
output_device/blocksize) + `save.py` banner; `radio.toml.example` regenerated. **Deps:** new
`hardware` optional extra (`pyserial`,`sounddevice`), lazily imported (CI stays hardware-free);
`sounddevice` also needs system `libportaudio2`. **Composition root** (`api/app.py`) passes the
baofeng kwargs. **New `radio_server/doctor.py`** (`python -m radio_server.doctor`): read-only pass/
fail table (enumerate the AIOC card @48 kHz, serial opens without keying, dialout access) + a guarded
**`--key-test`** (the ONLY RF path — refuses non-interactive/CI, demands typed `CONFIRM`, asserts the
line ~2 s, asks which line keyed) for the empirical RTS-vs-DTR answer. **`uv run pytest` → 452 passed,
5 skipped** (+16): new `tests/test_aioc_baofeng.py` (fake serial/audio seams — format-reject-before-
audio, one-shot self-key + drain-then-drop, streaming holds one stream across frames, ptt idempotency,
no-keying-on-construction parametrized RTS/DTR, lazy-import error, close/atexit line-drop); factory
test now builds baofeng (only `v71` still raises); settings-API canary 31→36 + asserts `ptt_line` enum
renders. 5th skip = the hardware-gated real-capture test (device present but this sandbox lacks
`libportaudio2`). **Bench-verified live this cycle:** doctor audio + serial all PASS against the
plugged-in AIOC; the sound card resolves as **`All-In-One-Cable: USB`** (sounddevice matches by
PortAudio-name substring / index, NOT a raw ALSA `hw:CARD=` string — a bare `All-In-One-Cable` is
ambiguous because PulseAudio also exposes the card; the `: USB` substring targets the raw ALSA
device) and **reads real 48 kHz audio** (the hardware-gated capture test now passes on the bench);
`--key-test` confirmed **DTR keys PTT** (RTS did not) → **default flipped RTS→DTR**. The backend also
constructs against the real `/dev/ttyACM0` holding both lines low (no keying) and closes clean.
**Only operator step left:** run `backend=baofeng`,`squelch=audio` with an API-token secret and
confirm full browser talk-through (TX keys, RX streams back, and — with TOTP+callsign+voice wired —
station ID fires). Docs: ADR 0029, `docs/hardware-bringup.md` rewritten (AIOC section real; V71
still pending), README status updated. **Deferred:** blocking `receive()` still inline on the event
loop (executor is a follow-up, fine at ~20 ms); a composition-root backend `close()` lifecycle hook
(atexit covers the safety-critical drop); `SignaLinkV71` still a stub (hardware not here). Next: the
bench acceptance above, then SignaLinkV71 when its box arrives, or recordings playback/download UI.

Cycle 28 complete: **async scan runner + `/scan/stop`** (ADR 0028), mock-only — makes scan
**stoppable**, closing the cycle-21 "Scan + live phase, no stop" gap that every HANDOFF since has
deferred. `POST /scan` used to run one **synchronous** `ScanEngine.sweep()` (blocks, no stop); it now
starts a **background async task** that steps the existing `ScanEngine.tick()` on the `scan.poll`
cadence — the async **driver** around the unchanged cycle-11 tick/sweep logic and cycle-16
arbiter/TX-suspend behavior, mirroring how `RxPump` drives `receive()`. New
**`radio_server/scan/runner.py`** holds **`ScanRunner`**: owns a single `asyncio.Task`, `start(plan)`
is a **single-scan guard** (builds the engine via an injected `engine_factory`, returns `False` if
already running), `stop()` clears its task ref **before** awaiting the cancel (RxPump discipline) and is
idempotent. It stays below the API — progress **and** the new `stopped` lifecycle event flow through the
same injected `on_event` (`_publish_scan`), so it never imports `EventHub`. **The clean-stop guarantee
is free from `tick()` being fully synchronous:** a `task.cancel()` can only land at the loop's
`await asyncio.sleep(poll)`, never mid-`tick`, so the in-progress tick always completes — no mid-tune
kill. **Stop-while-TX-suspended can't wedge** because while `arbiter.transmitting` the tick early-returns
and the loop keeps *polling* (spinning cheaply), never blocking on a resume that isn't coming. **API:**
`POST /scan` is now `async`, stays **501**-gated naming `"scan"` and **422** on an ambiguous plan, then
starts the runner and returns `{"scanning": true, "status"}` immediately; a start while running is a
**409**. New **`POST /scan/stop`** (capability-gated, idempotent) returns `{"scanning": false,
"stopped": <bool>}`. The old synchronous `held` return is **gone** (no async equivalent; `sweep()` is
retained on the engine but off the live path). `/status` gained a **`scan` block**
(`{running, frequency}`, mirroring `controller`) and `/events` carries the new `stopped` phase, so the
UI reflects running/stopped. **Lifecycle:** one `ScanRunner` in `create_app`
(`app.state.scan_runner`); the lifespan teardown `await app_.state.scan_runner.stop()` right after
`rx_pump.stop()` — a scan running at shutdown is cancelled with no leaked task. **UI (`web/src/`):**
`ScanControl.jsx` replaces the lone "Scan" button with a **Start/Stop pair** modeled on
`ControllerControl` (tracks `running` optimistically from the POST responses **and** from live `scan`
events, so a scan started/stopped elsewhere — or torn down at shutdown — is reflected; a `stopped` phase
means idle); `api.js` gained `scanStop()`. `web/dist` is gitignored (source + `package.json` committed;
`cd web && npm run build` rebuilds — verified clean, 51 modules). **`uv run pytest` → 436 passed, 4
skipped** (+10; 4 skips unchanged): new `tests/test_scan_runner.py` (async unit — background start emits
`scanning`, single-scan guard, clean stop emits `stopped` w/ no leaked task, idle-stop no-op,
stop-while-TX-suspended), and `tests/test_scan.py` endpoint tests rewritten for the async contract
(non-blocking ack, first `scanning`/`stopped` over the WS, 409 second start, 501 both endpoints on
audio-only, shutdown cancels the task, stop-while-TX-suspended). **Testing note:** a task spawned during
a request is cancelled by `TestClient` at request end unless driven as `with TestClient(app) as client:`
(one persistent loop) — the tests needing the scan to live across requests use that form. **Verified
end-to-end**: against a real bound server (uvicorn + `websockets`) the full lifecycle
(start→events→`/status` block→stop→`stopped` event→idempotent no-op→409) is green, and a headless
Chromium walkthrough confirmed Scan→(Scan disabled, Stop enabled, "Live: scanning @ …")→Stop→idle.
Docs: ADR 0028 + `docs/api.md` (async `/scan`, `/scan/stop`, the `scan` status block, the `stopped`
phase, 409). **Deferred, on purpose:** live hot-reload; Opus/compression; hardware backends (real
tune/busy timing — `scan.poll`/settle/dwell stay verify-on-hardware). Next: recordings
playback/download UI + a GET API for the JSONL ledger, or the hardware bring-up phase.

Cycle 27 complete: **Web UI — settings screen** (ADR 0027), mock-only — the browser face of the
cycle-26 endpoints and the close of the config arc. **Pure client feature; the backend is
unchanged** (`uv run pytest` stays **426 passed / 4 skipped**). The cycle-26 contract was verified
sufficient before building (endpoints + `test_settings_api.py`), so no Python edit was needed; the
standing rule (real gap → minimal backend fix + pytest) did not fire. The whole form **renders from
the schema** returned by `GET /settings` — no hardcoded field list — so a setting added to the
registry later needs zero UI change. New `web/src/components/`: **`SettingsView.jsx`** (fetches
`GET /settings`, groups fields by `group` into `.card` sections, dirty-tracks edits, Save PATCHes
**only changed keys**; on the atomic **400** it surfaces the named key inline and **keeps the
operator's edits**; on success shows a **restart-to-apply banner** off `restart_required` then
re-fetches), **`SettingsField.jsx`** (renders one setting **by `type`** — text/number/toggle/select
— with the schema **`description` always visible** as inline help, and required / required-unset
flagged), **`SecretsPanel.jsx`** (api-token + TOTP shown **present/absent only**; **Rotate API
token** reveals the new token **once** with copy + honest "active after restart; current session
still works" wording + a return-to-gate re-auth action; **Re-enroll TOTP** renders the returned
`otpauth://` URI as a **scannable QR** once via **`QrCode.jsx`**, with the URI as copyable text),
and the QR uses **`qrcode.react`** (the one new dep — zero-runtime-deps, MIT SVG). **Wiring:**
`api.js` gained four client methods (`settings`/`updateSettings`/`rotateApiToken`/`enrollTotp`);
`vite.config.js` proxies `/settings`; `ControlPanel.jsx` got a topbar **view toggle** (Control ⇄
Settings); `App.jsx` threads an `onReauth` (deliberate return-to-gate). **`web/dist` is gitignored** —
the commit carries source + `package.json`/`package-lock.json`; `cd web && npm install && npm run
build` rebuilds it (verified: 51 modules, clean). **Apply semantics: restart-to-apply (v1)** — the UI
says so on every write. **Acceptance is a browser walkthrough** against the mock server (see ADR 0027
/ the PR); the endpoint contract the UI consumes was also re-proven via a TestClient. **Deferred:**
live hot-reload; server-side scan-stop (standing, unrelated backend gap); hardware backends.

Cycle 26 complete: **Settings REST API + secret rotation** (ADR 0026), mock-only — a **thin,
token-gated HTTP surface over the cycle-25 config**, so the cycle-27 UI can read/edit settings. **No
new config logic:** endpoints serialize the `SettingSpec` registry and validate via `resolve_settings`
/ persist via `save_settings`/`save_secret`/`rotate`. New `radio_server/api/settings.py` with
`register_settings_routes(api, app)` (called from `create_app`, routes on the existing
`Depends(require_token)` router). **`GET /settings`** serializes every setting — `key, group, type
(+choices for enums), default, value, required, description` — with `type` **derived in the API
layer** (bool/enum/integer/number/string; `bool` checked before `int`; `station.id_mode` keyed off
its coercer) so `config/spec.py` stays untouched; a required-unset value serializes as `null` (no
raise); plus a `secrets` block that reports **presence only** (`{"set": bool}`) — a secret value can
never appear because secrets aren't in `SETTINGS`. **`PATCH /settings`** takes `{"values":{key:val}}`,
rejects secret + unknown keys up front, then validates the **whole** patch atomically by resolving
`{current values}|patch` (raises naming the bad key **before any write** → 400; file untouched), then
`save_settings` round-trips to `radio.toml` and updates `app.state.settings` for display; returns
`restart_required` (v1 = all changed keys). **`POST /settings/secrets/api-token/rotate`** and
**`POST /settings/secrets/totp/enroll`** are **write-only** — generate (or accept, for the API token)
a secret, `save_secret` it 0600, and return it **once** (the token in-body; the TOTP secret as an
`otpauth://` provisioning URI via `TotpVerifier.provisioning_uri`); they never read an existing
secret back. **Wiring:** the one real change was threading the config/secrets **file paths** to the
app — `build_app`/`create_app` gained `config_path`/`secrets_path` (+ the `Secrets` object) stored on
`app.state`; `--config`/`--secrets` flow through; `DEFAULT_CONFIG_PATH` moved into
`config/settings.py`. **Apply semantics: restart-to-apply (v1)** — writes persist but the running
server (the token `require_token` closes over, the scan route's startup settings) is **not**
hot-reloaded; every write response says so. `uv run pytest` **426 passed / 4 skipped** (412 + the new
`tests/test_settings_api.py`: schema+values with no secret leak, atomic-reject-naming-key with the
file byte-unchanged, unknown/secret-key rejection, token-gating, rotate persists+returns-once, enroll
fresh-URI-never-existing-secret). Docs: ADR 0026 + a `## Settings & secrets (ADR 0026)` section in
`docs/api.md`. **Deferred for cycle 27:** the web settings screen (renders off `GET /settings`, shows
a "restart to apply" banner off `restart_required`); live hot-reload; QR rendering of the URI.

Cycle 25 complete: **config foundation — a schema-driven `radio.toml` replaces the ~31 scattered
`RADIO_*` env reads** (ADR 0025), reversing the de-facto env-only decision. **Behavior-preserving
refactor:** with no config file, every default equals today's and the suite stays green (**412
passed / 4 skipped** — up from 386 because the config system added `tests/test_config.py`; five old
`load_api_token`/`load_totp_secret` env-reader tests were removed as those functions are gone). New
`radio_server/config/` package: **`spec.py`** (the `SettingSpec` registry — one source of truth for
key/default/coercion/description, 31 non-secret settings grouped into 11 TOML tables; every default
*references* the existing `DEFAULT_*` constant so there's no duplication), **`settings.py`**
(immutable `Settings` + `resolve_settings`/`load_settings` via stdlib `tomllib`), **`save.py`**
(`save_settings` round-trips via **tomlkit**, preserving hand-added comments; `render_example`
generates `radio.toml.example` from the registry), **`secrets.py`** (`load_secrets`/`save_secret`/
`rotate` — the two secrets on a separate 0600-enforced channel). **The load-bearing subtleties, all
verified against the old loaders:** empty-string handling is per-field (→default for floats, →fail
for callsign/tts, →False for record bools, →True for mock_cat); `time.tz` keeps its
`ZoneInfoNotFoundError`, the VAD `on>off` hysteresis stays a `ValueError` in `AudioLevelGate.__init__`
(a cross-field check, not in the schema); two bool coercers (strict fail-loud for `recording.*`,
permissive for `server.mock_cat`); **required-unset fails loud lazily on access** (so the default
mock app with no callsign/voice still starts — the invariant the whole refactor hinges on). Every
`load_*(env)` is now a thin `load_*(settings)` accessor; `build_app(settings, secrets)` /
`build_controller(settings, *, totp_secret=…)` thread the secret in explicitly (it is never a schema
setting). Bootstrap: `python -m radio_server --config PATH --secrets PATH` (argparse; `create_app`
gained an optional `settings=` only so the on-demand `/scan` route can read scan timing — otherwise
still the env-free DI seam). **Secrets split is the security-load-bearing part:** `RADIO_TOTP_SECRET`
/ `RADIO_API_TOKEN` are never in `radio.toml`, never in the `SETTINGS` schema, never serialized by
`save_settings` — so the future settings API/UI can't leak or clobber them. Also broke a latent
`eventlog↔api` import cycle (`eventlog/log.py`'s `Event` import is now `TYPE_CHECKING`-only) that the
new `config.spec` imports surfaced. **Apply semantics: restart-to-apply (v1)** — `save_settings`
persists but does not hot-reload; live reload is deferred on purpose. Docs swept off env vars to
`radio.toml` (README §Configuration, `docs/operating.md`, `docs/api.md`, `docs/architecture.md`,
`web/README.md`); ADRs left as historical record. **Deferred for cycles 26/27 (helpers built here):**
settings REST API + secret-rotation endpoints (26 — `save_secret`/`rotate` are built and tested),
the UI settings screen (27), and live hot-reload. `save_settings`/rotation have no endpoint yet.

Cycle 24 complete: **comprehensive documentation pass** (no ADR — docs cycle), **zero code change**.
The repo had 24 ADRs but no top-level user-facing docs (`README.md` was a 0-byte stub, `web/README.md`
was stale — it predated the cycle-22/23 audio work and still said "live audio arrives in later
cycles"). Wrote the user-facing set, every factual claim **verified against source, not memory**:
**`README.md`** (front door — the two modes, an honest mock-vs-hardware status block, the
two-auth-planes warning up top, quickstart, and a **complete 33-var `RADIO_*` env table** grouped by
concern with defaults + which 4 are fail-loud: `RADIO_API_TOKEN`/`RADIO_CALLSIGN`/`RADIO_TOTP_SECRET`/
`RADIO_TTS_VOICE`); **`docs/api.md`** (REST + WS reference — the 10 endpoints, the `501`
named-capability gate body `{"detail":{"error":...,"capability":...}}`, the three sockets with the
`{"status":"ready","format":{rate:48000,width:2,channels:1}}` handshake, the `/events` taxonomy
incl. `arbiter`/`auth`/`command`, and close codes `1008`/`1013`-with-the-accept-then-busy quirk/`1003`);
**`docs/architecture.md`** (the `Radio`/`CatRadio` protocol + capability split, the layer map, the
pure-leaf packages activity/arbiter/eventlog/recording, the duplex arbiter's TX-priority auto-resume,
and mock-first testability); **`docs/operating.md`** (Part 97 — the two auth planes, station ID
≤600s/forced/sign-off/cw|voice, the no-secrets whitelist log, security-reality, config guardrails);
**`web/README.md`** rewritten (RX/TX audio now ship, the static-mount-last serve path, the
AudioContext-gesture / mic-permission browser requirements, the dev proxy). Two deferred guides are
honest one-paragraph placeholders — **`docs/hardware-bringup.md`** and **`docs/deployment.md`** —
pointing to the pending bench bring-up; **no fabricated hardware specifics** (Hamlib model,
multimon flags, AIOC PTT line stay verify-on-hardware). ADRs are **linked, not duplicated**.
`uv run pytest` **386 passed, 4 skipped** (unchanged — proves zero behavior change); `git status`
shows only `.md` files. **Two doc/code discrepancies surfaced (flagged in PR #26 for a later cycle,
NOT fixed here):** (1) **duplicate ADR 0001** — both `0001-cycle-model.md` and
`0001-two-backend-radio-abstraction.md` exist; (2) **`api/events.py` is stale** — `EVENT_TYPES` still
lists `"busy"` (reserved/unused) and its docstring predates ADR 0019, omitting the `arbiter`/`auth`/
`command` types the app actually emits. (The suspected "web/dist committed despite gitignore" was a
non-issue — `web/dist/` is correctly gitignored/untracked, only a local build artifact.) **No
instruction issue exists in this repo** (`gh issue list` empty), so the CLAUDE.md end-of-cycle issue
comment/label step had no target — noted in the PR instead. Next: pick up either discrepancy as a
tiny code cycle, or the backend scan-stop / recordings playback-download UI deferred earlier.

Cycle 23 complete: **web UI — TX mic capture** (ADR 0024), mock-only. The browser operator can now
**talk through the gateway** — the mirror of cycle 22, an almost pure client feature over the cycle-15
`/audio/tx` socket. **Verified, not assumed:** the whole TX contract already exists — `?token=`→1008,
single-talker `TxSlot`→1013, the JSON format handshake
(`{"rate":48000,"width":2,"channels":1}`→`parse_tx_format`, non-canonical→1003), the
`{"status":"ready","format":…}` ack, whole-sample framing (odd→1003), PTT keyed on the first real
frame + dropped on close/idle (2 s), and `MockRadio.tx_log` (`list[AudioFrame]`, `.samples`==bytes
sent) — all covered by `tests/test_tx_audio.py`. **One minimal server change** surfaced in browser
verification (the "server gap gets a pytest" the brief anticipated): a browser **cannot see a
pre-accept WS close code** — a rejected handshake shows as generic **1006**, so the single-talker
**1013 was invisible**. Fixed by **accept-then-inform**: `api/app.py`'s busy path now `accept()`s,
sends an explicit **`{"status":"busy"}`** message the client reads, then closes 1013 (ordering is
load-bearing — the busy path returns before the `session`/`finally`, so it never releases the *other*
talker's slot). Token/1008 stays pre-accept (a browser 1008 is a rare rotated-token edge — token is
gate-validated first — surfacing as a generic error). The two second-talker tests now assert the busy
message then 1013. `uv run pytest` stays **386 passed, 4 skipped**. **The client** (new under
`web/src/`): **`txWorklet.js`** — a
`"tx-capture"` sink worklet (`numberOfOutputs:0`), the inverse of `rxWorklet.js`, forwarding each
captured Float32 quantum (a copy) to the main thread. **`useTxAudio.js`** — mirrors `useRxAudio`
(state/ref split, gesture gate, rAF meter of the *outgoing* audio): `startTalk()` (from the Talk click)
`getUserMedia({audio:{channelCount:1,…}})` (denial→clear "denied" state, no hang), builds
`MediaStreamSource → tx-capture` on a **default-rate** `AudioContext` (NOT forced 48k, so the
resampler is the real path), opens `/audio/tx?token=`, sends the canonical header, awaits the ready
ack, then streams. **The load-bearing piece: client-side resample** `ctx.sampleRate → 48000` (streaming
linear interpolation, carrying `prev` sample + fractional `pos` across quanta so it's click-free) +
Float32→Int16 LE, batched into ~20 ms (960-sample/1920-byte) frames — the exact inverse of cycle 22's
decode. **No auto-reconnect** (unlike RX): a keyed transmitter must never silently resurrect — close
codes map to states (1008→`onAuthError`/re-gate; **1013→"radio busy", no retry-hammer**;
1003→format-error). `stopTalk()` closes the WS (server `finally` drops PTT + frees the slot), **stops
the mic tracks** (clears the OS indicator), tears down. **`TalkControl.jsx`** — the TX pair to
ListenControl: a red `.ptt.keyed` toggle ("Talk"/"Stop talking"), an "on air" badge, a red mic level
meter (`.meter-tx`), and clear denied/busy states; reports its talking state up. **Half-duplex UX:**
because the RX **jitter buffer holds ~500 ms**, server-side RX suspension alone would let you *hear
yourself gate in/out* — so `ControlPanel` lifts the local `talking` state and passes `suspendedLocally`
→ `ListenControl` → a new **`forceMute`** input on `useRxAudio` (effective gain `=(muted||forceMute)?0:1`,
ramped live) that mutes the monitor **immediately** on local keying; gated on *our own* talk, not the
global `transmitting`, so a remote op's TX doesn't mute us. `PttControl` (REST `/ptt`) left untouched
(orthogonal manual key). Vite proxy already had `/audio/tx` (cycle 22); no `api.js` change (WS auth is
`?token=`). **Verified end-to-end in a real headless browser** (Chrome with a fake mic device): Talk
keys + streams canonical frames into `tx_log`; a forced-44.1k context still lands ~48k/s (resample
proven); release drops PTT + frees the slot; a second talker → "radio busy" no-retry; mic denial →
clear message, no hang; talking mutes the local RX monitor. Deferred, on purpose: recordings
playback/download UI, async scan + `/scan/stop` (noted backend gap), Opus/compression. Next: recordings
playback/download, or the backend scan-stop.

Cycle 22 complete: **web UI — live RX audio playback** (ADR 0023), mock-only. The browser now **plays
what the radio hears** — a pure client feature over the cycle-13 `/audio/rx` socket, plus **one minimal,
symmetric server change**. **Verified, not assumed** (the brief's caution): `/audio/rx` sent *no*
format header (just raw `send_bytes` after `accept()`), while `/audio/tx` has a declared-format
handshake — the "cycle-15 symmetry decision" was never actually implemented. Realized now: `/audio/rx`
sends **`{"status":"ready","format": asdict(CANONICAL_FORMAT)}` first**, mirroring TX's ready ack, then
the raw canonical PCM as before (the **only** Python edit — one `send_json` in `api/app.py`; the
demand-driven pump lifecycle is untouched). Three RX WS tests now read the header first
(`receive_json()`) and a new `test_audio_rx_sends_format_header` asserts it (reject-token tests
unaffected — they close `1008` before `accept()`). **The client** (all new under `web/src/`): an
**AudioWorklet ring-buffer player** (`rxWorklet.js`, processor `"rx-player"`) fed by **`port.postMessage`
Float32 — deliberately NOT SharedArrayBuffer, so no COOP/COEP headers and the cycle-21 same-origin
static mount stays intact**. A **jitter buffer** primes ~150 ms then drains and caps latency at ~500 ms
(drop-oldest, mirroring the server hub); an **underrun outputs silence + re-primes**, so *every* gap —
scripted RX silence, WS reconnect, and the arbiter suspending RX during TX (half-duplex, ADR 0017,
where `/audio/rx` just stops delivering frames) — is a **clean pause, no buzz, no crash**, resuming
cleanly when frames return. **`useRxAudio.js`** mirrors `useEvents` (`?token=` auth, backoff reconnect,
`1008` → `onAuthError` back to the gate) but `binaryType="arraybuffer"`; it **creates nothing until the
Listen gesture** (browsers start an `AudioContext` suspended — autoplay is impossible on load), then
builds the context at **48 kHz** (canonical PCM maps 1:1, no resample), loads the worklet, wires
`worklet → GainNode(mute) → destination`, reads the leading header (noted, but plays canonical
regardless → header-less older servers still work), and decodes each frame `Int16Array → Float32`
(`/32768`, LE). **`ListenControl.jsx`** (a `.card` in the left column): Listen/Stop, a mute (`GainNode`
0/1), a peak **level meter** (per-frame peak smoothed on `requestAnimationFrame`, reflects incoming
audio even when muted), a stream conn badge, and a **"receiving paused (transmitting)"** note driven off
the existing `/events` `transmitting`/`arbiter` state (no new server "suspended" marker). `vite.config.js`
gains `/audio/rx` (+ `/audio/tx`, reserved for 23) as `ws:true` dev-proxy entries. `uv run pytest` →
**386 passed, 4 skipped** (+1; the 4 hardware/model skips unchanged). **Verified end-to-end in a real
headless browser** (Chromium against the live mock seeded with an audible looping tone): Listen →
continuous audio + moving meter; a TX-suspend gap via a streaming `/audio/tx` client → paused
indicator, no buzz, clean resume; Stop → pump idle; autoplay confirmed impossible before the gesture.
Deferred, on purpose: **TX mic capture (cycle 23)**, recordings playback/download + a GET API for the
JSONL ledger, a distinct `/events` "suspended" marker, Opus/compression. Next: **TX mic capture.**

Cycle 21 complete: **web UI — control panel** (ADR 0022), mock-only. The first browser client and,
finally, a **real server entrypoint**. Control + visibility only; **live audio is deferred to cycles
22–23**. A **React + Vite SPA** under a new top-level `web/` (sources in `web/src/`, builds to
`web/dist/`; `node_modules/` + `web/dist/` gitignored, so `npm install && npm run build` is a
documented prerequisite — the chosen cost of a build toolchain in a uv-only repo). **Served
same-origin:** `create_app` gained a keyword-default `web_dir` that mounts the built bundle at `/`
via `StaticFiles(html=True)` **mounted last** (after the REST router + WS routes, so the token-gated
API always wins); an **unbuilt** `web_dir` serves a "run `npm run build`" placeholder instead of
crashing; `web_dir=None` (all prior tests) adds no `/` route → surface unchanged. `build_app` reads
`RADIO_WEB_DIR` (marked default → `web/dist`). **The missing entrypoint exists:** `python -m
radio_server` (`radio_server/__main__.py`) binds uvicorn to `RADIO_HOST` (default `127.0.0.1`) /
`RADIO_PORT` (default `8000`) around the env-composed app — thin; `build_app` still fails loud
without `RADIO_API_TOKEN`. **`websockets` is now a runtime dep** — plain `uvicorn` ships no WS
implementation, so a bound server 404s every `/events` upgrade (the TestClient masked this with its
own in-process WS); added explicitly, not `uvicorn[standard]`, to stay lean. **Mock CAT toggle:**
`RADIO_MOCK_CAT` (marked default `on`) — `off` yields an **audio-only mock** so guardrail 3's
control-greying is demonstrable in a browser without hardware. **The UI:** in-memory token (React
state only, never `localStorage`; `Authorization: Bearer` on REST, `?token=` on the WS); token gate
validates via `GET /capabilities` (bad token → clear error, not a hang); **capability-driven
greying** off `/capabilities` with a **defensive 501** backup (reads `detail.capability`, greys
exactly that control); **one `/events` WS** folds `status`/`ptt`/`scan`/`session`/`auth`/`command`/
`arbiter` frames into a live status panel + a **bounded (~500)** scrolling event log, **reconnects
with exponential backoff** on drop (a `1008` rejected-token close stops retrying → back to the gate).
Controls: tune (freq/channel/tone-with-clear/mode), PTT toggle, scan, controller start/stop. **Honest
about the API:** `/scan` is one synchronous sweep returning `held` with **no server-side stop**, so
there's a "Scan" button and live phase — no dead stop button; controller `503` "not configured"
renders as a disabled control with a message, not a dead button. **Backend behavior unchanged** — the
only Python edits are `api/app.py` (mount + env toggles), the new `__main__.py`, and the `websockets`
dep; every other package untouched. **Verified end-to-end in a real headless browser** (Chromium via
Playwright against the live server): token gate, CAT-vs-audio-only greying, each control hitting its
endpoint and reflecting result, `/events` driving status + log, controller 503, and **WS reconnect
after a server drop/restart** — all green. `uv run pytest` → **385 passed, 4 skipped** (+8 in
`tests/test_web.py`: static mount, unbuilt placeholder, static-never-shadows-gated-API, `web_dir=None`
unchanged surface, `RADIO_WEB_DIR` + `RADIO_MOCK_CAT` env wiring; SPA itself is browser-verified).
Deferred, on purpose: **live RX playback (cycle 22)**, TX mic capture (23), recordings
download/playback + a GET API for the JSONL ledger, a server-side scan-stop, an `/events` "suspended"
marker for arbiter RX-pause. Next: **live RX audio.**

Cycle 20 complete: **recording safety rails + TX recording** (ADR 0021), mock-only. Closes the three
cycle-19 footguns and folds in the deferred TX capture. **The backend is now genuinely complete and safe.**
Four pieces: **(A) Max-duration segment roll** — `Recorder` gained an **always-on** cap `max_seconds`
(`RADIO_RECORD_MAX_SECONDS`, default 3600, positive-or-fail-loud, **no disable sentinel**). `write()` checks
the injected clock **before** the lazy-open and, if the open segment has run `>= max_seconds`,
`end_segment()`s so the existing lazy-open rolls a fresh file — the triggering frame starts the new segment;
`_open_segment` stamps `_segment_started`, the `_wav is not None` guard makes a stale start after `_abort()`
harmless (no reset). So **no single WAV grows without bound even under `RADIO_SQUELCH=off`** — its endless-file
footgun is closed. FakeClock-deterministic (reuses the stamp clock). **(B) Squelch-off warning** — `build_app`
now logs a one-time `WARNING` (the repo's **first** `logging` use — a module `logger`, handler-free, `caplog`-
testable) when `RADIO_RECORD=on` and `RADIO_SQUELCH=off`, saying segmentation is time-based (the roll), not
activity-based. **It does not fail** — the roll makes it safe. **(C) Half-duplex split** — `RxPump.run`'s
existing `if self._arbiter.transmitting:` branch now also calls a **guarded** `self._recorder.end_segment()`
before sleeping, so a streaming-TX key-up mid-RX **finalizes the open RX segment** and resume lazy-opens a
fresh file — a recording reflects one continuous receive, no concatenation across the keyed gap. Idempotent →
no rising-edge flag. Correctly scoped to `arbiter.transmitting` (streaming TX only; REST `/ptt` keys directly
and never touches the arbiter, so it neither pauses nor splits — pre-existing behavior). **(D) TX recording** —
the same `Recorder` records transmitted audio, distinguished only by a **`tx-` filename prefix** (the
hardcoded `rx-` became a ctor `prefix` param). `TxSession` gained a `recorder` injection (a **local**
`TxRecorder` Protocol + `null_recorder` default mirroring `rx.pump.RxRecorder`, so `tx` **never imports**
`recording` — the arrow stays `tx -> {audio, backends}`); `feed()` writes each transmitted frame (guarded,
after `transmit`), `close()` finalizes. Opt-in via **`RADIO_RECORD_TX`** (default off, **independent** of
`RADIO_RECORD`); shares `RADIO_RECORD_PATH`, inherits `RADIO_RECORD_MAX_SECONDS`, ignores `RADIO_RECORD_MODE`
(gating is an RX concept). RX/TX are **separate `Recorder` instances** (own sequence counters) in the same dir,
disambiguated by prefix, both on `time.time` → filename stamps **timestamp-align** with the ledger's
`tx_key_up`/`tx_key_down`. **The sharpest failure-isolation call:** `close()`'s `end_segment` is placed **after**
the keying/arbiter-release work and **inside `if self._keyed`**, and **guarded** — the `/audio/tx` `finally`
runs `session.close()` **then** `tx_slot.release()`, so an exception escaping `close()` would skip the slot
release and **permanently wedge the single transmitter**; guard + ordering guarantee a disk fault can never
break keying or leak the slot. Concurrent isolation comes from `TxSlot` (a second talker is refused **before**
its `TxSession` is built), so the shared `tx_recorder` is only ever fed by one talker; sequential talkers share
it and get a continuous `tx-000001`, `tx-000002`… counter. **Wiring:** `create_app`/`build_app` gained a
`tx_recorder` param (keyword-default → existing callers unchanged), stored on `app.state.tx_recorder`, closed in
the lifespan teardown alongside `recorder`, and passed into each `/audio/tx` `TxSession`; `build_app` calls
`build_tx_recorder(env)`. `uv run pytest` → **377 passed, 4 skipped** (+25; the 4 multimon/piper skips
unchanged; all prior tests pass untouched — only keyword-default params were added). Deferred, on purpose:
Opus/compression, retention/cleanup, the playback/download API (the web UI), full-capture (pre-gate) mode
(seam only), decoupling recording from the demand-driven pump. Next: **the web UI.**

Cycle 19 complete: **audio recording — received audio → WAV** (ADR 0020), mock-only. The stack could
capture, stream, gate, and log audio but not **keep** it; this cycle adds a passive `Recorder` that
writes received audio to timestamped WAV files, one per RX activity session. **The load-bearing
design call:** the brief said tap the pump "as another sink alongside the WS listeners" (an
`AudioHub` subscriber), but the hub only ever carries **gate-open** frames — a subscriber can't see
the **gate-close** edge that bounds one session, so segmentation would need a wall-clock gap timeout
(not `FakeClock`-deterministic) or a racy second channel. So the recorder is a **`Protocol`-injected
sink the pump calls directly** (confirmed with the user): gate-open → `recorder.write(samples)`, a
non-empty frame the gate **rejects** → `recorder.end_segment()` (the close edge), empty frames reach
neither; the hub `publish` runs **first** so recording never adds stream latency. This sees exactly
the post-gate frames the hub streams, opens **no second capture reader**, is deterministically
testable, and gated-recording falls out for free. A new pure-leaf **`radio_server/recording/`**
package (sibling of `eventlog/`, imports only `..audio`) holds **`Recorder`**: WAV via the stdlib
**`wave`** module (no new dep; fixed canonical 48k/s16le/mono → deterministic header), lazy per-
segment open, filename `rx-{seq:06d}-{YYYYmmddTHHMMSSZ}.wav` (the **sequence counter** guarantees
uniqueness + lexical==chronological order; timestamp from an injected `Clock`). `rx/pump.py` gained
only a local **`RxRecorder`** Protocol + a **`null_recorder`** no-op default; `api/app.py` is the
only meeting point (`create_app(recorder=None)` → `app.state.recorder` → `RxPump`; shutdown
`close()`; `build_app` calls `build_recorder(env)`). **Config (opt-in):** `RADIO_RECORD` (default
**off** → no recorder, writes nothing), `RADIO_RECORD_PATH` (marked default `recordings`, validated
**fail-loud at construction** via makedirs+probe like `JsonlSink`), `RADIO_RECORD_MODE` (default
`gated`; `full`/pre-gate is a **reserved seam** — `build_recorder` raises `NotImplementedError`, not
silently gated). **Failure isolation (hard rule):** `Recorder` catches+drops internally **and** the
pump guards its calls (double guard — the pump is the single shared capture task whose death blinds
every listener; disk I/O is a broader fault surface than the non-raising leaves — the `EventLog.handle`
reasoning). **TX recording deferred** with a note (clean via the cycle-18 `on_key` edges + a `tx-`
prefix later; `feed` is the load-bearing keying path, its own cycle). **Documented, not fixed:** with
`RADIO_SQUELCH=off` there's no gate-close edge so all RX becomes one file (finalized on pump stop);
recording is coupled to the demand-driven pump (nothing records when nobody's listening); a
half-duplex TX pause concatenates across the keyed gap. `uv run pytest` → **352 passed, 4 skipped**
(+29; `create_app`/`RxPump` gained only keyword-default params, so all prior tests pass unchanged; 4
hardware skips unchanged). End-to-end smoke: scripted frames through `/audio/rx` produced both the
live stream and a valid on-disk WAV (canonical header, exact PCM). Deferred: Opus/compression,
retention/cleanup, playback/download API (the UI), full-capture mode (seam only), TX recording,
pump-decoupling. Next: the web UI.

Cycle 18 complete: **emit the deferred log events** (ADR 0019) — pure wiring, mock-only, **no new
record shapes**. Cycle 17 built the full ledger taxonomy but ~half was **dead in production**: the
`auth`/`command`/`arbiter` mapper branches and the `station_id` `callsign`/`mode` fields are pure
functions of events **nothing published**. This cycle connects the real producers. The load-bearing
constraint: every leaf (`auth`/`services`/`controller`/`arbiter`/`tx`) **deliberately does not import
`EventHub`** — the only `hub.publish` sites are in `api/app.py`; leaves emit domain events through
injected callbacks and the API adapts. So "publish in the site's own package" is impossible as
written; the faithful realization (and what "don't centralize" means) is **each producer surfaces its
own signal at its own site**, routed through a callback the API turns into a hub event. **Five
emissions, all via the callback → API-adapter pattern, zero `hub.publish` in any leaf:** (1)
`auth_accepted` + `auth_rejected` from the `Controller.step` outcome loop (auth signals carry **no
data** — never a code); (2) `command_dispatched {service}` on a **transmitted** dispatch only (a
registry miss is a graceful no-op, no record); (3) `station_id` enriched with `{callsign, mode}` —
`StationId` gained `callsign`/`mode` properties, `mode` threaded from `load_id_mode(env)` at
`build_controller`; (4) `arbiter_mode` via a new `RadioArbiter(on_change=...)` fired **only on a real
derived-mode change** (leaf-pure `Callable`, no import); (5) streaming-TX `ptt` via a new
`TxSession(on_key=...)` at both key edges — streaming keying now logs `tx_key_up`/`tx_key_down` with
duration like REST. The API adapter `_publish_controller` (renamed from `_publish_session`) fans the
controller's one `on_event` channel out by phase → `auth`/`command`/`session` hub types. **Correction
to the brief:** "auth_accepted already flows" was wrong — the accept path emitted only `session_open`;
both are now distinct records. **Fire-and-forget confirmed, not regressed:** `EventHub.publish` is
`put_nowait` onto unbounded queues (non-blocking, non-raising), so these synchronous emissions can't
break auth/dispatch/keying/arbiter. The cycle-17 `eventlog/` mappers changed **zero lines** — they
just light up. `uv run pytest` → **323 passed, 4 skipped** (+12; 3 existing controller assertions
updated for the richer stream, none weakened; 4 skips unchanged). End-to-end proof: a bad-code →
login → command → forced-ID → streaming-TX round-trip through `create_app` writes a JSONL file
containing **every** taxonomy type with no code/secret/token material. Deferred: SQLite sink, log
rotation/retention, query/`GET` API, audio recording (next cycle), web UI (the sequence after).

Cycle 17 complete: **event log / QSO ledger** (ADR 0018) — a durable, structured, timestamped
station log, mock-only and hardware-free. The events a log needs **already flow** through `EventHub`
(ADR 0011), so the ledger is **not new instrumentation** — it is **another SUBSCRIBER** of that flow
that writes durable records, adding **zero** `hub.publish` sites to `auth`/`arbiter`/`tx`/`controller`.
A new pure-leaf **`radio_server/eventlog/`** package (imports only stdlib + `..api.events.Event`)
holds it. **`LogSink`** is the storage protocol (`write`/`close`); the default **`JsonlSink`** writes
**append-only JSONL, one JSON object per line** (greppable, `tail -f`-able — the project's first
persistence). A **SQLite sink is the documented future swap**, not built. Path is `RADIO_LOG_PATH`
(marked default `radio-server.jsonl`, mirroring `time_service.load_timezone`); a **set-but-unwritable
path fails loud at construction** (`JsonlSink.__init__` opens in append mode → `OSError` at the
composition root). **`EventLog`** is the sync mapper: a lifespan-managed background task drains its
own `hub.subscribe()` queue and calls `EventLog.handle(event)` — the exact `/events` consumer shape,
passive, never blocks `publish` (unbounded queue). Records are flat `{"ts": <clock float>, "type",
...fields}`; `ts` from the injected **`Clock`** (`Callable[[], float]`, default `time.time`,
`FakeClock`-testable). `tx_key_up` remembers its timestamp so the paired `tx_key_down` records the
keyed **duration** (Part 97 value). **SECURITY (hard rule):** the mapper **whitelists** the fields
each record emits — it **never spreads `event.data`** — so a TOTP code/secret/API token can never
reach the ledger even if it appeared upstream; a rejected-auth record is just `{ts, type:auth_rejected}`
(tested with a fake `code`/`secret` payload → absent). **Failure isolation:** `EventLog.handle` catches
+ drops on any error (a logging fault never reaches the pump or a transmission), and the audio path
(`/audio/rx` `AudioHub`, `/audio/tx` `TxSession`) never flows through `EventHub` anyway; graceful
shutdown drains still-queued events before closing the sink (no lost entries). Live records today:
`ptt` (REST `/ptt` key-up/down), `scan` (phases incl. `active`+freq), `session` (open/id/close).
**Forward-compatible but NOT yet emitted to the hub** (mapper ready, `hub.publish` deferred to a
future instrumentation cycle): `auth_accepted`/`auth_rejected`, `command_dispatched`, `arbiter_mode`,
and ID `callsign`+`mode` fields. Wiring is confined to `create_app` (new `event_log=None` default →
existing tests unchanged) + `build_app` (opens the sink). `uv run pytest` → **311 passed, 4 skipped**
(+18; the 4 skips unchanged). Deferred: SQLite sink, log rotation/retention, a query/GET API, audio
recording (cycle 18), web UI (cycle 19+), and the live emissions above.

Cycle 16 complete: **RX/TX duplex conflict policy** (ADR 0017) — the **last pure-software cycle**,
mock-only. A half-duplex radio can't receive and transmit at once (keying blinds the receiver), so
this cycle adds the seam that enforces it: **TX takes the radio; the RX pump and any live scan stand
down while keyed and resume when TX drops.** A new pure-leaf **`radio_server/arbiter/`** package
(imports *nothing* from the rest of the tree, so `tx`/`rx`/`scan`/`api` all depend on it with no
cycles) holds **`RadioArbiter`** — "who has the radio right now" as **`RadioMode`** (`idle` /
`receiving` / `transmitting`), modeled as **two independent latches** (`_transmitting` set by TX,
`_receiving` set by the RX pump) with a **TX-priority derived mode** (`transmitting > receiving >
idle`). That beats preempt/restore bookkeeping: on `release_tx()` the RX latch is still set, so the
mode falls back to `receiving` on its own. **Coherence guard:** `acquire_tx()` raises
**`ArbiterStateError`** on a double-key (one transmitter, one talker); `release_tx()` is idempotent
(mirrors `TxSession.close()`). One shared arbiter is created in **`create_app`** (`app.state.arbiter`)
and injected into the RX pump and every per-connection `TxSession`. **TX (writer):** `TxSession.feed()`
calls `acquire_tx()` before `ptt(True)`, `close()` calls `release_tx()` after `ptt(False)` — the two
existing keying points. **RX (reader):** `RxPump.run()` asserts `begin_receive()`/`end_receive()` and,
while `arbiter.transmitting`, **does not pull `receive()` at all** (you can't read a blinded receiver);
listeners stay subscribed (`subscriber_count` unchanged) — only delivery pauses, then resumes to the
same queue. **Scan (reader):** `ScanEngine.tick()` early-returns while transmitting — no tune, no poll,
no advance; **resume needs only the flag** because all positional state (`_state`, `_i`, `_current_freq`,
`_tuned_at`, `_dwell_deadline`) already survives on the instance (noted wrinkle, not fixed: `_tuned_at`
is wall-clock, so a channel paused mid-settle polls one tick sooner after a long pause — harmless). The
`POST /scan` `sweep()` path is untouched (synchronous, can't interleave). Every consumer's arbiter param
**defaults to a private idle arbiter**, so standalone construction is behaviorally unchanged — all prior
tests pass untouched. `MockRadio`/`audio/format.py`/`activity/`/`controller/`/`auth/`/`events.py` are
untouched (tune + receive spies live in the test). `uv run pytest` → **293 passed, 4 skipped** (+10 — 6
arbiter unit, 2 RX-pump, 1 scan, 1 end-to-end; the 4 skips unchanged). Deferred: the optional `/events`
"suspended" marker (behavior delivered without it), Opus, and the real backends' audio I/O + on-bench
PTT-tail/turnaround timing (guardrail 1 — the arbiter models the *logical* exclusion, never the ms).

Cycle 15 complete: **TX audio ingest** (ADR 0016) — the **second half of "talk through the gateway,"**
mock-only, the mirror of cycle 13's RX path in the opposite direction. A binary WebSocket **`GET
/audio/tx`** accepts canonical PCM *in* from a LAN client and feeds it to `radio.transmit()`; it lands
in `MockRadio.tx_log`. Same `?token=` auth plane as `/audio/rx` (rejected pre-`accept()` with
`WS_1008`). A new **`radio_server/tx/`** package sits **below `api`** (imports only `..audio` +
`..backends`, never `rx`/`api`), mirroring the `activity` layering. **No hub, no pump** — TX is
**fan-in/serialized** (one radio, one talker), the opposite of RX's fan-out. **`TxSession`** is the
per-connection keying/ingest state machine (guardrail 2): `feed(data)` validates whole-sample framing
**first** (a bad frame raises before any `ptt()`, so it never keys), **skips empty `b""`** (mirrors
`RxPump`), keys **`ptt(True)` once** on the first real frame, `transmit`s each frame, stamps activity;
`close()` drops **`ptt(False)`** (idempotent) on any exit — PTT is keyed via `ptt()`, **never a CAT
TX**. **`TxSlot`** is the single-talker guard — a plain flag, **not** an `asyncio.Lock` (a Lock would
*queue* the second talker; we must *refuse* it): a second concurrent client is closed **`1013`** before
`accept()`, released in the endpoint's `finally`. Wire protocol: token → slot acquire → `accept()` →
**declared-format handshake** (`parse_tx_format` builds the client's declared `AudioFormat` and requires
`== CANONICAL_FORMAT`, else `AudioFormatMismatch` → **`1003`**; on success acks `{"status":"ready"}`) →
binary frame loop. **Idle timeout:** the endpoint wraps each receive in `asyncio.wait_for(...,
timeout=session.idle_timeout)`; on `TimeoutError`, `session.on_idle()` drops PTT — `wait_for` is only
the wakeup, the **decision** is the clock-injected `idle_elapsed()` (`FakeClock`-testable, no real
sleeps). Close codes: `1008` token · `1013` busy · `1003` bad format/frame · idle → normal `1000`.
`create_app` gained **`tx_idle_timeout=DEFAULT_TX_IDLE_TIMEOUT`** + an `app.state.tx_slot`; `build_app`
reads **`RADIO_TX_IDLE_TIMEOUT`** via `load_tx_idle_timeout`. `DEFAULT_TX_IDLE_TIMEOUT` is guardrail-1
**verify-on-hardware** (real PTT-tail/buffer/cadence). `MockRadio` and `audio/format.py` are
**untouched** — the `ptt` spy (`_PttSpyRadio`) lives in the test. `uv run pytest` → **283 passed, 4
skipped** (+26 tests — 12 WS-integration, 14 unit; the 4 skips are unchanged). Deferred: Opus, real
backend transmit + on-bench timing (hardware), and the **full-duplex RX-while-TX conflict policy**
(noted, not built).

Cycle 14 complete: **software squelch / activity detection** (ADR 0015) — the RX activity-gate seam
from cycle 13 is now filled with a real detector, mock-only. A new **`radio_server/activity/`**
package sits **below `rx`** (imports only `..audio` + `..backends`, never `rx`) so the same activity
signal is reusable — later it feeds scan's stop decision, not just the RX stream. **`frame_rms`** is
the pure, shared energy primitive (RMS of a canonical s16le frame via numpy; empty/odd-byte → `0.0`,
never raises). Two gates implement the one `(AudioFrame) -> bool` shape, picked by backend/config
(mirroring scan's busy-poll question): **`AudioLevelGate`** is software VAD with **hysteresis** (open
on the higher `on_threshold`, hold on the lower `off_threshold`, so a marginal signal doesn't chatter)
and **hang** (stay open `hang` s after the level drops so a speech gap doesn't clip — clock-injected,
`FakeClock`-testable, no real sleeps); construction fails loud if `on <= off`. **`CatBusyGate`** reads
the V71's hardware squelch over `status().busy` and **ignores the frame** (the noted interface tension:
it needs the radio at construction, not just the frame) — the only option for the busy-line-less
Baofeng is audio VAD. **`build_rx_gate(env, radio)`** selects via **`RADIO_SQUELCH`** (`off` | `audio`
| `cat`, fail-loud on anything else); **default `off`** returns the cycle-13 `pass_through_gate`
**unchanged** — the intended per-backend mapping (V71→`cat`, Baofeng→`audio`) is documented, not
hardcoded (auto-derive from capabilities is deferred). `create_app` gained an optional
**`rx_gate=pass_through_gate`** flowed into `RxPump`; `build_app` computes it from the env. VAD
thresholds/hang (`DEFAULT_VAD_ON_RMS`/`OFF_RMS`/`HANG`, env `RADIO_VAD_*`) are guardrail-1
**verify-on-hardware** — real noise floor and speech-gap timing are bench-tuned. `rx/`, `scan/`, and
the backends are untouched. `uv run pytest` → **257 passed, 4 skipped** (+19 model-free tests; the 4
skips are unchanged). Deferred: TX ingest (15), Opus, real capture + real threshold tuning (hardware),
scan rewire.

Cycle 13 complete: **RX audio streaming** (ADR 0014) — the **first half of the voice relay**, mock-
only. Received audio now leaves the box: a binary WebSocket **`GET /audio/rx`** streams raw canonical
PCM (48k/s16le/mono) via `send_bytes` — a **separate socket** from the cycle-10 `/events` JSON
stream, sharing only its `?token=` auth plane (rejected pre-`accept()` with `WS_1008`). A new
**`radio_server/rx/`** package holds the transport: **`AudioHub`** is the audio sibling of
`EventHub` but **bounded + drop-oldest** (each subscriber gets a bounded queue; on overflow `publish`
evicts the oldest frame so the live stream stays near-real-time) — a slow/stuck listener drops frames
without ever blocking the pump or other listeners. **`RxPump`** is a thin async loop over the
synchronous `receive()` (the `ControllerRunner` shape) that publishes each **live** frame's PCM; it
is **demand-driven** (`start()` on the first `/audio/rx` subscriber, `await stop()` on the last) and
**controller-independent**. It takes an injectable **`RxActivityGate`** predicate (default
`pass_through_gate`) — the **squelch seam only**; real software squelch/VAD is cycle 14. Distinct
from the gate, the pump **skips empty (0-byte) frames** (a transport sanity rule). `start()` sets
`running` synchronously and is idempotent; `stop()` nulls its task ref **before** awaiting the cancel
(a reconnect-during-teardown starts fresh, not stalled); a **lifespan shutdown handler** also stops
the pump — the real no-leaked-task guarantee. `MockRadio` gained a scriptable RX sequence
(**`rx_frames`** ctor arg + **`script_rx(*frames)`**, drained FIFO by `receive()` before falling back
to `canned_rx`) — the RX mirror of `tx_log`. RX cadence/buffering (`DEFAULT_RX_POLL` > 0,
`DEFAULT_AUDIO_QUEUE_MAXSIZE`) are guardrail-1 **verify-on-hardware** config. `uv run pytest` →
**238 passed, 4 skipped** (+11 model-free tests; the 4 skips are unchanged).

Cycle 12 complete: the **controller loop** (ADR 0013) — the **full software tower now runs live
end-to-end on the mock**. One clock-injected driver pumps everything on a `receive()` loop:
received audio → DTMF → TOTP auth → dispatch → a CW-ID'd transmission, with automatic periodic and
sign-off ID and an optional live scan. `Controller.step(now, rx_audio)` is the **pure, testable
core** (one iteration); `ControllerRunner.run()` is a **thin async shell** looping `radio.receive()`
→ `step()` on a poll cadence with no logic of its own. This is the cycle where `StationId`'s
session-lifecycle methods finally connect to real events (built cycle 4, deferred since): an
`ACCEPTED` outcome **opens a session and arms the ID** (`begin_session`); the periodic-ID safety net
(`check`) **forces an ID when overdue mid-session** (Part 97); an inactivity close **signs off**
(`sign_off`). Because `AuthGate` only demotes an idle session *lazily* inside `on_dtmf`, the
transition was surfaced as **`AuthGate.expire_if_idle(session, now)`** (a behavior-preserving
refactor mirroring `DtmfFramer.tick`) so the loop can detect and act on it. Lifecycle is emitted as
`ControllerEvent(phase, data)` (`session_open`/`id`/`session_close`) through an **injected callback**
— the controller never imports `EventHub`, so `api → controller` has no cycle; the API adapts each to
a **`"session"` event** on the cycle-10 `EventHub`. `build_controller(env, *, radio, decoder, tts,
clock)` is the composition root (fail-loud on the TOTP secret / callsign; `decoder`/`tts` injectable
so tests use `FakeDtmfDecoder` + `StubTts`). The API gained **`POST /controller {on}`** (token-gated,
**503** when unconfigured — never a silent no-op) and a **`controller` block in `/status`**
(`{running, session_open}`, `null` when unwired). Loop cadence is guardrail-1 **verify-on-hardware**
config. `uv run pytest` → **227 passed, 4 skipped** (+14 model-free tests; the 4 skips are unchanged).

Cycle 11 complete: the **software scan engine** (ADR 0012) — "scan channels remotely like in
person." A V71/CAT-only scan *loop* over the `CatRadio` surface (distinct from the radio's built-in
`scan(on)` toggle): it steps a `ScanPlan` of frequencies, tunes each (`set_frequency`), lets the
reading **settle**, polls `status().busy`, and acts on activity. Two drive surfaces share one set of
pure helpers: `ScanEngine.tick(now)` is the **clock-driven resume-mode machine** (carrier = dwell
while busy, resume on drop — the marked default; timed = dwell N s then move on; hold = stop on first
activity), and `ScanEngine.sweep()` is a **synchronous single pass** that stops-and-holds at the
first active channel (clear channels advance instantly — no clock, no sleeps). Lockout skips
channels; a **priority** frequency is re-checked between steps. Progress is emitted as
`ScanEvent(phase, frequency, channel)` (`scanning`→`active`→`dwelling`, plus `resumed`) through an
**injected callback**, so `scan` stays *below* the API (no `scan↔api` cycle); the API adapts it to a
`"scan"` event on the **cycle-10 `EventHub`** (now registered in `EVENT_TYPES`), so a WebSocket
client watches scan progress live. **Capability-gated** exactly like the other CAT endpoints:
`POST /scan` runs one sweep on a CAT backend and returns **`501` naming `"scan"`** (never a no-op) on
an audio-only one, where it is not advertised. `MockRadio` gained scriptable **`busy_frequencies`**
so a test can script per-channel activity and drop a carrier mid-scan — fully deterministic, no
hardware, no real sleeps. Timing (settle, poll cadence) is guardrail-1 **verify-on-hardware** config.
`uv run pytest` → **213 passed, 4 skipped** (+26 model-free tests; the 4 skips are unchanged).

Cycle 10 complete: the **FastAPI HTTP/WebSocket API layer** (ADR 0011) — the stack is reachable
over the network for the first time, and **guardrail 3 (the capability split) is enforced at the
HTTP boundary**. A thin, honest surface over the injected `Radio`: shared endpoints (`GET /status`,
`GET /capabilities`, `POST /ptt`, `POST /transmit`) always live; the CAT endpoints
(`POST /frequency` `/channel` `/tone` `/mode`) check `Capability` membership and, on an audio-only
backend, return **`501` with the missing capability named in the body** (`{"capability":
"set_frequency"}`) — never a silent no-op, so the web UI can grey out exactly the right control. A
**second, separate auth plane** lands here: a LAN-facing static **bearer token** (constant-time
compare, closed by default, `401`/WS-`1008` on missing/bad), kept deliberately distinct from the
over-RF TOTP/DTMF plane (different threat model — no replay window/burn). A `type`-discriminated
WebSocket `EventHub` pushes a `status` snapshot on connect and further events on control calls;
its shape is left **open for the scan engine's `scan` events next cycle**. `FastAPI`/`uvicorn` are
**core deps** (the API is the product's stated purpose), so the tests **run**, not skip. The API is
**independent of the DTMF/piper/voice-ID stack (#7–#9)** — it imports only `backends` + the new
`api` package and touches no `services/` file — so the two compose additively, as they now do on
`master`: with #7–#9 merged alongside, `uv run pytest` → **187 passed, 4 skipped** (cycle 10 added
18 API tests; cycles 7–9 added 38, with 4 hardware/model `skipif` gates).

Cycle 9 complete: **`VoiceId` + configurable ID mode** (ADR 0010) — the **audio-content
tower is now complete**. `VoiceId` is the second `IdEncoder` (after `CwId`): it speaks the
callsign as NATO/ITU phonetics (**9→"niner"**, so "AE9S" → "alpha echo niner sierra")
through an injected `TtsEngine` — `StubTts` in tests (byte-exact), `PiperTts` in production.
It satisfies the same one-arg `encode(callsign)` contract, so the **cycle-4 `StationId`
scheduler is untouched** — swapping CW for voice is an encoder swap, not a scheduler change.
The phonetic map (`PHONETIC`, `spell_callsign`) is **pure and separated from synthesis**, so
it is exactly assertable with no engine; unknown chars **fail loud** (`ValueError`), and the
accepted set matches `CwId`'s (A-Z, 0-9, `/`→"slash"). `RADIO_ID_MODE` (`cw` | `voice`)
selects the encoder via `build_id_encoder` (the first real composition root); **CW is the
marked default** (no model dependency, always works). Voice mode with no `RADIO_TTS_VOICE`
**fails loud** at construction — it never silently degrades to CW. **Guardrail 1:** the one
real-piper `VoiceId` test is `skipif`-gated (skips here) and property-asserted; on-air
intelligibility is a bring-up check. `uv run pytest` → **169 passed, 4 skipped** (+17
model-free tests in `test_voice_id.py`; the 4th skip is the new real-`VoiceId` test).

Cycle 8 complete: **real piper TTS** (`PiperTts`; ADR 0009) — the first real spoken audio,
behind the existing cycle-3 `TtsEngine` protocol. `render(text)` runs piper at the voice's
native rate and resamples up to canonical 48k, so `PiperTts` is the **first consumer of
`to_canonical`** — this cycle *proves the playback edge*, the symmetric mirror of cycle 7's
`to_multimon` decode edge (both ADR 0006 edges are now exercised). It is a **drop-in for
`StubTts`**: same one-method `render` contract, so the time service, dispatcher, `StationId`,
and `CwId` are untouched, and `StubTts` is **retained unchanged** as the deterministic
exact-assert baseline. The voice's native rate is **read from its `.json` sidecar**
(`audio.sample_rate`), never hardcoded to 22050 (voices vary; some are 16000). Model config
**fails loud**: `RADIO_TTS_VOICE` names the `.onnx` and has **no default** (like the TOTP
secret) — `load_tts_voice` raises when unset, and `PiperTts.__init__` raises on a missing
`.onnx`/sidecar/rate, *before* any piper import. **Guardrail 1:** piper + `onnxruntime` are
**not installed** here (declared as an optional `tts` extra, not a core dep), so the two
real-engine tests are `skipif`-gated (skip here, run where a model is present); the exact
piper version/API is isolated in `_synthesize_raw` and marked verify-against-build; neural
output is **property-asserted, never byte-asserted**; RF intelligibility is a bring-up check.
The `to_canonical` edge itself is proven **model-free** — a synthetic 16000/22050 Hz voice
buffer resamples to a canonical 48k frame of the expected length. `uv run pytest` →
**152 passed, 3 skipped** (+9 model-free tests in `test_tts.py`; the 3 skips are the 2 real
piper tests + cycle 7's real-decode test).

Cycle 7 complete: **DTMF decode + framing** (`radio_server/audio/dtmf.py`; ADR 0008) — the
audio-in → digits seam, and the **first full end-to-end on the mock**. Received `AudioFrame`
audio now drives the auth gate: `DtmfDecoder` (protocol seam; real `MultimonDtmfDecoder`
shells out to `multimon-ng -a DTMF -t raw -` over stdin, a `FakeDtmfDecoder` drives tests) →
`DtmfFramer` (pure, clock-injected grammar: `#` submit, `*` clear, inter-digit timeout
**discards** a stalled partial) → `DtmfInput.pump(frame)` returns completed entries → the
**unchanged** `AuthGate.on_dtmf`. Nothing in auth/session/dispatch/`station_id`/`CwId` changed
— the module is even **auth-free** (local `Clock` alias), so the layering arrow stays
audio → nothing-above. Fixtures are deterministic `synth_dtmf` dual-tones (sum two
`synth_tone` frames at the standard `DTMF_FREQS`), asserted by FFT — no on-disk WAVs
(multimon reads raw PCM on stdin). Config: `RADIO_DTMF_TIMEOUT` (default 3.0s) /
`RADIO_MULTIMON_BIN` (default `multimon-ng`), marked defaults. **Guardrail 1:** `multimon-ng`
is **not installed** in this environment, so the one real-decode test is `skipif`-gated on the
binary (skips here, runs where installed); the exact multimon flags/rate are marked
verify-against-build, and real weak-signal / HT-flutter decode robustness is a hardware
bring-up check, not proven here. `uv run pytest` → **143 passed, 1 skipped** (+13 tests in
`test_dtmf.py`). The headline: fixture audio (fake-decoded) → framed digits → TOTP `ACCEPTED`
→ authed `"1"` `COMMAND` → a real CW-ID'd time announcement in `mock.tx_log`.

Cycle 6 complete: **real CW station ID** (`CwId`; ADR 0007) — the first real transmission
content the server produces. `CwId` implements the existing one-method `IdEncoder`, so it is
a **drop-in for `StubId`**: `StationId`, `Dispatcher`, and every config loader are untouched,
and an authed `"1"` now prepends genuine keyed Morse to the time announcement. A pure PARIS
timing layer (`unit_ms`, `cw_timeline` → `(on, duration_ms)` segments) is isolated from PCM so
element/gap timing is exactly assertable; `encode` keys `synth_tone` on/off along it, with
canonical-zero silence for gaps (so concat stays format-identical). Unknown chars **fail loud**
(a wrong ID is worse than a loud failure). WPM/sidetone are **marked-default** config
(`RADIO_CW_WPM`=20, `RADIO_CW_TONE_HZ`=600, guardrail 1) — safe operator prefs, but **on-air CW
readability is an empirical bring-up check, not proven here.** `uv run pytest` → **131 total,
all green**. Still deferred: `VoiceId`, session-lifecycle wiring.

Cycle 5 complete: the **audio format is pinned and load-bearing** (guardrail 1; ADR 0006).
The opaque `AudioFrame = bytes` alias is gone — `AudioFrame` now carries its `AudioFormat`
(rate/width/channels) and **fails loud** (`AudioFormatMismatch`) on a mismatched concat or
transmit, closing the cycle-1 "bytes silently papers over a mismatch" risk by construction.
Canonical internal format is **48000 Hz / s16le / mono**; resampling happens only at the
tolerant software edges via `soxr` (VHQ, anti-aliased so a downsample can't corrupt DTMF). A
real `synth_tone` primitive (sine + raised-cosine anti-click envelope) proves the type with
real PCM and is the CW-ID substrate for cycle 6. **The remaining gate before hardware is now
just the real encoders (CW/voice ID, piper TTS) + empirical bring-up — the format no longer
blocks anything.**

Cycle 4 (merged, PR #4): automatic station ID (guardrail 5, Part 97). The transmit path is
**legality-clean** — every service transmission carries the station ID, there is a
forced-periodic ID timer, and a sign-off ID at session end. `StationId` is the single seam
through which all audio reaches the radio, so no transmission can go out un-ID'd. ID audio
is a deterministic stub (scheduling logic only). See ADR 0005.

Cycle 3 (merged, PR #3): command dispatch + the first voice service (announce-the-time),
the first thing the server transmits. Authenticated digit `"1"` → time announcement
rendered through a stub TTS → `MockRadio.tx_log`. Still fully mock/hardware-free;
unit-tested with the injected fake clock. See ADR 0004.

Cycle 2 (merged, PR #2): a DTMF-gated TOTP auth layer + session state machine, fed digit
strings directly (no audio/DTMF decode yet), unit-tested with an injected fake clock.
See ADR 0003.

Cycle 1 (merged, PR #1): the `Radio` protocol surface + full `MockRadio`, hardware
backends stubbed and wired into a factory. See ADR 0002.

### Controller loop (cycle 12)

- `radio_server/controller/` (new package). `engine.py` — the pure core, thin driver, and root:
  - `Controller.step(now, rx_audio) -> StepResult` — one loop iteration: `DtmfInput.pump` → for each
    entry `AuthGate.on_dtmf` (an `ACCEPTED` → `station.begin_session` + emit `session_open`); then
    `gate.expire_if_idle` (True → `station.sign_off` + emit `session_close`), else if authenticated
    `station.check(now)` (True → emit `id`); then tick an attached `scan`. `StepResult(entries,
    outcomes, session_open, id_sent, signed_off, scanning)`. Order is load-bearing (a session opened
    this step is not idle, so no false close). `on_event` + `scan` are public/reassignable.
  - `ControllerRunner.run()` — `while running: step(clock(), radio.receive()); await sleep(poll)`;
    `stop()` flips the flag. Thin shell, no logic not covered by `step`. Guardrail-1 poll cadence.
  - `ControllerEvent(phase, data)` with `CONTROLLER_PHASES = ("session_open","id","session_close")`.
  - Config: `load_controller_poll` / `load_session_timeout` (`_load_positive_float` shape,
    verify-on-hardware on the poll constant); `build_controller(env, *, radio, decoder, tts, clock)`
    assembles encoder→`StationId`→registry/time-service→`Dispatcher`→verifier/`AuthGate`→`DtmfInput`,
    sharing the **one** `StationId` with the dispatcher. Fail-loud on the TOTP secret / callsign.
- **Layering:** imports only `..audio/auth/services/scan/backends` (all below `api`), emits via the
  injected `on_event` — never imports `EventHub`. `api/app.py` adapts each `ControllerEvent` to
  `Event("session", {"phase":…, …})`, so the arrow stays `api → controller`.
- `auth/session.py` — extracted `AuthGate.expire_if_idle(session, now) -> bool` (returns whether it
  closed an idle authed session); `on_dtmf` now calls it. **Behavior identical** — the seam a polling
  loop needs, since `on_dtmf`'s inactivity demotion is otherwise only reachable by feeding a key.
- `api/app.py` — `create_app(radio, *, api_token, controller=None, runner=None)` rebinds
  `controller.on_event` to the hub adapter and stores both on `app.state`. `POST /controller {on}`
  (token-gated) starts/stops an `asyncio` task running `runner.run()`; **503** when unconfigured.
  `/status` merges a `controller` block (`{running, session_open}` or `null`). `build_app` wires a
  controller only when `RADIO_TOTP_SECRET` is set (prior no-hardware contract preserved).
  `api/events.py` docstring/`EVENT_TYPES` comment updated for the now-live `"session"` type
  (`EventHub` unchanged).
- Tests: `tests/test_controller.py` (12 new) — login opens+arms; authed `"1"` lands a CW-ID'd time
  announcement in `tx_log`; forced periodic ID at the interval; inactivity timeout closes + signs
  off; an attached scan ticks each step and holds on scripted busy; lifecycle events in order; a
  bounded `run()` pumps `step` each iteration; `POST /controller` flips `/status.running` + needs a
  token; `503`/null when unconfigured; `session` events over the WS in order. Plus
  `tests/test_session.py` `expire_if_idle` cases. `uv run pytest` → **227 passed, 4 skipped**. See
  ADR 0013.
- **Deferred (next):** the two hardware backends; optionally starting a *live* scan through the
  controller (the synchronous `/scan` sweep stays); running `receive()` in a thread executor.

### Software scan engine (cycle 11)

- `radio_server/scan/` (new package). `engine.py` — the pure engine + plan + config:
  - `ScanPlan` (frozen): `channels: tuple[int, ...]` (Hz), `lockout: frozenset[int]`,
    `priority: int | None`; `from_frequencies(...)` / `from_range(start, stop, step)`;
    `active_channels()` = order minus lockout. Addresses by **frequency**, not channel number.
  - `ResumeMode` (`carrier` default | `timed` | `hold`); `ScanEvent(phase, frequency, channel)` with
    `SCAN_PHASES = ("scanning", "active", "dwelling", "resumed")`.
  - `ScanEngine(radio, plan, *, on_event, mode, dwell, settle, clock)` — raises
    `UnsupportedCapability(Capability.SCAN)` on an audio-only backend. `tick(now)` is the clock-driven
    machine (settle → poll `status().busy` → dwell/resume/hold/advance, wraps); `sweep()` is the
    synchronous stop-and-hold pass the API uses (no clock, no sleeps). Pure helpers shared by both.
  - Config (guardrail-1 marked, verify-on-hardware on the constant): `load_scan_settle` /
    `load_scan_poll` / `load_scan_dwell` (`_load_positive_float` shape) + `load_scan_mode` (enum,
    fail-loud on unknown); `build_scan_engine(env, *, radio, plan, on_event, clock)` composition root.
- **Layering:** the engine imports only `..backends` and emits via the injected `on_event` — it does
  **not** import `EventHub`. `api/app.py` adapts each `ScanEvent` to `Event("scan", {...})` on the
  hub, so the arrow stays `api → scan`. `api/events.py` only gained `"scan"` in `EVENT_TYPES`
  (`EventHub` itself unchanged, as ADR 0011 promised).
- `api/app.py` — `POST /scan` on the token-gated router: `_require_cat(Capability.SCAN)` → `501`
  naming `"scan"` on audio-only (same body as the other CAT endpoints); else build a plan from
  `frequencies` **or** a `start/stop/step` range (exactly one, else `422`), run `engine.sweep()`,
  publish `scan` events, return `{"held", "status"}`. Live real-time pump **deferred** to the
  controller-loop cycle (like cycle 7's DTMF pump).
- `backends/mock.py` — `MockRadio` gained `busy_frequencies` (public mutable set): `status().busy`
  is true while tuned to a listed freq, on top of the flat `busy` flag (back-compat kept). This is
  the hook that scripts "channel X busy" and drops a carrier mid-scan (`.discard(x)`).
- Tests: `tests/test_scan.py` (26 new) — plan/config; capability gate; sweep holds first active /
  all-clear → None / lockout skips / priority peeked-and-held; tick carrier-resume, timed-move-on,
  hold-stops, settle-gates-the-poll; events in phase order; and the API (`/scan` sweeps on CAT,
  publishes `scan` over WS in order, `501`-naming-`scan` + unadvertised on audio-only, `422` on a bad
  body, `401` without a token). Plus `tests/test_mock_radio.py` busy_frequencies cases. `uv run
  pytest` → **213 passed, 4 skipped**. See ADR 0012.
- **Deferred (next):** the controller/API pump loop that ticks `ScanEngine` + `DtmfInput.pump` + the
  ID session lifecycle on a live `receive()` loop; then the two hardware backends.

### FastAPI API layer (cycle 10)

- `radio_server/api/` (new package). `app.py` — `create_app(radio, *, api_token) -> FastAPI` (the
  DI seam tests drive against `MockRadio`) and `build_app(env)` (the project's first top-level
  composition root: `create_radio(env["RADIO_BACKEND"] or "mock")` + `load_api_token(env)`, mirrors
  `build_id_encoder`). REST routes live on an `APIRouter` gated by a bearer-token dependency; CAT
  routes call `_require_cat(Capability.…)` before dispatching → `501` `{"error":…, "capability":…}`
  when absent (also catches `UnsupportedCapability` to the same body). `POST /transmit` wraps the
  raw request body in a canonical `AudioFrame` → `radio.tx_log`.
- `api/auth.py` — the LAN plane, **separate from `radio_server.auth`**. `RADIO_API_TOKEN` +
  `load_api_token` (fail-loud no-default, mirrors `load_totp_secret`); `token_matches`
  (`hmac.compare_digest`, constant-time); `bearer_token` (parses `Authorization: Bearer …`);
  `make_require_token(expected)` (the FastAPI 401 dependency). No `TotpVerifier`/`Session` reuse —
  static secret, no window/burn.
- `api/events.py` — `Event(type, data)` (`type` ∈ `status|ptt|busy|session`, `scan` reserved),
  `EventHub` (in-process asyncio fan-out: `subscribe`/`publish`/`unsubscribe`), `status_event(radio)`.
  WS `/events?token=…` accepts, sends an initial `status` snapshot, then streams published events;
  bad token → close `1008`.
- Decisions (see ADR 0011): `501` over `409` for gated CAT (permanent not-implemented, not a state
  conflict); token via `?token=` on the WS because browsers can't set WS handshake headers;
  FastAPI/uvicorn **core** (tests run) with httpx in the dev group (TestClient only).
- Tests: `tests/test_api.py` (18 new, `TestClient` over `MockRadio`, both `supports_cat` values) —
  `/status` mirrors state; `/capabilities` tracks `supports_cat`; a CAT route works on a CAT backend
  and returns a `501` naming the capability **with backend state unchanged** on an audio-only one;
  ptt/transmit reach the mock; WS emits a `status` event on connect and a `ptt` event on control;
  auth rejects missing/bad and accepts good; `load_api_token({})` raises. See ADR 0011.
- **Deferred (next):** the V71-only scan engine, which publishes `scan` progress on this
  `EventHub`, plus session-lifecycle wiring surfaced as `session` events on the same stream.

### VoiceId + configurable ID mode (cycle 9)

- `radio_server/services/voice_id.py` (new):
  - `PHONETIC: dict[str, str]` — NATO/ITU A-Z, digits 0-9 with the ham **9→"niner"**, and
    `/`→"slash". Accepted set matches `CwId`'s `MORSE`, so ID mode never changes which
    callsigns encode.
  - `spell_callsign(callsign) -> str` — pure; upper-cases, maps each char, joins with spaces.
    **`ValueError`** on any char outside `PHONETIC` (mirrors `CwId._morse_for`). Engine-free,
    so the map is exactly assertable.
  - `VoiceId` — `__init__(tts)` (DI at construction); `encode(callsign, format=CANONICAL)` →
    `tts.render(spell_callsign(callsign))`. Optional `format` honors the `CwId` shape so
    `isinstance(VoiceId(stub), IdEncoder)` holds and `StationId`'s one-arg call is unaffected.
  - `load_id_mode(env)` / `RADIO_ID_MODE_ENV_VAR` / `DEFAULT_ID_MODE="cw"` — marked-default
    (like `load_id_interval`); a set value outside `{cw,voice}` fails loud.
  - `build_id_encoder(env, *, tts=None)` — the ID composition root. `cw` → `CwId(wpm/tone from
    loaders)`; `voice` → `VoiceId(tts or PiperTts(load_tts_voice(env)))`. Voice mode with no
    voice **raises** (no CW fallback). The `tts` injection lets tests pick voice on `StubTts`.
- `radio_server/services/__init__.py` re-exports `VoiceId`, `spell_callsign`, `PHONETIC`,
  `RADIO_ID_MODE_ENV_VAR`, `DEFAULT_ID_MODE`, `ID_MODES`, `load_id_mode`, `build_id_encoder`.
  No new deps (voice mode reaches piper only via the cycle-8 optional `tts` extra).
- `tests/test_voice_id.py` (17 new) — phonetic map (spell, upper-case, slash, unknown→raise);
  `VoiceId` on `StubTts` byte-exact + canonical + protocol; `RADIO_ID_MODE` selection (default
  cw, reads voice, case-insensitive, unknown→raise); `build_id_encoder` cw/voice + voice-
  without-voice fail-loud-no-fallback; end-to-end authed `"1"` → voice-ID + time in `tx_log`
  (exact); 1 `skipif`-gated real-piper test (property-asserted). `uv run pytest` →
  **169 passed, 4 skipped**. See ADR 0010.
- **Deferred (next):** the FastAPI API layer, the V71-only scan engine, and the two real
  hardware backends. The audio-content tower is done.

### Real piper TTS (cycle 8)

- `radio_server/services/tts.py` (modified) — `PiperTts` added beside the **unchanged**
  `TtsEngine` protocol and `StubTts`:
  - `__init__(voice_path, *, config_path=None)` — default sidecar `<voice>.onnx.json` (piper
    convention, marked verify-against-build). Validates the `.onnx` + sidecar exist and reads
    `audio.sample_rate` into `self._rate`, all fail-loud, **without importing piper**.
  - `render(text) -> AudioFrame` — `to_canonical(AudioFrame(raw, AudioFormat(self._rate,
    2, 1)))`. Canonical 48k out regardless of the voice's native rate.
  - `_synthesize_raw(text)` — the **only** piper-touching seam (lazy import, marked
    VERIFY-AGAINST-INSTALLED-BUILD; missing piper/onnxruntime → fail-loud RuntimeError). A
    test subclass overrides it to drive `render` with a synthetic buffer, no model needed.
  - `load_tts_voice(env)` / `RADIO_TTS_VOICE_ENV_VAR` — fail-loud, **no default** (modeled on
    `load_totp_secret`).
- `radio_server/services/__init__.py` re-exports `PiperTts`, `load_tts_voice`,
  `RADIO_TTS_VOICE_ENV_VAR`. `pyproject.toml` gains an optional `tts` extra
  (`piper-tts`, `onnxruntime`) — declared, not core; piper unpinned (guardrail 1).
- `tests/test_tts.py` — the 5 existing StubTts baseline tests kept; +9 model-free PiperTts
  tests (config fail-loud ×4, rate read from sidecar, non-22050→48k and 22050→48k resample
  edge, protocol conformance) + 2 `skipif`-gated real-engine tests (canonical/nonzero/
  plausible-duration speech; wired into the time service → one canonical over with the CW ID
  prepended, structure asserted). `uv run pytest` → **152 passed, 3 skipped**. No new core
  deps. See ADR 0009.
- **Deferred (next):** `VoiceId` — a second `IdEncoder` speaking the callsign through this
  engine, with the phonetic/"niner" spelling map and `StationId` CW-vs-voice encoder
  selection. ID stays CW this cycle.

### DTMF decode + framing (cycle 7)

- `radio_server/audio/dtmf.py` (new) — two deliberately-distinct concerns plus fixtures:
  - **Decode:** `DtmfDecoder` (one-method `runtime_checkable` protocol, `decode(frame) -> str`,
    mirrors `IdEncoder`) and `MultimonDtmfDecoder` — `to_multimon(frame)` (ADR 0006 anti-alias
    edge) → pipe raw PCM to `multimon-ng` on stdin → parse `DTMF: <key>` lines. Missing binary
    fails loud with an install hint. `MULTIMON_ARGS`/`MULTIMON_RATE`/`RADIO_MULTIMON_BIN` are
    marked verify-against-build (guardrail 1).
  - **Framing:** `DtmfFramer` (pure, clock-injected). `feed(digit, now) -> str | None`: `#`
    emits the buffered run as one entry (empty buffer → nothing), `*` clears, any other key
    appends; inter-digit timeout discards a stalled partial (lazy on `feed`; `tick(now)` for a
    future real loop). Local `Clock` alias — the module imports no auth code.
  - **`DtmfInput`** composes decoder+framer: `pump(frame) -> list[str]` of completed entries.
    Auth-free; the caller feeds entries to `on_dtmf`.
  - **Fixtures:** `synth_dtmf(digit, …)` sums two `synth_tone` frames at `DTMF_FREQS` (standard
    697–1633 Hz pairs), `_mix` sums int16 as int32 + clips. Deterministic, FFT-assertable, no
    external assets. Unknown key fails loud.
  - **Config:** `load_dtmf_timeout` (`RADIO_DTMF_TIMEOUT`, default 3.0s, fail-loud on bad set
    value) and `load_multimon_bin` (`RADIO_MULTIMON_BIN`, default `multimon-ng`).
- `radio_server/audio/__init__.py` re-exports the new surface.
- `tests/test_dtmf.py` (13 new) — synth-fixture FFT (both tones present)/format/determinism/
  fail-loud; `skipif`-gated real multimon decode; framing (full run frames one entry, `*`
  clears, timeout discards partial via `FakeClock`, lone `#` no-op, `tick`); and **the**
  end-to-end (fake decoder → framed TOTP → `ACCEPTED` → authed `"1"` → CW-ID'd time in
  `tx_log`). `uv run pytest` → **143 passed, 1 skipped**. No new deps. See ADR 0008.
- **Deferred (empirical/next):** real recorded-WAV fixtures; a controller/API loop that pumps
  `radio.receive()` and calls `on_dtmf`; weak-signal/HT-flutter robustness + exact multimon
  flags (hardware bring-up); `VoiceId`.

### Real CW station ID (cycle 6)

- `radio_server/services/cw.py` (new) — `CwId` implements `IdEncoder`
  (`encode(callsign, format=CANONICAL_FORMAT) -> AudioFrame`). Built lowest-to-highest so the
  timing is pure: `MORSE` table (A–Z, 0–9, `/`); `unit_ms(wpm) = 1200/wpm`;
  `cw_timeline(text, wpm)` → ordered `(on, duration_ms)` segments using PARIS units
  (dit 1 / dah 3 / intra-char 1 / inter-char 3 / inter-word 7), **no leading/trailing gap**;
  `_silence` builds canonical-zero gap frames. `encode` keys `synth_tone` for each on-segment
  (its raised-cosine ramp kills per-element clicks) and concatenates via `AudioFrame.__add__`.
- **Encoder signature note:** the protocol is one-arg (`encode(callsign)`) and `StationId`
  calls it that way; the cycle-6 `encode(callsign, format)` shape is honored by an **optional**
  `format` param defaulting to canonical, so nothing above the seam changes and
  `isinstance(CwId(), IdEncoder)` still holds.
- Config: `load_cw_wpm`/`load_cw_tone_hz` follow the `load_id_interval` pattern —
  `RADIO_CW_WPM` (default 20) / `RADIO_CW_TONE_HZ` (default 600), marked defaults that still
  **fail loud** on a set non-numeric/non-positive value. WPM/tone injected into `CwId` at
  construction. Guardrail 1: safe operator prefs, not confirmed hardware facts.
- Swap point: `StubId()` → `CwId(...)` at the (still-to-be-written) composition root; nothing
  else changes.
- Tests: `tests/test_cw.py` (21 new) — PARIS `unit_ms`, exact `cw_timeline("AE9S", …)`
  dit/dah/gap sequence, total-duration = timing math, per-segment tone-energy/exact-zero-gap
  render check, sidetone FFT, unknown-char raises, canonical + concat, config loaders, and
  end-to-end via `StationId`/auth gate (authed `"1"` prepends real CW, no within-interval
  repeat — cycle-4 scheduler behavior unchanged). No new deps. See ADR 0007.

### Audio format + resample + tone (cycle 5)

- `radio_server/audio/` (new lowest layer). `format.py` — `AudioFormat(rate,width,channels)`
  and the frozen, format-carrying `AudioFrame(samples, format=CANONICAL_FORMAT)`; `__add__`
  and `MockRadio.transmit` raise `AudioFormatMismatch` on a format mismatch. Canonical =
  `AudioFormat(48000, 2, 1)`. The guard is **format identity, not PCM-length divisibility**,
  so the symbolic stubs (`b"<id:AE9S>"`) stay valid frames and `tx_log` stays assertable.
- `audio/resample.py` — `resample(frame, target_rate)` over `soxr` VHQ (anti-aliased),
  plus `to_multimon` / `to_canonical`. `MULTIMON_RATE = 22050` is a **verify-on-hardware**
  marked default (guardrail 1). Mono 16-bit only for now (raises otherwise).
- `audio/tone.py` — `synth_tone(freq_hz, duration_ms, format=CANONICAL_FORMAT, *,
  amplitude=0.5, ramp_ms=5.0)`: real sine PCM with a raised-cosine on/off envelope (no key
  clicks). Deterministic. This is the substrate CW ID (cycle 6) gates on/off.
- `AudioFrame` moved from `backends/base.py` to `audio/format.py`; `backends` re-exports it,
  so `from ..backends import AudioFrame` still works everywhere. `MockRadio` gained a
  `format` and a transmit guard; `StubTts`/`StubId` now wrap their symbolic payload in a
  canonical frame. New deps: `numpy`, `soxr` (first runtime deps beyond `pyotp`; wheels only).
- Tests: `test_audio_format.py`, `test_resample.py` (in-band survives + no aliasing into the
  DTMF band), `test_tone.py`; existing suites updated for the new frame type. `uv run pytest`
  → **110 total, all green**. See ADR 0006.

### Station ID scheduler (cycle 4)

- `radio_server/services/station_id.py` — `StationId(radio, encoder, callsign, *,
  interval=600, clock)` is the sole `radio.transmit` seam. `transmit(audio)` prepends the ID
  into the same over when *due* (due = first over of the session, i.e. `last_id is None`, OR
  `now - last_id >= interval`); within-interval overs do not repeat it. `check(now)` forces
  an ID-only over when the session is overdue (safety net for a real scheduler task).
  `sign_off(now)` sends a closing ID iff the station transmitted, then resets.
  `begin_session(now)` resets per-session state (for the inactivity-timeout path). The timer
  is measured from `last_id`, not the last transmission — the Part 97 invariant is "≤10 min
  since the last ID."
- Config mirrors the auth pattern: `load_callsign()` reads `RADIO_CALLSIGN` and **fails loud
  (no default)** — a station cannot legally transmit without a callsign (Kris sets `AE9S`).
  `load_id_interval()` reads `RADIO_ID_INTERVAL` (default 600) and **rejects** any value
  > 600 (legal max 10 min), non-numeric, or non-positive.
- `IdEncoder` protocol (`encode(callsign) -> AudioFrame`) + `StubId` (deterministic
  `b"<id:AE9S>"`, so `tx_log` is assertable). Real `CwId`/`VoiceId` are later cycles.
- `radio_server/services/dispatch.py` — `Dispatcher` now holds a `StationId` (`transmitter`)
  instead of a raw `Radio`, so no service transmission can bypass ID by construction.
- `tests/test_station_id.py` (23 new tests) + updated `tests/test_dispatch.py` (first over
  now asserts the ID prefix). `uv run pytest` → **88 total, all green**. No new deps.

### Dispatch + services (cycle 3)

- `radio_server/services/dispatch.py` — `Service = Callable[[Session, ServiceContext],
  AudioFrame]` (handlers *produce* audio, no radio I/O). `ServiceContext(clock, tts)` is
  radio-free. `ServiceRegistry` maps digit → `(name, Service)`. `Dispatcher(radio, ctx,
  registry)` is *callable* matching the auth layer's `Dispatch` contract, so it drops into
  `AuthGate(verifier, ..., dispatch=dispatcher)`; it owns the radio and is the single
  `transmit` seam. Returns `DispatchResult(digits, service, transmitted)` (unknown digit →
  `transmitted=False`, nothing sent — graceful, `Outcome.kind` stays `COMMAND`).
- `radio_server/services/tts.py` — `TtsEngine` protocol (`render(text) -> AudioFrame`) +
  `StubTts` (deterministic `b"<audio:...>"`, so `tx_log` is assertable). Piper is later.
- `radio_server/services/time_service.py` — `format_spoken_time(now, tz)` (pure, 24-hour
  local, isolated from dispatch); `load_timezone()` reads `RADIO_TZ` (IANA name) with a
  marked `UTC` default (bad zone → raises); `time_service(tz)`/`register(registry, tz)`
  bind digit `"1"`. Reads the SAME injected clock as the session timeout.
- `radio_server/services/__init__.py` — public surface re-exports.
- `tests/test_tts.py`, `tests/test_time_service.py`, `tests/test_dispatch.py` — 16 new
  tests (incl. full enroll→auth→`"1"`→exact `tx_log` on a fake clock). `uv run pytest` →
  65 total, all green. No new dependencies (stdlib `zoneinfo`/`datetime`).

### Auth layer (cycle 2)

- `radio_server/auth/totp.py` — `TotpVerifier`. `verify_and_burn(code, now=None)`:
  ±1-step windowed (== pyotp `valid_window=1`), constant-time compare, **single-use**
  (burns each consumed `(code, time_step)`; a replay inside the window is refused).
  Burn set is pruned each call so it stays bounded. `provisioning_uri()` emits the
  `otpauth://` enrollment URI. `load_totp_secret()` reads `RADIO_TOTP_SECRET` (env,
  never hardcoded) and raises if unset. `Clock = Callable[[], float]` alias, injectable.
- `radio_server/auth/session.py` — two-state machine (`SessionState`:
  UNAUTHENTICATED ⇄ AUTHENTICATED). `AuthGate.on_dtmf(digits, session, now=None)` is
  the single entry point → `Outcome(kind, detail)` where `OutcomeKind` ∈
  {ACCEPTED, REJECTED, COMMAND}. Inactivity `timeout` (injectable) drops the session.
  Unauth → TOTP verify; authed → injected `dispatch` hook (stubbed; cycle 3).
- `radio_server/auth/__init__.py` — public surface re-exports.
- `tests/conftest.py` — `FakeClock`, shared `TEST_SECRET`/`verifier`/`code_for`.
- `tests/test_totp.py`, `tests/test_session.py` — 22 new tests. `uv run pytest` → 49
  total, all green.
- ADR 0003 records the state machine, single-use burn strategy, and clock injection.
- `pyproject.toml` now depends on `pyotp>=2.9` (see `uv.lock`).

## Next up

The **entire software tower is now built and runs live end-to-end on the mock**, both halves of the
voice relay stream — receive (cycle 13, squelched cycle 14) and transmit (cycle 15) — and the
**half-duplex conflict between them is now arbitrated** (cycle 16: TX takes the radio, RX + scan
stand down and resume). **Cycle 16 was the last pure-software cycle.** What remains needs the box
(or is optional polish):

- **Optional software polish (mock-testable).** The `/events` **"suspended" marker** — surfacing
  the arbiter's mode to listeners on `/events` when RX pauses/resumes — is a cheap observability add
  cycle 16 deferred (the behavior is delivered; the marker is a nicety). **Opus/compression** on
  `/audio/rx` and `/audio/tx` remains a noted-not-built option for constrained links. And the RX
  pump is still a **second** `receive()` reader — consolidating it with the controller's reader (one
  capture fanned to both) is a bring-up decision (and would let the arbiter also gate the controller
  reader, not just the pump).
- **Real hardware backends** (`SignaLinkV71`, `AiocBaofeng`) — the last thing that needs hardware,
  and the "plug it in, it keys up clean" empirical bring-up phase. This is where the marked
  verify-on-hardware facts get confirmed: the Hamlib rig model + serial speed (V71 CAT), the AIOC's
  PTT line (RTS vs DTR), multimon-ng's exact input rate/flags, the piper voice, and the controller's
  real `receive()` cadence / audio chunk size / loop timing (guardrail 1). PTT stays off the DATA
  port / AIOC serial line, never CAT `TX` (guardrail 2).
- **Live scan through the controller (optional).** `Controller` ticks an *attached* `ScanEngine`, but
  nothing starts one over the API yet; the synchronous `/scan` sweep still stands. A later cycle could
  add start/stop-scan control that installs a live engine on the running controller and streams
  carrier/timed dwell over wall-clock.
- **`build_app` production wiring / the real entrypoint.** `build_app` wires the controller only when
  `RADIO_TOTP_SECRET` is set, and full wiring needs real multimon + a piper voice — that comes online
  with the hardware phase. No `uvicorn` entrypoint binds a server yet.
- **More services / auth strength per service (guardrail 4).** The time announce is read-only; guard
  anything that keys TX for real harder. `ServiceContext` is the place to thread per-service
  authority if needed.
- **Runtime hardening for the async driver.** On hardware, `receive()` blocks — run it in a thread
  executor rather than directly in the event loop; and the single-use TOTP `consumed` set is
  per-process in-memory (noted in ADR 0003).

## Open questions / blocked

(none)

## Notes for the cycle runner

- Single-use `consumed` state is in-memory per process; a restart mid-window or a
  multi-process deployment would need it shared/persisted. Out of scope now; noted in
  ADR 0003.
- There is no GitHub instruction issue in this repo — cycles have arrived via the
  prompt. The CLAUDE.md "comment PR URL / swap label on the issue" close step has no
  issue to act on; PRs are still opened for human merge as required.
