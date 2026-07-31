# 0161 — The host asks the radio instead of remembering

Status: Accepted
Date: 2026-07-31

## Context

Two halves of one interlock existed and neither was connected to the other.

[ADR 0159](0159-the-radio-refuses-to-transmit-while-deaf.md) put the transmit interlock in the
firmware — F9 appends a clause to `RADIO_PrepareTX` so a UV-K5 playing broadcast FM will not key —
and reported that refusal on `0x087A` `flags` **bit 1**. It changed no `radio_server/` code, so
`frames.py` went on reading bit 0 only.

[ADR 0160](0160-the-bench-answers-back.md) then measured, on hardware, that the *host* half cannot
see the state at all. `tuner.broadcast_fm` is written in exactly one place —
`SetVfoTuner.clear_broadcast_fm`, called only from `AiocBaofeng._assert_boot_broadcast_fm`, called
only from `__init__` — and **the boot assert clears the very condition the gate tests**. After
startup the block is permanently `{on: false}`, `refuse_if_deafened` never fires, and bench items 8
and 9 were recorded NOT RUN because the gate was not bypassed but *blind*.

So the radio held a true, live answer that nothing read, while the host held a boot-time one and
acted on it. **This cycle connects them: the host stops remembering and starts asking.**

### Three things checked rather than carried

| claim | what was found |
|---|---|
| "F9 is on the radio" (the brief) | **True, and now measured rather than asserted** — see B1. It had never been confirmed from the host side. |
| "dock.c on fork main at the F9 commit" (the brief) | **Fork PR #7 is still OPEN.** `origin/main` is `d086a23` (F8). F9 is `d903881` on `f9-fm-tx-interlock`. Goldens are transcribed from there, and the discrepancy is recorded rather than quietly resolved — ADR 0158 corrected the same shape of error in the other direction, where a verified fact expired between writing and merging. |
| the deployed checkout | `6e5becf` (ADR 0158 merge), i.e. **not** 12 ADRs adrift as ADR 0160 found it: 0159 and 0160 changed no host code, so `radio_server/` was byte-identical to master. Checked before anything else, because ADR 0160's finding 1 is that nobody checks. |

## Decision

### 1. `frames.py` reads bit 1

`FLAG_FM_BLOCKS_TX = 0x02`, `BroadcastFmReply.fm_blocks_tx`, and `BroadcastFmReply.will_key`
(`tx_ok and not fm_blocks_tx`, the rule `dock.h` states on the wire).

It is documented **`0x087A`-only**: `0x0878`'s flags byte has no bit 1, so `SetModulationReply`
deliberately has no such accessor — reading it off that frame would be a host inventing a firmware
refusal from a byte the firmware never set. `fm_blocks_tx` is a plain `bool` rather than tri-state,
which looks like a violation of this module's "never coerce an unknown into `False`" rule and is
not: `0` is the *correct* reading for a refusal that measured nothing and for every image without
the interlock, because neither is blocking anything. The unknown lives in `status`.

### 2. The re-read goes before a key-up, and nowhere else

**The wire offers no read that is not also a repair, and that decided the design.** `0x0879` has
three actions — OFF, ON, TUNE; only an `APPLIED` reply carries live `state` *and* live `flags`;
every refusal blanks both; `ClearBroadcastFm` builds only OFF by design (ADR 0157); and the firmware
reports state **after** acting. So the single frame that can answer *"is this station deaf?"* is the
frame that gives it its ears back.

`AiocBaofeng._clear_if_deafened` therefore replaces the attribute read at the head of `_key_on`. A
key-up on a deaf station is **repaired and allowed**, not refused, and `refuse_if_deafened` survives
for the radio that was asked and did not stop. That is not a weakening: ADR 0160 finding 3 recorded
that there is no host route to clear broadcast FM at all, and this is its second caller, at the one
moment the answer matters.

**Gated on the earned `Capability.CLEAR_BROADCAST_FM`** — ADR 0158 R2's design, unchanged. Firmware
that cannot answer pays one frozenset membership test per key-up instead of `SetVfoTuner`'s 3.0 s
timeout before every over and a `TuneError` that would refuse every key-up on every station. The
circularity that forbids this gate at *boot* does not apply in the key path.

**Cost on the serial link the tuner shares with tuning traffic**, measured rather than estimated: the
pre-key-up sequence goes from two one-shot frames to three, and the added one is **0.100 s**
time-to-first-byte (B2, and matching ADR 0160). It arms no transmit lockout, and it **writes no
flash when the receiver is already off** — the firmware's `memcmp` short-circuit, which is what makes
a per-key-up frame affordable rather than EEPROM wear on every over. Against a 0.5 s TX lead-in it is
inside the noise.

**Not on a poll**, and each of the three reasons would be sufficient alone:

1. **A GET that mutates.** The only live read is the OFF action, so polling `/status` would switch an
   operator's broadcast FM off from a status read, and would fight someone at the front panel within
   one poll interval. "This station is about to transmit" justifies the repair; a poll does not.
2. **`ERR_TX`.** `0x0879` is refused while the radio is keyed, and a refusal blanks `state` and
   `flags` — so a polled block would go **unknown exactly during an over**, and every over would log
   a `TuneError`.
3. **Contention.** One serial link carries tuning traffic, and `/status` is polled by every open
   browser tab plus `acceptance.py`. A per-poll frame turns a quiet link into continuous traffic that
   can collide with a tune in flight, for a reading nothing is about to act on.

Not "both", for the same three reasons. The residual is stated rather than hidden: between key-ups
the block is a snapshot from the last key-up — *bounded* now instead of frozen at boot — and the
browser sub-line says so.

**The limit this cannot escape.** Because the reply describes the state *after* the OFF, and ADR 0160
measured both OFF legs byte-identical, **the host can never know it just rescued a deaf station.** It
repairs silently; there is no log line, event or counter that could be honest about it. Confirmed in
the field: `blocks_tx` on a healthy station always reads `false`, because by the time the host reads
it the receiver is already off. Only a read-only `0x0879` action could change that (R2).

### 3. The block carries bit 1; `tx_ok` still does not

`BroadcastFm` gains `blocks_tx: bool | None`, recorded by `clear_broadcast_fm` from the same reply as
`on` and `hz`, and surfaced in `GET /status`.

This is not a reversal of `test_clearing_broadcast_fm_never_touches_tx_ok`. **Bit 0 has a partner**:
`tx_ok` is the BK4819 demodulator, read from `0x0878` and paired with `modulation`, so writing it
from this frame would break ADR 0155's invariant that the two are `None` together and would leave
`_refuse_if_tx_disabled` reading a flag from a different frame than the demodulator its message
names. **Bit 1 has none** — it belongs to the broadcast-FM cause, it is recorded inside the
broadcast-FM block, and it can never masquerade as the demodulator's answer.

`will_key` exists on the *frame*, for a host holding only that frame. This server does not use it:
the two causes stay apart all the way to the operator, with two refusals, two messages and two
remedies. A collapsed answer is a diagnosis nobody can act on.

### 4. ADR 0158's latch is dropped, and the reversal is recorded as a reversal

The latch was never a mechanism — it was the emergent property of never re-reading. Decision 2
removes it by construction, so an operator who presses EXIT gets the transmitter back at the next key
attempt, with no restart and no `POST /radio/select`.

**Why this is correct rather than an about-face.** ADR 0158 decision 5 judged that "a deaf
transmitting station is worse than a restart", and it was right *for a radio without F9*: the host
gate was the only thing between a deaf station and a blind carrier, so failing closed was the safe
direction even at the cost of an unclearable refusal. F9 moved that job into the radio, and ADR 0159
said so in its own correction — *"the host's latch is no longer a safety mechanism… it is a UI
accuracy problem."* A lockout that cannot clear now protects nothing and starts refusing key-ups the
radio would allow, on a station that has been hearing perfectly well since EXIT was pressed. **The
trade did not change sides; the thing on one side of it moved into the firmware** — and B2 below is
the measurement that it really did.

### 5. The wording stops claiming the host is the thing refusing

Three strings said, in the operator's terms, that the server was the obstacle and that only a restart
would clear it. After F9 the first is wrong and the second is no longer true at all.

- **`refuse_if_deafened`** drops "AND restart the server", says the receiver *was asked to stop and
  did not*, and **branches on `blocks_tx`** — because the consequence is a property of the image and
  a host cannot see a build flag from the far end of a cable. `True` reports that the radio refuses
  its own PTT path too; anything else keeps the blunter sentence, that the radio will transmit into
  the channel regardless. The unknown takes the harsher wording deliberately: it over-warns, and the
  alternative under-warns about a station that transmits blind.
- **A clear that was attempted and failed is its own refusal**, sharing no claim and no remedy with
  the other two. It refuses the key-up — which is not the "an unmeasured field must never lock a
  transmitter" rule being broken, because that rule is about a radio nobody has *asked*: this one was
  asked, having earned the capability, and did not answer, which `_reassert_channel` refuses on three
  lines later anyway. It also **blanks the block to `None`**, since a previous key-up's `on: false`
  left standing after a failed re-read is a reading old enough to be a lie, rendered as a
  measurement.
- **`TalkControl`'s sub-line** says EXIT is the whole remedy, states its real limit (it reflects the
  last key-up, not this instant), and names the firmware as a second line of defence rather than
  implying the host is the only one.

### 6. Found while wiring that up: the browser never saw any of it

`session.feed(data)` raising `RadioUnavailable` inside the `/audio/tx` frame loop was **caught by
nothing** — the handler's `except` names only `WebSocketDisconnect` and `CancelledError` — so the
socket died and `useTxAudio`'s classifier reported **"Transmit connection dropped."** Every carefully
written refusal reason in this arc reached the 503 body, the `tx_failed` event and both bridges'
logs, and **none of it reached the one consumer it was written for.** ADR 0158 decision 7 asserted
those paths worked untouched; the browser was not among the paths it checked.

The mechanism already existed and is used for exactly this reason: the endpoint sends an explicit
`{"status": "busy"}` text message before closing *because a browser cannot read a close code*. A
refusal now gets the same treatment — `{"status": "refused", "reason": …}`, verbatim — and
`useTxAudio` surfaces it. One path, not three fixes: the AM refusal, the broadcast-FM refusal and a
receiver that could not be checked all arrive diagnosably instead of collapsing into one dropped
connection.

## Acceptance

### Fail-first, with the weak evidence labelled weak

**Red run 1 — 33 failed, 207 passed, and none of it is evidence.** Every failure was "a name is
absent" (`BroadcastFm.__init__() got an unexpected keyword argument 'blocks_tx'`, `module 'frames'
has no attribute 'FLAG_FM_BLOCKS_TX'`), and a fake that cannot construct takes every test using it
down with it — so it *buried* the behavioural evidence rather than providing it. Recorded because
stopping there and calling 33 failures a red run would have been the easy mistake.

**Red run 2 — declarations only added, no behaviour changed. 16 behavioural failures.**

| file | n | what master does |
|---|---|---|
| `test_aioc_baofeng_tuning.py` | 9 | never asks and keys blind; keys while the radio says `on`; refuses for ever after EXIT; no frame and no refusal when the clear fails; leaves a stale `on: false`; nothing to order against the re-assert; asks zero times per key-up; bit 1 unrecorded; the message demands a restart |
| `test_uvk5_tuner.py` | 5 | bit 1 not recorded on any of the four reply shapes; a failed clear leaves the previous reading standing |
| `test_tx_audio.py` | 2 | the exception escapes the handler and the socket dies with no reason |

**Vitest — 8 failed / 108 passed.** 3 behavioural (`TalkControl.test.jsx`: the sub-line still said
"restart the server", "it only checks at startup", and "not proof the radio is hearing"), 5 weak
(`classifyTxMessage` not exported). Only the 3 are counted.

**Not evidence, and labelled so in the tests themselves:** a tuner that never earned the capability
sends no frame (green on master, and the direction that would take the fleet off the air if it
regressed); unkeying sends no frame (vacuous on master, where nothing sends it anywhere); a pre-F9
radio gets the blunter sentence (master has only that one sentence).

**Mutations, because the goldens' own fail-first was only "a name is absent":**

| mutation | result |
|---|---|
| `FLAG_FM_BLOCKS_TX = 0x04` | **3 failures.** Only 3, and the reason is the honest limit: every vector whose `flags` is 0 or 1 reads identically under either mask. |
| `fm_blocks_tx` returning readiness instead of blocking | **12 failures** |
| `will_key` reading bit 0 alone — the pre-F9 host | **2 failures** |

**Green.** `uv run pytest` → **2067 passed, 5 skipped**, against 2043 / 5 — **+24, no regressions**.
`npx vitest run` → **12 files, 116 tests**, against 11 / 110 — **+6, no regressions**.

### The bench, on the deployed station, over SSH

Deployed to `d396eaa` (`git switch --detach`), `uv sync`, web bundle rebuilt. `radio.toml` and
`radio-secrets.toml` confirmed gitignored and hashed first; both are **byte-identical afterwards**
(`ead78a44…`, `ae86f7f1…`) — unlike ADR 0160, because no `POST /radio/select` ran. `GET /status`
carried the new field immediately: `broadcast_fm {on: false, hz: 64000000, blocks_tx: false}`.

**B1 — F9 is on the radio, measured.** The brief asserted it; guardrail 1 says do not carry a
hardware fact. `0x0879` ON at 104.3 MHz answered `flags=0x03`, i.e. bit 1 set — which *is* the
Fusion-F9-with-`ENABLE_DOCK_FM_TX_INTERLOCK` probe, since every other image this fork publishes
answers 0 while deaf. The flags byte matches the fork's published F9 vector exactly. It was free,
because B2 needs the receiver on anyway, and without it B2's absence would have proved nothing.

**B2 — the firmware interlock, on hardware. ADR 0159's headline claim, which had never met a radio.**
Station on 445.800 (the witness is a UHF-only SA818; an absence outside its band is *no* evidence).
Service stopped for the whole sequence — since this cycle the host clears broadcast FM before every
key-up, so a running server would repair the condition under test. DTR asserted by hand.

| leg | `0x087A` | witness RMS |
|---|---|---|
| control A — FM off | `flags=0x01` · on=False · tx_ok=True · will_key=True | **1065.9** |
| **interlock — FM ON** | **`flags=0x03`** · on=True · **tx_ok=True** · **fm_blocks_tx=True** · will_key=False | **0.0** |
| control B — FM off | `flags=0x01` | **965.5** |

Carrier, then **no carrier while deaf**, then carrier again. **PASS.** The `on=True` *with*
`tx_ok=True` in the same frame is ADR 0156's dangerous pair measured again, and bit 1 beside it is
the reason it is no longer dangerous on this image. Time-to-first-byte: 0.100 s on both OFF legs,
0.401 s on the ON leg.

**B3 — bench items 8 and 9, in the form this cycle gives them.** Recorded plainly: items 8 and 9 ask
that the *host* refuse; this cycle makes the host repair instead, so their expectations changed with
the code, exactly as ADR 0159 changed the fork `BENCH.md` §9's.

- **The frame is sent at every key-up.** Three `POST /ptt` cycles produced **exactly three**
  `0x0879` round trips in the journal. On master this line appears once, at boot, and never again.
- **Placement, on hardware.** Each key-up logs `broadcast FM is off` **before** `tuned rx=445800000`
  — the deafness question asked before a `0x0873` or `0x0877` is spent.
- **A genuinely deaf radio is repaired and then transmits.** The instrument put the radio into
  broadcast FM (`flags=0x03` — deaf, and the firmware refusing); the service was started; the boot
  assert cleared it; `/status` read `{on: false, hz: 104300000, blocks_tx: false}`, where the `hz`
  corroborates that the block is read off the radio rather than fabricated. The following key-up was
  witnessed at **RMS 1100.9**, in line with both controls above.
- **What still needs an operator.** The literal item 8 — F+0 at the front panel *after* boot, then a
  key-up — cannot be produced remotely, and the reason is structural rather than a gap in this run:
  the boot assert clears broadcast FM at every construction, so there is no way to hand a *running*
  baofeng backend a deaf radio except by hand at the keypad. Item 9's latch is likewise unreachable
  without a radio that refuses to leave broadcast FM, which is a malfunction rather than an operating
  state. Both are covered by fakes in pytest and are recorded here as operator items, not as passes.

**B4 — ADR 0160's one open question, settled: the RX pump DOES relay broadcast-FM audio.** ADR 0160
proved the BK1080 reaches the sound card and recorded the relay half as not established, because the
only way to have broadcast FM on while the service runs is the front panel. Answered instead with a
temporary radio-server on port 8099 configured **without `uvk5_tuner`** — no tuner, so no boot
assert, so the receiver survives into a running server — with the audio path otherwise identical
(same AIOC card, same blocksize, same squelch mode). Bracketed on both sides:

| BK1080 | `/audio/rx` RMS | peak | bytes in 8 s |
|---|---|---|---|
| OFF (control A) | 0.0 | 0 | **0** |
| **ON** | **3841.2** | 17504 | **768 000** |
| OFF (control B) | 0.0 | 0 | **0** |

The byte counts are the sharper number: on the controls the audio squelch never opened at all, while
broadcast FM held it open for the entire window. The magnitude matches ADR 0160's at the sound card
(3308 / 3029), so the pump passes it essentially unattenuated. **A deaf station does not merely fail
to hear its own channel — it feeds a commercial broadcast station to every `/audio/rx` consumer**,
which includes the browser and both bridges. See finding 3.

**Restored, and proved rather than asserted:** station back on **147.555** (where ADR 0160 left it),
FM, `tx_ok` true, broadcast FM verified off, `tune_persist` back at its configured `true` after the
restart, both units active, no temporary instance left running and its config deleted. Persistence
was turned **off** before the 445.800 work, so no bench tune wrote EEPROM — ADR 0160 finding 6,
applied.

### The deployed checkout is left ahead of master, on purpose

`origin/master` still contains the boot-snapshot behaviour this cycle measured as wrong on hardware,
so redeploying master would restore a known defect for tidiness.

**And the guard that is supposed to notice was tested rather than trusted.** ADR 0160 finding 1 asked
for a deploy-state check; `acceptance.py` has none — no `git`, no `rev-parse` — so the only guard is
the shell snippet in `server-notes.md`, and a guard nobody has watched trip is a guard nobody has
tested. Run against the exact end state:

```
$ git status -sb | head -2
## HEAD (no branch)
$ git log --oneline -1
d396eaa Bench instrument: does F9 actually refuse to key while deaf?
$ git log --oneline origin/master -1
b668023 Merge pull request #217 from kbennett2000/adr-0160-bench-acceptance-f7-f8
$ git merge-base --is-ancestor HEAD origin/master; echo "on-master=$?"
on-master=1
```

**The documented snippet as it stood does not report drift.** `git log --oneline -1` prints a commit
and says nothing; `git status -sb` prints `## HEAD (no branch)` and says nothing. Only the
`merge-base` line answers the question, and it answers by **exit code** rather than by prose. The
snippet in `server-notes.md` is extended accordingly — a doc change, not code — and both
`server-notes.md` and `HANDOFF.md` now carry the deployed SHA, the PR it belongs to, and the literal
command to put the station back on the mainline once that PR lands. A note saying the station is
ahead of master is a fact; a note saying what to run when it is not anymore is a remedy.

## Consequences

- **The interlock is real, end to end, for the first time.** The firmware refuses (B2, measured), and
  the host now asks before every key-up instead of remembering a boot-time answer. Neither half had
  ever been shown to work on a radio.
- **A key-up repairs a deaf station rather than refusing it**, so the operator remedy that ADR 0160
  finding 3 said did not exist now does — it is the Talk button.
- **The refusal no longer latches.** Pressing EXIT is the whole remedy, and `docs/api.md` no longer
  documents a restart.
- **Every refusal in this arc is now visible in the browser**, which was assumed rather than checked
  for three cycles.
- **`blocks_tx` will read `false` on a healthy station essentially always**, because the frame that
  reads it is the frame that clears the condition. It earns its place on the two paths where it does
  not: a radio that refuses to leave broadcast FM, and a host reading `0x087A` for the firmware level.
- **The `uvk5` backend still gains nothing** (ADR 0158 R3 / 0159 R3). Its keying does not enter
  `RADIO_PrepareTX` and its `broadcast_fm` is permanently `None`.

### Findings carried forward

1. **The fork's F9 is not on `main`.** PR #7 is open at `d086a23`; the goldens here come from
   `d903881` on the branch. Anything transcribed from "fork main" between now and that merge is
   transcribed from F8.
2. **There is still no host route to clear broadcast FM, and now it is one finding rather than two.**
   ADR 0158 **R4** (`AiocBaofeng` advertises `CLEAR_BROADCAST_FM` with no `Radio`-level method behind
   it, while `MockRadio` has one) and ADR 0160 **finding 3** (the only caller is the boot assert) are
   the same missing piece seen from the capability side and the operating side. One successor closes
   both: a `Radio`-level `clear_broadcast_fm` and the route that reaches it.

   **The residual, precisely:** until that exists, **pressing Talk is the operator's only way out of
   broadcast FM from the host side.** If the pre-key-up clear misbehaves there is no second path, and
   the remedy is a hand on the radio's EXIT key — which on an unattended LAN station means nobody.
   That is acceptable for one cycle *because* the failure is now loud rather than silent (decisions 5
   and 6), and it is why this successor should not sit in the backlog: it is the only redundancy the
   host has.
3. **A deaf station relays broadcast FM to `/audio/rx`** (B4, measured: 768 000 bytes against a
   floor of zero). Every consumer of that stream gets it — the browser monitor, the Mumble bridge and
   the D-STAR bridge — with nothing anywhere marking it as not-the-channel. The RF transmit side is
   gated twice over now; **the relay side is gated nowhere**, and a bridge feeding a reflector with an
   RF gateway on the far end is worth thinking about before it happens. Not fixed here: it needs its
   own design pass, and it is not obvious that the answer is "mute", since a silent bridge is its own
   failure mode.
4. **`acceptance.py` still has no deploy-state stage.** ADR 0160 finding 1, unmoved. This cycle
   proved the manual guard fires and made its remedy paste-ready, which is not the same as building
   the check.
5. **The `0x0879` OFF leg cannot report what it changed**, so a pre-key-up clear repairs silently. A
   read-only fourth action (`STATUS`) on the wire is the only fix, and it is a firmware cycle.
6. **The temporary-instance technique in B4 is reusable and is not written down anywhere.** A
   radio-server with no `uvk5_tuner` is the only way to observe the audio path in a state the boot
   assert exists to destroy. It belongs in the bench notes rather than in this ADR's memory.

## Out of scope

- **No ON path and no new UI.** ADR 0160 finding 8 — the keypad is *repurposed* in broadcast FM, not
  dead, so an operator typing a frequency moves the wrong receiver — changes what an ON path has to
  look like, and it deserves its own design pass rather than riding along.
- **The fork was not touched.** Read-only, at `d903881`.
- Carried unchanged: ADR 0158 R3/R5, ADR 0159 R3/R4/R5/R6/R7, ADR 0160 findings 2, 5, 7, 8, 9.

## Source of truth

Firmware claims are read from fork `kbennett2000/uv-k1-k5v3-firmware-custom` at **`d903881`**
(branch `f9-fm-tx-interlock`, PR [#7](https://github.com/kbennett2000/uv-k1-k5v3-firmware-custom/pull/7),
**open**). Goldens transcribed from `tests/host/test_dock.c:1405` there and cross-checked against
`PROTOCOL.md` at the same commit and against this repo's independent reference framer.

Bench results were measured on the deployed station (`kb@192.168.1.62`, unit `radio-server` on 8090,
witness `radio-server-kv4p` on 8091) running commit `d396eaa` of this branch, on 2026-07-31.
