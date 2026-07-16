# ADR 0044: Retro-ham visual refresh of the web control panel

## Status

Accepted

## Context

The web control panel (ADR 0022 and successors) shipped with a generic dark dev-dashboard look
that shares nothing with the project's visual identity (`docs/banner.html` / `docs/banner.png`:
cream panels, amber accents, chocolate ink, a dial-scale motif). A design handoff — an interactive
HTML prototype plus a token table and per-element spec — defines a full re-skin in that brand,
with two themes (warm **Day**, "tube glow" **Night**) and a modest layout restructuring: a
masthead, a "radio face" hero grouping the everyday controls (state lamp, frequency LCD, dial
scale, Monitor, Transmit), a compact status card, an LCD-style over-the-air code chip, typed
badges in the operating log, collapsible settings groups, and a redesigned login gate.

## Decision

Implement the redesign in the existing React + Vite app as a **re-skin with zero functional
changes**. Every behavior, hook, API call, capability gate, and ADR-referenced pattern stays
exactly as it is: pointer-capture PTT and Spacebar hold-to-talk (ADR 0024/0037), auto-listen
(ADR 0037), hide-when-unconfigured cards and 501 capability greying, TOTP chip polling,
settings dirty-tracking and atomic-400 handling (ADR 0027), and all accessibility roles/labels.

Specifics worth recording:

- **Theming**: all colors/shadows are CSS custom properties — Day values on `body`, Night
  overrides on `body[data-theme="night"]`. A masthead toggle sets the attribute and persists the
  choice in `localStorage["radio.theme"]` (restored before first render). This is the one piece
  of new behavior. The page-background glow is set with `background-*` longhands, not the
  `background:` shorthand (the shorthand broke custom-property re-resolution on theme switch in
  prototype testing).
- **Fonts**: UI text uses the web-safe Trebuchet MS stack (matches the banner). Numerals, code,
  the log, and LCD readouts use IBM Plex Mono, **vendored via `@fontsource/ibm-plex-mono`** so a
  LAN-only (possibly offline) deployment never reaches for a font CDN.
- **Layout restructuring, not behavior**: the state pill moves from the Status card into the
  radio-face header; the frequency/mode readout becomes the face's LCD (rendered under the same
  `set_frequency` capability gate that used to gate the status row); Listen/Talk render inside
  the face as Monitor/Transmit. Cards regroup below the face. Render conditions, props, and
  hooks are unchanged.
- **Decorative-but-live dial scale**: 144–148 MHz, needle at
  `clamp(2%, (hz − 144 MHz) / 4 MHz × 100%, 98%)`, tracking the scan frequency while a scan
  runs. Marked `aria-hidden`; the LCD and status rows remain the accessible readouts.
- **Reduced motion**: the prototype's blanket `@media (prefers-reduced-motion: reduce)
  { * { animation: none !important } }` replaces the previous single-selector rule — strictly
  stronger.
- The design-handoff HTML files are references and are **not** shipped or committed.

## Consequences

- The UI is brand-consistent and themeable; future components must use the token set and must
  keep literal `#3a1d0b` text on amber-gradient surfaces (tokens would flip it light in Night
  mode and break contrast).
- No server, API, or test changes. There is still no JS test suite; visual fidelity is verified
  against the prototype by eye (screenshots in the PR).
- `web/package.json` gains one runtime dependency (`@fontsource/ibm-plex-mono`); the font ships
  in the built bundle (`npm run build` as before).
