# ADR 0168 — The second receiver is a control, not a consent form

**Status:** Accepted · 2026-08-01 · amends
[ADR 0164](0164-the-on-path.md) §7 (the card's shape only — the route, the frames, the capabilities
and the relay mute are untouched); closes issues **#225**, **#226**, **#227**

## Context

Three issues landed on one component a cycle after it shipped:

- **#225** — remove the "Turning this on:" consequence notice, make **Turn on broadcast FM** a single
  click, and remove the repurposed-keypad warning shown while the receiver is running.
- **#226** — *"Retune button does not seem to do anything."*
- **#227** — *"in order to change freq you must stop FM playback, set a new frequency, and start it
  again."*

#225 is a request. **#226 and #227 are the same defect**, and it is worth stating precisely because
the shape recurs:

```jsx
{canSet && !on && (            // BroadcastFmPanel.jsx:105 — the frequency box and the band select
  ...
)}
{on && (                       //                    :185 — Turn it off, and Retune
  ...  <button onClick={() => send({ action: "tune", ...asked() })}>Retune</button>
)}
```

**One control is gated on `!on`; its only consumer is gated on `on`.** The instant the receiver came
up, the only two inputs that could change what `asked()` returns unmounted — so Retune could do
nothing but re-send the frequency the radio was already on. The server answered 200, re-reported the
same `hz`, and the card visibly did nothing. That is #226 exactly, and #227 is the same sentence from
the operator's side: with no editable field while running, moving the receiver meant stopping it.

### What this says about ADR 0164, and it is not "it was wrong"

ADR 0164 designed this card **around the moment of commitment**. Every decision in its §7 is about
the operator who has not yet pressed the button: the three consequences above the button, the
two-step arm/confirm, the keypad warning aimed at *"whoever walks up to the radio later"*. That is
also why the defect is there. Everything the operator needs **after** committing was outside the
frame — the inputs were treated as part of the commit form, so they were unmounted when the form was
done, and the one control that only exists after the commit was left with nothing to read.

The two halves of this ADR are therefore one change: the card stops being a consent form and becomes
a control panel, and #226/#227 fall out of that rather than being patched around it.

### It shipped green, and the reason is in the test file

`uv run pytest` and `npx vitest run` were both green through ADR 0164 and every cycle since.
`BroadcastFmPanel.test.jsx` had 14 cases covering the consequences, the arm/confirm, the self-disarm,
the keypad alert, the tri-state and the capability gating. The string **`"tune"` appeared nowhere in
it.** ADR 0164's B3 measured `tune` over `curl` on the real radio and it worked — 200, read-back
`hz: 98500000` — so both ends were proved and the wire between them never was.

### The backend needed nothing, and that is the honest answer to #227

`action: "tune"` on a running receiver has been a first-class path since ADR 0164
(`app.py:1717-1801` → `tuner.py:566-684` → `frames.py` `BROADCAST_FM_ACTIONS`), it publishes a
`status` event, and `tune` on a receiver that is **off** is deliberately refused 409/`ERR_OFF`
(*"TUNE is not a cheaper ON"*, ADR 0156 D9). **This cycle writes no Python.** #227 asked for a
feature the server already had and the UI could not reach.

---

## Decision

### 1. The consent furniture comes out (#225)

Removed from `BroadcastFmPanel.jsx`: the `armed` state and its 8 s self-disarm, the `Confirm — turn
it on` / `Cancel` pair, the `restart-btn` styling, the `role="note"` consequence block, and the
`role="alert"` keypad banner. One click on, one click off.

**Recorded as a reversal, not a correction.** ADR 0164's §7 argued each piece from measurement and
the arguments still hold. They are removed because the operator asked for them to be removed on a
card they use, and a warning that is read every day is furniture. The residual risk is stated rather
than softened: **one click now silences both bridges** until the next transmission takes the receiver
back.

**What was never the protection.** The Part 97 control here is the relay mute (ADR 0162) and the
pre-key-up clear (ADR 0161). Neither is in the web UI, neither is reachable from it, and neither
moved. A commercial broadcast still cannot reach Mumble or a D-STAR reflector, and a key-up still
takes the receiver back first. The confirm step was courtesy; removing courtesy does not remove a
control. Anyone tempted to restore the confirm "for safety" should be clear about which property they
think it was holding.

**The facts survive their deletion.** The four consequences are unchanged and now live in two places
written for two readers: `docs/api.md` (for whoever drives the route) and a new
`docs/troubleshooting.md` section, *"Both links went silent, or the station is hearing a music
station"* (for whoever is looking at the symptom, which is the person consequence 4 was aimed at and
who never had a doc entry at all — `troubleshooting.md` had zero mentions of broadcast FM).

### 2. The frequency and band controls stay on screen while the receiver runs (#226, #227)

Gated on `canSet` alone. This is the whole fix: Retune finally has something new to send, and
changing station is one request instead of off → set → on.

One handler serves both directions — `{action: on ? "tune" : "on", hz, band}` — and the button reads
**Retune** or **Turn on broadcast FM** off the same flag. The verb is chosen from `on` rather than
the rendering, because the radio is what enforces the difference (`ERR_OFF`), and two code paths
building the same request is how they drift apart.

**Enter in the frequency field applies**, because a number field invites it and the alternative is an
operator typing a frequency, pressing Enter and watching nothing happen — which is the shape of the
bug being fixed.

### 3. The box follows the radio, not the last thing typed

Making the field live while the receiver runs introduces a hazard the old card could not have: the
value was a hardcoded `"104.3"` with nothing syncing it, so a reload, a front-panel `F+0` or a second
browser would leave a stale number in a field whose button now sends it. Closed by seeding `mhz` and
`band` from the read-back (`broadcastFm.hz` / `.band`).

**The subtlety is that it syncs on the READING, not on every render.** `status` frames arrive
continuously and most carry the same reading — a `rescues` tick, an unrelated field — so an
unconditional sync would erase a half-typed frequency under the operator's cursor. A `useRef` holding
the last synced `hz:band` means typing survives every frame that did not move the receiver, and a
front-panel tune is still picked up. `hz == null` (the OFF block's shape) is not a reading and never
overwrites the box. `(hz / 1e6).toFixed(1)` is exact because the raster is 100 kHz.

### 4. Every action reports the radio's own answer

`POST /broadcast-fm` returns the read-back block and the card discarded it. It now renders a
transient line — *"The radio reports broadcast FM on at 98.5 MHz"* — cleared at the start of the next
action and self-clearing after 5 s so it cannot go stale next to the live status line above it.

This is not decoration; it is the other half of #226. **A retune to the frequency the receiver is
already on changes nothing else on the card**, so without an answer the operator gets a disabled
flicker and concludes the button is dead — the reported symptom surviving the fix that removed its
cause. It is the radio's `hz`, never an echo of the request (ADR 0156), which is also the only way to
notice a radio that took a different frequency from the one it was sent.

### 5. The button row is a `.btn-row`, not a `.tune-row`

Found by looking at the rendered card rather than at the code. `.tune-row` is a
`96px 1fr auto auto` grid built for label+field rows; a lone button dropped into it lands in the
**96px label column** and wraps to three lines. That is why *"Turn on broadcast FM"* has been three
lines high for the whole of ADR 0164 — visible in issue #225's own screenshot. `.btn-row` (flex,
`gap: 0.5rem`) already existed for exactly this and is what the row uses now.

### 6. The host validates that a number was entered, and nothing else

An empty box serialises `hz: null` and earns a 422 about the wrong thing, so the apply button is
disabled unless `Number.parseFloat` yields a finite positive number. The 100 kHz raster and the
band's own Hz limits stay where they were — the server and the radio (guardrail 1). A second copy of
`BK1080_GetFreqLoLimit` in a React component is the drift hazard `dock.h` refuses for the same
reason.

---

## Verification

`npx vitest run` **14 files, 146 tests** (baseline 14/138). `BroadcastFmPanel.test.jsx` goes 14 → 22.
`uv run pytest` **2244 passed, 5 skipped** — unchanged from baseline, because no Python changed; run
as a regression check and offered as nothing more. `npm run build` succeeds.

**Red run — the new tests against the unmodified component: 16 failed, 6 passed.** Every failure is
behavioural, and the reasons are named rather than counted: *"Unable to find a label with the text of
`/frequency/i`"* (the inputs are not on screen while on), *"Unable to find an accessible element with
the role `status`"* (no read-back), the notice and confirm-button assertions failing `toBeNull`, and
the tune spy called with the wrong payload. No import errors, no cascade.

**The 6 that pass on the old component are regression pins, not reds**, and are reported as such:
*offers a way out with no confirm step*, *shows where the receiver is*, *labels the way in and the way
on differently*, *does not render 'never asked' as 'off'*, *hides itself entirely on a radio that has
earned neither capability*, and *keeps the way out while greying the way in*. They pin behaviour this
ADR deliberately preserves.

`"tune"` now appears in 6 cases where it appeared in none.

## Bench — a real browser, no radio

**No hardware measurement was taken this cycle, and none is implied.** This is a browser-only change
to one React component; the route, the frames and the wire are byte-identical to what ADR 0164 B3
measured on the deployed radio (`tune` to 98.5 MHz on a running receiver → 200, read-back
`hz: 98500000`). What ran instead is a live `python -m radio_server` on `MockRadio` serving the
**built** `web/dist` bundle, driven by **headless Chrome over CDP** — the real bundle, real
`fetch`/`/events`, real React, not jsdom.

**B1 — the route, with the exact payloads the card now sends.**

| leg | result |
|---|---|
| `on` 104.3 | **200** `{on: true, hz: 104300000, band: 0}` |
| `tune` 98.5 on a running receiver | **200** `{on: true, hz: 98500000}` |
| `tune` 98.5 again (the #226 case) | **200**, same block |
| `tune` **98.55**, off the raster | **422** *"…off the 100000 Hz raster… refusing rather than rounding, because the next step is a whole adjacent station"* |
| `off` | **200** `{on: false}` |

**B2 — the card in Chrome, 9 legs, all PASS.** Card renders with the frequency box present while
off; **no "Turning this on" notice and no "keypad" text anywhere on the page**; one click turns it on
and no `Confirm` appears; the frequency box is **still on screen while the receiver runs**; a retune
moves it 104.3 → 98.5 with no stop/start and the card follows; the read-back line self-clears; a
**retune to the frequency it is already on still answers**; **Enter** in the box retunes to 88.7; one
click turns it back off.

**Two things worth recording rather than smoothing.**

1. **The mock cannot stand in for the radio on the refusal paths.** `MockRadio.set_broadcast_fm`
   treats `tune` exactly like `on` — a `tune` against a stopped mock receiver returned **200** where
   an F9 radio returns **409 `ERR_OFF`** — and it models no `ERR_TX` / `ERR_BAND`. Those paths are
   covered by pytest and by ADR 0164's bench, and by nothing run here.
2. **A 22-second observation of the running card was taken deliberately**, because the first harness
   run looked like the frequency box was vanishing after a retune. It was not: the harness was
   deleting a `[role=status]` node out from under React, which corrupts React's tree. Sampled at
   100 ms with the receiver on, the card's DOM did not change once. The harness was fixed to wait for
   the line to self-clear instead — which is now a leg of its own.

## Consequences

- **A stray click on `Turn on broadcast FM` now silences both bridges**, where before it armed a
  button. It is undone by one click on `Turn it off`, or by any transmission, or by `F`+`0` at the
  radio. Accepted deliberately; see decision 1.
- **The card is taller in the ON state** — the frequency and band rows no longer disappear. That is
  the fix, not a layout regression. Both states are now the same height.
- **`Turn on broadcast FM` is one line instead of three**, a side-effect of decision 5 that changes
  how the card reads at a glance.
- **`.notice`, `.notice-rx-paused`, `.restart-btn` and `.confirm` are now unused by this component
  but still live in `styles.css`**, because `ListenControl`, `TransportBanner` and `RestartButton`
  use them. Do not garbage-collect those rules on the strength of this card.
- **`docs/api.md`'s statement of the UI contract is inverted**, not deleted: it now says the card
  states none of the four consequences and points at `troubleshooting.md`.

## Findings carried forward

1. **`docs/uvk5-setup.md` was stale by four ADRs** — *"Nothing on the server drives this yet; when
   something does, it is the operator's call to make."* — untrue since ADR 0157/0161/0164. Fixed
   here, found by accident. The F-level table around it was not audited for the same drift.
2. **A UI control and its inputs can be gated on opposite conditions and no test will notice**, as
   long as no test drives the control. The cheap guard is a test per *action string* the component
   can send; `"off"` and `"on"` had one and `"tune"` did not.
3. **The card still shows only `broadcast_fm` from `/status`**, which is the key-up snapshot. ADR
   0163's open question — whether `/status` should render the cadence's reading, since the two
   legitimately disagree during the front-panel window — is untouched and still open.
4. **Nothing in `acceptance.py` covers this card**, as with every UI cycle in this arc.
5. **The `.tune-row`-as-a-button-row misuse was audited across the UI and this card was the only
   instance** — every other `className="tune-row"` holds a label+field pair, which is what the grid
   is for. Named so the next person does not re-run the search.
6. **A jsdom test cannot see a three-line button.** Decision 5 was found by screenshotting the
   rendered card, and nothing in the 146-test vitest suite would ever have failed on it. Worth a
   look at the real page whenever a UI cycle changes what is in a row.

## Out of scope

The route, the frames, the capabilities, the relay mute, the cadence and the pre-key-up clear — all
unchanged. Preset stations for the broadcast receiver; a band-limit hint in the UI (the radio's
verdict stays the radio's); the `/status` vs `/link/status` disagreement above; and the UV-K5
firmware fork.

## Source of truth

`web/src/components/BroadcastFmPanel.jsx` and its tests. The consequences of running the second
receiver: `docs/api.md` (`POST /broadcast-fm`) and `docs/troubleshooting.md`. The reasoning this ADR
amends: [ADR 0164](0164-the-on-path.md) §7.
