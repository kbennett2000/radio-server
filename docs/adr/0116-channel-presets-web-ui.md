# 0116 — Channel presets: the web UI

Status: Accepted

## Context

Cycle 6 (ADR 0115) shipped the channel-presets model, config, apply path, and HTTP API (`GET /presets`,
`POST /presets/apply`), and made every tuning change publish a `status` event — but with no browser
surface. This cycle adds the surface that is the point of the whole arc: a card in the control panel
where clicking a named channel tunes the radio. **No server changes** (the API and events already exist);
**no new dependencies**.

## Decision 1 — a standalone card patterned on `DStarPanel`

`PresetControl.jsx` is a standalone card modelled on `DStarPanel.jsx` — the closest existing analogue: a
tap-a-preset button row (`DStarPanel`'s `PRESETS.map(... <button onClick={() => link(p)}>)`), a card that
self-hides when there is nothing to show, and errors surfaced as `<div className="error" role="alert">`.
The apply-on-click shape reuses the shared `useAction` hook (actions.js) like every other control. Two
one-line client methods were added to `api.js` (`presets`, `applyPreset`) matching the existing idiom; no
new error class — a 404/409/422 arrives as the generic `ApiError` (its message carries the server
`detail`), a 501 as `Unsupported("set_frequency")`.

## Decision 2 — visibility mirrors the API with the two existing hide gates (no third state)

The kickoff required matching the existing hide-vs-disable precedent, not inventing a third state. There
are exactly two, and the card uses both:

- **Capability gate → hide by not mounting.** `POST /presets/apply` is server-gated on `SET_FREQUENCY`
  (like `POST /frequency`). The card is therefore mounted behind `{hasCap("set_frequency") &&
  <PresetControl/>}` in `ControlPanel.jsx` — the *same predicate as `showDial`* and the same
  hide-by-conditional-mount model as `ScanControl`/`TuneControls`. On an audio-only radio the card
  simply is not there (an unusable control is noise — the ADR 0044/0077 rationale).
- **Config-absence gate → self-hide.** `PresetControl` fetches `GET /presets` on mount and returns
  `null` when the list is empty, exactly like `DStarPanel`/`LinkPanel`/`DvapPanel` self-hide on absent
  config. A deployment with no `[[presets]]` shows nothing.

## Decision 3 — the active-channel highlight is DERIVED, never stored

The card highlights the preset whose **honoured** fields exactly match the live tuned state, computed by
the pure `activePresetName(presets, state, hasCap)`:

- `state.frequency === preset.frequency` is always required (the card only mounts when `set_frequency`
  is honoured);
- `state.mode` / `state.tone` are compared **only** when the backend advertises `SET_MODE` / `SET_TONE`
  (via the reactive `hasCap`), so a field the radio can't set doesn't spuriously break the match;
- the highlight appears **only when exactly one** preset matches. Zero matches → nothing highlighted; an
  ambiguous two-or-more (e.g. tone-less duplicates of a frequency, or two presets differing only in a
  tone the backend can't honour) → nothing highlighted.

Because it is derived from `state` (folded from the `status` events by `reduceStatus`), a manual
tune-away in the Tune card changes `state.frequency` and clears the highlight naturally. There is **no
new client-side state store** — the only local state is the fetched preset list and the transient
apply-result.

## Decision 4 — non-silent results: skipped as a notice, failures as an alert

`run(fn)` returns the resolved body on success, so the apply's `{applied, skipped, status}` is captured:
a non-empty `skipped` (a field the active backend couldn't honour) is shown as a `notice` — the same
success-path idiom `TuneControls` uses for an unsupported block — never dropped silently. A failure (a
mid-TX **409**, an out-of-band **422**, an unknown-name **404**, or an audio-only **501**) surfaces
through `useAction`'s `error` as the standalone-card `role="alert"` div — the same way a `/frequency`
failure shows today; the 501 also routes through `onUnsupported` to grey the control, like every other
CAT action.

## Decision 5 — reactive across browsers, for free

Applying publishes a `status_event` server-side (ADR 0115), which every connected browser folds into its
`state` over `/events` (ADR 0076/0077). So applying from one browser updates the FreqLcd, the status
readout, and the derived preset highlight in **every** browser with no polling and no extra machinery —
the reactive path already carries it.

## Consequences

- New Vitest suite `web/src/components/PresetControl.test.jsx` (config-absence hide; apply-by-name;
  skipped-field notice; mid-TX 409 alert; 501 → `onUnsupported`; highlight derivation incl. the
  exactly-one-match ambiguity rule and the honoured-field gating) plus an extension to
  `ControlPanel.test.jsx` proving the card mounts under CAT caps and not on an audio-only backend. The
  web tests run under `npm test` (Vitest) — a local gate, separate from `uv run pytest`; the Python
  suites are untouched (no server change).
- No new dependencies (`web/package.json` unchanged); one small CSS accent for the active preset button
  reusing the existing theme tokens.

## Out of scope (named; built here: none)

Preset **editing** in the UI (the config file stays the source of truth — v1), split/offset (a follow-on
that would touch the `CatRadio` interface), the stuck-key **watchdog/TOT** arc, **any** server-side
change (there are none), and bench items.
