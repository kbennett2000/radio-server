# 0081 — Remove the decorative frequency-dial scale from the control panel

Status: Accepted

## Context

The control panel's "face" carried a horizontal **dial scale** (`DialScale`, `web/src/components/
ControlPanel.jsx`): a 144–148 MHz ruler with minor/major tick strips and a red needle whose position
tracked the tuned (or, during a scan, the scanning) frequency. It was introduced with the retro-ham
visual refresh (ADR 0044) as decoration — it was marked `aria-hidden="true"` and carried no readout
the operator relies on.

The accessible, authoritative frequency readouts sit right above and beside it: the `FreqLcd` numeric
display and the status rows. The dial duplicated that information as a purely visual flourish, hard-
coded to the 2 m band (144–148 MHz) so it was meaningless on any other band, and added visual clutter
without adding function. The operator asked for it to go.

## Decision

Remove the `DialScale` component, its single mount point, and its dedicated CSS.

- **`web/src/components/ControlPanel.jsx`** — delete the `DialScale` function and the
  `{showDial && <DialScale state={state} />}` mount inside `<section className="face">`.
- **`web/src/styles.css`** — delete the `.dial` / `.dial-bands` / `.dial-minor` / `.dial-major` /
  `.dial-needle` (+ `::after`) block.

The `showDial` flag (`hasCap("set_frequency")`) stays — it still gates the `FreqLcd` numeric readout,
which is unchanged. The shared CSS vars the dial happened to use (`--tick`, `--ticksoft`, `--red`)
stay too; they are defined for and still used by other elements (e.g. the `.decor-dial` gate
decoration and the needle-free chrome elsewhere).

## Consequences

- The face is simpler: LCD + status rows carry the frequency; nothing visual is lost that conveyed
  information the operator needed.
- Nothing else changes — no server, endpoint, capability, or setting is touched, and the numeric
  frequency display and scanning behaviour are unaffected.
- No test changes: no test referenced the dial (the `ControlPanel` test exercises the CAT cards via
  `state.caps`, never `.dial`/`DialScale`). The existing web suite stays green and the bundle builds.

## Non-goals

No change to the `FreqLcd` numeric readout, the CAT tuning/scan cards, scanning, or any server-side
behaviour. This removes decoration only. Cross-ref: ADR 0044 (the retro-ham refresh that introduced
the dial), ADR 0048 (the red cockpit theme it was styled under).
