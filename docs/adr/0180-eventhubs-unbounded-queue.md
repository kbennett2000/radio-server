# ADR 0180 — EventHub's unbounded queue

**Status:** Accepted
**Date:** 2026-08-03
**Supersedes / relates to:** [ADR 0179](0179-three-instruments-nobody-can-read.md),
[ADR 0171](0171-a-dead-rx-listener-gets-reaped.md),
[ADR 0170](0170-a-stranded-slot-stops-being-invisible.md),
[ADR 0018](0018-event-log.md),
[ADR 0014](0014-rx-audio-streaming.md),
[ADR 0011](0011-api-layer.md)

## Context

`EventHub.subscribe()` handed out a plain `asyncio.Queue()` — no `maxsize` — and `publish()` did a
plain `put_nowait` into every one of them. This was not an oversight; it was a stated choice that
outlived its reasoning. The class docstring said so ("`publish` is synchronous and non-blocking
(unbounded queues)"), and `AudioHub` — built three years of ADRs later — named it as the contrast it
was deliberately departing from:

> **Bounded + drop-oldest, not unbounded.** `EventHub` uses unbounded queues — correct for low-rate
> control events. A continuous ~96 KB/s audio stream to a slow consumer would grow such a queue
> without limit […]

"Correct for low-rate control events" is true of the *producer* and says nothing about the
*consumer*. A subscriber that is registered and slow grows its queue at whatever rate the station
publishes, forever, and every published event is referenced from every queue.

The `/events` handler's own comment had already written the finding down and left it:

> This handler is the OLDEST of the four with that shape — it predates all three RX paths — and it
> leaks the worst: `EventHub`'s queue has no maxsize and no overflow handling, where `AudioHub` caps
> at 64 frames and drops the oldest.

ADR 0171 reaped the zombie *subscription* — a peer that has gone away is now noticed within 40 s.
What it could not fix is a peer that is still there and still slow.

## The drop policy, against the alternatives

Three options, and the choice turns on one property of `/events` that neither of the others has.

**Drop-oldest everywhere (AudioHub's policy).** Rejected. A PCM frame is fungible: the next one
supersedes it and a listener hears a glitch. An event is not. `ptt`, `alarm`, `auth`, `command`,
`busy` and `activity` are each a distinct fact with no successor, and dropping the oldest is
precisely dropping the `alarm` to keep the `status`. Worse, the client is never told: it stays
*connected*, rendering state it has no way to know is wrong. That is this repo's recurring failure
shape — ADR 0170's stranded slot, ADR 0166's dead reader, ADR 0179's unreadable zero — and it is the
one we have learned to refuse.

**A hard cap that raises.** Rejected outright. `put_nowait` would throw `QueueFull` into whatever
called `publish`: a REST handler, the RX pump, the duplex arbiter, the transport watcher. A slow
browser could fault a transmission path. ADR 0018's rule for the ledger holds for the hub verbatim —
a passive consumer is a place data goes to rest, never a place a fault comes from.

**Drop the subscriber. Chosen.** `/events` sends a full `status` snapshot on connect. That single
line is what makes disconnection the *cheap* failure here: **a reconnect is a resync.** So the client
is sent an `overflow` frame saying what it missed, closed `1013 Try Again Later`, and comes back with
correct current state. The web client already retries every close code except `1008`, starting at
1 s — so recovery is about a second, and the only permanent loss is the edge events inside the gap.
Loud and self-healing beats silent and indefinite.

**What a disconnected client experiences**, in order: an `{"type":"overflow","data":{"missed":161,
"queue_maxsize":160}}` frame → close `1013` → ~1 s of backoff → a fresh socket → a `status` snapshot.
The frame comes *before* the close deliberately: a browser cannot act on a close code alone, which is
the same reason `talker_slot` accepts a refused talker just to tell it `{"status":"busy"}` (ADR
0161/0170).

**The backlog is discarded, not delivered.** A dropped subscriber's queue is emptied and the notice
put in its place. Draining 160 stale events to a consumer that is by definition too slow to keep up
takes *longer* than reconnecting does, and every one of them is about to be superseded by the
snapshot. Measured in the test that pins this: 162 events published to a stalled peer, **3 frames
delivered** — the snapshot, the one event already in flight, and the notice.

## The log-drain task — checked, as the brief asked, and it is not the slow consumer

`_drain_log` is a subscriber of this same hub, and it is not a socket. The check matters both ways.

```python
while True:
    event = await log_queue.get()
    app_.state.event_log.handle(event)
```

`asyncio.Queue.get()` **does not suspend when the queue is non-empty** — it returns through
`get_nowait()` without ever awaiting — and `EventLog.handle` → `JsonlSink.write` is synchronous
throughout. So this loop spins its queue to empty inside a single event-loop step and only ever parks
on an *empty* queue. Its backlog is bounded by the largest **synchronous** publish burst (two, at
`POST /ptt` and at `POST /radio/select`), not by elapsed time. **It cannot grow the queue without
limit.**

It also must never be dropped. It has no reconnect and no snapshot; dropping it would stop the
Part 97 operating log permanently, silently, with no gap marker. So it keeps `DROP_OLDEST` — belt and
braces for a branch the analysis says never fires, with `dropped_deliveries` as the number that says
whether the analysis was wrong.

Its real hazard is the opposite one and is **carried named, not fixed**: `JsonlSink.write` does a
blocking `write` + `flush()` on the event loop. A hung disk stalls the loop, and therefore PTT. That
is a bigger change than this cycle and belongs to its own.

## Decision

**One bound, two policies, chosen per subscriber at `subscribe()` time.** The hub has two kinds of
subscriber and only one of them has a way back, so the policy is a property of the subscriber, not of
the hub. `DROP_OLDEST` is the default, so a consumer that cannot recover cannot silently inherit the
policy that would end it; `/events` opts in to `DROP_SUBSCRIBER` explicitly. A source-scanning test
pins both call sites, in `test_relay_subscribers`'s idiom — a registry is a mechanism a subscriber can
decline to use, the text of the call is not — so the next person to subscribe has to state a choice
and reads the argument for it when CI stops them.

### The bound is derived, not chosen

```
DEFAULT_EVENT_QUEUE_MAXSIZE = PEAK × DEAD_PEER_SECONDS = 4 × 40 = 160
```

**`4` is measured**, on the deployed station, against master, before any of this was written: the
busiest one-second bucket on `/events` through a full `scripts/bench/acceptance.py` run — a run that
tunes, keys, authenticates over RF, dispatches a voice service and restarts the unit. The whole run
produced **25 frames in 116 s**. An idle station produced **nothing at all in 60 s** beyond the
connect snapshot.

**`40` is `ws_ping_interval + ws_ping_timeout`** — the 40.0 s ADR 0171 measured against real uvicorn
as the worst case for the server to notice a peer that has gone silent with its socket still open.

The product is what makes the number mean something instead of being round: **at the busiest rate
this station has been measured at, a subscriber can only fill this queue by being slower than real
time for longer than it takes the server to give up on a peer that is not there at all.** Overflow is
worse than dead — which is exactly the condition under which dropping the subscriber is correct. Read
the other way it is the stall this hub tolerates: `160 / rate` seconds, so a client hammering the
REST surface buys itself proportionally less patience, which is right, because it is also the one
making the backlog.

Worst-case memory is ~765 B (the largest frame, a `status`) × 160 ≈ **122 KB per subscriber**, and
less in practice: one `Event` is referenced from every queue, not copied into each.

### The counters, named for what they count

`GET /status` grows an `events` block — the scattered nested-block shape ADR 0179 settled on, beside
`slots` and `rx_demand`.

| field | it counts |
|---|---|
| `published` | events handed to the hub since start — the denominator that makes the zeroes readable |
| `subscribers` | live subscriber queues now (the ledger drain is one) |
| `queue_maxsize` | the bound; the threshold that makes `deepest_queue` readable, as `stale_after_s` does for `age_s` |
| `deepest_queue` | the greatest depth **any one** queue has been observed at — a high-water mark, not a current depth |
| `dropped_subscribers` | queues **unregistered** for reaching the bound. Not "slow clients"; not "disconnects" — the hub closes nothing, the handler does |
| `dropped_deliveries` | **per-subscriber deliveries** discarded. One event missed by two subscribers is 2 |

`deepest_queue` is the one that is not a fault counter, and it is the most useful of the six: it is
the standing evidence for whether `4 × 40` is still the right arithmetic. A *current* depth would read
0 on every healthy station and answer nothing.

### Nothing renders it, and that is a decision

`StatusPanel`'s instrument rows (ADR 0179) take only counters whose nonzero value means something is
wrong **right now**, on `TransportBanner`'s rule that absence of a fault is not a status worth a
banner. A dropped subscriber repairs itself in about a second by construction — that is the entire
design — and the only party that lost anything has already resynced. A row that is a reassuring zero
on every healthy station is a row people stop reading, which is what must not happen to the two rows
already there.

**What an operator does instead:** reads `GET /status` → `events`, where `api.md` documents each
field's nonzero meaning; or reads the journal. Every drop writes an `event subscriber dropped …` line
— the third trace of one fact (ADR 0167), and the durable one, since the counters reset with the
process and the notice goes to a client that is about to leave.

The one web change is a test, not a feature: the hook's reconnect-on-1013 is now pinned, because the
server-side design is only correct while the client comes back.

## Evidence

### The fail-first red, on the behaviour and not on a signature

The first red was a `TypeError` on the new keyword argument, which proves nothing about queues. The
parameter was added as accepted-and-ignored so the recorded red is the defect itself:

```
E   AssertionError: a subscriber that never drained holds 250 events after 250 publishes —
    the queue is unbounded
E   assert 250 <= 200
E    +  where 250 = qsize()
E    +    where qsize = <Queue ... maxsize=0 ... tasks=250>.qsize
```

`maxsize=0` in the failure's own repr is the defect stated by the object under test.

### Measured on the bench, before the constant was written

| window | frames | peak 1 s | largest frame |
|---|---|---|---|
| idle, 60 s | 1 (the connect snapshot) | 1 | 746 B |
| full `acceptance.py`, 116 s | 25 | **4** | **765 B** (a `status`; every other type ≤ 69 B) |

Types seen across the run: `status` 12, `rx` 8, `auth` 2, `session` 2, `command` 1. The recorder was
disconnected mid-run with `1012 service restart` — acceptance restarts the unit — which is itself
evidence for the design: the client reconnected and carried on, exactly as a dropped one will.

### The drop, engaged in-process

Driving the real handler coroutine against a peer that accepts and then stops reading:

```
published to trigger the drop: 162
frames the slow peer actually received: 3  ['status', 'ptt', 'overflow']
notice: {'missed': 161, 'queue_maxsize': 160}   close: 1013
stats: {'published': 162, 'subscribers': 0, 'queue_maxsize': 160,
        'deepest_queue': 160, 'dropped_subscribers': 1, 'dropped_deliveries': 161}
```

### On hardware

<!-- BENCH -->

### Counts

pytest **2420 passed / 5 skipped** (from 2402 / 5). vitest **14 files / 163 tests** (from 14 / 160).

## Findings — recorded, not fixed

- **`JsonlSink.write` blocks the event loop.** The ledger drain's real hazard, found by the check the
  brief asked for. A hung disk stalls everything on the loop, including PTT. Its own cycle.
- **`PolledGate` still has no staleness expiry.** Unchanged from ADR 0178/0179, restated so it is not
  lost.
- **Nothing throttles a reconnect flap.** A permanently-too-slow client can be dropped, reconnect, be
  dropped again, indefinitely. `dropped_subscribers` makes the flap visible; nothing limits it.
- **`overflow` is not a ledger record.** `EventLog._record_for` returns `None` for it, so a drop
  leaves a journal line and a counter but no durable ledger entry.
- **`rx_demand.requested` read `1` on an idle station with no browser open** during the BEFORE probe.
  Consistent with ADR 0171's bounded-not-instantaneous window, but nobody has confirmed which it was.

## Out of scope

The firmware fork, the kv4p witness checkout, `PolledGate`, and `/events`' auth and snapshot
behaviour — the snapshot is load-bearing for this design and was deliberately left exactly as it was.
No new measurement of the radio, and no keying-path change: nothing under `radio_server/tx/`, and no
added line inside `_key_on`, `_key_off`, `ptt` or `transmit`.
