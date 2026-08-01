# ADR 0170 — A stranded slot stops being invisible

**Status:** Accepted · 2026-08-01 · finishes the observability surface
[ADR 0167](0167-a-claim-and-its-release-are-one-scope.md) carried; publishes the `"busy"` event
reserved since [ADR 0011](0011-api-layer.md)

## Context

ADR 0167 closed the strand and **carried its own finding**: nothing anywhere reported slot state.
`TxSlot` was one bare bool — no holder, no acquire timestamp, no counter, no `on_change`. `/status`
had no occupancy field for any of the three talk slots, and the two fields that *look* like they
answer the question are decoys: `transmitting` is PTT state and `busy` is squelch state, and **both
read `false` while a slot is stranded**, so the card positively suggested an idle station while every
Talk press was refused.

**This cycle finishes something that was designed and abandoned, not something new.** `"busy"` has
sat in `EVENT_TYPES` ([`api/events.py`](../../radio_server/api/events.py)) as a
**reserved-but-never-published** name since ADR 0011 built the event surface — 159 ADRs ago. The
reservation is the evidence that somebody meant to build this surface and stopped. Recording that
matters, because a successor asked to "add a busy event" is completing a design, not inventing one.

And the UI stated the opposite of the truth: `useTxAudio.js` hard-coded *"Radio busy — another
operator is transmitting."* That sentence is **false in exactly the case it is most needed** — during
a leak nobody is transmitting and a dead socket owns the flag.

## The measurement that shaped the design

The brief asked what the `/audio/rx` park actually costs **before** building a surface that reports
it. Measured against real uvicorn on the production `websockets` sans-io path, with the deployed
`squelch = "audio"` gate (an `AudioLevelGate`, closed on a quiet channel) and continuous non-empty
silence out of `receive()` — so the transport's empty-frame skip is not what is doing the work:

| drop mode | quiet channel | after a signal opens the gate |
|---|---|---|
| clean close (closed tab) | **still counted at +3 s** | released |
| RST (yanked wifi, killed client) | **still counted at +50 s**, past uvicorn's 20 s keepalive | **still counted** |

The mechanism: the handler parks on `await queue.get()` with **no timeout** and only ever *sends*, so
it learns its client is gone from the next `send_bytes`. On a squelched quiet channel nothing is
published, so nothing wakes it; and on a reset transport uvicorn drops sends silently rather than
raising, so even a signal does not tell it.

Two variables in the first rig were not production and either could have manufactured this answer —
`ws_ping_interval=None` disabled the keepalive, and only RST was tried. Both were corrected and the
measurement re-run. **The keepalive does not save it.**

**Now the same question of a talk slot**, which is where the design turns:

| | talk slot (`/audio/tx`) | RX listener (`/audio/rx`) |
|---|---|---|
| clean close | freed in **43 ms** | held until the next published frame |
| RST | freed in **1.85 s** | held **indefinitely** |

The RST case is the tell. The app never sees that disconnect either — what frees the talk slot is
`wait_for(..., tx.idle_timeout)` on its receive, at 2.0 s. **One loop has a bounded await and the
other does not, and that is the entire difference.** It is why one number in `/status` is liveness
and the other is only intent, and it is a measured constraint on this cycle rather than caution.

## Decision

### 1. `TxSlot` records who, since when, and who was refused — and gates nothing

`try_acquire(holder=None)`. The label is **recorded, never consulted**: passing it cannot change who
wins, and `try_acquire()` with no label still works. This cycle observes.

- **The age is monotonic; the wall-clock `since` is derived from it at render time.** Storing a
  wall-clock acquire time would make "held for 4 hours" an artefact of an NTP step or a suspend, and
  that number is the entire diagnosis. Deriving it the other way round gives a clock time to display
  and an age that cannot be moved.
- **Refusals are counted per claimant, not as one integer.** The RF slot has three claimants and one
  of them — the Mumble relay — refuses at *frame rate*, so a single total would let a relay storm
  bury the one browser refusal an operator is trying to explain. That is ADR 0153's rule about
  `dropped_key_refused` vs `relay_errors` applied one layer down.
- **The ledger survives release.** Clearing it when the slot goes free would erase the evidence of
  the contention precisely when the question becomes answerable.

The three claimants are labelled: `browser`, `mumble-relay`, `dstar-relay`. ADR 0167 named the last
two audited-clean; they are the reason a holder is worth having at all.

### 2. `/status` grows **two** blocks, and the names carry the difference

```jsonc
"slots": {                                   // self-reaping; this is liveness
  "tx":     {"held": true, "holder": "browser", "since": 1785594794.35, "held_s": 0.22,
             "stale_after_s": 180.0, "refused": {"mumble-relay": 41}},
  "mumble": {...},  "dstar": {...}           // null when that subsystem is unconfigured
},
"rx_demand": {"requested": 1, "reader_running": true}   // intent, and the name says so
```

**`requested`, never `listeners` or `active`, and `rx_demand` is not a fourth entry in `slots`.** A
note in `api.md` does not travel with the value to the card that renders it; the label is the only
thing that does. This is the Bandwidth-vs-Demodulation lesson from the 0150→0154 arc: two things that
read alike in one card get confused, and the fix was the name. Folding the RX number in beside the
talk slots would lend it their trustworthiness, which the measurement above says it has not earned.

`stale_after_s` is the transmitter time-out — the station's own answer to how long one over may last
— handed out so a client can say "longer than a transmission" without hard-coding it. **The server
publishes no `stale` verdict of its own:** it cannot tell a long legitimate over from a stuck slot,
only the timeouts can, and they already reap. Asserting one would be the tri-state trap ADR 0163
named.

### 3. `busy` is published — from the websocket refusal only

`Event(type="busy", data={slot, holder, held_s})`, beside ADR 0167's log line. **Not** from the relay
paths: those refuse per frame and would flood `/events`; they are counted instead. A refusal now
leaves three traces of one fact — `/status` for state, `busy` for the edge, the journal line for
after the fact — which is how this repo already treats `rx`/`ptt`/`alarm`.

### 4. The UI stops lying, in two places

- **The sentence.** The refusal message carries `holder`, `held_s` and `stale_after_s` (the browser
  cannot read a close code, so this message is its only channel — the ADR 0161 mechanism), and
  `classifyTxMessage` renders *"Radio busy — browser has held the transmit slot for 12s."* Past
  `stale_after_s` the **phrasing changes**, not just the number: *"…held by mumble-relay for 4h 20m,
  longer than any single transmission should last. Nobody may actually be transmitting."* "Held for
  4h 20m" only reads as wrong to somebody who already knows what normal looks like, and the operator
  staring at a stuck Talk button does not. A server that sends no holder gets the **old sentence
  back** rather than an invented one — "held by unknown" would replace one untrue sentence with
  another.
- **The row.** A standing `Talk slots` row in the Status card, plus a separately-worded `RX demand`
  row. This is the cycle's title: a refusal message reaches only the operator who presses Talk, so
  without the row a leak stays invisible until somebody trips over it. Seeded and polled rather than
  pushed — WS `status` frames are RadioStatus-only by design, and a stranded slot is a standing
  condition with no edge to listen for.

## Acceptance

- **Red run**, against master's implementation: **11 failed, 1 passed**, all behavioural, no
  collection error. The 1 pass is the reaping pin, reported as a pin and not counted in the red.
- `uv run pytest` **2265 passed / 5 skipped** (baseline 2253/5). `npx vitest run` **14 files / 155
  tests** (baseline 14/146).
- **Real-uvicorn smoke** (not `TestClient` — the ADR 0165 lesson): hold, `GET /status`, refuse a
  second talker, RST the socket, read again. PASS, including the refusal line surviving
  `basicConfig`: `talk slot tx refused: held by browser for 0.3s`.
- **Bench, on the deployed station** (`962f847`, UV-K5 V3 over the AIOC dock):

  | | |
  |---|---|
  | at rest | `held: false`, everything `null` |
  | while held | `holder: "browser"`, `since` matching the clock, `held_s` advancing |
  | refusal message | `{status: busy, slot: tx, holder: browser, held_s: 0.23, stale_after_s: 180.0}` |
  | closed tab → freed | **0.93 s** |
  | yanked connection → freed | **2.0 s** (the idle timeout, as measured locally) |
  | ledger across two drops | `{"browser": 1}` → `{"browser": 2}`, surviving both releases |
  | next talker | `ready` — the surface said free and the slot *was* free |

  The journal carried the refusal lines with their new text. `acceptance.py`: **9 of 10 stages PASS,
  `split-minus` SKIP, `web` FAIL, exit 1** — see below.

## Consequences

- A stranded slot is now diagnosable from three directions without reproducing it.
- `/status` gains two blocks and one of them is deliberately less trustworthy than its neighbour.
  That asymmetry is documented in `api.md`, in the field name, and in the UI wording — three places,
  because the one that reaches the operator is the wording.
- `try_acquire` grew an optional parameter. Every existing call still compiles and behaves
  identically; a pin asserts that.
- The refusal message grew fields. Four in-repo tests asserted its exact shape and were updated to
  assert the richer contract rather than loosened.

## Findings

1. **`acceptance.py` is exit 1, not the clean 3, and the cause is not this cycle.** The only failing
   check in `web` is `kv4p GET /healthz → 404`. The witness (8091) is on **`a6a4cd4`** — the #222
   merge, three ADRs stale — and `/healthz` arrived with ADR 0166, so its build does not have the
   route at all (`grep -c healthz` on its own HEAD: **0**). ADR 0169 flagged this checkout and
   deliberately left it: it carries **uncommitted local edits** to `radio_server/audio/dtmf.py`,
   `radio_server/link/entries.py` and `update-radio-server.sh`. Updating it would discard somebody's
   work, which is a destructive act this cycle will not take unasked. **Every station-side check in
   `web` passes**, including its own `/healthz` and `radio serial reader: alive`.
2. **A `tx`/`split` FAIL that did not reproduce, reported rather than buried.** The first full
   acceptance run failed both with zero carrier at the witness; the second full run passed both
   (`kv4p saw carrier 10`, RMS 12309, 1000 Hz at 0.965, CTCSS 0.0217, `infx` leg ratio). The `tx`
   stage in isolation also passes. No cause is claimed and none is assumed — the run before it was a
   restart storm, which is the same neighbourhood as ADR 0169's one-off `systemd` FAIL.
3. **CARRIED: the `/audio/rx` park, with the successor's difficulty named.** Measured above. Cost is
   bounded and real: one queue, `rx_demand` pinned ≥ 1, and therefore the single capture reader held
   open for the life of the process. **No talk slot is involved** — it is a subscription, not a
   claim. The obvious fix is to bound that await the way the TX loops bound theirs, but **an idle
   wakeup or a keepalive frame is a change to what travels the audio path, and that path feeds a
   Part 97 control** — ADR 0162's broadcast-FM relay mute and the DTMF decode both sit on it. That
   needs its own fail-first cycle, not a corner of an observing one.
4. **The same unbounded-`queue.get()` shape is in `/audio/mumble/rx` and `/audio/dstar/rx`.** Same
   class, smaller blast radius: they hold a subscription but take no reader demand, so nothing is
   pinned. Named here so the successor fixes three loops rather than one.
5. **`rx_demand` read `2 requested` on an idle station**, of which the controller accounts for one
   and a connected browser the other. It is *not* evidence of a leak — and the fact that this ADR
   cannot tell you whether it was one, from the number alone, is precisely the property the naming
   exists to communicate.
6. **The station is deployed on this branch and stays there.** ADR 0169 removed the put-it-back
   ritual: once this PR merges the bare update command starts working again on its own. **The
   station's `update-radio-server.sh` could not perform this deploy** — it is still the pre-0169
   script, because 0169's own fix has not reached the box; the documented long form was used instead.
   Left on **147.555**, tune persist **off**, broadcast FM **off**, `tx_ok: true`, service active.

## Out of scope

The fork; the squelch-composition successor; reaping the `/audio/rx` park (finding 3); any change to
acquire/release **semantics** — this cycle observes, it does not gate.
