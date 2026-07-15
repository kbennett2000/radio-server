# 0046 — Optional over-RF auth (`controller.require_auth`)

Status: Accepted

Amends (does not supersede) ADR 0003. The session/auth state machine there is unchanged when auth is
on; this ADR adds a switch that bypasses it entirely when the licensee turns it off.

## Context

Over-RF TOTP auth (ADR 0003) gates every DTMF service behind a login. On a personal gateway that login
is friction: a repeater lets anyone key it, and a licensee running their own station may deliberately
want the same posture — digits in, service out, no challenge. This is the operator's call to make, and
this cycle gives them the switch: `controller.require_auth`, **default on** (today's behavior, unchanged).

**Be honest about what "off" means.** Guardrail 4 says *match auth strength to what a service can do* —
and **every** over-RF service keys the transmitter. "Announce the time" is a transmission. So with auth
off, **anyone on your frequency can key your transmitter, repeatedly, by sending digits.** That is a
legitimate choice a licensee may make on their own station. It is not a small one, and this ADR does not
soften it: turning auth off trades the guardrail-4 gate for convenience, knowingly.

Today auth is not even a setting — it is implied by the *presence of the TOTP secret*. `build_app` builds
the `Controller` only when `secrets.totp_secret` is set; otherwise the controller is `None` and
`/controller` + `/services` return 503. This cycle makes the intent explicit.

## Decision

- **`controller.require_auth`, a strict bool, default `true`.** A schema setting (so it appears in the
  admin Settings UI for free — no UI code), env `RADIO_CONTROLLER_REQUIRE_AUTH`. Default `true` reproduces
  today exactly, byte-for-byte; the entire existing auth suite stays green and untouched.

- **When `true`: nothing changes.** The controller is still gated on the TOTP secret's presence,
  `AuthGate.on_dtmf` still routes an unauthenticated session through `verify_and_burn` and an
  authenticated one to dispatch, and a `require_auth=true`-but-secretless deployment still 503s at
  `/controller`. ADR 0003 stands verbatim.

- **When `false`: the session/TOTP machine is bypassed.** `AuthGate` gains a `require_auth` flag; when
  off, `on_dtmf` dispatches the digits directly and returns `COMMAND` — no challenge, no session state,
  no idle timeout. This is the exact shape of the existing `Controller.trigger` operator path (dispatch
  without the TOTP gate), now reachable over RF by the licensee's choice. No `TotpVerifier` is built and
  **`RADIO_TOTP_SECRET` becomes unnecessary**: `build_controller` is called with `totp_secret=None`, and
  `build_app` builds the controller when *a secret is present OR auth is off*. So the `/controller` 503
  for a missing secret **must not fire in this mode** — the controller exists, it simply doesn't
  challenge.

- **The composition to pin, and this is the point of the ADR.** `require_auth=false` + an enabled Link
  = **anyone with an HT can command a station that is connected to the internet.** Today the blast radius
  of auth-off is "he makes it announce the time." Once inbound link control lands on the keypad (a later
  cycle), it becomes "a stranger connects your transmitter to any reflector." Decide the rule now, while
  it is free:

  > **`POST /link/enable` is refused, by name, when `controller.require_auth` is false.**

  Same fail-loud shape as the `audio.squelch="off"` refusal (ADR 0044): a 400 that states the reason and
  the key. Auth off is a choice the licensee may make; pairing it with a live internet link is not a
  choice this server makes silently on their behalf. Turning the link on requires turning auth back on.
  (One could argue the two settings are independent and the operator should be free to combine them —
  but the combined blast radius is categorically larger than either alone, and the refusal is trivially
  reversible by setting `require_auth=true`, so the safe default costs the operator nothing they can't
  undo.)

- **Unauthenticated TX access is never silent.** When auth is off, `build_app` logs a one-time WARNING
  at startup — the same warn-don't-fail posture as the recording+squelch rail (ADR 0021). A station that
  will key on anyone's digits should say so in its own log.

## Explicitly unchanged (regardless of this setting)

- **The LAN API token / `TokenGate`.** A *different auth plane* — over-RF TOTP guards who may command the
  station by voice; the LAN token guards who may reach the HTTP/WS API. Never conflated. Every REST
  endpoint stays token-gated whether `require_auth` is on or off. This cycle does not touch it.
- **Automatic station ID and the Part 97 scheduler.** Unchanged. Every transmission the server makes is
  still the licensee's station and is still identified on the <=10-minute interval and at session end.
  Auth being off changes *who may trigger* a transmission, never *whether it is lawfully identified*.
- **The session idle timeout.** Still applies when auth is **on**. Auth-off has no session, so there is
  nothing to time out — by construction, not by omission.

## Consequences

- **Default deployments are identical to before.** `require_auth` defaults on; the only new surface is a
  settings row, the canary count (54 → 55), and the regenerated `radio.toml.example`.
- **A licensee can knowingly run an open gateway** — digits dispatch with no login — and the server makes
  that intent explicit (a setting, a startup warning) rather than implicit (a missing secret).
- **The dangerous composition is blocked before it can exist.** The inbound-link-control cycle can wire
  `link.receive() → radio.transmit()` without re-litigating auth: an open gateway simply cannot enable
  the link, decided here while the rule is cheap.
- **Still ahead:** the wiring cycle that drives `TxLimiter` (ADR 0045) from the `radio.transmit()` path,
  and routing inbound link audio to the transmitter — each its own ADR + PR, each landing on a mainline
  where "open gateway + live link" is already refused.
