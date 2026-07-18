# 0077 — The backend selector in the web UI

Status: Accepted

## Context

ADR 0076 shipped the live backend switch **server-side** — `GET /radio/backends`, `POST /radio/select`,
an atomic rollback-safe `RadioHolder.rebuild`, and a re-emitted `capabilities` event over `/events` —
and stopped there: "the UI dropdown is the next cycle." This is that cycle. It adds **no server
behaviour**; it is purely the web control panel consuming those endpoints.

The load-bearing piece is **reactive re-greying without a reconnect**. Today the capability set the UI
greys controls by is a **one-shot** value: fetched once at login (`GET /capabilities`), stored in
`session.caps`, and threaded to `ControlPanel` as an immutable prop. The `/events` reducer
(`reduceStatus` in `web/src/useEvents.js`) had **no `capabilities` case**, so the switch's re-emitted
event was received and silently dropped. Without wiring caps into the live event stream, flipping
AIOC→kv4p would change the radio but leave the tuning/scan controls greyed until a page reload — the
opposite of the payoff the whole switch arc was built for.

The web UI also had **no JS test tooling** (no runner, no CI; browser-verified by hand). Writing the
component/integration tests this feature warrants meant bootstrapping one.

## Decision

### Make capabilities reactive (the crux)

- `reduceStatus` gains a `case "capabilities": return { ...prev, caps: data.capabilities }`, folding the
  ADR 0076 event into reactive `state.caps`. The function is now exported so it can be unit-tested.
- `ControlPanel` computes `advertised = new Set(state.caps ?? caps)` — it **prefers the reactive set**
  and falls back to the one-shot login prop only until the first `capabilities` frame arrives. Every
  downstream gate already depends on this (`hasCap` → `anyCat`/`showDial` → the render gates that
  mount/unmount `TuneControls`/`ScanControl`), so the CAT cards appear for the kv4p and vanish for the
  AIOC the moment the event lands — no per-control edits, no reconnect.
- The runtime `disabledCaps` set (a 501 greys a control defensively) is **additive-only**, so a switch
  clears it (`useEffect(… , [state.caps])`) — otherwise a capability the *new* radio supports would stay
  greyed by the *previous* radio's 501.

### The selector — `BackendPanel.jsx`

A `.card` in the left column (first — a switch that reconfigures the whole radio belongs at the top),
built from the existing `ModeControl` idiom (a labelled `<select>` in a `.tune-row`, `useAction` for the
POST, an `.error` banner) so it looks native. It fetches `GET /radio/backends` on mount and
**self-hides** when fewer than two backends are configured (nothing to switch to — the LinkPanel
hide-when-unconfigured pattern). `api.js` gains `backends()` and `selectBackend(backend)` beside the
Mumble-link methods; no new error mapping is needed (401→gate, 503→the typed `ControllerUnavailable`
carrying the still-active backend, 409→a generic `ApiError` message).

The dropdown tracks the **live** active backend (`state.backend`, folded from the WS status), not an
optimistic pick. So the failure story is honest by construction:

- **In progress** — `pending` disables the control and shows "Switching…" (the kv4p reboots on open, so
  a switch takes a beat).
- **503 (failed, rolled back)** — `state.backend` never changed, so the sync effect snaps the dropdown
  back to the radio you're **still** on and the error banner names it. The control never implies a
  switch that didn't happen.
- **409 (unconfigured)** — unreachable from a list built off the configured backends, but the generic
  error surfaces it rather than failing silently.
- **Mid-transmit** — a caption warns that switching drops the current transmission (the server de-keys
  on teardown); we surface that rather than hide it behind an instant-looking control.

### Bootstrap Vitest

Vitest + `@testing-library/react` + `jsdom` (a `test` block in the existing `vite.config.js`, a
`test-setup.js` for the jest-dom matchers, an `npm test` script). Vitest reads the same Vite config, so
`vite build`/`dev` are unaffected. Tests: the selector renders the configured list with the active radio
marked, selecting POSTs the right backend, an in-flight switch shows "Switching…"/disabled, a 503 shows
the error and snaps back; `ControlPanel` re-greys the CAT cards as `state.caps` is driven through a
switch and back (the reactive set is preferred over the prop); `reduceStatus` folds the `capabilities`
frame. The dev proxy also gains `/radio` (the ADR 0076 endpoints predate any UI caller).

## Consequences

- A live AIOC↔kv4p switch now re-greys the tuning/scan controls in the browser without a reconnect —
  the visible payoff of ADR 0073→0074→0076→0077. The face's backend label and the selector both follow
  the live status.
- The frontend gains a test runner where it had none. It is dev-only (`web/dist` unaffected); there is
  still no CI, so `npm test` is a local/manual gate like `npm run build`.
- No server change: `uv run pytest` stays green (1050 passed, 5 skipped) and `tests/test_web.py`'s
  static-mount contract is untouched.

## Non-goals

No server or endpoint changes (ADR 0076 owns those). No new backends, no per-backend DTMF twist. The
TM-V71A CAT backend remains unimplemented — this selector switches only the backends a node actually has
configured. Docs limited to this ADR, the "switching radios" note in `using-it.md`, the README
supported-features line, and HANDOFF.
