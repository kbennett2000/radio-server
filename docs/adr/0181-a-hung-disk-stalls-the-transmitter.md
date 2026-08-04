# ADR 0181 — A hung disk stalls the transmitter

**Status:** Accepted
**Date:** 2026-08-03
**Supersedes / relates to:** [ADR 0180](0180-eventhubs-unbounded-queue.md),
[ADR 0040](0040-nonblocking-dtmf-feed.md),
[ADR 0018](0018-event-log.md),
[ADR 0127](0127-bounded-graceful-shutdown.md),
[ADR 0117](0117-uvk5-tx-timeout-wiring.md),
[ADR 0016](0016-tx-audio-ingest.md)

## Context

ADR 0180 carried this forward by name. `JsonlSink.write` does a synchronous `write` + `flush()`, and
`EventLog.handle` calls it inline from `app.py`'s `_drain_log` task — **on the event loop**.

`EventLog.handle`'s docstring said the ledger "never breaks the flow or a transmission." That is true
of a disk that **raises**: `handle` catches everything and drops the record, which is ADR 0018's
failure isolation and it works. It is false of a disk that **blocks**, and the docstring did not
distinguish the two. A full, stalled or network-backed filesystem does not raise. It waits.

## Blast radius — every path that reaches the sink

The brief asked for every path, and specifically whether a keying path reaches `handle`
synchronously. **None does.** `EventLog.handle` has exactly two callers and both are the lifespan's:

1. `app.py`'s `_drain_log` task — `while True: log.handle(await log_queue.get())`.
2. The lifespan's shutdown drain — `while not log_queue.empty(): handle(get_nowait())`.

That is the wrong question to stop at, though, because the damage is not in the call stack. It is in
**what else is on the loop that `handle` blocks**:

| path | where it runs | what a blocked loop costs |
|---|---|---|
| **`/audio/tx`** | **event loop.** `session.feed(data)` is called inline from the `async def` handler, and `TxSession._key_up` calls `radio.ptt(True)` from there | the browser Talk button. Audio frames stall mid-over; the idle timeout, the disconnect handler and the `finally: session.close()` that **drops PTT** cannot run |
| `POST /transmit` | event loop (`async def`) | the transmit waits out the stall |
| `POST /ptt` | threadpool (`def`) — but still blocked | uvicorn must *dispatch* the request on the loop before the threadpool ever sees it |
| `RxPump` → `controller.step` | loop task | DTMF decode falls behind; RF-authenticated services stall |

So the path that matters is **`/audio/tx`**, and it is named in this ADR rather than left as "the
pump" because the shape it produces is this project's oldest enemy: **a keyed transmitter the server
cannot unkey.** The ordering inside `TxSession.close` means the un-key is not delayed by *its own*
record — `radio.ptt(False)` runs before `on_key(False)` — but it is delayed by the *key-up's*, queued
one loop step earlier. Measured below: 1000 ms behind a sink that blocks 500 ms, because a single
key-up queues **two** ledger records (`arbiter_mode` from the arbiter latch, then `tx_key_up`).

**There is a backstop, and it is not the control path.** `TotRadio._fire` runs on an independent
timer thread and drops PTT outside its lock, with the comment *"this must run while a stuck caller is
parked in transmit()/decode — that is the whole point of the independent timer thread."* Confirmed on
the deployed station: `tx.tot = 180.0`, `uvk5.tot = 180.0`. So a hung disk gives an unattended
carrier of up to 180 s that ends by *latching* streaming TX off (`_locked = True`), not an infinite
one. That is a Part 97 problem with a bounded blast radius, not a mitigation — and `tx.tot = 0`,
which the config explicitly allows, removes it.

**The precedent that decided the shape.** ADR 0040 already made this exact move one module over:
`MultimonStream` hands PCM to a bounded `queue.Queue` drained by a daemon writer thread *"so a
slow/stuck pipe can never block the event-loop caller"*, and `test_streaming_dtmf.py` proves it by
stalling the consumer and asserting the caller returned anyway. The ledger was the last blocking
write left on the loop. This cycle applies the house pattern to it and copies the house test.

## The decoupling shape, against the alternatives

**Chosen: `ThreadedSink`, a decorator on the existing `LogSink` protocol — one bounded
`queue.Queue`, one daemon writer thread.** `EventLog` and `JsonlSink` are unchanged; the protocol was
already the swap seam, so the composition root now reads
`EventLog(ThreadedSink(JsonlSink(load_log_path(settings))))`.

**Why not `asyncio.to_thread` per write.** The brief offered it as the simpler option, and the repo
does use it elsewhere. It is wrong twice, and neither reason is the thread hop:

- Its default executor has several workers, so fire-and-forget writes **interleave**. The ledger
  loses record order, which the brief pinned as non-negotiable and which a station log cannot give up.
- Awaiting each write to restore that order makes `_drain_log` **suspend during the write** — and
  that destroys the property ADR 0180 proved and wrote into `app.py`: the ledger's drain can never
  fall behind, *because* `asyncio.Queue.get()` does not suspend on a non-empty queue and `handle` is
  synchronous, so it spins its queue empty inside one loop step. A suspending drain is a genuinely
  slow consumer of a 160-deep `DROP_OLDEST` queue — **silent Part 97 record loss, from the one
  subscriber ADR 0180 established must never lose anything.** Fixing this hazard would have opened
  that one.

One queue and one thread keeps `handle` synchronous and O(1), so ADR 0180's proof holds verbatim and
ordering is guaranteed by construction rather than by discipline.

**The hop cost, measured anyway, because the brief asked.** On the deployed station an
`asyncio.to_thread` round-trip is **86.3 µs** (n=2000). At the busiest one-second bucket ADR 0180
measured on `/events` (4/s) that is 0.03 % of a second of loop time — irrelevant, as the brief
suspected. But it is **46× the 1.9 µs write it would be avoiding**, which is the more interesting
number: `to_thread` would make the ledger dramatically more expensive in the healthy case to protect
the pathological one. The one-way hand-off chosen instead costs **1.03 µs** per record — *less than
the write it replaces*, because there is no round-trip to await.

**Shutdown behaviour, stated.**

- **SIGTERM** (`systemctl stop`, `systemctl restart`). uvicorn's handler → `timeout_graceful_shutdown
  = 5.0` → the lifespan teardown still runs (ADR 0127: `force_exit` is only set by a second SIGINT).
  The existing drain enqueues the residual events — now fast, no longer an unbounded run of blocking
  flushes on the loop — then `event_log.close()` → `ThreadedSink.close()` puts a sentinel, joins the
  writer, and closes the inner sink. **Nothing is lost on a healthy disk.** The join is bounded at
  `LEDGER_CLOSE_TIMEOUT_S = 2.0` so a *hung* disk cannot spend the unit's `TimeoutStopSec = 20` and
  earn a SIGKILL — which would be strictly worse, because SIGKILL skips the rest of the teardown,
  including the unkey. A timed-out join is counted into `dropped_records` and logged, not swallowed.
- **SIGKILL.** Whatever is still queued dies. The old code guaranteed "on disk before the next event
  is handled"; this guarantees "on disk within the write latency, and certainly by graceful
  shutdown". **The loss window is exactly the stall window** — the same microseconds that were being
  spent blocking the transmitter. You cannot have both, and a station that cannot unkey is the worse
  failure. `atexit.register(self.close)` (ADR 0040's idiom) covers exits that never reach the lifespan.

## The writer's own backlog — and the answer inverts ADR 0180's

The brief predicted the drop policy would differ here, and it does, in the opposite direction from
every other queue in the tree. `AudioHub`, `MultimonStream` and `EventHub`'s default all drop the
**oldest**. `ThreadedSink` drops the **newest**.

There is no peer to drop — ADR 0180's answer for `/events` — because this sink is the terminal, not a
subscriber; and there is no snapshot to resync from, because a ledger record is *history* and nothing
supersedes it. What is left is which end of the queue to sacrifice:

- **Drop-oldest** would leave a log missing records from the middle, with jumping timestamps, and a
  counter that says how many are gone but not *which* or *where*.
- **Drop-newest** leaves a **contiguous prefix**. The last written record's `ts` plus
  `dropped_records` pins the gap exactly: everything after that time, this many.

For an operating log, being able to say where the hole is *is* the property. Nothing is raised at the
publisher either — ADR 0018's rule stands, the ledger is a place data goes to rest and never a place
a fault comes from — and **no gap record is written**, because the brief forbids changing what the
ledger records. The gap surfaces as a counter and one `logger.warning` per gap.

### The bound is derived, and the measurement overturned the plan

```
DEFAULT_LEDGER_QUEUE_MAXSIZE = PEAK_RECORDS_PER_SECOND × GRACEFUL_SHUTDOWN_SECONDS = 241 × 5 = 1205
```

The plan for this cycle proposed inheriting ADR 0180's `160` under the rule "never a tighter bound
than the hub feeding it". **The measurement killed that**, which is the argument for measuring first.
Parsing the station's own `radio-server.jsonl` — 71,455 records across 455.9 h — gives:

| | |
|---|---|
| records | 71,455 |
| span | 455.9 h |
| busiest 1 s bucket | **241 records/s** |
| typical bucket | 1-2 records/s (2,699 buckets of 1, 475 of 2) |
| types | `tx_key_down` 68,139 · `arbiter_mode` 1,443 · `station_id` 570 · `tx_key_up` 439 · `command_dispatched` 270 · `session_open` 152 · `auth_rejected` 151 · `auth_accepted` 150 · `session_close` 139 · `tx_failed` 2 |

At 241/s a 160-deep queue absorbs **0.66 s** of hung disk. The organic rate is 1-2/s and the peak is a
bench driver key-cycling `POST /ptt` — which is exactly the load a bound has to survive, so it is the
right number to use, not one to discount. (It is also why `tx_key_down` is 95 % of the log: ADR 0180's
own driver called `POST /ptt {"on": false}` ~11,000 times. The ledger recorded that faithfully.)

`5` is `GRACEFUL_SHUTDOWN_SECONDS` — the window uvicorn allows in-flight work on SIGTERM before it
cancels and runs the teardown that flushes this sink. The product is what makes the bound mean
something rather than being round: **a backlog deeper than this could not be written even if the disk
recovered the instant shutdown began**, so queueing it would be a promise the process cannot keep —
the same "worse than dead" shape ADR 0180 used for its own bound. At the organic rate the same 1,205
slots absorb roughly ten minutes of hung disk without losing a record.

`timeout_graceful_shutdown` was load-bearing for ADR 0127 and now for this bound, and **nothing
pinned it**; `test_entrypoint_tls.py` pinned only the two ping kwargs beside it. It is pinned now.

## Decision

- `ThreadedSink` in `eventlog/sink.py`: bounded `queue.Queue`, one `ledger-writer` daemon thread,
  drop-newest on overflow, bounded idempotent `close()`, `atexit` belt-and-braces.
- `build_app` composes `EventLog(ThreadedSink(JsonlSink(...)))`. The inner sink is constructed first,
  so the fail-loud open of an unwritable `logging.path` still happens at the composition root.
- `EventLog.sink_stats()` — an optional passthrough, so `LogSink` stays a one-method protocol and no
  test double is forced to invent counters. `None` reaches `/status` as `ledger: null`.
- `GET /status` gains a `ledger` block beside `events`, documented per field in `api.md`.
- The two docstrings that claimed the safety property the code did not have are corrected — the
  brief's requirement, and the reason this is a failure class rather than an oversight.

### `write_errors` is new information, not just a new number

ADR 0018 isolated sink failures and **counted none of them**. A full disk, a revoked permission or a
vanished mount therefore stopped the Part 97 operating log *silently and permanently* — `handle`
swallowed every `OSError` and nothing anywhere recorded that a record had been lost. That blind spot
is the substance of the docstring being wrong, so it gets a number. `write_errors` climbing while
`written` stays flat is a ledger that is not being written at all.

### Nothing renders it, and that is a decision

`StatusPanel` takes only counters whose nonzero value means something is wrong **right now**, on
`TransportBanner`'s rule that the absence of a fault is not a status worth a banner. A ledger gap is
a compliance fact discovered by reading, not an alarm to act on in the moment, and a row that is a
reassuring zero on every healthy station is a row operators stop reading. An operator reads it at
`/status`, where `api.md` documents each field, or reads the journal warning. There is no web change
in this cycle at all.

## Evidence

### The fail-first red, on the behaviour and with numbers

`tests/test_ledger_does_not_stall_the_loop.py`, against master's composition:

```
E  AssertionError: a key-up waited 502 ms behind a ledger write that blocks 500 ms —
   the ledger is writing on the event loop
E  AssertionError: PTT stayed asserted 1000 ms after the talker let go, behind a ledger
   write that blocks 500 ms — that is a stuck key, not a slow log
E  AssertionError: the event loop was unavailable for 500 ms while the ledger wrote
   (probe ticks every 5 ms); nothing else on the loop ran — including PTT
3 failed, 2 passed
```

The two that passed are the invariants the fix must not break: record ordering, and a graceful
shutdown losing nothing. They passed before and pass after.

### Green, same instruments

| measurement | before | after |
|---|---|---|
| key-up delay behind a 500 ms blocking sink | **502 ms** | **1.3 ms** |
| un-key delay after the talker let go | **1000 ms** | **0.1 ms** |
| worst event-loop gap while the ledger wrote | **500 ms** | **5.3 ms** |
| cost of one `write()` on the loop | 1.9 µs (the inline write) | **1.03 µs** (the enqueue) |

The 5.3 ms is the probe's own 5 ms tick: the loop was never blocked at all.

### Measured on the deployed station, before the constant was written

| measurement | value |
|---|---|
| `write` + `flush` on the station's storage (n=2000) | min 1.7 µs · **median 1.9 µs** · p99 10.2 µs · max 41.4 µs |
| `asyncio.to_thread` round-trip (n=2000) | **86.3 µs** |
| peak ledger record rate | **241/s** (organic: 1-2/s) |

The healthy write is ~2 µs, which is why this survived 180 ADRs unnoticed. The hazard was never that
the ledger is slow; it is that nothing bounded how slow it could become.

## Findings — recorded, not fixed

- **`Recorder.write` is the same hazard, one module over.** `rx/pump.py` calls it from the pump's
  loop task, so a hung disk stalls RX capture, the browser fan-out and DTMF decode the same way.
  Found by this cycle's blast-radius sweep. Same fix shape, its own cycle.
- **`tx.tot = 0` removes the only backstop.** The config allows disabling the transmitter time-out,
  and with it disabled a blocked loop mid-over has nothing left that can drop PTT.
- **The lifespan's shutdown drain is still unbounded in count.** It is now cheap per record, but
  nothing caps the loop itself.
- **A SIGKILL now loses queued records** where the synchronous flush did not. Bounded by the write
  latency and stated in `api.md`; it is the price of the trade, not a defect.
- **`PolledGate` still has no staleness expiry.** Unchanged from ADR 0178/0179/0180, restated.
- **The ledger is 95 % bench noise.** 68,139 of 71,455 records are `tx_key_down` from drivers calling
  `POST /ptt {"on": false}`, most with `duration: null` because no key-up paired them. Not a defect —
  the ledger recorded what happened — but anyone reading this log for operating history should know.

## Out of scope

The firmware fork, the kv4p witness checkout, `PolledGate`, `/events`' auth and snapshot behaviour,
and the ledger's record taxonomy and format — no new record type, no changed field. No keying-path
change: nothing under `radio_server/tx/`, and no added line inside `_key_on`, `_key_off`, `ptt` or
`transmit`.
