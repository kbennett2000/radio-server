# ADR 0179 — Three instruments nobody can read

**Status:** Accepted
**Date:** 2026-08-04
**Supersedes / relates to:** [ADR 0178](0178-checks-that-silently-answer-the-safe-looking-default.md),
[ADR 0177](0177-the-key-up-race-is-real-and-it-costs-the-whole-transmission.md),
[ADR 0176](0176-the-broadcast-fm-cadence-must-not-read-while-transmitting.md),
[ADR 0175](0175-signal-strength-on-the-deployed-backend.md),
[ADR 0170](0170-a-stranded-slot-stops-being-invisible.md),
[ADR 0163](0163-a-cadence-for-the-probe.md)

## Context

ADR 0178 ended by recording a gap it deliberately refused to close. It had just added a counter
(`pause_errors`) and, in the same breath, noticed that the counters already in the tree reached
nobody:

> Three instruments measure things nobody can see. […] That is a readability problem that wants one
> small cycle of its own — **not another counter**, which is why `pause_errors` ships with a log line
> beside it and `PolledGate` gets no counter at all.

This is that cycle. **The point is a reader, not more measurement.** No counter is added anywhere in
this diff. What every existing number gets is a place an operator can read it, a documented meaning
for a **nonzero** value, and a decision about whether anything renders it.

## The map, corrected

The finding was carried as *three instruments*. Checked against source rather than recalled, it is
one and a half, and both corrections shrink the cycle.

| instrument | surface before | documented before | rendered before |
|---|---|---|---|
| `RssiPoller.stats()` | **none — zero production callers** | no | no |
| `BroadcastFmPoller.stats()` | 2 of 6 keys | those 2, well | no |
| `WireStats` | **`/status.wire`, all 6 fields** | **`api.md`, per field** | no |
| `PolledGate` | **has no `stats()` at all** | — | — |

**1. ADR 0177's counters are not unread. They are unrendered.** `AiocBaofeng.status()` has set
`wire=self.wire_stats()` since ADR 0177; `RadioStatus.wire` is in the `asdict` the `/status` route
returns; and `api.md` already documents each field's nonzero meaning — including precisely the
distinction this cycle was briefed to draw. `wire_busy_at_key_up` / `key_ups_with_wire_traffic` are
documented as *"the race fired […] not the same as an over being damaged: only received audio at a
witness can say that"*, and `keyed_with_wire_busy` — the drain barrier **expiring** and the station
keying anyway — as *"the one that deserves attention: the only case that can still reach the air
damaged."* That prose is right, so this ADR does not touch it. The gap for `wire` is a renderer.

**2. `PolledGate` has no stats to surface.** ADR 0178 gave it log lines only, on purpose, and said
why: *"counters that reach no endpoint repeat the sin this cycle names against itself."* There is no
`stats()` on the class. It is not a third instrument; it is the thing this cycle must not touch.

So **nine** numbers were genuinely unreachable: six from `RssiPoller.stats()`, which had no caller
anywhere in the server, and three of the six the bridges compute from `cadence_stats` and drop at
the last step before HTTP — `skipped` among them, which is ADR 0176's entire deliverable.

## The design question: scatter, using the nested-block idiom

Each number goes beside the state it explains, inside its own block. **No diagnostics block.**

**1. The precedent is unanimous where it exists.** `pa`, `transport`, `wire` and `broadcast_fm` are
all instrument-shaped blocks sitting beside the state they explain. There is no diagnostics block in
this API. Inventing one now forces `wire` — already scattered, already documented — either to move,
which is an API break for the sake of layout, or to stay put and become the exception that proves
the rule.

**2. ADR 0170 already decided this against grouping, for a stronger reason than tidiness.**
`rx_demand` was kept *out* of `slots`, and the comment that did it is still in `app.py`: *"the talk
slots are self-reaping and this is not, and one block holding both would lend this number the
trustworthiness of the ones beside it."* A diagnostics block does the same thing in reverse. It
would sit `wire.key_ups: 412`, a zero that is a measurement, next to `pause_errors: 0`, a zero that
is structural on today's hook (see findings), and a reader carries one's reliability onto the other.

**3. The reader arrives from the state, not from the diagnostics.** The operator's question is *why
is `rssi` null*, and the answer has to be next to `rssi`. A diagnostics block is a place you only
look once you already suspect the instrument — which is the state of knowledge this block exists to
create, not one it can assume.

The counter-argument is real: instrument-shaped numbers do not belong loose in an operational block.
It is answered by **nesting** rather than by grouping. Every number here lands inside its own block,
so no flat field of `RadioStatus` grows an instrument.

## Naming

One vocabulary across both cadences. They are the same class of thing, and a second vocabulary is
the readability cost this cycle exists to remove. `deafened_unknown` / `deafened_age_s` fixed that
vocabulary in public API at ADR 0163, so the new block reuses it and the new bridge keys extend the
`deafened_` family.

Names say what they **count**, never what they imply — the rule ADR 0170 set when it renamed
`listeners` to `requested` because the number counted requests and a dropped listener stayed counted.
Applied here:

- `pause_errors` counts *ticks whose pause hook raised*. It is not `unguarded_key_ups`. Nonzero says
  the guard is broken; it says nothing about whether a transmission was damaged, because only RF at
  a witness says that.
- `skipped` counts *rounds deliberately not taken*. It is not `failures` — it is the ADR 0175 guard
  working, and `api.md` says so in the same bullet.

## Decision

**1. `RadioStatus.rssi_cadence`,** a frozen `RssiCadence` block, `null` where nothing polls this
radio (every backend but a `baofeng` station with a register-reading tuner — `uvk5` reads its own
RSSI inline and has no cadence). `polls`, `unknown`, `skipped`, `pause_errors`, `age_s`,
`stale_after_s`. `AiocBaofeng.rssi_cadence()` sits beside `wire_stats()` and `transport_health()`
with the same no-I/O guarantee.

**It exists because `rssi: null` is three different answers** — never measured, measured and expired,
or the station is keyed and a receiver cannot measure a channel through its own carrier — and until
now they rendered identically.

Two deliberate choices inside it:

- **No `reading` field.** `RssiPoller.stats()` has one, and it is the *raw* last value, before the
  expiry and the keyed suppression `RadioStatus.rssi` applies. Two nearly-identical numbers in one
  response is the trap ADR 0170 named: two numbers that read alike in one card get confused. There
  is one signal-strength number on this API and it is `rssi`. `age_s` answers what `reading` would
  have, without the collision.
- **`stale_after_s` is the one field that is not an existing counter, and it is not a measurement.**
  It is `STALE_AFTER * interval`, the threshold without which `age_s` cannot be read at all. It is
  taken off the poller rather than recomputed, through a property that `reading()` now also compares
  against, so the published threshold and the enforced one cannot disagree. Called out here so it is
  not mistaken for the scope creep this cycle was told to avoid; the shape is `slots.stale_after_s`,
  which ADR 0170 shipped for the same reason.

**2. `deafened_polls` / `deafened_skipped` / `deafened_pause_errors`** on both bridges' `tx_stats()`,
straight from the `cadence_stats` dict they already build. `deafened_polls` is load-bearing and is
the `key_ups` argument one level out: `deafened_unknown: 0` beside `deafened_polls: 0` means *no
probe has ever run*, and beside `deafened_polls: 900` it means *every probe answered*. Without the
denominator a cadence that never started reads exactly like a healthy one.

**3. `docs/api.md` gets a documented nonzero meaning for every counter**, each saying what it does
*not* mean as well as what it does. The `wire` section is deliberately unedited.

**4. What renders: the two facts that mean something is wrong, and nothing else.** An exported pure
`describeInstruments(state)` in `StatusPanel.jsx` returns **zero rows on a healthy station**, and a
warn row for `wire.keyed_with_wire_busy > 0` (the only wire counter that can still mean damaged RF)
and for `rssi_cadence.pause_errors > 0` (the ADR 0177 hazard re-armed). `> 0`, never truthiness, so
`null` and `0` stay distinct.

The other ten numbers are **not** rendered, and that is a decision rather than an omission. The rule
is `TransportBanner`'s, stated in its own header: *"Absence of a fault is not a status worth a
banner."* `StatusPanel` is compact label/value rows for an everyday operator and already hides what a
backend cannot do rather than showing a column of "—". Eight instrument rows there would cost the
four operational facts their prominence, and a row that is almost always a reassuring zero is a row
people stop reading — which is the one thing that must not happen to the two above. **What an
operator does instead:** reads `GET /status` or `GET /link/status`, where every field is now
documented, or runs `scripts/bench/acceptance.py`.

## The instrument

`tests/test_counters_have_a_documented_meaning.py`, because this cycle's own deliverable is the kind
that rots quietly — `api.md` is written by hand at the end of a cycle and nothing fails when it
isn't, which is `test_docs_contract`'s founding observation one level down. A field added to
`RssiCadence` or `WireStats` without a paragraph fails here.

**Two halves, and they are not equally strong.** The dataclass half reads `dataclasses.fields()` and
is rename-proof by construction. The bridge half is a source scan, and a source scan is blinded by a
rename — ADR 0167 recorded exactly that against `test_relay_subscribers`. It is a guard, not a proof,
and it still catches the failure that actually happened: a key added to `tx_stats()` that nobody
documented. **The residual neither half closes:** a name appearing in a document is not a true
sentence about it. This proves a paragraph exists; it cannot prove the paragraph is right.

## Evidence

**Suite.** pytest **2402 passed / 5 skipped**, from 2384/5. vitest **14 files / 160 tests**, from
14/155. `test_docs_contract` unchanged in count; no new route.

An existing exact-shape assertion in `test_link_api.py` went red on the three new keys, which is the
check working. Two of them are moved by the cadence thread between the POST and the response, so
they are popped and range-asserted like `deafened_unknown` already was; `deafened_pause_errors` is
pinned at `0` — and pinned at `0` rather than `null` on purpose, because that deployment does wire a
hook, and "there is no guard here" is a different station from "the guard is fine".

**Bench** — `kb@192.168.1.62`, station on 8090 over HTTPS, every probe `https://`.

Before: **`54f9e56`**, 0 ahead / 3 behind, clean. After: **`4a58875`**, `0 0` against the branch,
bundle `index-CUx4HnZr.js` → **`index-DSwQ1qxo.js`**. The witness is **0 ahead / 42 behind and
dirty**, so it was **not moved**; that is also why the known `/healthz` 404 persists.

Reported as found, not flipped: `uvk5_tune_persist = true` in `radio.toml` and `tune_persist: true`
in `/status`. Station config confirmed rather than inherited from this checkout — `backend =
"baofeng"`, `[audio] squelch = "audio"`, `[baofeng] squelch_mode = "audio"` with `uvk5_tuner =
"hybrid"`; the `squelch_mode = "cat"` that would build a `PolledGate` is in the `[uvk5]` block, which
is ADR 0178's correction holding.

**The block was absent on master and present on the branch**, read through the API on the real
station:

```
master   54f9e56 :  'rssi_cadence' in /status  ->  False
branch   4a58875 :  {"polls": 20, "unknown": 1, "skipped": 0, "pause_errors": 0,
                     "age_s": 0.576, "stale_after_s": 1.5}
```

**The deliberate nonzero, as a before/after around one 1.2 s transmission** on 144.545, links down:

| | `wire.key_ups` | `rssi_cadence.polls` | `unknown` | `skipped` | `pause_errors` |
|---|---|---|---|---|---|
| before | 0 | 89 | 9 | 0 | 0 |
| after | **1** | 94 | **9** | **3** | 0 |

Three polls suppressed by the guard across the over — right for a 0.5 s cadence — and `unknown`
**did not move**, which is the distinction `api.md` asserts, holding on hardware: a deliberate skip
is not a failed poll. After a full acceptance run the same block read `polls: 143, unknown: 40,
skipped: 118`, with `key_ups: 7` and every race counter still `0`.

**The case the block exists to explain, caught live** rather than argued. After a 2.5 s over, polling
`/status` at 80 ms:

```
transmitting=False   rssi=None   age_s=3.85  >  stale_after_s=1.5   skipped=123
```

An un-keyed station reporting no signal strength **because the reading expired**. On master that was
a bare `null` with nothing beside it to say which of the three reasons applied.

*(A first attempt at this printed the conclusion from a fixed string while the sample it had actually
read showed `age_s: 0.75` — not expired. It is recorded because it is the failure mode this whole
arc is about: a claim that renders identically whether or not the thing happened.)*

**The three bridge keys, read through the API** with a link up to the Murmur on the box itself (a LAN
entry, not the public demo server), then taken down and asserted down:

```
deafened            False        deafened_polls          9    <- new
deafened_age_s      1.674        deafened_skipped        0    <- new
deafened_unknown    1            deafened_pause_errors   0    <- new
```

`deafened_unknown: 1` beside `deafened_polls: 9` is the readability argument working: before this
cycle the numerator shipped and the denominator did not.

**`acceptance.py` 9 of 10.** `split-minus` SKIP for a missing fixture preset. `web` FAIL re-run alone
and confirmed to be **only** the known `kv4p GET /healthz` 404 — every other check in that stage
passed, including the station's own `/healthz` and its serial reader reporting alive. `services`
passed, so the announcement path guardrail 5 depends on is intact: 521824 B over a 5.3 s span, heard
at the witness at RMS 2967 with 0.98 speech-band energy.

**Left as found.** Station restored to **145.145 / TX 144.545 / 107.2 / FM / low**, frequency before
split, verified by read-back **after a service restart** (`tx_ok: true`, transport alive, rssi 155).
Links and D-STAR down, asserted from the endpoints rather than from a POST's return code.
`frequency: null` immediately after the restart is ADR 0155's *"a reconnecting host asserts; it does
not assume"*, not a regression. The journal logged **74 `uvk5: tuned rx`** lines this session; under
`hybrid` with persist on each of those is an EEPROM write, and it is stated that way because the
journal has no eeprom-specific line to count.

## Findings — recorded, not fixed

1. **`PolledGate` still has no staleness expiry.** Unchanged and restated so nobody reads this cycle
   as having closed it. ADR 0178's log lines cover the *event*; the *standing state* is uncovered. A
   gate whose poller thread died holds its verdict indefinitely and nothing ages it out, so an hour
   later it reads exactly like a quiet band and no log line reaches whoever is looking then. The
   successor's shape is `RssiPoller.reading()`'s `STALE_AFTER` return-`None` — which this cycle has
   now also *published*, as `stale_after_s`, making the contrast between the two visible in one
   response. Its own cycle: it is a behaviour change on the RX path, and the gate is not live on this
   station's config.
2. **`pause_errors` is structurally `0` on both shipped cadences.** `AiocBaofeng.cadence_paused` is
   `self._transmitting or self._keying > 0` — two attribute reads that cannot raise. So this ships as
   an instrument for a hook that does not exist yet, and its documented meaning is the part that has
   to be right today. `0` here is not evidence about anything else, and `api.md` says so. Not a
   defect; recorded so a future reader does not infer a measurement from it.
3. **`RssiPoller.stats()["reading"]` stays internal**, deliberately, per the argument above. Named
   here so a later cycle does not re-derive it and surface a second signal-strength number.
4. **`RxPump` and `PolledGate`'s duty and drop counts are still unmeasured.** ADR 0125 measured pump
   duty on the bench with a script; nothing in the running server counts a frame the gate dropped. It
   is the number that would have made the ADR 0125 fault visible without a bench. **Unmeasured, so
   carried named rather than added** — this cycle adds no measurement.
5. **The bridges' own `tx_stats()` counters have no renderer either.** `dropped_rx_active`,
   `relay_errors`, `rx_deafened` and the rest reach `/link/status` and are documented, and the web UI
   shows none of them. That is the same shape as `wire` was, one subsystem over, and this cycle
   deliberately did not widen to it.
6. **Nothing type-checks this repo**, so `RssiCadence`'s field types are documentation until a test
   pins them — which is why the block is asserted through a real HTTP round trip and not only
   constructed in a unit test. ADR 0178's rule, still true.

## Out of scope

The firmware fork, the witness checkout, EventHub's cap. No new measurement of any kind. No keying-
path change: nothing under `radio_server/tx/`, no `_key_on`, no `_reassert_channel`, no `ptt()` or
`transmit()` body — checkable from the diff. `PolledGate`'s staleness expiry is finding 1 and not
this cycle.
