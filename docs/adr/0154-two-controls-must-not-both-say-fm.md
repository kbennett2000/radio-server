# 0154 — Two controls must not both say FM

Status: Accepted

The web UI ADR [0150](0150-the-host-learns-to-listen-to-am.md) deferred, unblocked by ADR
[0153](0153-one-frame-must-not-take-the-relay-down.md): a demodulation control, and `tx_ok` surfaced
before an operator presses a button that will 503. Extends the Tune card of ADR
[0022](0022-web-ui-architecture.md)/[0037](0037-web-ui-simplification.md) and the transmit lockout of
ADR [0144](0144-instant-and-persistent.md). **No backend change** — `POST /modulation` and the
`modulation`/`tx_ok` status fields shipped in #207. No firmware, nothing flashed, no bench claim.

## Context

`ModeControl` rendered `<label>Mode</label>` over a select that POSTs `/mode` — wide/narrow
**bandwidth**. A demodulation control offers FM and **AM**. Two selects in one card, both spelling
one of their values `FM`, and choosing wrong on the new one disables TX with no obvious cause.

The argument was already written in this repo, twice, before the defect could surface —
`backends/base.py:44-46`:

> Channel **bandwidth** — wide (`FM`) or narrow (`NFM`). Not the demodulator: see `SET_MODULATION`
> … **The two will be confused otherwise, because both spell one of their values `"FM"`.**

and `docs/api.md:137-139`. This is the confusion `Capability.SET_MODE` vs `SET_MODULATION` was
flagged for in #207, arriving where an operator can act on it. Third cycle running where the
reasoning was on disk ahead of the fault.

Before this cycle, `grep -rn "modulation\|tx_ok" web/src` returned **zero hits**.

## Decision

**1. Both labels change. Neither survives with its meaning altered.**

| control | label | option text | POSTed value |
|---|---|---|---|
| existing | **Bandwidth** | `Wide (FM)` / `Narrow (NFM)` | `FM` / `NFM` — **unchanged** |
| new | **Demodulation** | `FM` / `AM` | `FM` / `AM` |

Keeping `Mode` for the demodulator is the ham convention (mode = FM/AM/SSB) and was rejected for one
reason: it is the only option where a label *survives with its meaning changed*, so a returning
operator's muscle memory lands on the wrong control — the exact failure this cycle exists to prevent.

**The `(FM)`/`(NFM)` parentheticals are deliberate.** They are the only thing joining the new wording
back to the raw values, which still appear in `radio.toml` (`mode = "FM"`), in preset records — the
active-channel highlight compares `state.mode` against a preset's raw `mode` — and on the face LCD.
Strictly, `FM` therefore still appears in both controls; what has gone is any control whose *primary*
token is ambiguous, and any control called "Mode".

**2. The bandwidth select is trimmed to the two values `POST /mode` accepts.**

It offered `["FM","NFM","AM","USB","LSB","CW"]`. Every real backend rejects the last four with a
`ValueError` (`aioc_baofeng.py:671-672`, `uvk5/radio.py:211`, `kv4p/radio.py:78`;
`presets.VALID_MODES` is exactly `{"FM","NFM"}`). So the **bandwidth control already offered AM**,
meaning the wrong thing — this cycle's trap, already shipped. Not cosmetic: leaving it would have
made the card offer AM twice, in two controls, meaning two different things.

**3. Only a *measured* `tx_ok: false` locks the transmitter out.**

`=== false`, never falsy — the rule `AiocBaofeng._refuse_if_tx_disabled` states at `:472`. `null` is
"nobody has asked this radio", and a transmitter disabled by an unknown is a worse failure than the
one being prevented.

`docs/api.md:147-150` notes that AM stops the `baofeng` backend keying (AIOC DTR into the disabled
PTT pin) while the `uvk5` dock's register keying is unaffected — which reads like an argument for
gating the lockout on the backend name. **It is not, and no backend-name check went into the
browser.** `uvk5/radio.py:1169-1181` passes neither `modulation` nor `tx_ok`, so the dock backend
reports `null` and cannot lock anything out. Only `aioc_baofeng.py:809-810` and `mock.py:148`
populate the fields. The tri-state rule *is* the backend gate, structurally. Recorded with its
expiry condition: the doc describes an intent the dock backend has not implemented, and the day it
does, this reasoning must be revisited.

The same weld makes the Status row's capability gate safe: every tuner that measures `tx_ok`
advertises `set_modulation` (`SETVFO_CAPS = TUNING_CAPS | {SET_MODULATION}`; `EepromTuner` fixes
`tx_ok = None`), so the gate can never hide the warning.

**4. The lockout folds into the existing `lockedOut`, inside its guards.**

`source === "rf" && !talking && (txRefused || txReadyIn > 0)`. **The parentheses are load-bearing**
and are pinned by tests: `&&` binds tighter than `||`, so dropping them moves `txRefused` outside
both guards — Talk-on-Mumble goes dead over a radio nobody is using, and a refusal arriving mid-over
strips `onPointerUp`/`onLostPointerCapture` and strands the key, the half-state the pointer-capture
comment exists to prevent.

The label and the sub-line branch by reason, and **the standing reason is named first**: the
countdown clears itself, AM does not, so a label that expired into a still-dead button would be the
silent failure over again. The sub-line is *actionable*, not merely explanatory like the tune-mute
one it sits beside — the remedy is in a different card — and it names the cost: not just the over,
but the station ID Part 97 requires and every voice service (guardrail 5, `aioc_baofeng.py:463-465`).

`tx_ok: false` means **non-FM**, not AM. The wire reserves `USB`, so the demodulator is quoted from
`state.modulation` and falls back to "not on FM" when the radio never reported one.

**5. The face LCD had to change too. A correct select beside a misleading LCD is not a fixed
collision.**

`FreqLcd` printed a bare `state.mode`, so a radio demodulating AM showed **`FM`** in the most
prominent readout on the screen while the new select correctly said AM. Each token now says what kind
of thing it is — `DEMOD AM` / `BW NFM` — keeping both raw values, because they are what `radio.toml`
and preset records spell and what the parentheticals join back to. The demod token is absent, never
defaulted, when the server has not asserted a modulation. It is painted with the `warn` red on
`tx_ok: false`, never the `.on` accent: an alarm in the reassurance colour reads as reassurance
(ADR [0134](0134-repeater-keyup-in-the-field.md)).

**6. The Demodulation control has no Set button and no draft state.**

It renders `state.modulation` and posts on change. This departs from the select+Set shape of every
other row deliberately: `apply_preset` writes the modulation on **every** preset apply —
unconditionally, `presets.py:315-322`, because a channel list with one airband entry has to return to
FM when the operator taps a repeater. A local draft would have sat there showing the operator's last
pick while the radio was moved out from under it. Rendering what the radio **confirmed** is
`PowerControl`'s stated rule in the same file, for the same reason. Nothing is claimed before the
server has asserted a modulation.

**Greyed rather than hidden** where the backend cannot do it — the brief's choice, kept against
`PowerControl`'s "an unusable control is just noise" precedent three rows away. An unusable power
control is noise; this one is the remedy the Talk button's AM lockout names, and hiding a remedy
while showing the symptom is the silent-failure shape this project keeps closing. Two idioms now sit
close together in one file: an honest cost, recorded rather than smoothed over.

## Acceptance

**Three fail-first runs, recorded red.**

1. **`tx_ok` lockout** and **2. Spacebar**, against unmodified `TalkControl.jsx` — **6 red.** Four AM
   lockout assertions plus both Spacebar guards.
2. **`null` must not lock**, against a deliberately mutated build (`state?.tx_ok !== true`) — **5
   red**, and the blast radius is the interesting part: as well as the target test, it broke **four
   pre-existing `tx_ready_in` tests**, because every backend but a dock-tuned UV-K5 omits the field
   and an unmeasured field would lock out essentially every radio. Mutation reverted.

**`cd web && npm test`: 101 passed, 11 files** (71 before — 30 new). `npm run build` clean.
`uv run pytest`: **1973 passed, 5 skipped, unchanged** — no Python changed.

## Consequences

- AM is selectable, and a station in AM says so in three places instead of failing as a dropped
  connection: the Talk button names the cause and the remedy, the Status card carries the alarm, and
  the face LCD stops asserting FM.
- The Spacebar no longer keys a locked-out transmitter (see below).
- `POST /mode` can no longer be sent a value it would 500 on **from this UI**. The route is unchanged.
- `web/vite.config.js` gains `/modulation`. Those keys are string prefixes and `"/modulation"` does
  not start with `"/mode"`, so without it the control 404s under `npm run dev` while working in
  production.
- `docs/adr/README.md` lost a stray blank line inside the index table, which had been splitting
  0151–0153 into a second header-less table. `test_docs_contract.py` misses it because `_index_rows()`
  scans line-by-line and never checks contiguity.

### Shipped beyond the brief, recorded rather than blamed

**The Spacebar guard also closes a pre-existing `tx_ready_in` bypass.** The keyboard listener called
`startTalk()` with no lockout check at all — `holdProps` only ever stripped the *pointer* handlers —
so a greyed Talk button has always still keyed from the keyboard. That was cosmetic while the only
lockout lasted six seconds. It is not cosmetic now: an AM refusal never expires, so the keyboard
would have kept keying a radio that cannot transmit, indefinitely. The fail-first run confirms it
empirically rather than by reading: `does not key a radio still inside its post-write mute` is red on
`master`. This is a behavioural change to an existing keying path that was not asked for, recorded
here the way ADR [0151](0151-a-failed-key-up-must-give-the-radio-back.md)'s addendum recorded the
`tx_ok` refusal.

**The keyup was verified and deliberately left unconditional.** It *can* stop an over the Spacebar
did not start — `stopTalk` sets `disposed` and closes, so a stray tap during a pointer-held over
unkeys it, and during `startTalk`'s async setup aborts it. Ownership tracking was considered and
rejected: it introduces a path where a release does **not** stop, and failing to stop is strictly the
more dangerous direction in a transmitter. A redundant unkey is harmless; a missed one is a stuck
key.

## Findings carried forward

**1. `POST /mode` reaches the client as a 500, not a 422.** `app.py:1270-1278` catches only
`UnsupportedCapability`, and `ModeBody.mode` is a bare `str`. `/tone`, `/frequency` and `/modulation`
all catch `ValueError → 422`; `/mode` does not, and `docs/api.md:682` does not list it among the 422
routes. **Verified empirically this cycle**, not asserted from the source — a throwaway probe
(uncommitted) substituting the validation every real backend has: `{"mode":"NFM"}` → 200,
`{"mode":"AM"}` → **500**, `/modulation` with a bad value → 422 for contrast. Precedent, not theory:
`app.py:1253-1256`, one route above, documents this exact failure already having happened on the
bench — *"which used to reach the backend's range check and escape as an unhandled ValueError (HTTP
500, seen on the bench)"*. A backend change, so it is named rather than fixed here.

**2. `MockRadio.set_mode` accepts anything, and that is why nothing caught finding 1.** `mock.py:182`
is `self._mode = mode`, unvalidated, while every real backend raises. Every API and UI test runs
against the mock, so `{"mode":"AM"}` is 200 in tests and 500 on the bench. `MockRadio.set_power`
validates for precisely this stated reason — *"a double that swallows 'vhigh' would let a 422 the
real backend returns go untested"* — and `set_mode` never got the same treatment. **This will hide
the next divergence too**, which is why it is a finding in its own right rather than a footnote to
finding 1.

**3. `GET /presets` does not serve `modulation`, so the preset highlight will now lie.**
`app.py:1344-1357` serves `mode` but not `modulation`, while `presets.py:315-322` applies modulation
on every apply and `activePresetName` does not compare it. This cycle makes manual demodulator
changes possible for the first time, so: apply an FM preset, switch to AM by hand, and the preset
stays highlighted. **The UI cannot fix it** — the field is not on the wire — and adding it is a
response-field change this cycle excludes.

**4. `TuneControls.test.jsx` claimed coverage that did not exist.** Its header asserted the
frequency/channel/tone/mode rows were "covered through ControlPanel"; `ControlPanel.test.jsx` mocks
the entire component away to a stub, so those rows had never had a single assertion. The comment is
corrected and the bandwidth relabel is their first.

**5. `vite.config.js` `REST_PATHS` has drifted.** `/power`, `/presets`, `/split`, `/tuning`,
`/dstar` and `/dvap` are all missing, and the WebSocket block proxies only `/audio/rx` and
`/audio/tx` — so `/audio/mumble/tx` and `/audio/dstar/tx` hit the SPA in dev, not the API. Nothing
tests this file, which is why it rotted. Only the entry this cycle needs was added.

**6. The three websocket talker-slot leaks** — `app.py:1873` (`tx_slot`), `:1995`
(`mumble_talk_slot`), `:2095` (`talk_slot`) — still carried from ADR 0153, still untouched, still
their own cycle.

## Out of scope

- **Any API path, response field or config key.** UI code, UI text, UI tests and one dev-proxy entry.
- **Every finding above.** Each is a backend change or a different mechanism, and each deserves its
  own fail-first.
- **Any hardware claim.** The radio is not flashed with F7. Every `tx_ok` and `modulation` state here
  is modelled by fakes; nothing has been seen on the air.
