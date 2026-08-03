# ADR 0177 — The key-up race is real, and what it costs is the whole transmission

**Status:** Accepted · 2026-08-03 · closes finding 1 of [ADR 0176](0176-the-broadcast-fm-cadence-must-not-read-while-transmitting.md) ·
fixes the guard [ADR 0175](0175-signal-strength-on-the-deployed-backend.md) shipped ·
narrows [ADR 0120](0120-uvk5-v3-rx-audio-path.md)'s starvation finding to the keying path

## Context

ADR 0176's audit found a defect in ADR 0175's own fix. Both cadences guard the shared AIOC wire with
a **check-then-act** pause: `poll_once` reads `paused()` and then, holding nothing, issues a dock
exchange that owns `Uvk5Transport._wire`. A poll that passes the check can still be on that wire
when the station keys.

It was recorded as narrow and left alone, on the grounds that ADR 0176's paused arm landed on the
no-cadence arm to the byte — if the race fired often, that arm should not have been clean. That
reasoning was right about the *rate* and wrong about everything else.

## Reading the code moved the window before anything was measured

ADR 0176 described the exposure as *"the gap between the last key-up frame and the assert"*. It is
wider, and the difference decides where a fix can sit. `_key_on` ran:

| # | step | on the wire? |
|---|---|---|
| 1 | `_clear_if_deafened()` | **yes** — `0x0879`/`0x087A`, blocking wire acquire |
| 2 | `_reassert_channel()` | **no** — `uvk5_tune_persist = true` ⇒ `HybridTuner.volatile` is `False` |
| 3 | `_refuse_if_tx_disabled()` / `_await_tx_lockout()` | no |
| 4 | `open_playout_stream()` | **the audio stream opens AND STARTS here** |
| 5 | `_AiocTxPacer(...)` | thread start |
| 6 | `setattr(self._serial, "dtr", True)` | the DTR assert |
| 7 | `self._transmitting = True` | the flag — **set after the line went high** |

Three things follow. The flag was set *after* the assert, so there was additionally a window in
which the transmitter was keyed and every pause check still answered "not transmitting". The audio
stream was live from step 4, two steps before the line. And nothing serialises the two halves at
all: `_wire` guards frames, while the DTR assert is a lockless `TIOCMBIS` ioctl on the same fd —
pyserial's POSIX backend has no lock of its own.

## Measured, because the rate makes sampling the wrong instrument

The natural rate is about **0.5 %** of key-ups: a poll must start in the ~2.5 ms between the key-up's
own frames releasing the wire and the assert, against a cadence that offers a fresh poll roughly
every 0.55 s. Seeing that naturally with 95 % confidence needs ~600 key-ups — hundreds of
transmissions to answer one question, and a clean run would still prove very little.

So `scripts/bench/keyup_race.py` makes the collision **certain**: exactly one register exchange,
timed to be in flight as the line goes high, with the overlap *evidenced* from timestamps at both
ends rather than assumed. Exactly one, deliberately — speeding the cadence up would confound "does
one exchange hurt" with "does more traffic hurt", and ADR 0175/0176 already answered the second.

| arm | 1000 Hz | audio | **witness carrier** | straddled |
|---|---|---|---|---|
| **control** `--forced 0` | 0.989 | 434852-438074 B / **4.42-4.52 s** | **48 / 83 polls** | — |
| **hazard** `--forced 1` | **0.000** | **0 B / 0.00 s** | **0 / 81 polls** | 3/3 |
| **fix** `--forced 1 --collide-via cadence` | 0.989 | 434852-442906 B / **4.42-4.53 s** | **47 / 82 polls** | 0/3 (skipped) |

Three trials each, same script, same port, minutes apart. The control's 434852 B / 4.42 s matches
ADR 0176's baseline arm **to the byte**, which is what says the borrowed-port rig reproduces the
deployed station rather than some other radio.

**The finding is not ADR 0175's damaged audio.** There, a 4.4 s over arrived as 0.70 s. Here the
witness's *hardware carrier detect* — a measurement that does not go through its audio path at all —
never saw RF. **One control exchange in flight across the assert and the station does not
transmit.** `transmit()` returned normally, the pacer reported no error, and nothing reached the
air.

The carrier poll was added precisely because the first run of this arm returned 0 bytes, and a
capture-only rig cannot tell "damaged audio" from "no RF". Those are different findings and only one
of them is true.

**Leading hypothesis, explicitly not proven here:** the same shape as ADR 0120's starvation finding
— a dock command starves the firmware's 10 ms timeslice, and the main loop is what samples the
hardware PTT pin the AIOC's DTR line drives. A level that goes high inside that window may simply
never be noticed. It is a *shape*, not a proof: 0120 measured starvation under the `0x0870`
full-control loop, and this is a single top-level command, so the mechanism is a pointer for a
firmware cycle rather than a conclusion drawn here.

## Decision — reserve the wire, do not lock the keying path

1. **`_keying` goes up first**, before any refusal and before their frames, so **no new poll can
   start for the whole key-up**. This is the part that does the work. A **counter, not a flag**, for
   the reason `BroadcastFmPoller._assumed_on` is one: the station ID, `POST /transmit` and a bridge
   can key from different threads, and a bool lets the first to finish unguard the second.
   Released in a `finally`, because a stranded reservation would silence both cadences for the life
   of the process — a worse fault than the one being fixed, and a silent one.
2. **A bounded barrier then drains what was already in flight**, and on expiry **keys anyway** and
   counts it separately. It is a barrier, not a held lock: it takes the wire and gives it straight
   back, which is only meaningful *because* step 1 has already stopped new frames. Written down
   because "the barrier makes the wire quiet" reads like a guarantee and is not one.
3. **`_transmitting = True` moves above the assert.** A by-product, not the fix — step 1 already
   covers that window — taken because a fix that relies on a second mechanism to hide a wrong
   ordering breaks when the second one moves.

**The barrier sits before the audio stream opens, and that placement now has two reasons.** ADR
0175's is that the isochronous stream is live from that point. This ADR's is stronger: it is the
last moment before the assert at which anything else could still take the wire. Either reason alone
justifies it; someone "simplifying" it up next to the line assert would reopen both.

`KEY_UP_WIRE_DRAIN_S = 0.3` is **derived, not picked**: 3 × the 96.3-97.7 ms a register exchange
measured here (n=7, timestamped both ends), sized to cover both cadences' exchanges back to back,
and a twentieth of the six seconds `_await_tx_lockout` already sits through for the firmware's own
lockout. It deliberately does **not** cover the transport's pathological bound (a stalled write plus
the reply budget, ~3.0 s): waiting that out would let a diagnostic delay a transmission, and on this
station the Part 97 station ID is a transmission.

**Added key-up latency, measured on the deployed hardware:** **0.01 ms** when the wire was free, and
**96.8 ms** when a cadence poll was genuinely in flight — one register exchange, exactly what the
barrier exists to wait out. Worst case is bounded at 0.3 s and pinned by a test that asserts on wall
time.

### `cadence_paused()`, and why `transmitting` was not widened

A second predicate rather than a broader one. `status().transmitting` is a **user-visible field**
answering *is the PTT line asserted* — reporting `True` before the line is up is a flag that lies,
and widening the underlying flag would also silently change three refusal guards that read it
(`set_tune_persist`, `commit_tuning`, `reboot_radio`). `cadence_paused()` is named for the question
rather than the mechanism, so a later cycle can add a term without touching callers. Both are plain
flag reads with **no I/O**, which is the ADR 0176 rule: a pause check that does I/O to decide
whether to do I/O rebuilds the fault one layer up.

### Rejected, and why — including one rejected by measurement

- **A bounded lock held across the assert.** Holds a lock on the keying path, and this arc has taken
  the station ID off the air twice by touching that path.
- **Moving `_transmitting = True` up, and stopping there.** Closes the window *after* the assert and
  leaves the one spanning the stream open — a partial fix that reads as a complete one.
- **Bounding the key-up's own dock frames.** Tried, and **backed out after it failed on the bench
  and in test.** Capping their wire wait turns a *delayed* key-up into a **refused** one:
  `clear_broadcast_fm` reports a wire timeout as no reply, ADR 0161 reads no reply as "this station
  may be deaf", and `_key_on` raises `RadioUnavailable`. A late station ID is a worse-but-legal
  transmission; a refused one is silence where guardrail 5 requires a transmission. It is safe to
  leave unbounded *because of* the reservation, not by luck: no **poller** can be what those frames
  wait behind any more. What remains is a concurrent tune or a stalled write on dying hardware,
  bounded by that caller's own budget.

## The instrument ships, and it ships for durability

Not for completeness. A claim in an ADR is true on the day it is written and nothing re-checks it —
ADR 0157 shipped a verification that was already stale when it merged. A counter re-checks it for
ever.

`GET /status` grows a `wire` block. **Every counter is named for what it counts, not for what it
implies**, and `docs/api.md` says of each what a climbing value does *not* prove:

| counter | what NONZERO means |
|---|---|
| `key_ups` | the denominator. `0` beside it means *nobody has transmitted*; beside `412` the others are a measurement |
| `wire_busy_at_key_up`, `key_ups_with_wire_traffic` | **the race fired** — never "an over was clipped". Only received audio at a witness can say that |
| `keyed_with_wire_busy` | the drain gave up and keyed anyway. **The one that deserves attention** — the only case that can still reach the air damaged |
| `key_ups_that_waited_for_the_wire`, `longest_wire_wait_ms` | the drain doing its job and what it cost. **Not a race rate**: ~45 % of key-ups wait, and a server without the reservation waited in the same place anyway, inside its own first dock frame |

`Uvk5Transport` gains `wire_busy()` (a lock-state read — no acquire, no I/O, safe on the keying
path) and a monotonic `exchanges` counter, incremented while the wire is still held so it needs no
lock of its own.

**Two instruments because one under-reports, and that was caught the hard way.** The first version
sampled only the lock and closed its interval when the lead-in was queued — and a forced collision
timestamped from 2.5 ms *before* the assert to 96 ms after was recorded as `key_ups_with_wire_traffic:
0` on all three trials. The exchange outlived the window meant to catch it. An instrument that
reports zero on a collision somebody caused on purpose would have reported zero on every real one.
The interval now runs to un-key.

## Acceptance

- Red run **10 failed / 0 passed**. pytest **2351 passed / 5 skipped**, from 2330/5. vitest
  **14 files / 155 tests**, untouched.
- Bench, on the deployed station against the kv4p witness: the three arms above, plus **40 key-ups
  on the live service** — `key_ups_with_wire_traffic: 0` and `keyed_with_wire_busy: 0` (nothing got
  past the reservation), `key_ups_that_waited_for_the_wire: 18`, longest wait **41.7 ms**.
- `acceptance.py` full run: **9/10 PASS**, `split-minus` SKIP (no fixture preset), `web` FAIL on the
  known witness `kv4p /healthz` 404 — re-run alone to confirm it is that check and nothing else.
  `tx` **0.989 / 431632 B / 4.52 s**; `services` **515382 B / 5.2 s**, speech band 0.98.
- Station restored to **145.145 / TX 144.545 / 107.2 / FM / low** and verified, links left
  disconnected as found, `uvk5_tune_persist` reported **as found (`true`)**, not flipped.

## Findings, recorded rather than fixed

1. **ADR 0176's pause hook is inert on the `uvk5` backend.** `app.py` resolves it with
   `getattr(radio, "transmitting", False)` and `Uvk5Radio` has no such attribute — only a local
   inside `status()`. It answers `False` for ever. Harmless *today* only because that backend has no
   `probe_broadcast_fm`, so the cadence never reaches a wire. One method away from being live. This
   cycle makes `create_app` **warn** when a radio that can be probed exposes no pause predicate,
   which is the silent-degradation class rather than this instance.
2. **The `uvk5` backend has the same physical hazard and no reservation anywhere.** Its `status()`
   does a register read per call. Not the deployed mode; not touched. Carried from ADR 0176.
3. **A key-up can still be delayed by a concurrent tune**, bounded by that caller's own request
   budget rather than by anything here — see the rejected option above for why bounding it made
   things worse.
4. **`key_ups_that_waited_for_the_wire` ran at 18/40 on the live service**, well above the ~7 % a
   naive duty-cycle estimate predicts. The cadence's wake times are correlated with the key-up cycle
   (it free-runs at 0.5 s while paused rounds return instantly), which plausibly explains it and was
   not chased further. It does not affect the race rate, which depends on the ~2.5 ms window.

## Out of scope

What the cadences read and how often. The firmware fork, the witness checkout, EventHub's cap, the
ADR 0172 mock audit items. No UI.
