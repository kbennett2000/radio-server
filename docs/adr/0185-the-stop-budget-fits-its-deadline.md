# ADR 0185 — The stop budget fits its deadline

Status: Accepted

## Context

[ADR 0184](0184-the-sigkills-stopped-before-we-credited-them.md) made the teardown's worst case
finite and said plainly that it did not fit: the bounded steps summed to **23.25 s** against a
documented `TimeoutStopSec=20`, with `radio.ptt(False)` and `radio.close()` still unbounded.

Two things about that were wrong, and both were found by measuring rather than by reading:

1. **23.25 s was an undercount.**
2. **Some of the bounds do not bound the process at all** — a deadline can expire and the stop still
   ends in the SIGKILL it existed to prevent.

## A bound that expires is not a worker that goes away

`radio_server/shutdown.py`'s docstring — written by ADR 0184 — blessed
`wait_for(run_in_executor(None, fn))` on the grounds that cancelling the wrapper *"succeeds whether
or not the worker thread notices, so that deadline really does hold — measured at 0.50 s against a
wedged 30 s worker"*. That measurement was of the **await**. The **process** is a different question,
and the answer is the opposite. On the station's own interpreter (Python 3.14.4,
`THREAD_JOIN_TIMEOUT = 300`), wedging a worker for 30 s:

```
default executor:  await returned 0.50s   asyncio.run() returned 30.00s   process exited 30.14s
daemon thread:     await returned 0.50s   asyncio.run() returned  0.50s   process exited  0.61s
dedicated pool:    shutdown(wait=False) returned in 0.02 ms  ...          process exited 20.13s
```

`asyncio.Runner.close()` joins the default executor with `THREAD_JOIN_TIMEOUT`, and
`concurrent.futures.thread` registers an atexit hook that joins every worker of every pool with **no
timeout at all**. So an executor worker is never abandoned: the hang moves from the lifespan, where
it is logged, to interpreter shutdown, where it is not — and still lands on `TimeoutStopSec`.

**Only a daemon thread walks away.** This is load-bearing for everything below, because a budget is a
sum of bounds, and summing a bound that does not release the process asserts something false.

## The corrected enumeration

Five terms ADR 0184's table missed, each verified at source:

| missed term | site | s |
|---|---|---|
| D-STAR gateway reader join | `dstar/client.py` | 1.0 |
| pymumble library-thread join | `link/pymumble_client.py` | 2.0 |
| uvk5 transport reader join (inside `radio.close()`) | `backends/uvk5/transport.py` | 1.0 |
| TX pacer join (inside **both** `ptt(False)` and `close()`) | `backends/soundcard.py` | 5.0 ×2 |
| `PolledGate.stop()` join, inside `RxPump.run()`'s `finally` | `activity/gate.py` | 1.0 |

The last one is the structural lesson. It runs *during cancellation delivery*, so the pump's async
bound cannot preempt it: `asyncio.wait`'s timer cannot fire while the loop thread sits inside
`thread.join()`. **A bound expressed in async time does not cover synchronous work done inside it.**
On-loop thread joins are additive terms, not absorbed ones — and that is now the rule the budget
function encodes.

Honest worst case before this cycle: **≈32 s**.

## What a stop actually costs, measured

Nothing in the package had ever timed a teardown step, which is why every bound was a round number.
`timed()` was landed first, deployed, and the derivations come from what it reported (n=19 stops):

| step | median | max |
|---|---|---|
| `holder: radio.close` | **110 ms** | **209 ms** |
| `holder: rx pump stop` | 6 ms | 18 ms |
| `lifespan: ledger close` | 0 ms | 86 ms |
| `lifespan: dstar bridge stop` / `mumble disconnect` / `scan stop` / `decoder reap` | 0 ms | 0 ms |

`holder: ptt(False)` collected **zero samples**, and that is reported rather than papered over: a
quiescent shutdown never has the arbiter transmitting, so the step is skipped. n=0 is not a
measurement, so its bound is derived structurally instead and says so.

Whole-phase figures over n=201: signal delivery median 84.4 ms / max 194.7 ms; lifespan teardown
median 113.8 ms / **max 240.9 ms**. The teardown's real worst case is **1 %** of its theoretical one.

## Co-occurrence: pessimistic by more than half, and it still has to be covered

Measured on the station, not inferred. `GET /dstar/status` reports `configured: true, mode: "idle",
gateway.running: false` — the bridge is built-not-started (ADR 0089) with `reflector = ""`, so its
6.0 s is not paid, and the **DV Dongle is not even plugged in** (`/dev/serial/by-id/` has two DVAPs
and no `DV_Dongle`). Mumble has no `autoconnect`. `decode_mode = "native"`, so the DTMF reap is zero.
The cadence is refcounted and only starts inside a bridge. The pacer join exists only while keyed.

The **unconditional floor** is ≈10.25 s — half the old deadline with no bridges at all.

But every zeroed term is enabled by **config, not code**, and the deadline lives in a unit file
edited separately from `radio.toml`. A deadline sized to today's configuration expires the first time
someone links a reflector, and nothing connects the two edits — [ADR
0178](0178-checks-that-silently-answer-the-safe-looking-default.md)'s defect class exactly. So the
pessimism is reported and the deadline still covers the full sum.

## What a SIGKILL actually costs now — and it changes why this is worth doing

`HUPCL` covers the carrier (ADR 0183), so this stopped being a stuck-transmitter question. Three of
the four inherited claims did not survive checking:

- **Queued ledger records: trivial.** `ThreadedSink._pump` writes immediately on dequeue and organic
  load is 1–2 records/s, so a random kill loses **0–2 records**. "Up to 1205" is the ceiling of an
  already-stalled disk — where a clean stop's own 2.0 s close timeout cannot drain it either.
- **The DV Dongle wedge: asserted, never measured, and moot.** ADR 0104's claim traces to
  [ADR 0100](0100-dvap-autoheal-usb-wedge.md), which is about the **DVAP** — a different device,
  wedged by restarting an external gateway process. ADR 0094's wedge self-recovers on the next frame;
  ADR 0099's was a software bug ADR 0099 fixed. No `kill -9` re-open test exists anywhere, and the
  dongle is unplugged.
- **The Part 97 sign-off: the framing was wrong.** `StationId.sign_off` is reached only from
  `Controller._close_session`; **no shutdown path calls it, clean or SIGKILL.** That is a standing
  compliance gap, not a SIGKILL cost. Only `TxSession.sign_off_id` — a live browser/Mumble streaming
  session — is genuinely SIGKILL-specific, and it is one missed closing ID.
- **The recorder: trivial.** `wave.Wave_write.writeframes()` patches the RIFF header on every call.

**So the honest severity is low, and the reason to do this work is not SIGKILL damage.** It is that
an unbounded teardown means an unbounded *stop*, and a restart that never completes is an
availability fault. This ADR states that instead of inheriting ADR 0104's dongle framing.

## Decision

### Bound the two remaining steps, off the loop

`call_bounded(fn, timeout, *, label)` — `join_bounded`'s sibling for synchronous work, on a **daemon
thread** for the reason above. It returns elapsed seconds, or `None` when the deadline expired, so a
caller can tell "released" from "we do not know".

- **`PTT_OFF_TIMEOUT_S = DEFAULT_WRITE_TIMEOUT + TX_PACER_JOIN_TIMEOUT_S`** — derived from the two
  declared bounds the unkey path can spend, because n=0. On uvk5 the unkey proper is one
  `_write_registers` frame bounded at 2.0 s; on the AIOC it is a DTR ioctl and the pacer join is the
  whole cost. It deliberately does **not** cover uvk5's `_restore_rx_frequency` (≥12 s): that
  restores the *receive* leg, and abandoning it at teardown is correct.
- **`RADIO_CLOSE_TIMEOUT_S = 2.0`** — ~10× the measured max (209 ms). Deliberately **not** the sum of
  `close()`'s own internals (≈4 s on the AIOC): at shutdown the process is about to exit, the kernel
  reclaims the fd, and `HUPCL` still drops the carrier, so waiting them out buys nothing.
- **`REBUILD_CLOSE_TIMEOUT_S`** is larger, sized to those internals, because on a swap nothing is
  exiting and the device genuinely has to let go.

### A swap whose close did not return is refused, not half-done

`rebuild()` now raises `RadioUnavailable` (→ 503) rather than proceeding. Proceeding would open a port
**this same process still holds**; the rollback then reopens the *same* port, fails identically, and
leaves the holder radio-less — the one outcome the rollback exists to prevent. Refusing is retryable
for free, because `close()` early-returns once `_closed` is set. `_safe_close` is bounded too: it ran
synchronously on the loop of a *live* server.

### Shrink what derives

- **`TX_PACER_JOIN_TIMEOUT_S = DEFAULT_TX_LEAD_SECONDS × 2`**, replacing a bare `5.0`. `stop()`
  clears the deque before joining, so it waits for at most **one** in-flight `write()`, and the
  largest chunk any producer enqueues is the lead-in slug. The 2× margin mirrors `SCAN_JOIN_TIMEOUT_S`
  over `DEFAULT_SCAN_POLL`. This mattered because the join sits inside *both* newly bounded calls.
- **`CONTROLLER_CLOSE_BUDGET_S`** — one deadline for the whole DTMF reap instead of three
  independent `timeout=1.0` waits. `proc.terminate()` fires first, so writer, process and reader are
  dying concurrently; three waits restarted the clock three times for one event. Nothing is lost when
  it expires that was not lost before.

Not shrunk: **`GRACEFUL_SHUTDOWN_SECONDS`**, because ADR 0181 derives
`DEFAULT_LEDGER_QUEUE_MAXSIZE = 241 × 5` from it — trimming it trades a stop-budget problem for Part
97 operating-log loss. And **D-STAR's `+2.0` margin**, which is *chosen, not derived*; it is now a
named constant that says so rather than a literal that hides it.

### Fix three waits a budget cannot contain

- `kv4p/transport.py` set `handle.timeout` but never `handle.write_timeout`, so pyserial did
  `select.select(..., None)` — an infinite wait. It also made `kv4p/pacer.py`'s docstring claim ("the
  join is bounded by the transport write timeout") false; it is true now.
- `uvk5/transport.py`'s blocking `self._wire.acquire()` had no timeout. Bounded at 3 ×
  `DEFAULT_REQUEST_TIMEOUT` — far above any real contention, so the property the blocking path exists
  for ("the key-up always gets its turn") is preserved, while a wedged holder can no longer block
  forever.
- The D-STAR vocoder close moved from `run_in_executor` to `call_bounded`, per the finding above.

### One declared budget, and a test that adds it up

`teardown_budget_seconds()` sums every **outer** bound in lifespan order; `stop_budget_seconds()`
adds signal delivery, the graceful window and the exit reserve. Only outer bounds are charged — the
pacer and transport joins are inside `radio.close()`, which now has its own deadline, and that is
exactly what bounding the outer call buys.

| | s |
|---|---|
| signal delivery (measured max, n=201) | 0.20 |
| uvicorn graceful window | 5.00 |
| teardown bounds | 25.25 |
| exit reserve (measured) | 0.25 |
| explicit margin (a term, not a multiplier) | 2.00 |
| **required** | **32.70** |
| **shipped `TimeoutStopSec`** | **35** |

### Ship the unit, and check the box

`scripts/radio-server.service` is now in the repo, and an identity test asserts the file and
`TIMEOUT_STOP_SEC` agree and cover the computed budget. `docs/deployment.md` points at it instead of
carrying a copy — which matters because `test_docs_contract.py` deliberately blanks fenced blocks, so
**the one number the budget is sized against was invisible to every test in the repo**.

Shipping a file does not make a machine adopt it, so `acceptance.py` now reads the **installed**
deadline (`systemctl --user show -p TimeoutStopUSec`) and asserts it covers the budget. That is the
only mechanism the repo has to verify the box, and it makes drift visible immediately rather than at
the next SIGKILL.

### Sizing the deadline, and why it is not the raise the journal refuted

`TimeoutStopSec` goes 20 → 35. The station's own history says a raise did not work: it went 10 → 20
and **31 more SIGKILLs followed at the new deadline**. That raise was made against an *unbounded*
stall — `timeout_graceful_shutdown` was unset, i.e. wait forever — so the deadline moved and nothing
else did. What fixed it was bounding the window in code (ADR 0127).

This is the opposite operation: the budget is a finite sum of named constants that a test adds up,
and the deadline is sized *to* it. The test is what keeps the distinction real — raise a bound and
the identity test names it rather than letting the deadline quietly absorb it.

What a longer deadline costs is only a delay to SIGKILL, which fires when the teardown has already
failed — and the measured teardown is 241 ms at its worst.

## Concurrency was considered and refuted, with the price attached

Gathering `dstar_bridge.stop()` and `link_manager.disconnect()` saves **2.0 s of ~32 s — about 6 %** —
because the synchronous joins hold the single loop thread and cannot overlap at all. It pays for that
with two RF-safety races, not latency regressions:

- `TxSlot.release()` has **no ownership check**, so D-STAR's `_force_unkey` can free a slot Mumble
  holds, and a third claimant can key while Mumble believes it owns the transmitter.
- D-STAR's synchronous `ptt(False)` can land between Mumble's `await await_tx_ready` returning and
  its `ptt(True)` — ADR 0099's measured 15 s stuck key, resurrected by concurrency.

The orderings that look incidental mostly are not: both bridges must precede `holder.stop()` because
they hold and key the radio it closes, and every ordering inside `holder.stop()` and
`DStarBridge._teardown` has a named, measured failure behind it.

**Where concurrency would pay is different work**: ~9.75 s of the budget is on-loop synchronous thread
joins of the same shape (`Event.set()` then `thread.join(timeout)`). Split signal from wait and
collect them against one shared deadline and that becomes ~2 s — which is exactly what would let a
20 s deadline stand. Five modules and a *partial* ordering (some joins must finish before the step
that frees their resource), so it is carried with its arithmetic rather than attempted here.

## Consequences

- The budget is computable, tested, and enforced at both ends — the file the repo ships and the value
  the box has installed.
- `radio.ptt(False)` and `radio.close()` no longer run on the event loop. Measured: a wedged device
  made the loop unavailable for **3006 ms** before, and the loop-gap probe now pins it.
- A failed swap is a 503 instead of a silently half-open station.
- pytest **2475 passed / 5 skipped** (from 2464/5); vitest **14 files / 163 tests** unchanged.
- **The deployed unit changed.** `TimeoutStopSec` on the reference station is now 35; the previous
  unit is at `/tmp/radio-server.service.pre-0185` on the box.

## On the bench, after deploy

Per-step teardown on the deployed branch, n=59 stops:

| step | median | max | bound |
|---|---|---|---|
| `holder: radio.close` | 114 ms | 236 ms | 2.0 s |
| `holder: rx pump stop` | 7 ms | 35 ms | 2.25 s |
| `lifespan: ledger close` | 0 ms | 86 ms | 2.0 s |
| everything else | 0 ms | 0 ms | — |

**Abandoned workers (a bound that expired): 0.** That is the property that separates a bound from a
guillotine — it has never fired on a healthy close, which is what the derivation was for.

Stop wall time, unchanged by the bounds (12 per arm, stubborn client): no client **0.32 s**,
handshake-then-silence **5.34 s**, 24/24 teardowns completed. Reported as a regression check, not as
a before/after: this diff does not touch the WebSocket or graceful path.

`acceptance.py` full run: **`systemd` PASS**, including the new check reading the *installed*
deadline — `installed TimeoutStopSec covers the budget: 35s want >= 32.70s`. `presets`, `rx`, `dtmf`,
`auth`, `tx`, `split`, `services` PASS; `web` FAIL on the known witness `kv4p GET /healthz 404`;
`split-minus` SKIP.

**A process finding worth carrying.** The station was found at 147.555 / no split / no tone / high
power at the start of this cycle. ADR 0184's cycle restored it correctly and *then* ran two more
`--only systemd` passes, each of which restarts the service. **The restore has to be the last bench
action**, and this cycle's is.

## Carried, named and not fixed

- **The signal-then-join restructure** — ~9.75 s → ~2 s, the single most valuable structural change
  available, with the partial ordering it needs.
- **A server-initiated WS close at shutdown** — the only thing that *removes* the 5 s graceful window
  rather than bounding it.
- **`rx/pump.py`'s reader executor** has the same abandon-is-not-abandon hole, but converting it
  touches the RX hot path rather than a teardown-only call.
- **uvk5's `_restore_rx_frequency`** (≥12 s) — abandoning it at teardown is correct; the real fix
  needs a UV-K6 on the bench.
- **PortAudio's draining `stream.stop()`**, where `abort()` exists and is used nowhere. The
  RF-correct rule for a later cycle: `abort()` only when `pacer.stop()` reported `discarded > 0`,
  because `wait_drained` returns when the pacer has *written*, not when the card has *played*.
- **`TxSlot.release()`'s missing ownership check**, found while proving concurrency unsafe.
- **The standing Part 97 gap** (no shutdown path calls `StationId.sign_off`) and **no
  `UNLINK_URCALL`** on `dstar_bridge.stop()`.
- **`Recorder.write`** and the **~1.3 s synchronous key-up** — excluded by the brief.
