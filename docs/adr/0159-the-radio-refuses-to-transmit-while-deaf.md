# 0159 — The radio refuses to transmit while deaf (F9)

Status: Accepted

Extends the dock wire protocol established in ADR [0119](0119-uvk5-v3-dock-protocol-port.md) and
[0140](0140-the-first-key-is-always-lost.md), and specified for outside readers by ADR
[0148](0148-the-firmware-is-a-product-too.md). Follows the split ADR
[0149](0149-a-new-opcode-is-cheaper-than-a-changed-one.md) set: **firmware only — no code under
`radio_server/` changes this cycle.** Reasoning here, code in the fork.

## Context

ADR [0156](0156-a-deaf-station-can-still-transmit.md) established the hazard by reading the firmware:
the UV-K5 carries a **second receiver**, a BK1080 covering 64–108 MHz, and while it runs it holds the
speaker line the AIOC listens on. The station hears **nothing** of its own channel — and transmits
normally anyway, including the automatic station ID guardrail 5 makes required controller behaviour.

ADR [0158](0158-the-host-refuses-to-key-a-station-that-cannot-hear-itself.md) built the host half. It
is server policy, and it fails open in three places: a host crash, a direct API call, and an operator
pressing F+0 on the radio's own keypad — which leaves the station deaf with the browser showing a
live Talk button. 0158 **R1** named the firmware term as the successor and recorded that PR #6 had
merged, so it waited only on a decision and a cycle.

This is that cycle. **The radio itself now refuses.**

### Ground truth, verified rather than inherited

Three claims this cycle was told to re-confirm rather than carry forward. All three were read out of
`d086a23`, and one overturns a worry the plan started with.

| question | answer |
|---|---|
| Does the AIOC's DTR keying path route through `RADIO_PrepareTX`? | **Yes.** DTR drives `GPIO_PIN_PTT` (GPIOB pin 10) — the *same pin* the rubber button drives. There is no separate external-PTT path in this firmware: `gpio.h:78` → `app.c:1444`/`:1499-1505` → `ProcessKey(KEY_PTT)` → `GENERIC_Key_PTT` → `generic.c:192 gFlagPrepareTX = true` → `app.c:2611-2614 RADIO_PrepareTX()`. |
| Is the FM screen's `goto start_tx` a bypass? | **No**, and this was the one worth checking, because it looked like one. `generic.c:147-148` jumps *forward* to the `start_tx:` label at `:190-192`, which sets the same flag. The deaf-transmit path lands on the gate this ADR adds. |
| Does dock `REG_30` keying bypass it? | **Yes, entirely.** `dock.c:381 dock_set_tx` → `Dock_TxSet` (`uart.c:801`) → `Dock_ForceTx` (`uart.c:777`) → raw BK4819 calls. No `RADIO_PrepareTX`, no `FUNCTION_Select`, no `VfoState[]`, no battery or frequency check. |

Also confirmed: `RADIO_PrepareTX` (`radio.c:1184-1317`) contained **zero** `gFmRadioMode` references
before this cycle — the only one in the whole file is a VOX gate at `:897`. And `gFmRadioMode` is the
firmware's *only* authority on whether the BK1080 is running: there is no chip read-back anywhere.
`gIsInitBK1080` is file-static to `bk1080.c`, has no extern declaration in the tree, and is never
cleared, so it means "the register table was loaded once since boot" rather than "running".

## Decision

**Put the term in `RADIO_PrepareTX`, behind a fork-owned build flag, and report it on `0x087A` from
the same predicate that enforces it.**

### 1. One clause, appended to the chain

`RADIO_PrepareTX` is a linear `else if` chain that sets `State`, funnelling into a single
`if (State != VFO_STATE_NORMAL)` at `:1245` which calls `RADIO_SetVfoState`, plays
`BEEP_500HZ_60MS_DOUBLE_BEEP_OPTIONAL` and returns. The new clause is appended after the
`ENABLE_TX_WHEN_AM` gate and sets the same `VFO_STATE_TX_DISABLE`.

ADR 0158 placed the host's deafness check *before* its demodulator check, on severity: the two
produce different messages and an operator told only about AM would fix that and end up with a
station that now transmits and still cannot hear. **That argument does not transfer here, and the
ordering deliberately does not follow it.** In firmware both causes set one state and produce one
beep. There is nothing to order, so the clause goes where the diff is smallest.

### 2. It is a bit on `0x087A`, never a ninth byte

`0x087A` is `[status:u8][state:u8][freq_hz:u32 LE][band:u8][flags:u8]` — eight bytes, no padding, no
reserved fields. Three consumers hard-require that length, and **one of them was out of scope this
cycle**:

- `frames.py:1271-1272` raises `ValueError` on any length but 8. Since no `radio_server/` code may
  change here, a ninth byte would have made every F9 radio unparseable to the server deployed today.
- `test_dock.c`'s decoder requires `param_len == 8u`.
- `dock.c:93` drops a reply longer than `DOCK_REPLY_MAX_PARAMS` and sends **nothing**, which is
  indistinguishable from firmware that never got the command.

Bits 1–7 of `flags` were unallocated. `DOCK_FM_FLAG_FM_BLOCKS_TX = 0x02u`, and no other byte moves.

### 3. It reports blocking, not readiness — the polarity is load-bearing

`dock.c` forces `flags = 0` on every non-`APPLIED` path, and pre-F9 firmware answers `0` because the
bit does not exist. A **readiness** bit would read "will not key" in both cases, letting a lost frame
or an old radio stop a transmitter. A **blocking** bit reads "not blocked" in both — which is *true*
of a refusal that measured nothing and *true* of an F8 radio.

This is the `tx_ok` rule again, in the place it now bites: an unmeasured field must never lock a
transmitter. ADR 0158 pinned it in both directions on the host side; this pins it on the wire.

**Bit 0 keeps its meaning exactly.** It is the BK4819 demodulator, it is published, and `frames.py`
already reads it. Redefining it to mean the conjunction would have silently changed a documented bit
under a deployed host. So the second cause gets the second bit, and hosts read:

```
will_key = (flags & TX_OK) && !(flags & FM_BLOCKS_TX)
```

All four combinations are real states of some image this fork ships, and a host reading bit 0 alone
gets exactly one of them wrong — the dangerous one.

### 4. One predicate, three consumers

`Dock_BroadcastFmBlocksTx()` in the new `App/app/dock_tx_interlock.h` is read by the gate in
`radio.c`, by the flag builder in `uart.c`, and by the host tests.

F7 reported its `TX_OK` flag through `Dock_ModulationCanTx` (`uart.c:900-912`), which is a **hand copy**
of the AM gate in `radio.c:1232-1243`. Two statements of one rule with nothing keeping them in step.
That was accepted once. Accepting it twice would mean a host could be told the radio will key after
the radio had already decided otherwise — which is the exact failure this whole arc exists to prevent,
arriving through the instrument built to detect it.

The header has no firmware includes: the single symbol it needs is a plain `bool`, so it is declared
rather than included. That keeps it inside the spirit of the fork's guardrail 4 and — the reason it
matters — lets `tests/host/` compile **this exact code**, not a copy, under every preprocessor shape.

### 5. Fusion only, opt-in — **this revises the brief**

The cycle brief asked for the `ENABLE_TX_WHEN_AM` idiom. That idiom is `#ifndef`, so an *undefined*
flag leaves the gate **active** — which would have switched the interlock on in the `Broadcast`,
`Basic` and `Game` presets too. It is `#ifdef ENABLE_DOCK_FM_TX_INTERLOCK`, on in `Fusion` alone.

The reason is that **ADR 0158's "correction, not regression" argument does not reach that far.** It
was argued for a *host-controlled station* whose operator cannot hear what they are keying over and
may not be in the room. It does not transfer to a handheld operator who deliberately opened the FM
radio and deliberately pressed PTT: that person knows exactly what their receiver is doing. Extending
the interlock to editions with no dock and no host would breach the fork's guardrail 5 on behalf of
users the argument was never about.

Fusion is the build radio-server stations flash, so coverage of the actual hazard is complete.

**The opposite polarity to the AM gate is not a cost, it is the provenance showing.** The AM gate is
upstream behaviour behind an upstream flag, so it is opt-out; this is a fork addition serving the
dock, so it is opt-in. Different origins should read differently, and the `ENABLE_DOCK_` prefix is
what makes that legible at a glance. Both gates carry a comment saying so, because the obvious
"cleanup" is to make them match, and that cleanup would silently widen a behaviour change.

Guardrail 5 in the fork's `AGENTS.md` says *"nothing in the radio's own operation changes until a host
sends `0x0870`"*. **F9 breaks that on purpose** — an interlock that needed a host would not solve the
problem a host gate already fails at. The guardrail now records F9 as its one deliberate, confined
exception rather than standing as a rule the tree quietly violates.

### 6. The flag reports the build, and both compilations are pinned

Because decision 4 puts the build condition in the predicate that *both* the gate and the flag read,
this falls out instead of being maintained:

| image | broadcast FM | bit 0 | bit 1 | and it is true |
|---|---|---|---|---|
| Fusion F9 | on | 1 | **1** | the radio refuses |
| Fusion F9 | off | 1 | 0 | it keys |
| Broadcast / Basic / Game | on | 1 | **0** | it keys, exactly as F4HWN's does |
| F8 and earlier | on | 1 | 0 | it keys |

A non-interlock image reporting "not blocked" while playing broadcast FM is **correct**, because that
image will in fact key. A flag that lied on any build this fork publishes would be worse than no flag,
and a single compilation could not have caught a constant-returning predicate in both directions.

### 7. Dock keying is not covered, and this ADR will not imply otherwise

`Dock_ForceTx` bypasses `RADIO_PrepareTX`, so the `uvk5` backend's identical hazard — ADR 0158 **R3** —
**stays open**. A term in `Dock_ForceTx` was **rejected, not deferred**: `0x0850` carries no reply, so
a refusal there produces a host that keys, radiates nothing, and is told nothing. Silent no-RF is
worse than the diagnosis it replaces, and it is precisely the failure mode F3 and F5 each cost a bench
cycle to find.

There are exactly two keying authorities in this firmware. This closes the one the AIOC and the front
panel use, which is every station radio-server runs on the `baofeng` backend.

## Correcting the record

**ADR 0156 decision 6 is now stale where it says there is no bidirectional interlock.** Its words were:

> **There is no bidirectional interlock here, and this ADR does not describe one.** Broadcast FM does
> not gate TX. The interlock that exists runs one way — the key blocks FM, not the reverse […]

That was true of F8 and is false of F9. The interlock now runs both ways: a key still blocks an FM-on
(`ERR_TX`), and FM-on now blocks a key. 0156's surrounding reasoning is unaffected — `tx_ok` really is
orthogonal to the BK1080, which is why bit 1 had to be a *new* bit rather than a redefinition of bit 0.

**ADR 0158's latch changes character, and anyone reading 0158 alone will get this backwards.** Its
decision 5 recorded that the host gate fails closed and stays closed until a restart, and judged that
the right trade because a deaf transmitting station is worse than a restart. With the firmware
refusing, **that trade no longer holds**: the radio is now the thing that stops a deaf key-up, so the
host's latch is no longer a safety mechanism. It is a **UI accuracy problem** — it can refuse a key-up
that the radio would have allowed, on a station that has been hearing perfectly well since the
operator pressed EXIT.

That raises the priority of the R2 pre-key-up re-clear from "the named successor" to the thing that
should happen before the host gate causes more trouble than it prevents.

## Acceptance

**Host tests: 161 checks, 0 failures** (`make -C tests/host run`) — 155 in `test_dock.c`, up from
**144**, plus **6** in the new `test_interlock.c`, compiled three times because preprocessor state is
per-translation-unit.

**Fail-first, four runs, with what stayed green recorded rather than omitted:**

| mutation | result |
|---|---|
| `dock.c` carrying only bit 0 (an F8-era core) | **4 failures / 155.** The blanking case, the OFF-vector case and the two bit-1-clear combinations **stayed green** — recorded as *not evidence*. |
| predicate hardwired `false` | **1 failure**, Fusion compilation only. Correct: `false` is the right answer for the other two shapes. |
| predicate hardwired `true` | **5 failures across all three compilations.** |
| interlock configured without `ENABLE_FMRADIO` | CMake `FATAL_ERROR` fires. |

**A defect in the harness was found by the fail-first process itself and is recorded rather than
quietly fixed.** The `run` target used `|| exit 1`, so a failing first binary stopped the loop and the
other two compilations never ran — the third mutation initially under-reported its own evidence. The
target now runs every binary and ORs the exit status, and the mutation was re-run to get the 5.

**The honest limit:** `radio.c` and `uart.c` are not host-compilable, so no host test can prove
`RADIO_PrepareTX` *calls* the predicate. The dock checks stayed green under **both** predicate
mutations, which is that limit showing rather than a gap in the mutations. The seam is covered by a
new `check-fm-interlock` target — **a grep, not a test**, beside the existing `check-fm-restore`, and
labelled as one in the Makefile. Its own red run: rewriting the gate as a hand copy of `gFmRadioMode`
drops `radio.c`'s call-site count to 0 and fails the target.

**Goldens came from an independent oracle**, per the rule ADR 0150 set: a Python reference framer
written separately from `dock.c` that reproduced **all three** already-published F8 vectors
byte-for-byte before its new output was trusted. The new `0x0879` OFF vector was additionally
cross-checked against `ClearBroadcastFm().to_frame()`; the two agree exactly.

**The wire is otherwise untouched.** `0x0873` / `0x0877` / `0x0879` request vectors and the `0x0874` /
`0x0878` replies are pinned byte-identical by tests that were not modified. F8's published ON and
probe vectors are unchanged, because the F9 golden is a new case rather than an edit to an old one —
so radio-server's transcription of them stays accurate.

**FLASH 106,152 B / 118 KB (87.85 %)**, **+16 B** over F8's 106,136 B, **14,680 B free**. RAM
unchanged at 13,088 B (79.88 %). A `Fusion` build with `ENABLE_DOCK_FM_TX_INTERLOCK=OFF` links clean
at **exactly 106,136 B** — F8's figure to the byte — so the feature genuinely compiles out and its
entire cost is those 16 bytes.

**`uv run pytest` 2043 passed, 5 skipped — unchanged. No `radio_server/` code changed.**

**Not flashed; no hardware claim.** `BENCH.md` §9 gained a new `⚠ CONFIRM AT BENCH` item, and — this
part matters more — its **existing expectations were rewritten**, because a bench run against §9 as it
read at F8 ("Assert the AIOC's DTR line. Expected: also transmits.") would now report the feature as a
fault. Three `⚠` items inherited from F8 remain open and untouched.

Pre-release [`radio-server-f9-v5.7.0`](https://github.com/kbennett2000/uv-k1-k5v3-firmware-custom/releases/tag/radio-server-f9-v5.7.0)
(`f4hwn.fusion.v5.7.0.f9-fm-tx-interlock.bin`, sha256 `25c234c6…`).

## Consequences

- **The station is protected without the host.** A crashed server, a direct API call and the front
  panel's F+0 are all covered on a Fusion F9 radio, which is what the host gate could never do.
- **ADR 0158's host gate is now the weaker of two, and its latch is a liability rather than a
  backstop.** See the correction above; R2 rises in priority.
- **The `uvk5` backend gains nothing.** Its keying does not enter `RADIO_PrepareTX`. R3 stays open and
  is now the only uncovered keying authority.
- **The host still cannot read bit 1.** `frames.py` was out of scope, so the server keeps predicting
  from a boot-time snapshot while the radio holds a live answer. The instrument exists and nothing is
  reading it yet.
- **Upstream divergence grew by one clause in one upstream file**, confined to one preset, and
  guardrail 5 now names it.
- **`PROTOCOL.md` stops being misleading about the OFF direction**, and three level tables that all
  stopped at F7 now reach F9.

## Out of scope

**R1 — the host decodes bit 1.** The successor this cycle most obviously implies. `frames.py` gains
`FLAG_FM_BLOCKS_TX` and `BroadcastFmReply` a `fm_blocks_tx` property; `tuner.py` records it where it
already records `tx_ok`. Until then `0x087A` carries an answer no host reads.

**R2 — the pre-key-up re-clear, now more urgent than 0158 judged it.** Unchanged in shape: gate it on
the **earned** `Capability.CLEAR_BROADCAST_FM` so radios that cannot answer pay a frozenset membership
test rather than `SetVfoTuner`'s 3.0 s timeout. What changed is the justification — it is no longer
only about closing the front-panel blind spot, it is about not refusing key-ups the radio would allow.

**R3 — dock `REG_30` keying is still ungated**, and after this cycle it is the *only* ungated keying
authority. Decision 7 rejects the obvious fix. The honest fix is a reply-bearing key opcode, which is
a protocol addition rather than a term.

**R4 — `Dock_ModulationCanTx` is still a hand copy** of the AM gate in `radio.c`. F9 fixed this for
its own flag and left F7's as it found it. Folding `TX_OK` onto the same shared-predicate pattern is a
small, purely mechanical firmware cycle, and it removes the last place where a reported flag and the
behaviour it reports are written twice.

**R5 — nothing checks that the fork and `frames.py` stay byte-compatible.** ADR 0148 recorded this as
open and it stayed open through F9, which added a wire bit by hand on one side. The cross-check that
caught nothing this time is the one that will matter when it does.

**R6 — the fork's CI is dead weight.** It runs no host tests, builds the `Custom` preset rather than
`Fusion` (so it never compiles the dock at all), uploads an artifact path that a commented-out packing
step stopped producing, and calls a script that passes `docker run -it` on a TTY-less runner. Found
while looking for a place to run the new tests; not fixed here.

**R7 — `docs/api.md`'s prose remains unguarded by any test.** Carried unchanged.

Everything carried by ADR [0158](0158-the-host-refuses-to-key-a-station-that-cannot-hear-itself.md)
stays carried, except its R1, which this ADR closes, and its R6, which the published OFF vector closes.

## Source of truth

Fork `kbennett2000/uv-k1-k5v3-firmware-custom`, branch `f9-fm-tx-interlock`, cut from `origin/main` at
**`d086a23`** (F8 merged), commit **`d903881`**, PR
[#7](https://github.com/kbennett2000/uv-k1-k5v3-firmware-custom/pull/7). Upstream
`armel/uv-k1-k5v3-firmware-custom` pinned at `3bd3ebb` (v5.7.0), Apache-2.0.

Firmware claims in this ADR were read from that tree. **No firmware was flashed in this cycle and no
bench result is claimed.** The image was built twice from `d903881` and differs in exactly two bytes
(the embedded build clock); the released artifact was rebuilt *after* committing, because the
abbreviated commit hash is embedded too and a pre-commit build carries one that resolves to nothing.
