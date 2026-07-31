# 0157 — A station that cannot hear itself: the server clears broadcast FM and knows it did

- **Status:** Accepted
- **Date:** 2026-07-31
- **Supersedes:** nothing
- **Related:** [0156](0156-a-deaf-station-can-still-transmit.md) (the firmware half),
  [0155](0155-a-restart-must-not-inherit-the-demodulator.md) (the assert this extends),
  [0150](0150-the-host-learns-to-listen-to-am.md) (the capability/status shape it follows),
  [0153](0153-one-frame-must-not-take-the-relay-down.md) (why the two asserts fail independently),
  [0149](0149-a-new-opcode-is-cheaper-than-a-changed-one.md) (the two-repo split)

## Context

[ADR 0156](0156-a-deaf-station-can-still-transmit.md) added firmware `0x0879`/`0x087A`, reaching the
UV-K5's **second receiver** — a BK1080 commercial-FM chip (64–108 MHz) beside the BK4819 that every
other opcode drives. In doing so it made a state *reachable* that this server had no way to leave.

When the BK1080 runs it holds the speaker line the AIOC listens on. The station hears **nothing** on
its own channel. And it transmits normally throughout: `RADIO_PrepareTX` has no broadcast-FM term at
all — re-verified for this cycle, the only `gFmRadioMode` reference in the whole of `App/radio.c` is
the VOX gate at `:897`. So the automatic station ID that guardrail 5 requires goes out into a
channel nobody is monitoring. Guardrail 5 is met in form and defeated in substance.

The firmware **persists that state to flash behind the host**. `app.c:1761-1767` calls `FM_Start()`
— write and all — about five seconds after any squelch close, armed by `functions.c:106-108`. So a
host crash or an unplugged cable mid-FM leaves `CURRENT_STATE = 3`, and the radio boots straight
back into broadcast FM with no host present to stop it. That is carried finding 1 of ADR 0156, whose
fix was explicitly assigned to this cycle.

Hence the ordering of the arc: **the server must be able to clear this before anything can set it.**
This cycle ships an OFF path and, deliberately, no way whatsoever to turn broadcast FM on.

### The firmware this is written against is not merged

Verified first, and it changes several decisions below:

| claim | result |
|---|---|
| fork `origin/main` | **`4e1d9dc`** |
| does it contain F8? | **No.** `git merge-base --is-ancestor 5f4c581 origin/main` → false; `0x0879` has 0 hits in its history and `DOCK_CMD_SET_FM` 0 hits in its tree |
| where F8 lives | branch `f8-dock-broadcast-fm` @ **`5f4c581`**, **PR #6 open, unmerged, unflashed** |

**All goldens in this cycle were read from `5f4c581`, never from `main`** — reading the default
branch would have silently produced nothing at all rather than an error. The consequence that
matters: **no radio in existence runs F8 today**, so `0x0879` is dropped in silence by every radio
this server will meet until someone merges and flashes. That is the normal case, not a fault, and
decisions 2 and 5 are shaped by it.

### The goldens, and one gap in them

The four published vectors were transcribed from `5f4c581:tests/host/test_dock.c` and independently
re-derived, both directions, as [ADR 0150](0150-the-host-learns-to-listen-to-am.md) requires:

| vector | opcode | params | CRC slot |
|---|---|---|---|
| cmd ON | `0x0879` | `01 00 b5 26 06 00` → ON, 103 200 000 Hz, band 0 | real `4fc5` |
| reply | `0x087A` | `00 01 00 b5 26 06 00 01` → APPLIED, state **ON**, 103.2 MHz, band 0, **TX_OK** | dummy `ffff` |
| empty probe | `0x0879` | param_len 0 | real `a318` |
| probe refusal | `0x087A` | `01 ff 00 00 00 00 ff 00` → ERR_SHORT + all three sentinels | dummy `ffff` |

The reply vector is ADR 0156's thesis in bytes — `state=ON` and `TX_OK` set in the **same frame** —
and is pinned as a test for exactly that reason.

**The gap: there is no published OFF vector.** `PROTOCOL.md` publishes those four; the fork's OFF
tests (`test_dock.c:1252`, `:1272`) assert behaviour rather than bytes. So the one frame this server
actually sends is the one the fork never pinned. It is **derived** here rather than transcribed,
labelled as such in the test, and cross-checked three ways — against the independent reference
framer, by decoding the payload back field by field, and against the firmware's own
`DOCK_SET_FM_PARAM_LEN`. Carried as a finding: `PROTOCOL.md` should gain an OFF vector in a firmware
cycle.

## Decision

### 1. The request can only build OFF; the reply parses every state

`ClearBroadcastFm` takes **no action parameter at all**. The wire carries three actions — OFF, ON,
TUNE — and this server ships a class that can express one, so *"this server cannot turn broadcast FM
on"* is a property of the code rather than something a reviewer has to keep verifying. A test asserts
`ClearBroadcastFm(1)` raises `TypeError` and that the class has no `unpack`.

`BroadcastFmReply` decodes **every** state including ON. The asymmetry is deliberate and worth
naming: **you can always learn the radio is in broadcast FM; you can only tell it to stop.** A
decoder that could not represent ON would be unable to report the very fault this cycle exists to
surface.

Consequences of the asymmetry, both stated in the docstrings rather than left to be discovered:

- **Only the reply is registered in `_DISPATCH`.** This is not bookkeeping. Without it `0x087A`
  decodes to a `RawMessage`, the tuner's `isinstance` match never fires, and every clear times out
  against a radio that answered correctly — a total failure indistinguishable from firmware that
  lacks the command. Pinned by a test.
- **The request is deliberately absent from `_DISPATCH`**, because an OFF-only class cannot `unpack`
  an ON frame and a decoder that silently mangled one would be worse than one that declines. The
  host is never a radio, so it never needs to decode an inbound `0x0879`. `SetVfoProbe` is the
  existing precedent for a frame class outside the table.

### 2. The capability is **earned by a reply**, never claimed from configuration

`Capability.CLEAR_BROADCAST_FM` joins `Capability` and `CAT_CAPS`, but `SetVfoTuner.capabilities()`
includes it **only after a radio has actually answered `0x087A`**.

Every other member of that enum is a static claim derived from config. `SET_MODULATION` already
stretches that — it asserts "F7 firmware" on the strength of a tuner mode — and a static
`CLEAR_BROADCAST_FM` would stretch it past breaking: **F8 is unmerged and unflashed, so every
station on earth would advertise a command no radio can execute.** That is a hardware fact asserted
from memory, which is guardrail 1 exactly, and the fact that the analogous claim is already half
wrong for `SET_MODULATION` is a reason not to double it rather than a licence to.

Two details settle the design:

- **Any `0x087A` earns it, including a refusal.** A radio that refuses has still demonstrated the
  firmware has the opcode, which is precisely what the capability claims. Only *silence* earns
  nothing, because silence is what pre-F8 firmware and an unplugged radio both produce.
- **Re-earned on every successful reply, not once at boot.** A boot-only probe would leave a radio
  that was switched off at startup missing the capability *for ever*, and unlike ADR 0155's unknown
  modulation there is **no operator remedy** — a preset tap can re-assert a demodulator, but nothing
  an operator can do conjures a missing capability back. The flag is set inside
  `clear_broadcast_fm`, so the next cycle's route re-earns it too. A backend re-select also
  re-probes, because [`RadioHolder.rebuild`](0076-live-backend-switch.md) and `_restore` construct a
  fresh backend and therefore a fresh tuner.

**It is the first capability with no route**, and that is stated in `docs/api.md` rather than left
to be noticed. It exists to say what a radio has *proved* it can do — the F8-detection signal a host
otherwise has no way to get — not to gate a request. There is nothing to gate: the server clears
broadcast FM at startup and has no way to turn it on.

`EepromTuner.clear_broadcast_fm` **raises** `UnsupportedCapability`, mirroring its `set_modulation`.
Stock firmware has no `0x0879` case, and returning would report a cleared receiver on a station that
is still deaf — the no-signal/no-measurement confusion in the one place where it means an operator
trusts a channel nobody is listening to (guardrail 3).

A structural benefit worth recording because it was **predicted and then checked rather than
assumed**: because the capability is earned, `test_setvfo_advertises_the_tuning_capabilities` and
`test_the_three_tuners_no_longer_advertise_the_same_set` — both exact-set assertions — **pass
unchanged**. A static member would have required editing both. The honest design was also the
smaller diff.

### 3. Status is a **tri-state block**, not two flat fields

```jsonc
"broadcast_fm": null                                  // the server does not know
"broadcast_fm": { "on": false, "hz": 103200000 }      // it knows: off, and where it would resume
"broadcast_fm": { "on": false, "hz": null }           // ERR_NO_HAL — no such receiver in this image
"broadcast_fm": { "on": true,  "hz": 103200000 }      // it refused to stop. Deaf, and transmitting.
```

`PaState` is the in-repo precedent and the argument is the same: the two values are only meaningful
together, so grouping them makes `on=None, hz=<a number>` unconstructible. On the OFF leg the
firmware still reports a real frequency — the BK1080 remembers its tuning and `Dock_SetFm` reads it
straight out of `gEeprom.FM_FrequencyPlaying` regardless of state — so `hz` is *the frequency the
receiver would resume on*, not what anything is hearing. Read alone it looks like a station happily
monitoring 103.2 MHz. The block makes that impossible to read alone.

**The block itself is tri-state, not just its fields.** A `null` block means the server does not
know; `{"on": false}` means it does, and the answer is no. Those must never render the same:
"we never asked" reading identically to "verified hearing" is precisely how a deaf station gets
trusted, and is the failure this arc has spent nine cycles removing.

The status→tri-state mapping is the load-bearing part, so it is explicit rather than implicit:

| reply | block | why |
|---|---|---|
| APPLIED, state=0 | `{on: false, hz: N}` | read back off |
| APPLIED, state=1 | `{on: true, hz: N}` | it refused to stop — real, and dangerous |
| **ERR_NO_HAL (5)** | `{on: false, hz: null}` | see below |
| ERR_BUSY (2), ERR_TX (8), ERR_SHORT (1) | `null` | genuinely unknown |
| ERR_FIELD (4), ERR_BAND (6) | `null` | unreachable on the OFF leg — dead branches, noted not omitted |
| timeout / silence | `null` | pre-F8 firmware, or the radio is off |

**`ERR_NO_HAL` is a definitive negative, not an unknown**, and treating it as one would throw away
the only status on this wire that is a certainty. It means the image was built without
`ENABLE_FMRADIO` — there is no BK1080 driver in it — so broadcast FM **cannot** be running. ADR
0156's acceptance already established this is a real firmware shape: that build links clean.
`hz` stays `null`, because there is no receiver to have a tuning and the firmware blanks the field
anyway.

One more shape worth stating: a stuck receiver (`APPLIED` with `state=1`) is **not** raised as a
`TuneError`. The frame was answered and the state is *known* — it is `on=True`. Raising would
discard a measurement in favour of an exception, and the caller needs the measurement more. Only a
refusal or silence raises, because only those leave the state genuinely unknown.

### 4. `tx_ok` is read off `0x087A` and deliberately thrown away

The reply carries the bit, and this server records it nowhere.

Writing it here would break the ADR 0155 invariant that `tx_ok` is `None` whenever `modulation` is —
pinned at `tests/test_aioc_baofeng_tuning.py:669-671` and `:750-753` — and would leave
`_refuse_if_tx_disabled` reading a flag from a *different frame* than the demodulator its error
message names. It is carried on the wire so a host holding only this frame can see the dangerous
combination; this server sees the same fact via `0x0878` instead. Recorded as a deliberate
non-decision, with a test that pins `tx_ok` still `None` after a successful clear.

### 5. Two asserts, broadcast-FM first, each with its own `try`/`except`

`_assert_boot_broadcast_fm()` runs before `_assert_boot_modulation()` in `AiocBaofeng.__init__`.

**Independent handlers, and this is the load-bearing half.** One shared `try`/`except` would let a
failure of the first skip the second — [ADR 0153](0153-one-frame-must-not-take-the-relay-down.md)'s
lesson in the place it would bite next, and it would bite *universally*: F8 is unmerged, so
`0x0879` times out on every radio today, and a shared handler would silently cost every station the
ADR 0155 demodulator assert as collateral. A test drives exactly that case.

**Order is severity-first:** a station in broadcast FM hears nothing at all, which is strictly worse
than one on the wrong demodulator, so the worse fault is repaired first. Both orders converge on the
same end state — `Dock_FmOff` calls `RADIO_SelectVfos(); RADIO_SetupRegisters(true)`, which
re-applies the demodulator from `gEeprom.VfoInfo[]` where `Dock_SetModulation` stores it for **both**
VFOs — so this is a choice about which fault survives a partial failure, not a correctness
requirement.

**Two orderings arguments were considered and rejected on the evidence.** Both are recorded, because
a wrong reason in the record is worse than no reason — the next cycle inherits it:

- *"Modulation-first re-deafens the radio, because `Dock_SetModulation` calls
  `Dock_RestoreFmAudio()`."* **Wrong.** That helper restores audio for a BK1080 that is *already*
  running; it preserves the deafness, it cannot create it. This was the cycle's own first argument
  and it did not survive being checked.
- *"OFF-first stalls the following `0x0877` behind the OFF leg's flash erase, and ADR 0131's rule
  says the link drops frames arriving while the firmware is busy."* **Also wrong.** `dock.c` calls
  `dock_send_set_fm_reply` as its **last** statement, after `hal->set_fm` returns
  (`5f4c581:App/app/dock.c:297`), and `SetVfoTuner` is synchronous request/reply — it does not send
  the next frame until the reply lands. The erase is absorbed by the round trip rather than
  colliding with a neighbour. It lengthens the OFF exchange; it drops nothing.

**The gate is `SET_MODULATION`, not `CLEAR_BROADCAST_FM`**, and the circularity is the reason: the
latter is *earned by this frame's reply*, so gating on it would mean never sending the frame that
earns it. The gate asks "is this a fork-firmware tuner, so is it worth trying" — a heuristic whose
failure is handled two lines later — rather than claiming F8. It is still a **capability** gate and
never `hasattr`: every tuner in the package has a `clear_broadcast_fm` and one of them exists only
to raise (guardrail 3).

**Log level splits on what silence means.** Silence is INFO, because it is indistinguishable from
pre-F8 firmware and therefore the norm on every radio today; a warning on every boot of every
station would train operators to ignore the ADR 0155 warning printed beside it, which is rare and
means something. A radio that *answers* and refuses to leave broadcast FM gets a WARNING naming the
frequency and the remedy, because that one is a real fault with a real action behind it.

### 6. The startup round trip moves off the event loop — and boot is a different case

Verified separately rather than assumed, because the two cases deserve different sentences:

- **Boot does not block a loop.** `build_app` (`api/app.py:2175`) is plain `def` and calls
  `build_radio` at `:2211`; `__main__.py:88` hands the finished app to uvicorn *afterwards*. There
  is no event loop yet. That cost is **startup latency**, and it is accepted.
- **A backend switch does block one, and pays it twice.** `RadioHolder.rebuild` called
  `self._radio_factory(...)` synchronously inside an `async def`, and `_restore` calls it again on
  the rollback tail. On firmware older than F8 — every radio — the clear runs to its full 3 s
  deadline, freezing the API and stalling the RX pump, which from outside reads as the server having
  crashed.

Both factory calls now go through `asyncio.to_thread`, the idiom this repo already uses for blocking
radio work at `api/app.py:1390` (`apply_preset`), with the same justification. `_restore` becomes
`async def`; it is private with exactly two call sites, both already inside `rebuild`'s lock.
`test_radio_holder.py`'s two-concurrent-rebuilds test is the existing guard that the lock still
serialises them.

**The timeout was not shortened and no config key was added.** Any shorter number is a guess, and it
would truncate exactly the case that matters — a radio genuinely in broadcast FM, whose OFF leg pays
the unmeasured flash erase ADR 0156 flagged as a bench item. During the round trip the status block
is `null`, which is **true**, so the tri-state already covers the window.

### 7. No UI, and the reason is not scope

A status row would be **actively misleading**. This server never re-reads the state: `broadcast_fm`
is a record of what it asserted at startup, so an operator pressing the radio's own FM key afterwards
is invisible to it, and the row would keep saying "off" while the station is deaf. That is a
confident false negative where today there is no claim at all — the no-signal/no-measurement
confusion in a new place. It is documented as a limitation in `docs/api.md` rather than rendered.

## Acceptance

- **Fail-first, twice, both recorded.**
  - **Run 1, before any `radio_server/` change: 3 collection errors.** Import-level, and therefore
    **weak evidence** — it proves the names are absent, not that any behaviour is wrong. Recorded as
    such rather than counted as the cycle's red.
  - **Run 2, after the wire layer landed: 18 failed, 1996 passed, 5 skipped.** Behavioural, and for
    the right reasons: the named fail-first, the read-back and blanking rules, the earned capability,
    the `tx_ok` non-decision, both refusal shapes, and `test_docs_contract` catching the new
    capability string.
- **Tests that stayed GREEN in the red run, named because they are not evidence:** every new frames
  golden (they exercise the codec written in the same step, so they never had a behavioural red to
  fail on), plus `test_a_failed_broadcast_fm_clear_does_not_skip_the_demodulator_assert`,
  `test_an_eeprom_tuner_is_never_asked_to_clear_a_receiver_it_cannot_reach`,
  `test_a_plain_uv5r_is_not_asked_about_a_receiver_it_does_not_have` and
  `test_clearing_broadcast_fm_arms_no_transmit_lockout` — all four of which passed **vacuously**,
  because nothing was being sent yet. They are regression guards from here on, not proof of this
  cycle.
- **Green: `uv run pytest` 2014 passed, 5 skipped** (1981/5 before — **+33 tests, no regressions**).
- **`npx vitest run` 11 files, 101 tests passed, unchanged** — no UI change, so this is a pure
  regression check.
- Both exact-set capability assertions pass **unchanged**, as decision 2 predicted; verified by diff
  rather than asserted.
- Every ADR filename referenced here was resolved against `docs/adr/` **before** the links were
  written.
- **No firmware, no flashing, no bench claim, and no route that can turn broadcast FM on.**

## Consequences

- A server restarted against a radio left in broadcast FM now clears it and reports what the radio
  said. The crash-leaves-`CURRENT_STATE=3` residual carried from ADR 0156 is closed for the restart
  path.
- Until F8 merges and an operator flashes, **every** station logs one INFO line at boot saying the
  state is unknown, and reports `broadcast_fm: null`. That is the honest answer and it is the
  expected one.
- One new local-fake obligation, discovered the hard way: `ModulationTuner` in
  `tests/test_aioc_baofeng_tuning.py` advertises `SET_MODULATION`, so the capability gate asks it to
  clear broadcast FM. A fake that lacks the method raises `AttributeError` **out of a constructor**,
  which no `(TuneError, OSError)` handler catches. The same trap applies to `FakeSetVfoRadio`, whose
  `request()` falls through to a `SetVfoReply` builder that reads `msg.rx_hz`. Both were extended
  before the backend change, in that order.
- `RadioHolder._restore` is now `async`. Private, two call sites, but it is a signature change.

## Out of scope

Named rather than omitted, so the next cycle does not rediscover them.

**R1 — the transmit interlock, and an open question this cycle does not answer.** `gFmRadioMode`
does not gate `RADIO_PrepareTX`, so a **host-side TX gate is the only protection against
transmitting while deaf, and it fails open** — on a host crash, and on any direct API call that
reaches the keying path without passing it. Where the interlock belongs is genuinely unresolved:

- *In the firmware* — a broadcast-FM term in `RADIO_PrepareTX`. Closes the failure-open hole
  completely, including for the front panel. But it also changes front-panel behaviour for an
  operator who deliberately wants to listen to FM and key occasionally, and it diverges the fork
  further from upstream, which [ADR 0148](0148-the-firmware-is-a-product-too.md) treats as a real
  running cost.
- *In the host alone* — cheap, testable, no firmware change, and wrong the moment anything keys
  without going through it.
- *Both* — defence in depth, and two places to keep in agreement.

**R2, and it sharpens R1.** `_reassert_channel` already re-asserts the *modulation* before every
key-up, so `tx_ok` is measured milliseconds before the line goes high. This cycle gives the **more**
dangerous state only a boot-time snapshot. Clearing broadcast FM pre-key-up is the host half of R1
and is deliberately not built here — but the asymmetry with the sibling code in the same function is
the argument for building it, not an oversight. Its cost is lower than it looks: `Dock_FmOff`'s
`SETTINGS_WriteCurrentState` short-circuits on `memcmp`, so a clear on a receiver that was never on
writes no flash.

**R3 — the `uvk5` backend has the identical hazard and no assert.** It advertises neither
`SET_MODULATION` nor this capability, has no boot assert, and holds full control (`0x0870`), which
per ADR 0156 makes `0x0879` answer `ERR_BUSY` anyway. A `uvk5`-backend station left in broadcast FM
is deaf and structurally unfixable over this wire today.

**R4 — firmware-version negotiation.** The empty-`0x0879` probe is published and byte-pinned here,
and it is the real fix for paying a 3 s round trip on every construction against firmware that
cannot answer. Moving the call off the event loop does not make the round trip unnecessary.

**R5 — the fork publishes no OFF vector.** `PROTOCOL.md` should gain one; this cycle derived it.

**R6 — `docs/api.md`'s prose counts are unguarded.** The contract test checks only that each
capability *string* appears. The "nine members of `CAT_CAPS`" sentence and the 501 handler list were
updated by hand here and nothing would have caught it if they had not been.

**Carried unchanged from ADR 0156 and earlier:** `doctor` builds no tuner; the EventHub does not
exist at backend construction, so no boot-time diagnostic can reach the operating log; the
AM-mismatch branch; `POST /mode`'s 500; `MockRadio.set_mode`; `GET /presets` omitting `modulation`;
the `REST_PATHS` drift; the three websocket slot leaks; and ADR 0156's three
`⚠ CONFIRM AT BENCH` items, including the OFF-leg erase duration whose only source is a firmware
comment.

## Source of truth

- Firmware read at **`5f4c581`** on branch `f8-dock-broadcast-fm` of
  `kbennett2000/uv-k1-k5v3-firmware-custom` — **PR #6, open and unmerged**. `origin/main` is
  `4e1d9dc` and contains none of it.
- `App/app/dock.h`, `App/app/dock.c` (dispatch at `:297`), `App/app/uart.c` (`Dock_SetFm`,
  `Dock_FmOff`), `App/radio.c` (`RADIO_SetupRegisters` at `:812`, `RADIO_PrepareTX`), `App/app/app.c`
  (`:1761-1767`), `App/functions.c` (`:106-108`), `tests/host/test_dock.c` cases 39-41.
- Host side: `radio_server/backends/uvk5/frames.py`, `radio_server/backends/uvk5/tuner.py`,
  `radio_server/backends/base.py`, `radio_server/backends/aioc_baofeng.py`,
  `radio_server/backends/mock.py`, `radio_server/api/holder.py`.
