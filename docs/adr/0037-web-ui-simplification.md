# 0037 — Web UI simplification & approachability

Status: Accepted

## Context

The control panel (the Vite + React SPA under `web/`, ADR 0022) grew a control and a status field
for every backend capability the server can have. For the intended operator — a non-technical ham
running a home voice gateway, the person the docs were rewritten for (PR #47) — much of it is noise:

- The **Status** card always renders the five CAT-only fields (Frequency / Channel / Tone / Mode /
  Scan) even on an audio-only backend, where they are permanently `—`, plus an internal **Arbiter**
  row that means nothing to an operator.
- **Key up (PTT)** is a bare carrier-key toggle (`POST /ptt`) that only overlaps confusingly with
  Talk, which already keys and speaks.
- **Controller Start/Stop** is jargon, and is the *only* way to start the controller loop (DTMF
  services, over-the-air TOTP login, automatic Part-97 station ID).
- You must click **Listen** every session before you hear anything.
- **Talk** is click-to-start / click-to-stop only — no push-to-talk feel.
- **Settings** renders every one of the ~47 schema settings in one flat, endless scroll.

The goal of this cycle is *cleanup without loss of functionality*: a simpler, friendlier surface for
the common operator, with the power features still reachable (via config, or unchanged endpoints).

Two existing seams make this cheap and keep it honest:

- **Capabilities** (`/capabilities`, ADR 0022) are already fetched at the token gate and threaded
  through `ControlPanel` as `hasCap(name)` — the same signal that hides the Tune/Scan cards on an
  audio-only backend. Status fields now ride it too.
- **Settings are schema-driven** (ADR 0025/0026/0027): one `SettingSpec` renders in the API and the
  UI for free. New behavior is a new spec, not new plumbing.

## Decision

1. **Status: capability-gate the CAT rows; collapse the rest to one state pill.** Frequency /
   Channel / Tone / Mode / Scan render only when the backend advertised the matching capability
   (`set_frequency`/`set_channel`/`set_tone`/`set_mode`/`scan`). The separate Transmitting / Channel
   busy / Arbiter rows become a single prominent **On air / Receiving / Idle** pill. Backend and (when
   a controller session is open) Session remain.

2. **Remove the Key up card; keep the endpoint.** `PttControl` leaves the UI. `POST /ptt` /
   `radio.ptt` stay — Talk keys through them via `TxSession`, and power users/tests keep the route.

3. **Auto-listen on login, as a setting.** New `web.auto_listen` (default **on**). The SPA reads it
   at startup and, once authenticated, auto-starts Listen. Browsers block audio before a user
   gesture, so "startup" means "the moment you log in" — the login click unlocks the AudioContext
   (Chrome/Firefox/Edge auto-start; Safari may need the one Listen click, which remains the fallback).

4. **Remove the Controller card; auto-start the loop instead.** New `controller.autostart` (default
   **on**). `build_app` opts the app in; at lifespan startup a configured controller is activated
   exactly as `POST /controller {on:true}` did. This preserves DTMF services + automatic station ID
   without a manual button. `POST /controller` stays. Autostart is gated on an explicit `settings`
   object so the DI seam `create_app(...)` used by tests (which passes `settings=None`) never
   autostarts — only the real `build_app` path does.

5. **Selectable Talk mode.** A per-browser toggle on the Talk card — **Hold to talk** (pointer /
   Spacebar, default) or **Click to toggle** (the prior behavior) — persisted in `localStorage`.

6. **Opt-in token persistence.** A "Remember on this device" checkbox at the gate (default off)
   stores the LAN token in `localStorage` for silent re-auth on refresh, with a forget affordance.
   This is a deliberate, opt-in relaxation of the previous in-memory-only rule (ADR 0022): the token
   is a LAN bearer credential and everything is already in the clear over RF, so at-rest storage on a
   trusted home machine is an acceptable convenience — but off by default.

7. **Approachable Settings.** A new `advanced: bool` on `SettingSpec` (serialized in the API) splits
   the schema into **Basic** (everyday: callsign, ID, timezone, squelch, service URLs, the two new
   toggles, TTS voice) and **Advanced** (VAD, DTMF, recording, scan, server/host/port, Baofeng
   tuning, announcements, CW). `SettingsView` renders Basic expanded and Advanced collapsed, each
   group a native `<details>` panel. Save/dirty-tracking is unchanged.

8. **Visual polish.** A self-contained pass over `web/src/styles.css` (spacing/type scale, calmer
   card surfaces, a state-colored pill, larger Listen/Talk targets, mobile). No new dependencies; a
   deeper redesign can follow separately.

## Consequences

- The audio-only Baofeng operator sees a Status card with just the state pill + Backend, matching the
  already-hidden Tune/Scan controls. The full-CAT V71/mock still shows everything.
- Removing the two cards removes no capability: `/ptt` and `/controller` endpoints remain, and the
  controller now runs by default when configured (a behavior change flagged by `controller.autostart`
  and revertable in Settings).
- Two new settings (`web.auto_listen`, `controller.autostart`) regenerate `radio.toml.example` and
  bump the settings count (47 → 49). The `advanced` flag is additive and defaults false.
- No hardware facts changed; guardrails 1–5 (empirical hardware config, PTT-not-over-CAT, capability
  split, gated auth, Part-97 ID) are all preserved — auto-ID in particular is *more* likely to run
  now that the controller autostarts.
