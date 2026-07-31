# 0156 — A deaf station can still transmit: broadcast FM over the dock (F8)

Status: Accepted

Extends the dock wire protocol established in ADR [0119](0119-uvk5-v3-dock-protocol-port.md) and
[0140](0140-the-first-key-is-always-lost.md), and specified for outside readers by ADR
[0148](0148-the-firmware-is-a-product-too.md). Follows the split ADR
[0149](0149-a-new-opcode-is-cheaper-than-a-changed-one.md) set: **firmware only — no code under
`radio_server/` changes this cycle.** The host side is its own cycle, against a firmware that
exists.

## Context

The UV-K5 carries a **second receiver**. The BK1080 is a commercial-FM chip covering 64–108 MHz,
sitting on the same I²C bus as the BK4819 that every other opcode in this protocol drives, sharing
the antenna front end and the audio amplifier and nothing else. `0x0850`/`0x0851` cannot reach it:
those are BK4819 register access and the BK1080's registers are not in that address space. It is
compiled into the Fusion image already — `ENABLE_FMRADIO` is `true` at `CMakePresets.json:193`, and
`BK1080_Init`, `FM_Start` and `gFmRadioMode` are all in the linked map.

So the capability is on the hardware, in the image, and unreachable from the server. That is the
same shape ADR 0149 opened and ADR 0150 closed one level down.

Nothing in radio-server has ever mentioned it: **`BK1080`, "broadcast FM", "commercial FM" and
"88-108" have zero hits** across ADRs, code, docs, `HANDOFF.md` and config. `"WFM"` appears once, as
a CHIRP import alias, and `presets.VALID_MODES` is `frozenset({"FM", "NFM"})` — bandwidth, not a
band. This is new ground rather than a gap in something already modelled.

### What it costs the station, which is the thesis

Turning this on puts the BK1080 on the speaker line — **the line an AIOC cable listens on**. The
station stops hearing its own channel.

It does not stop transmitting. That was checked rather than assumed, and the check overturned the
expectation this cycle started with:

- `RADIO_PrepareTX` (`App/radio.c:1189`) has no broadcast-FM term anywhere in its refusal chain. Its
  only `gFmRadioMode` reference in the whole file is a VOX gate at `:897`.
- `MAIN_ProcessKeys` (`App/app/main.c:1030`) refuses every front-panel key while `gFmRadioMode` is
  set — **except `KEY_PTT` and `KEY_EXIT`, whitelisted by name**.
- `GENERIC_Key_PTT` (`App/app/generic.c:147`) is `if (gScreenToDisplay == DISPLAY_FM) goto start_tx;`
  — an explicit jump to *start transmitting*, commented "listening to the FM radio .. start TX'ing".
- `FUNCTION_Transmit` (`App/functions.c:158-160`) does not abort; it calls `BK1080_Init0()` and keys.

**So a radio playing broadcast FM transmits normally, and hears nothing but broadcast FM.** Under
guardrail 5 the transmission it cannot hear itself make includes the automatic station ID. This is
strictly worse than the AM fault the 0149→0155 arc spent five cycles on: AM produced *silence over
the air*, which is at least detectable at the far end. This produces a perfectly good transmission
into a channel the station cannot monitor — a controller talking over someone it has no way to know
is there.

That hazard is why `state` is on every `0x087A` reply rather than only on request, and it is why
this ADR is named after it.

### The opcode census, run before a number was picked

ADR 0149's standing consequence is that the census lives in three places that do not agree and
nothing reconciles them. It still does. All three were read:

| Source | Says |
|---|---|
| ADR [0111](0111-uvk5-dock-transport-and-control-path.md):52-53 | the classic Dock's extras — `0x0871` exit, `0x0872` modulation, `0x0873/4` backlight, `0x0875/6` AM emulation |
| ADR [0119](0119-uvk5-v3-dock-protocol-port.md):43-46 | what this fork ported (`0x0850/1`, `0x0870/1`) and what it dropped |
| `radio_server/backends/uvk5/frames.py` `DockCommand` | the live enum |

Union of every number any of them claims: `0x0514`, `0x0801`, `0x0803`, `0x0808`, `0x0809`,
`0x0850`, `0x0851`, `0x0860`, `0x0861`, `0x0870`, `0x0871`, `0x0872`, `0x0873/4`, `0x0875/6`,
`0x0877/8`, `0x0888`, and the replies `0x0515`, `0x0908`, `0x0951`, `0x0961`, `0x0988`.

**`0x0879`/`0x087A` appear in none of them**, and grep confirms neither number occurs anywhere in
either repository. `0x0875/6` stay skipped: claimed by source 1, unverifiable from either tree, and
therefore not free (guardrail 1). Taken.

Two corrections to the record while it was open. **The citation `frames.py:113-140`, carried in ADR
0149, ADR 0150 and `docs/HANDOFF.md`, is stale** — the enum grew to `frames.py:96-165` when
`SET_MODULATION` landed. And the census is *still* three-way unreconciled; this cycle read it and
allocated around it, as the last one did, and did not fix it.

## Decision

### 1. A new opcode, because it is a different chip

`0x0873` and `0x0877` drive the BK4819. Broadcast FM is not a `DockModulation` value and could not
become one: `MODULATION_FM` is what the BK4819 demodulates, and the BK1080 is a separate receiver
that does not appear in that enum at all. There is no frame to extend here — only a frame to add.

### 2. `0x0879` carries Hz and **refuses** what it cannot tune, rather than rounding

```
0x0879  [action:u8][freq_hz:u32 LE][band:u8]                       6 bytes
0x087A  [status:u8][state:u8][freq_hz:u32 LE][band:u8][flags:u8]   8 bytes
```

The BK1080 tunes on a 100 kHz raster — `BK1080_SetFrequency` computes a channel as the frequency
minus the band's low limit in 100 kHz units, and `gEeprom.FM_FrequencyPlaying` is a `uint16` on that
scale. The wire carries plain Hz anyway, as `0x0873` does, and the firmware converts.

**It converts by refusing.** Anything off the raster comes back `DOCK_FM_ERR_FIELD`, exactly as
`0x0877` refuses an unaccepted modulation and `0x0873` refuses an out-of-range field. `0x0873` does
silently truncate sub-10 Hz detail, and the difference is the whole argument: 10 Hz of a repeater
channel is nothing, while 100 kHz of the broadcast band is **a whole adjacent station**. Silent
substitution of a nearby value for the one that was asked for is the fault class this protocol has
spent the 0149→0155 arc removing; it is not being reintroduced to save two bytes.

The reply reports the frequency the radio is **actually on**, in the same Hz, read back from the
firmware's own state — so the raster behaviour is visible to a host that never opens `PROTOCOL.md`.

**The band is on the wire because the firmware's field for it would clamp silently.**
`gEeprom.FM_Band` is declared `uint8_t FM_Band : 2` (`App/settings.h:194`). Assigning `4` yields `0`
— a clamp performed by the assignment operator itself, no diagnostic anywhere, leaving the radio on
87.5–108 while the host believes otherwise. So the band number is range-checked in `dock.c` before
any binding sees it (guardrail 2).

Out-of-band is also a **safety** requirement, not only doctrine: `channel = frequency - loLimit` is
`uint16` arithmetic with no guard, so a frequency below the band floor underflows into a huge
channel number.

### 3. The validation splits across the seam, deliberately

`dock.c` owns what the wire can decide: length, full-control, the dock's own key, the action value,
the band **number**, and the raster. The HAL owns what only the radio knows: the band **limits**
(they live in `BK1080_GetFreqLoLimit`/`HiLimit`, and a second copy of a hardware table in `dock.c`
would be a fact maintained in two places), whether the receiver was already running, and the radio's
own `FUNCTION_*` state. This is the same split `0x0873` already draws between its field checks and
its `Dock_FreqInBand` check.

### 4. Status numbers are `0x0874`'s, holes and all — plus two that are new to the shared table

`0` APPLIED · `1` ERR_SHORT · `2` ERR_BUSY · `4` ERR_FIELD · `5` ERR_NO_HAL · `6` ERR_BAND ·
**`8` ERR_TX** · **`9` ERR_OFF**. `3` (DIRECTION) and `7` (TONE) cannot arise here and are left
unused rather than renumbered.

`8` and `9` belong to the shared table, not to this opcode — the same way `6` and `7` were
`0x0873`'s alone and are still in it. A host decoding a `0x0874` or `0x0878` can never see them,
because neither command can produce them.

`6` is reused rather than invented: it already means "outside every band", which is exactly what an
out-of-band FM frequency is.

### 5. Blanking uses a different sentinel per field, because the fields disagree about what is real

`0x0878` established that a refusal must never ship a plausible claim, and that copying `0x0874`'s
blank-to-zero literally would have answered a refusal with "you are on FM". The same reasoning
applied field by field here gives **three different answers**:

| field | blanks to | why |
|---|---|---|
| `state` | `0xFF` | `0` is real — it is OFF. Blanking to `0` answers a refusal with "the receiver is idle", a specific and possibly wrong claim. |
| `band` | `0xFF` | `0` is real, and it is the band nearly every host actually wants. |
| `freq_hz` | `0` | `0` is *not* real — no band's low limit is anywhere near it. Matches `0x0874`'s frequencies for the identical reason. |
| `flags` | `0` | matches `0x0878`. |

Enforced in `dock.c` on every non-APPLIED path, never left to the binding (guardrail 3). The host
test drives a fake that deliberately writes `off, band 0, 103.2 MHz, and you can transmit` on its
refusals — every one of those the answer a host most wants to hear — so `dock.c` must overwrite them.

### 6. `tx_ok` is carried, and it is **orthogonal** — saying so is the point

`flags` bit 0 is `0x0878`'s `DOCK_MOD_FLAG_TX_OK`: same bit, same meaning, fed by
`Dock_ModulationCanTx(gEeprom.VfoInfo[0].Modulation)`.

This cycle expected broadcast FM to disable TX, and to get most of the host side free from that.
**It does not, and the expectation was wrong in the dangerous direction.** The flag reports the
BK4819 demodulator's keyability, which the BK1080 does not touch — so it answers exactly what it
would have answered without F8. It stays in the reply because a host holding only this frame must be
able to read `state = ON` and `tx_ok = true` off it together. That combination is not a
contradiction to be explained away; it is the actual state of this radio, and it is the hazard.

**There is no bidirectional interlock here, and this ADR does not describe one.** Broadcast FM does
not gate TX. The interlock that exists runs one way — the key blocks FM, not the reverse — and it
has two halves (below).

### 7. Refused while keyed — one interlock, two halves, and not an exception to `0x0873`/`0x0877`

Those two apply mid-over because **their state survives the over**. This one does not:
`FUNCTION_Transmit` calls `BK1080_Init0()` on key-up, so an FM-on applied mid-over would be torn down
immediately and the read-back would report `ON` for a receiver already being shut off. That breaks
the `0x0874` doctrine, which is the argument — "consistency with the other opcodes" is not.

The radio's own `ACTION_FM` guard is `gCurrentFunction != FUNCTION_TRANSMIT && != FUNCTION_MONITOR`
(`App/app/action.c:402`). **Both terms are mirrored**, MONITOR included rather than silently dropped:
it holds the audio path open for the operator, and taking the speaker from someone who is listening
is the same discourtesy as taking it mid-over.

But that guard **cannot see the dock's own key**. `Dock_ForceTx` writes REG_30 and never enters
`FUNCTION_Select`, so mid-dock-over `gCurrentFunction` still reads FOREGROUND or RECEIVE. Hence two
halves reporting one status: `dock.c` refuses on `ctx->tx_on` (the dock's REG_30 keying — pure state
it already owns, and therefore the one new refusal a host test can prove), `uart.c` refuses on
`FUNCTION_TRANSMIT`/`FUNCTION_MONITOR` (the PTT pin and the front panel).

Without both, an FM-on mid-over raises the speaker amplifier into a live microphone and flips the LNA
GPIOs mid-transmission.

### 8. OFF is never refused for a field it ignores

An OFF carrying a stale off-raster frequency or an out-of-range band still turns the receiver off —
both of which are refused outright on an ON. Turning it off is how a host gives the station its ears
back, and the direction that restores the radio must always stay available. That is ADR
[0151](0151-a-failed-key-up-must-give-the-radio-back.md)'s instinct one repository over.

### 9. TUNE is not a cheaper ON

ON brings the chip up and takes the speaker; TUNE only moves an already-running receiver, at the cost
of three I²C writes. A host stepping across the band must not be able to switch the station deaf by
accident, so TUNE on a receiver that is off is refused (`ERR_OFF`) rather than promoted.

TUNE also deliberately does **not** use the firmware's `FM_Tune`, which mutes on the way in because
it is built for the scan engine's tune-then-lock loop. The dock path keeps the audio up.

### 10. The command path does not write flash — and the OFF leg does

`FM_Start` and `FM_TurnOff` each end in `gEeprom.CURRENT_STATE = 3/0; SETTINGS_WriteCurrentState();`
(`App/app/fm.c:661-662` and `:120-121`) under `ENABLE_FEAT_F4HWN_RESUME_STATE`, which is `TRUE` in
this build. That is a flash sector erase — the driver's own comment puts it at ~300 ms — spinning in
`WaitWIP` inside the UART handler, and `CURRENT_STATE` genuinely flips `0`↔`3`, so the `memcmp`
short-circuit does not save it. So the ON leg reproduces `FM_Start`'s work without the write, and
TUNE writes nothing at all.

**The OFF leg does write, and that is not an exception granted to the rule.** The brief for this
cycle said "no EEPROM write" without knowledge of two facts found while verifying it:
`App/app/app.c:1761-1767` calls `FM_Start()` — write and all — when `gFM_RestoreCountdown_10ms`
expires, and `App/functions.c:106-108` arms that countdown on every return to FOREGROUND while
`gFmRadioMode` is set. **The firmware persists FM mode behind the host about five seconds after any
squelch close, and no command path can prevent it.**

If OFF also declined to write, flash would be left at `CURRENT_STATE = 3` permanently and
`App/main.c:290-305` would boot the radio into broadcast FM for ever after — deaf, with no host
present and no dock command able to undo it. That is ADR
[0155](0155-a-restart-must-not-inherit-the-demodulator.md)'s argument exactly inverted. **Clearing a
bit the firmware set is not the same act as routing a command through `SETTINGS_Save*`; it is the
only way to undo one.** It costs nothing when the firmware never set it and one erase exactly when it
did.

### 11. The screen follows the radio, because TX keys off it

`Dock_FmOn`/`Dock_FmOff` call `GUI_SelectNextDisplay` **directly**, not through
`gRequestDisplayScreen`.

The flag route was checked and is a no-op from here: `gRequestDisplayScreen` is consumed only at
`App/app/app.c:2625`, the last statement of `static void ProcessKey(...)`, and `gFlagReconfigureVfos`
likewise at `:2579`. Setting either from a UART handler does nothing until the operator's next
keypress and then fires at a moment nobody asked for. `GUI_SelectNextDisplay`'s own side effects
would never run either, and they are load-bearing — it clears `gScanStateDir`, `gFM_ScanState` and
`gCssBackgroundScan`, which is where `ACTION_FM` gets "turning FM on stops the scan" from. Without
it a dock FM-on leaves a scan running, and every scan hit calls `APP_StartListening` →
`BK1080_Init0()` and kills the audio.

**The leading reason is `generic.c:147`, not the keypad.** PTT decides whether to start transmitting
by testing `gScreenToDisplay == DISPLAY_FM`. Leaving the display alone would split host-initiated and
operator-initiated broadcast FM into two radio states that only *look* identical — one radio, two
behaviours, which is precisely what ADR [0137](0137-let-the-radio-be-a-radio.md) says not to build.
The unexplained dead keypad is the second reason.

### 12. The dock glue restores FM audio after `RADIO_SetupRegisters` — and this is no risk, not a mitigated one

`RADIO_SetupRegisters` opens with `AUDIO_AudioPathOff(); gEnableSpeaker = false;`
(`App/radio.c:752-754`) and never clears `gFmRadioMode`. All four dock glue paths end in it, so a
host that turns FM on and then tunes would get a running, correctly-tuned BK1080 and **silence**,
with no timer to recover it — `gFM_RestoreCountdown_10ms` is only armed on leaving TX/RX. That is the
F3a fault in mirror image.

One helper, `Dock_RestoreFmAudio()`, called from all four sites, so a fifth added later inherits it
rather than having to remember it.

**F7 behaviour is unchanged by construction, not by mitigation:** the state requiring the restore
cannot exist until `0x0879` creates it, because before F8 nothing but a thumb on the front panel
could set `gFmRadioMode`, and a thumb is not driving `0x0873` at the same time.

**Its guard is a grep, and that is stated rather than dressed up.** `tests/host/Makefile` compiles
`test_dock.c` and `dock.c` only — `uart.c` is not host-compiled, which is the whole point of
guardrail 4 — so no host test can see these functions. A `check-fm-restore` target counts call sites
instead. It cannot tell anyone the restore *works*; it can only catch a fifth `RADIO_SetupRegisters`
added without one. It was verified to fail on a negative control before being relied on.

## Acceptance

**Fail-first, twice.**

- **Behavioural**, against an always-succeeds `dock.c` case — binding called, status reported, no
  length check, no full-control check, no keyed check, no field ranges, no blanking: **139 checks,
  18 failures.** Every refusal status, both blanking assertions, the keyed refusal, the raster
  refusal and the band-range refusal went red.
- **Honest about what did not go red.** Both byte-exact goldens, the binding-reached-once check, the
  read-back checks and the out-of-band HAL report all stayed **green** against that stub — the
  goldens exercise the framer, and an always-succeeds path still frames correctly and still passes
  decoded values through. Recorded so nobody cites them as evidence the validation works, exactly as
  `test_dock.c`'s own test 31 already does.
- **One failure in that run was a defect in the new tests, not the stub**, and is recorded rather
  than quietly fixed: the F7-golden regression check omitted `g_mod_force_flags = 0`, which the
  published vector requires. Corrected; the honest stub count is 18.
- **Compile-time, free:** adding an eighth member to `dock_hal_t` fails the build under
  `-Wextra -Werror` until all four positional initialisers are updated.

**Green: `make -C tests/host run` → 142 checks, 0 failures** (98 before), plus the
`check-fm-restore` target reporting `4 RADIO_SetupRegisters, 4 Dock_RestoreFmAudio — paired`.

**Goldens derived from an independent reference framer**, written separately from `dock.c`, which
reproduces the two already-published F7 vectors byte-for-byte before its new output was trusted —
ADR 0150's method, applied in the other direction.

**`0x0874` and `0x0878` are byte-identical**, pinned by a regression check that replays F7's
published reply vector after F8's dispatch case and HAL member exist.

**`uv run pytest` 1981 passed, 5 skipped — unchanged. No `radio_server/` code changed.**

## Consequences

- **The station can now be made deaf over the wire, and the wire says so.** `state` is on every
  reply. Any host that turns this on owns the consequence, including for the station ID.
- **The two repositories can still drift and nothing checks it.** ADR 0148 named that gap, ADR 0149
  declined to widen it, and so does this — `PROTOCOL.md` and the goldens are the contract, verified
  by hand.
- **`uart.c` now duplicates about eight lines of `fm.c`.** `Dock_FmOn` is `FM_Start` minus the flash
  write. If upstream changes `FM_Start`, this does not follow. Named as a drift hazard rather than
  hidden; the alternative was splitting `fm.c` and repointing `app.c`'s two restore paths, which
  touches three upstream files and breaks the one legitimate persistence case.
- **The census remains in three places that disagree.** Third cycle running that this has been read
  and worked around rather than fixed.
- **F8 is not flashed and nothing is measured.** The F8 pre-release's `PROVENANCE.md` says so.

## Out of scope

- **The host side entirely** — `frames.py`, a `Capability`, an endpoint, the UI. Its own cycle.
- **R1 — a host crash or an unplugged cable mid-FM leaves `CURRENT_STATE = 3`** and the radio boots
  into broadcast FM. The OFF leg is the only thing that clears it, and a crash never sends one. **The
  fix belongs to the host cycle**: extend ADR 0155's startup assert to assert broadcast-FM OFF as
  well as a demodulation. Not implemented here.
- **R2 — the OFF leg's erase blocks the UART handler.** `PY25Q16_WriteBuffer` calls `SectorErase`
  followed by a spinning `WaitWIP`, so the main loop stalls. The firmware's own comment says
  *"Erase takes ~300ms"* — **that comment is the only source, and the true duration is a bench item,
  not a number to design around** (guardrail 1). Two things bound the damage and both were chosen,
  not lucky: it is on the OFF leg, where the FM audio is being torn down anyway, and it never happens
  on TUNE, the high-frequency operation.
- **R3 — `SETTINGS_WriteCurrentState` flushes more than `CURRENT_STATE`.** It also writes
  `SCAN_LIST_DEFAULT`, `SCAN_LIST_ENABLED`, `SCANLIST_PRIORITY_CH[0..1]` and `CHAN_1_CALL` out of
  RAM, so an unrelated divergence rides along on the OFF leg. A cost of using the firmware's own
  function rather than a reason to hand-roll a narrower one.
- **R4 — the audio-restore guard is a grep.** See decision 12.
- **FM memory channels, scan and seek.** `FM_Play`/`FM_CheckFrequencyLock` exist and
  `FM_PlayAndUpdate` writes flash unconditionally. Not exposed; a scan over this wire is its own
  decision about who owns the loop.
- **Reconciling the opcode census.** Still three places, still unreconciled.
- **Anything on the bench.** New `⚠ CONFIRM AT BENCH` items: that FM audio actually reaches the
  AIOC, the erase duration on the OFF leg, and that a keyed radio really does refuse.
- The carried findings stay carried: `doctor` builds no tuner, the EventHub does not exist at backend
  construction, the AM-mismatch branch, `POST /mode`'s 500, `MockRadio.set_mode`, `GET /presets`
  omitting `modulation`, the `REST_PATHS` drift, and the three websocket talker-slot leaks.

## Source of truth

Fork `kbennett2000/uv-k1-k5v3-firmware-custom`, branch `f8-dock-broadcast-fm`, cut from `origin/main`
at `4e1d9dc` (F7 merged). Upstream `armel/uv-k1-k5v3-firmware-custom` pinned at `3bd3ebb`
(v5.7.0), Apache-2.0.

Pre-existing symbols reused rather than reimplemented: `BK1080_Init`, `BK1080_Init0`,
`BK1080_GetFreqLoLimit`/`HiLimit`, `FM_SetFrequency`, `FM_AudioPathOn`,
`BK4819_PickRXFilterPathBasedOnFrequency`, `RADIO_SelectVfos`, `RADIO_SetupRegisters`,
`GUI_SelectNextDisplay`, `SETTINGS_WriteCurrentState`, `Dock_ModulationCanTx`. `FM_SetFrequency` and
`FM_AudioPathOn` gained prototypes in `App/app/fm.h`; both already existed and were already
non-static.
