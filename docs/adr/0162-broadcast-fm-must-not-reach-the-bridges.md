# 0162 — Broadcast FM must not reach the bridges

Status: Accepted
Date: 2026-07-31

## Context

[ADR 0161](0161-the-host-asks-the-radio.md) measured, on this station, that a UV-K5 playing broadcast
FM relays a commercial station to `/audio/rx` at **3841.2 RMS / 768 000 bytes**, and left the
consequence as its finding 3: one `AudioHub` feeds three subscribers, and two of them are bridges.
`MumbleBridge._rx_to_mumble` and `DStarBridge._rf_to_reflector` both drink from it, so a station in
broadcast FM puts a commercial broadcast onto a Mumble channel and onto a D-STAR reflector — whose
far end may be somebody else's RF repeater. **F9 stops the local transmitter and knows nothing about
an internet link.** 97.113(b) is why this is not merely untidy.

The asymmetry is the design: the **browser should** receive it — hearing FM radio in the browser is
the feature — and the **bridges should not**.

### Three things checked rather than carried. Two of them changed the cycle, and one of them is a correction to ADR 0161.

**1. `broadcast_fm.on is True` is unreachable on F8 and F9, so the mute cannot fire on this radio
today.** `Dock_FmOff()` sets `gFmRadioMode = false` as its **first statement**, unconditionally, with
no failure path (`App/app/uart.c:1135`), and the reply's `state` is read back from that same variable
(`:1231`). `ClearBroadcastFm` can build only OFF (ADR 0157). So *"the radio was asked to leave
broadcast FM and did not"* — the one case a measured-state gate can fire on — cannot happen.
`MockRadio(left_in_broadcast_fm=True)` is the only producer of `on=True` in the repository.

That is stated first, and up front, because a Part 97 control with a green test suite and no on-air
effect is worse than no control: it will be believed. See **decision 2** for what ships anyway and
why, and **decision 6** for the successor that makes it real.

**2. A non-mutating read exists today, in the frame ADR 0161 said had none.** That ADR's central
argument — *"the wire offers no read that is not also a repair"* — is **wrong**, and this is the
correction. `Dock_SetFm` runs its guards in this order:

```
gCurrentFunction == TRANSMIT || MONITOR   -> ERR_TX,   return
action == TUNE && !gFmRadioMode           -> ERR_OFF,  return    <-- the receiver is off
raster32 < lo || raster32 > hi            -> ERR_BAND, return    <-- the receiver is on
... only past here does anything happen
```

An out-of-band TUNE returns before the firmware touches anything, and *which* refusal comes back is
the complete answer. Both statuses were **already decoded** by the deployed codec
(`frames.py:1170-1172`), and the TUNE guard is in **F8**, which is on fork `main` — no firmware work,
and it works on radios without F9.

The error was not a missing fact. ADR 0161 reasoned from the *action table* — OFF, ON, TUNE, none of
them named "read" — and never sent the frame. `broadcast_fm_on.py` had shipped a `--status` flag
described in its own help text as *"a read probe"* since ADR 0160, and nobody ran it.

**3. The brief's fallback rule was wrong, not merely incomplete**, and the operator recorded it as
such. *"A tell in `0x0874`/`0x0878`, or else it needs firmware"* pre-committed to two branches that
both miss the answer. Measured (B2): `0x0874` and `0x0878` are **byte-identical** with the BK1080
running. The tell was in `0x0879` all along, in a branch nobody had sent.

---

## Decision

### 1. The mute lives in each bridge's relay loop, not at the `AudioHub` — and the brief was wrong here too

The brief asked for *"per-subscriber policy at the hub… `subscribe()` may need to carry who is
asking."* [ADR 0085](0085-mumble-rx-guard.md) lines 36-40 **already decided this exact question, for
this exact asymmetry, with the same two exemptions** — browser Listen and the recorder — and put
suppression in the relay loop *precisely so those stay untouched by construction* rather than by an
exemption somebody has to remember. Recorded as the brief being wrong rather than as a preference.

Four reasons beyond precedent, in descending order of force:

- **The hub cannot express what D-STAR needs.** A mute there is not "drop this frame", it is "drop
  this frame **and reap the outbound over**" (decision 3). A hub can only deliver or not.
- **`publish` is the pump's hot path.** Its docstring leans on there being no `await` and no user
  code between `get_nowait` and `put_nowait`; a per-subscriber predicate reaching backend state
  inside that loop puts an exception path in front of **every** consumer, the recorder included.
- **Counters live per bridge.** `tx_stats()` surfaces through `link/manager.py:134` and
  `dstar/manager.py:195`; a hub-side drop is invisible to both and would have to be plumbed back out.
- **The hub is deliberately radio-independent** (`holder.py` calls it exactly that) and survives
  backend swaps.

It reuses the `rx_guard` shape rather than inventing a sibling: one optional collaborator, `None`
keeps the raw relay, polled at the top of the loop. It is a **callable** rather than a timed latch
because the state lives on the radio, and it closes over the composition root's live `radio` — the
`rx_active=lambda: rx_pump.active` precedent — which also fixes a real staleness window for free: the
D-STAR bridge is built once in the lifespan and restarted rather than rebuilt, so its own
`self._radio` survives a `/radio/select` that the closure would not.

**The hub seam's one real advantage is answered, not dismissed.** A future subscriber inherits no
policy: the next relay somebody adds will call `audio_hub.subscribe()`, will work perfectly, and will
retransmit a broadcast station the day an operator presses F+0. A Part 97 control that must be
*remembered* will eventually be forgotten — so `tests/test_relay_subscribers.py` pins the set of
subscribe sites to the three known ones. A fourth fails CI and lands the reviewer on a docstring
telling them which decision they now have to make.

### 2. Gate on measured state only, and say plainly that it cannot fire

Mute iff the block **is not `None` and `on is True`**. `None` never mutes — the `tx_ok` rule, one
layer up and for the same reason: an unmeasured field must never silence a link any more than it may
lock a transmitter. `None` is every backend with no dock tuner and every pre-F8 radio, which is most
of the fleet.

**The coverage bound, in plain terms.** This fires when the radio was **asked** to leave broadcast FM
and did not. Per context finding 1 that cannot happen on F8 or F9, so **on this station the mute is
armed and blind**. The front-panel window — operator presses F+0, no key-up since — is not covered
and *cannot* be while the only read the relay path is allowed to make is one that repairs.

**Option B — one read when a bridge link comes up — rejected on ordering, not only on
GET-that-mutates.** It covers only *FM-then-link*; the likelier ordering on a station with a standing
link is a **later F+0**, which it misses entirely. It buys one sub-case at the cost of silently
ending the operator's broadcast listening every time a link connects.

**Rejected in advance so nobody reaches for it later: continuous audio is not a proxy for broadcast
FM.** A busy repeater looks identical, and a heuristic that mutes a link on a long over is worse than
the hazard it treats.

### 3. Placement, which is where the defect would have been

**D-STAR: after the `rf_gate` check, before the busy check, and it must call `_reap_stale_tx()`
before the `continue`.** A bare `continue` leaks an open outbound over, and the broadcast-FM case is
exactly where every existing net fails — the audio arrives at full frame rate so the `wait_for`
timeout never fires, and it is loud so the gate never closes. `_last_tx_feed` is refreshed only
inside `_feed_rf`, which we skipped. `_mode` would stay `"tx"` for ever, no terminating frame would
go out, and because `send_operator_audio` latches on `_mode`/`_tx_source` **the browser operator
could no longer key the reflector either.** The tidy one-line version of this change mutes the
broadcast and jams the D-STAR transmit path in the same edit.

After the gate rather than before, because `AudioLevelGate` is stateful: skipping the call freezes
its hysteresis, so on unmute it is still latched open from pre-mute audio and keys the reflector on
the first frame regardless of level.

**Mumble: first check in both loop copies**, above the RX guard. It is a standing legal condition
rather than a timed latch and must not be shadowed by the guard's ordering.

**Not folded into `_send_to_mumble` or `_feed_rf`**, which is the tidier place and the wrong one:
`send_operator_audio` shares both helpers, so muting there would silence **the web operator's own
microphone** — not a retransmission of anything, and nothing to do with what the radio can hear.

### 4. A counter and a tri-state, because the counter alone cannot be read safely

`rx_deafened` in `MumbleBridge.tx_stats()`, `tx_deafened` in `DStarBridge.tx_stats()` — each matching
its own bridge's direction convention.

A bare count is ambiguous in the dangerous direction: `rx_deafened: 0` means *"this station was
measured and can hear itself"* when `deafened` is `false`, and *"nobody has ever asked this radio"*
when it is `null`. Those render identically, which is `BroadcastFm`'s own *"'we never asked'
rendering identically to 'verified hearing' is how a deaf station gets trusted"* reappearing one
layer up. So each `tx_stats()` carries **`deafened: true | false | null`** beside the count, derived
at read time and never stored, plus `deafened_reason` — the sentence an operator can act on — and a
throttled `log.warning`. Silence on a link is indistinguishable from a dead link, which is the fault
class this repo keeps closing.

### 5. The probe: one frame, key-up path only, and it writes nothing

`ProbeBroadcastFm` is `0x0879` action=**TUNE**, band 0, **0 Hz**, with **no parameters** — so it is
out of band by construction exactly as `ClearBroadcastFm` is OFF-only by construction. Two
independent safety properties rather than one: TUNE can never promote to ON (`dock.h`: *"TUNE is not
a cheaper ON"*), **and** the frequency can never be in band.

**0 Hz and not the 64 MHz the fork's own test 48 names.** `BK1080_GetFreqLoLimit` is
`{875, 760, 760, 640}`, so 64.0 MHz is out of band *only under band 0* and is a legal band-3
frequency; a firmware that ignored or defaulted the band field would tune to it. 0 Hz is below every
floor in the table and is on the 100 kHz raster, so `dock.c` forwards it rather than answering
`ERR_FIELD` itself — which it must, because only the HAL can see `gFmRadioMode`.

Called **once, from `_clear_if_deafened`, immediately before the clear that was already going to
happen.** One frame, at a point where a frame already goes. This does not open the cadence question.

**`APPLIED` is impossible for this vector and is treated as an alarm.** If some build **clamps** an
out-of-band tune rather than refusing it, the probe has just retuned the operator's broadcast receiver
and is not a read at all. Measured before shipping (B1: this radio refuses), but the host cannot
assume every image it meets does — so `APPLIED` logs at `ERROR` and **disarms the probe for the life
of the tuner**. It can mutate at most once, never silently twice.

#### The hard condition, structural rather than promised

> A failed, refused or timed-out probe **never** blocks a key-up.

Guaranteed by construction, not by a `try` block: **the probe writes nothing.**
`clear_broadcast_fm` remains the sole writer of `tuner.broadcast_fm`, so no probe result can reach
`refuse_if_deafened` and no probe result can refuse anything. It also raises nothing — every status,
every timeout and every `OSError` becomes `None`.

The alternative is not hypothetical. Probe measures a deaf station → the clear that follows is
declined with `ERR_TX` (which deliberately leaves the last reading standing) → a recorded `on=True`
would refuse the key-up of a **busy** station. That is ADR 0161's defect one cycle later, on the same
code path — the defect that took the Part 97 station ID off the air twice in four minutes while 2067
tests were green. Writing nothing forecloses it, and the test that pins it
(`test_the_probe_runs_before_the_clear_and_never_blocks_a_key_up`) is the deliverable of this cycle.

### 6. ADR 0161 finding 5, closed — and the successor named

A pre-key-up clear rescued a deaf station and could never report it, because both OFF legs answer
byte-identically (ADR 0160 measured that). With the probe running first, `ERR_BAND` immediately
before an OFF **is** the rescue. It is logged at `WARNING` and counted as `broadcast_fm.rescues`,
which rides in the status block and so reaches `GET /status` and the `status` WebSocket event through
`asdict` with no new plumbing — the `rx_guarded` rule (ADR 0085) applied to a repair instead of a
drop. A repair nobody can see is barely better than the silent one it replaced.

It is a plain `int` and not a third tri-state, because *"how many key-ups did this server rescue"*
has no unknown: a backend that cannot probe has rescued nobody.

**The successor, and it is the one that matters.** The probe is non-mutating, so it can be called
from places the clear never could — and a cadence on the relay path is what turns decision 2's armed
blindness into a gate that covers **both orderings**, including the front-panel window. That is its
own ADR: it must decide where the cadence lives, what it costs the serial link the tuner shares with
tuning traffic, and what `ERR_TX` during an over means for a block that something is about to act on.
Two of ADR 0161's three grounds for refusing a poll fall away for a read that does not mutate;
contention does not.

### 7. The recorder is unreachable from this seam, not spared by choice

`RxRecorder` is not a hub subscriber — `RxPump` calls `self._recorder.write(...)` directly
(`rx/pump.py:264`) — so neither seam could have touched it. Saying "we decided to keep recording"
would overclaim a decision that was never available.

Two things follow that this ADR should not leave implied. 97.113(b) governs *transmission*, so
capture is outside its scope — but recording commercial FM is a 17 USC question that the Part 97
analysis does **not** clear, and nothing here decides it. And the recording and the relayed stream
now diverge with nothing marking the divergence in the segment: a recording used to reconstruct what
went on the air will **overstate** it.

---

## Acceptance

**Red run, declarations only so every failure is behavioural: 8 failed, 328 passed.** The two
bridges relaying broadcast FM (Mumble and D-STAR), the D-STAR over leaking, and four probe
behaviours. A first attempt was discarded for ADR 0161's reason in reverse — the `probe_broadcast_fm`
declaration landed inside the `runtime_checkable` `Uvk5Tuner` protocol, making it mandatory for every
tuner and taking down nine unrelated tests with "does not satisfy the protocol". A cascade is not
evidence.

**Named as passing-but-not-evidence rather than counted:** the four "must never mute" guards and the
operator-microphone guard (nothing mutes on master, so nothing regresses), the two goldens
(specification, and a decode of bytes measured on the radio), the probe-failure sweep (the stub
returned `None` unconditionally), the tuner-without-a-probe guard, and the hub-subscriber pin (a
tripwire for a fourth subscriber, not a defect today).

**Green:** `uv run pytest` **2088 passed, 5 skipped** (from 2070/5). `npx vitest run` **12 files, 116
tests** — unchanged, and expected to be: no UI in this cycle.

## Bench, on the deployed station

Run on `kb@192.168.1.62` (unit `radio-server` on 8090, witness `radio-server-kv4p` on 8091) on
2026-07-31, at `a021e1d` and `ef5f5c9` of this branch.

### B1 — refuse or clamp? The gate for everything after it

`scripts/bench/fm_probe.py`. **PASS.**

| leg | reply | payload |
|---|---|---|
| FM off, probe | `ERR_OFF` | `7a08080009ff00000000ff00` |
| FM **on** (104.3 MHz), probe | `ERR_BAND` | `7a08080006ff00000000ff00` |
| probe again | `ERR_BAND` | byte-identical |
| then OFF | `APPLIED` | receiver reported still on **104.3 MHz** |

The two answers differ in **exactly the status byte**, a repeat changes nothing, and the OFF leg
proves no probe moved the receiver. The firmware **refuses**; it does not clamp.

Worth recording: the fork's host tests force `ERR_BAND` with `g_fm_force_status`
(`test_dock.c:1176`), so the real band-limit branch had **never executed anywhere**, on host or
radio, until this run.

### B2 — the `0x0874`/`0x0878` byte diff the brief asked for. A null result, which is the point.

| frame | FM off | FM on |
|---|---|---|
| `0x0878` (set-modulation reply) | `7808040000000001` | `7808040000000001` |
| `0x0874` (empty `0x0873`) | `74080c00010000…` | `74080c00010000…` |

**Byte-identical both times.** Neither carries a tell, which is what makes `0x0879` the only one —
and it is why the brief's fallback rule needed correcting rather than following.

One thing fell out of it that is worth its own line: **`0x0878` reports `tx_ok = 1` on a station that
is deaf and that F9 will refuse to key.** A host reading only the modulation frame is misled; the
`will_key = TX_OK && !FM_BLOCKS_TX` rule is not a formality. (The ON reply carried `flags=0x03` —
F9's interlock confirmed on the wire for the second cycle running.)

### B3 — the relay, both directions, on real broadcast audio

`scripts/bench/fm_relay_mute.py`: real radio, real AIOC sound card, real `AudioHub`, real `RxPump`,
real relay loops. The **sinks** are mocks — `MockMumbleClient`, the gateway mock, a stub vocoder —
because the thing under test is what each relay loop passes onward, and because pointing this at a
live reflector is the exact hazard it exists to prevent. **PASS.**

| block | browser | Mumble | D-STAR | counters |
|---|---|---|---|---|
| `None` — nobody asked | 4065.7 RMS / 122 880 B | 4111.5 RMS / 378 240 B | **197 AMBE frames** | 0, `deafened: null` |
| **`{on: true}`** — measured deaf | 4246.6 RMS / 122 880 B | **0.0 / 0 B** | **0 frames** | 200, `deafened: true` |
| `{on: false}` — measured hearing | 3858.3 RMS / 122 880 B | 4073.5 RMS / 384 000 B | 200 AMBE frames | 0, `deafened: false` |

Browser bytes are identical across all three legs: the asymmetry holds. ADR 0161's finding 3 is
reproduced in the first row — and note it reaches the **reflector**, which that ADR inferred rather
than measured.

**The `{on: true}` leg uses a stub tuner, and that is the limitation of context finding 1, not a
convenience.** F8/F9 cannot produce that block. The third row is the blind spot of decision 2,
measured rather than described: a station that really is deaf, whose last measurement says otherwise,
relays.

### B4 — the key-up fall-through, on a real refusal

Probe sent with DTR asserted, i.e. from a genuinely transmitting radio: **`ERR_TX`**, payload
`7a08080008ff00000000ff00`. The kv4p witness on 445.800 independently caught the carrier
(`t=2.45s busy -> True`, `t=2.86s -> False`), so this was measured against real transmit state rather
than a stub returning a status. `probe_broadcast_fm` maps it to `None`; `_clear_if_deafened` ignores
it entirely.

### B5 — `acceptance.py`

**9 of 9 attempted stages PASS** — `systemd`, `web`, `presets`, `rx`, `dtmf`, `auth`, `tx`, `split`,
`services`. `split-minus` **SKIP** (no `Bench Split Minus` preset), which still prints
`RESULT: FAIL` at the banner: **ADR 0161 finding 8, unmoved, and read past for the second cycle
running.** The `services` stage keyed a real announcement at 2986 RMS on the witness, so the key path
ran end to end with the probe in it, 42 times across the session.

**No rescue fired on hardware, and it could not have.** Staging one needs broadcast FM switched on
*after* boot with the service running, which only the radio's front panel can do — the same
unrunnable-by-nature shape as ADR 0161's bench items 8 and 9. The rescue path is pytest-proven and
recorded here as an operator item.

### B6 — restore

Station on **147.555**, `tune_persist` **false**, broadcast FM **off**, both units active, and
`radio.toml` / `radio-secrets.toml` **byte-identical** to the pre-cycle snapshot
(`ead78a44…` / `ae86f7f1…`).

**13 EEPROM writes** during this cycle's bench window (first deploy 20:14:16 onward), in clusters
aligning with the tuning stages of the two service-restart-bracketed runs. The mechanism is **ADR
0161 finding 9, observed again directly**: `/status` reported `tune_persist: true` immediately after
every service restart, despite having been set false before it. It was set false again before each
sweep and is false now. Precise per-write attribution was not established, and is not claimed — last
cycle shipped a "zero writes" claim that the journal falsified.

## Findings carried forward

1. **The relay gate cannot fire in the front-panel window, and on F8/F9 it cannot fire at all.** The
   mute ships armed and blind. Decision 6's cadence is what makes it real, and it is the named
   successor rather than a backlog item.
2. **ADR 0161's "no read that is not also a repair" is wrong** and is corrected here. The failure mode
   is worth more than the fact: it reasoned from an action table instead of sending the frame, and an
   instrument that would have answered it (`broadcast_fm_on.py --status`, described in its own help as
   *"a read probe"*) had been sitting in `scripts/bench/` since ADR 0160.
3. **`0x0878` reports `tx_ok = 1` on a station F9 will refuse to key** (B2). ADR 0159 R4
   (`Dock_ModulationCanTx`) is answered empirically: it does not see broadcast FM.
4. **The deploy-state guard ADR 0161 certified was only ever tested in one direction.**
   `merge-base --is-ancestor HEAD origin/master` returns 0 for *on master* **and** for *behind
   master*; it was proved against an "ahead" checkout and adopted. Between the cycles a
   `git reset --hard origin/HEAD` against a stale symbolic ref left the box **six commits behind**,
   reporting `on-master=0`. Replaced in `server-notes.md` with `rev-list --left-right --count`, which
   reports both numbers. A guard tested in one direction is a guard tested for the case you were in.
5. **`acceptance.py` still exits non-zero for a SKIP** (ADR 0161 finding 8) and still has no
   deploy-state stage (ADR 0160 finding 1). Both unmoved, both now read past twice.
6. **The mock cannot model `gCurrentFunction`,** so the whole `ERR_TX` class of behaviour is
   invisible to pytest **by construction** and `acceptance.py` is the only guard on it. Promoted from
   ADR 0161's observation to a standing rule about what pytest can and cannot certify on the key path
   — and the reason B4 measured the refusal against a keyed radio rather than a fake.
7. **F9 is not on fork `main`.** `main` is `d086a23` (F8); F9 is `d903881` on `f9-fm-tx-interlock`,
   PR [#7](https://github.com/kbennett2000/uv-k1-k5v3-firmware-custom/pull/7), **open**, pre-release
   `radio-server-f9-v5.7.0`. **A build from `main` has `0x0879`/`0x087A` but no TX interlock and will
   key while playing broadcast FM.** Nothing said so; the fork's README now does.
8. **No host route to clear broadcast FM** — ADR 0161 finding 2, unmoved. Pressing Talk is still the
   operator's only way out from the host side.
9. **The recorder and the relayed stream now diverge**, and nothing marks it in the segment.

## Out of scope

- **No ON path, no clear route, no UI.** Both routes are still their own cycles (ADR 0160 finding 8:
  the keypad is repurposed in broadcast FM, which changes what an ON path has to look like).
- **No cadence for the probe.** Decision 6.
- **The fork's code was not touched** — documentation only, its own PR.

## Source of truth

Firmware claims are read from fork `kbennett2000/uv-k1-k5v3-firmware-custom` at **`d903881`**
(branch `f9-fm-tx-interlock`). `Dock_SetFm`'s guard order and `Dock_FmOff`'s unconditional clear are
quoted from `App/app/uart.c`; the band tables from `App/driver/bk1080.c:131-141`; the blanking
contract from `App/app/dock.c`. The two probe replies in `tests/test_uvk5_frames.py` are
**measured on the radio**, not transcribed.

The station is **left on `ef5f5c9`**, ahead of master on purpose; `HANDOFF.md` and `server-notes.md`
carry the SHA, the PR and the command to put it back.
