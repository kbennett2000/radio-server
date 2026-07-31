# ADR 0163 — A cadence for the probe, and what the probe actually answers

**Status:** Accepted · 2026-07-31 · supersedes nothing; makes [ADR 0162](0162-broadcast-fm-must-not-reach-the-bridges.md)'s relay mute able to fire

## Context

ADR 0162 put a broadcast-FM mute in each bridge's relay loop and said in its own first paragraph that
it **could not fire**: `Dock_FmOff()` clears `gFmRadioMode` as its first statement, so the block a
key-up measures can never report `on=True` on F8/F9. It named the cadence on the non-mutating
`0x0879` probe as the successor that would make the gate real.

The brief that opened this cycle asked one question before any of that, and was right to:
**does the probe work in the state it exists to detect?** `Dock_SetFm` refuses with `ERR_TX` while
`gCurrentFunction` is `FUNCTION_TRANSMIT` or `FUNCTION_MONITOR`. If the radio parked in `MONITOR`
for the duration of broadcast FM, a poll built on this frame could never observe it, the mute would
be permanently unfirable, and the only remaining path would be a firmware read-only action.

**Nothing was built until that was measured.** Phase A ran with the service stopped and no code
changes on the branch.

### The hypothesis is refuted, on the wire

`gCurrentFunction` during broadcast FM is **`FUNCTION_FOREGROUND` (0)**. `App/app/fm.c` never calls
`FUNCTION_Select` — the only writer of that variable (`App/functions.c:263`) — and the caller sets
it: `ACTION_FM` (`App/app/action.c:400-426`) runs `RADIO_SetupRegisters(true)` at `:420`, which
selects `FUNCTION_FOREGROUND` (`App/radio.c:926-927`) one line before `FM_Start()`. The `ERR_TX`
guard (`App/app/uart.c:1184-1187`) tests exactly two values and FOREGROUND is neither.
`FUNCTION_MONITOR` is additionally *unreachable* while `gFmRadioMode` is true: the monitor action is
refused in FM mode (`action.c:361-364`), the keypad is filtered to PTT/EXIT (`main.c:1031-1035`), and
`ACTION_FM` clears `gMonitor` on the way in (`action.c:417`).

**M1 measured it: 35 probes over 35 s with the second receiver running, `ERR_BAND` every time, zero
`ERR_TX`.** M4 then caught a front-panel `F+0` live — 47 × `ERR_OFF`, then 93 × `ERR_BAND`, exactly
one transition, no flapping.

### The finding that changed the cycle: `ERR_BAND` is a coarser claim than it looks

`gFmRadioMode` has exactly **four** writers — `uart.c:1105`/`:1135` (dock on/off) and
`fm.c:648`/`:104` (`FM_Start`/`FM_TurnOff`) — and **none of them is `APP_StartListening`**, which
tears the BK1080 down (`app.c:712-715`) and passes real channel audio without touching the flag. The
firmware's own restore logic proves the flag survives an over: `FUNCTION_Foreground()` arms the FM
restore **conditioned on `gFmRadioMode`** (`App/functions.c:106-109`), 5 s later (`App/misc.c:26`).

**M3 measured exactly that.** With broadcast FM playing and the kv4p witness keying a 1000 Hz tone on
the station's own channel:

| leg | AIOC audio | 1000 Hz power | probe |
|---|---|---|---|
| before the over | RMS 5083.1 | 0.004 | `ERR_BAND` |
| **during the over** | RMS 25608.2 | **0.995** | **`ERR_BAND`** |
| 0-2 s after | RMS 3910.8 | 0.010 | `ERR_BAND` |
| 6 s after (FM restored) | RMS 5592.9 | 0.009 | `ERR_BAND` |

The station was hearing the witness, not the broadcast — and the probe said "FM" throughout.

**So `ERR_BAND` means *broadcast FM is selected*, not *this station is deaf right now*.** A mute
driven by it therefore also withholds real overs, for as long as an operator listens to broadcast FM.
That is not a defect to be worked around later; it is the gate's actual semantics and this ADR is
where it gets written down.

### A correction to ADR 0161 decision 6

ADR 0161 wrote that `ERR_TX` fires when the radio is "transmitting **or monitoring**", that "an open
squelch is *most of an active QSO*", and that "`MONITOR` makes that window far wider than during an
over". **That is wrong.** An ordinary open squelch is `FUNCTION_INCOMING` / `FUNCTION_RECEIVE`
(`App/functions.h:27-29`); `FUNCTION_MONITOR` is the *forced*-open monitor key alone. Neither
INCOMING nor RECEIVE trips the guard. The `ERR_TX` window is narrow, not wide — the refusal ADR 0161
actually observed was the station's own key-up, via `dock.c:309-319`'s `ctx->tx_on` — and that error
was one of its three stated grounds for refusing a poll.

## Decision

### 1. Serialise the wire. This is a prerequisite, not an extra.

`Uvk5Transport.request()` had **no wire lock**. `_cond` guards the waiter list; `send()` writes
outside it; `_dispatch` hands a reply to the first not-done waiter whose `match` is true. That was
survivable only while every `match` was discriminated — ADR 0125's thread-safety argument for
`PolledGate` rests entirely on `CatBusyGate` matching `m.register == reg`.

Broadcast FM has no discriminator: `probe_broadcast_fm` and `clear_broadcast_fm` both match
`isinstance(m, BroadcastFmReply)` and nothing else. **Once anything polls, a key-up's clear can
consume the poll's refusal, read it as "the radio refused to clear", and refuse the key-up.** That is
ADR 0161's defect, rebuilt by the very thing this cycle ships. The firmware wants serialising anyway:
`Dock_EnterFullControl` blocks, so a frame arriving while it is busy is dropped silently, and writes
are fire-and-forget (ADR 0131).

One frame in flight at a time. `request()` gains `wire_timeout`: `None` — every caller written before
this cycle, and every key-up — **blocks**, so the key-up path always wins the wire; the poller passes
`0` and skips the round rather than queueing behind a tune for a reading nothing is waiting on. The
poller also passes a **1.0 s** request timeout, shorter than `SetVfoTuner`'s 3.0 s, because a poll
that already holds the wire is time a key-up spends waiting — that number *is* the bound.

### 2. `BroadcastFmPoller`, shaped like `PolledGate` and different in one respect

`radio_server/activity/broadcast_fm_poll.py`: daemon thread, idempotent restartable `start`/`stop`,
`stop.wait(interval)`, every exception swallowed, never joins from its own thread — ADR 0125's
template, and ADR 0125's rule that a serial read never happens on the thread draining the sound card.

The difference worth naming: **`PolledGate` caches a verdict recomputed from scratch every tick; this
holds a reading.** A tick that learns nothing changes nothing.

It **is** the `Callable[[], BroadcastFm | None]` the bridges already take, so ADR 0162's seam is
untouched and every test injecting a bare lambda is unaffected. It **never writes
`tuner.broadcast_fm`** — `clear_broadcast_fm` stays the single writer, which is what keeps any
polling result structurally unable to reach `refuse_if_deafened`. `on` is what the poll measured;
`hz`, `blocks_tx` and `rescues` are **carried** from the key-up snapshot, because `dock.c` blanks
every field but the status byte on a refusal and the probe is a refusal by construction.

**Interval 2.0 s**, marked *verify against hardware* (guardrail 1). Ten times `PolledGate`'s 0.2 s
because this watches a state that changes only when a human presses `F+0`, and it is not in the audio
path. **That interval is the leak window**: up to ~2 s of broadcast programming reaches the far end
of a link before the mute arms. Measured on the bench in the other direction too — the mute *released*
3 s after the rescue switched the receiver off.

### 3. The failure rule, argued on its own terms

The brief was explicit that this must not be inherited from `tx_ok`, and it must not be:

- **Failing open** relays broadcast programming onto somebody else's repeater. The `tx_ok` rule does
  not transfer, because there the cost of being wrong lands on our own station and the operator can
  recover it; here it lands on third parties and on **97.113(b)**.
- **Failing closed** is worse in practice, because unknowns are **routine**: the dock refuses
  `0x0879` whenever the host is keying. A link that mutes on every over is a dead link, which is
  ADR 0161's defect with the sign flipped.
- **So: a non-answer is not a state transition.** `ERR_OFF` and `ERR_BAND` are measurements;
  everything else — `ERR_TX`, a timeout, a busy wire, an `OSError`, an exception — leaves the last
  definite reading standing and increments a counter. This is `TuneBusy`'s discipline (ADR 0161
  decision 6) applied to a poll instead of a key-up.
- Initial `None` never mutes and falls back to the key-up snapshot, which is strictly more evidence
  than a guess.
- **No staleness bound is enforced**, and the reason is that every available action on "stale" is one
  of the two rules just rejected. The age is *surfaced* instead, and left to the operator.

### 4. Poll only while a bridge is relaying

Refcounted `start`/`stop` reached by `getattr` — the idiom `RxPump` already uses for `PolledGate`.
Called from `MumbleBridge.start`/`stop`, and from `DStarBridge`'s **crossband branch specifically**,
not beside it: with `rx_to_reflector` false (this station's shipped state since ADR 0099) no RF audio
reaches the reflector, so there is no hazard to gate and no reason to put frames on the dock link.
D-STAR releases in `_teardown` under its own flag, because that path also runs on a start-rollback
and a refcount leaked there would keep the poller on the wire for the life of the process.

**No bridge relaying → no thread → no serial traffic.** That is also why the shipped station polls
nothing until a link is brought up.

### 5. Counters, and a divergence that has to be documented rather than hidden

`deafened_age_s` and `deafened_unknown` join ADR 0162's `rx_deafened`/`tx_deafened`/`deafened`/
`deafened_reason` in each bridge's `tx_stats()`, `null`/`0` without a cadence — the same tri-state
discipline one level out, because `deafened: true` renders a two-second-old reading and a
ten-minute-old one identically.

**`GET /status`'s `broadcast_fm` block and `/link/status`'s `deafened` legitimately disagree during
the front-panel window, and the bench showed it doing so:** with the operator in broadcast FM,
`/status` reported `on: false` (the key-up snapshot, untouched, by design — decision 2) while
`/link/status` reported `deafened: true` and withheld 2299 frames. Both are correct about different
questions. Whether `/status` should render the cadence's reading instead is a real question and it is
**not** answered here; it changes what an operator sees on the dashboard and deserves its own cycle.

**Rejected in advance, carried forward from ADR 0162:** continuous audio is not a proxy for broadcast
FM. A busy repeater looks identical, and a heuristic that mutes a link on a long over is worse than
the hazard.

### 6. What this does not cover

- **The mute withholds real overs** while broadcast FM is selected (the M3 semantics). Deliberate.
- **`acceptance.py` does not exercise the cadence** — see the bench section. Do not read a green
  acceptance run as covering any of this.
- No ON path, no clear route, no UI, no fork changes.

## Fail-first

**Red run: 23 failed, 199 passed** across the five touched files, all behavioural. A first attempt
errored at *collection* because the poller module did not exist; that is a cascade, not evidence, so
the module was declared with empty methods and the run redone — ADR 0162's lesson applied on purpose
rather than relearned.

**Named as passing-but-not-evidence rather than counted:** the undiscriminated-reply test (it passed
on master *by thread-scheduling luck*, which is exactly what its own docstring predicts and why the
load-bearing assertion in the sibling test is on the writes instead), the "no cadence, no lifecycle"
and "no crossband, no cadence" guards (nothing starts a poller on master), the FM-off and
snapshot-fallback readings (the skeleton returned the fallback unconditionally), and
`test_a_tuner_without_the_probe_still_keys`, which predates this cycle.

**Green:** `uv run pytest` **2117 passed, 5 skipped** (from 2088/5). `npx vitest run` **12 files, 116
tests** — unchanged, and expected to be: no UI.

## Bench, on the deployed station

`kb@192.168.1.62`, 2026-07-31, at `357a487`.

### Phase A — measured before anything was built (service stopped)

**M1 — PASS.** FM on at 104.3 MHz, probe every 1.0 s for 35 s: **35/35 `ERR_BAND`**, payload
`7a08080006ff00000000ff00` every time, zero `ERR_TX`. The `~100.5 ms` latency in the log is the
instrument's own read granularity (`_READ_TIMEOUT`), **not** the radio's response time — stated so
nobody mistakes it for a measurement of the radio.

**M2 — a null result, and a correction to ADR 0162.** `0x0878` is byte-identical FM off vs on
(`7808040000000001` both times), so it carries no tell — reproducing ADR 0162. But the `0x0874`
control in **both** ADRs is **vacuous**: an empty `0x0873` is refused `ERR_SHORT` and the reply is
`74080c00010000000000000000000000` — status 1 and every field zeroed. It could not have differed
between states. ADR 0162 reported it as a null result without noticing it could never be anything
else, and this ADR says so rather than repeating the claim.

**M3 — the semantics, table above. PASS.**

**M4 — the front-panel ordering. PASS.** `F+0` pressed at t≈47 s: 47 × `ERR_OFF` → 93 × `ERR_BAND`,
**one** transition, caught on the first poll after the press.

**`gCurrentFunction` is not reachable by any existing read** — no dock reply carries it. That answers
the brief's third measurement item.

### B1 — the relay mute firing on a real radio, for the first time

ADR 0162 measured its mute with a *stub* block, because nothing could produce `on=True`. With the
cadence live it can. Service up, Mumble link connected, operator presses `F+0` mid-window:

| path | over the same 120 s |
|---|---|
| browser `/audio/rx` | **4 571 520 B, RMS 4221.2** — unaffected |
| Mumble relay | **2299 frames withheld**, `deafened: false → true`, `deafened_unknown: 0` |

The asymmetry ADR 0162 designed for, measured on a real radio rather than an injected reading.

### B2 — the rescue, on hardware

ADR 0162 recorded that no rescue had ever fired on hardware and could not be staged. It fires here:
**`rescues: 0 → 1`**, with the whole chain in the journal — poller sees the transition (23:13:53) →
relay muted → throttled warnings at 1 / 1453 / 2926 frames → `rescue #1` naming 104.3 MHz (23:15:02)
→ **relay resumed 23:15:05**, three seconds later.

### B3 — contention, the ground ADR 0161 said does not fall away

20 tunes in 2.0 s against the running cadence: **0 failures, 0 new `deafened_unknown`, 0 "dock link
is busy" refusals.** Not observable at a 2 s interval. Priced, not assumed.

### B4 — `acceptance.py`, and what it does **not** cover

Final run: **9 of 9 attempted PASS**; `split-minus` **SKIP** for the missing `Bench Split Minus`
preset, which still prints `RESULT: FAIL` at the banner — **ADR 0161 finding 8, unmoved for a third
cycle running.**

Two things reported rather than smoothed over:

- **`auth` FAILED on one of three full runs.** It passed on the two subsequent full runs and on an
  isolated `--only auth` run. The failing run's per-check detail was not captured and **no cause is
  claimed**.
- **The cadence was not running during any acceptance run.** The `systemd` stage restarts the
  service, the Mumble link does not autoconnect, and with no bridge relaying there is no poller. So
  acceptance neither exercises the cadence nor could have been affected by it — which also means a
  green acceptance run says nothing about this cycle's code, and B1/B2 are the only coverage.

### B5 — restore

**147.555**, `tune_persist` **false** (re-set after the restarts — ADR 0161 finding 9 again),
broadcast FM **off**, both units active, `radio.toml` / `radio-secrets.toml` **byte-identical** to the
pre-cycle snapshot (`ead78a44…` / `ae86f7f1…`). **22 EEPROM writes** in the session window, reported
from the journal; per-write attribution is **not** claimed.

## Findings carried forward

1. **A second process on the AIOC tty kills the dock link, and the service does not notice.** Staging
   broadcast FM from a bench script while the service ran produced
   `SerialException('device reports readiness to read but returned no data (device disconnected or
   multiple access on port?)')`, the reader thread stopped, and **the transport never recovered** —
   while `/status` went on answering with a `broadcast_fm` block and `tx_ok: true`. The only visible
   symptom was the cadence's own `deafened_unknown` climbing (13 → 28) and `deafened_age_s` rising
   past 57 s. The counters earned their place on their first day. This is a **pre-existing** transport
   fragility that the cadence made observable, not one this cycle introduced; a reader thread that
   dies and stays dead is a candidate for its own cycle.
2. **`0x0878` reports `tx_ok = 1` while F9 refuses to key — a firmware defect**, to be fixed alongside
   whatever merges F9 to fork `main`. `FLAG_TX_OK` reads the BK4819 demodulator path only; the
   interlock bit `FLAG_FM_BLOCKS_TX` exists on `0x087A` alone. ADR 0159's rule that a published flag
   must not lie was applied to `0x087A` and not to the F7 frame carrying the same field. Measured
   again this cycle (M2, FM on: `flags=0x01`). **The fork is not touched by this cycle.**
3. **The `ERR_TX` window is narrow** — the ADR 0161 decision-6 correction above. Two of that ADR's
   three grounds for refusing a poll now fall away on measurement rather than assertion; the third,
   contention, was priced in B3 and is not observable at this cadence.
4. **The mock cannot model `gCurrentFunction`** (ADR 0161 finding 7, standing): the whole `ERR_TX`
   class is invisible to pytest by construction.

## Out of scope

- Whether `GET /status` should render the cadence's reading instead of the key-up snapshot
  (decision 5).
- A transport that recovers from a dead reader thread (finding 1).
- An ON path, a clear route, UI, and any fork change.

## Source of truth

Firmware read at fork `2988431` (branch `f9-fm-tx-interlock`). Bench figures from
`scripts/bench/fm_cadence.py` legs `m1`/`m2`/`m3`/`m4`/`live` and `scripts/bench/acceptance.py`.
