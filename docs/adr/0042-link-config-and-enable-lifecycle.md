# 0042 — Link config, composition, and the enable lifecycle

Status: Accepted

## Context

ADR 0041 shipped the `Link` port — the protocol, `MockLink`, and the `create_link`
factory — as a **pure** cycle: nothing in the running app constructs or reaches a
Link. This cycle closes that gap. It makes a Link real in the composed app, gives
it a config section and an HTTP surface, and — the load-bearing part — pins the
**enable lifecycle** every later audio cycle will obey.

It deliberately routes **no audio**. A Link is a peer on the audio bus, but wiring
RF↔network audio splits by direction across later cycles because the two directions
carry very different risk: local RF → the internet (`link.transmit`) is third-party
traffic leaving the station; the internet → the antenna (`radio.transmit`) is
putting arbitrary network audio on the air under the licensee's callsign. Neither is
plumbed here. This cycle establishes only *who exists* and *the gate that guards
them*.

A `Link` is a **peer collaborator** to the `Radio`, not a `Radio` backend — it is
the network side, not a second antenna. So it mirrors the `controller` precedent
(ADR 0012/0037), not the radio-backend factory branch: an optional collaborator that
`create_app` accepts and `build_app` constructs from config, reporting a clean 503
when the deployment did not configure it.

## Decision

- **`link.backend` selects the Link; `none` is the default.** A single new setting,
  `link.backend` (`none` | `mock`), mirrors `server.backend` exactly — a plain
  string coerce whose value `build_app` dispatches on, with `create_link` raising on
  an unknown name (the same validation posture as `server.backend`). `none` (the
  default) builds no Link; `mock` builds `MockLink`. Real backends (M17/mrefd,
  AllStar) register later with no consumer change.

- **There is no `link.enabled` key — and the absence is the feature.** Non-stickiness
  (ADR 0041) means enable is a **runtime act, not a persisted setting**. A config key
  that could enable a Link would let a reboot put a transmitter on the internet
  unattended — exactly the `controller.autostart` × sticky-enable composition ADR
  0041 forbids at the leaf. So the schema simply has no such key. The app **always
  comes up with the Link disabled**, regardless of config, regardless of
  `controller.autostart`; there is **no code path from startup to enabled**. Enabling
  requires an explicit `POST /link/enable` and nothing else.

- **A Link is a `create_app` collaborator, wired like the controller.** `create_app`
  gains a `link: Link | None = None` keyword arg, stashed on `app.state.link`;
  `build_app` constructs it from `link.backend` and injects it. Tests inject a
  `MockLink` directly through the same seam. When it is `None`, every `/link` route
  answers **503 "link not configured in this deployment"** — the identical fail-loud
  shape `POST /controller` uses when no controller is wired, never a silent no-op.

- **A `/link` surface on the existing bearer-gated router.** `GET /link` returns the
  `LinkStatus`; `POST /link/enable|disable` toggle the enable flag; `POST
  /link/connect|disconnect` join/leave a reflector/target; `GET /link/directory`
  returns the peer directory. The directory route **501s by name** when the backend
  lacks `DIRECTORY` (guardrail 3) — it names the missing capability rather than
  returning an empty list that pretends the feature exists. The routes are thin: one
  Link method each. In particular `connect` is **not** gated on `enabled` here —
  `enabled` gates *audio routing*, which is a later cycle's concern; conflating the
  two now would bake a policy the wiring cycle should own.

- **Link lifecycle events reach the hub and the ledger.** Each state change publishes
  a `link` event on the shared `EventHub` (the inline `/ptt` idiom, since these
  originate at the API layer — no sub-engine adapter needed), and the ledger's
  `_record_for` gains a `link` branch mapping the phase to a `link_enabled` /
  `link_disabled` / `link_connected` / `link_disconnected` record. It follows the
  whitelist discipline (ADR 0018): only `target` is copied, on connect — never a
  wholesale `event.data` copy, never the station roster.

## Consequences

- With `link.backend = "mock"` the app boots **disabled**, `GET /link` reports it,
  and enable/connect work over HTTP — the whole surface is exercisable with no
  network, against `MockLink`.
- The dangerous autostart × sticky-enable composition **cannot emerge**: the schema
  offers no enable key and no startup path sets the flag, so the safety rule ADR 0041
  pinned is now enforced in the composed app, not just the leaf. A test boots the app
  under every plausible config (including `controller.autostart` on) and asserts the
  Link is disabled.
- The `/link` surface is complete before any audio flows, so the wiring cycle adds
  only the RF↔network bridge — obeying the rule already in place (never auto-enable;
  gate routing on `status().enabled`) — not the control surface.
- **Trade-off:** the surface exists ahead of the audio it will eventually guard, so
  today `enable`/`connect` change reported state and emit ledger records but move no
  audio. That is intentional — pinning the gate and its lifecycle *before* the
  audio-routing cycles is the whole point, so those cycles inherit a settled rule
  rather than inventing one alongside the plumbing.
