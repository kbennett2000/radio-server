# 0043 — The link-off combo works without a login

Status: Accepted

## Context

Every DTMF combo keyed over RF sits behind the TOTP gate (`AuthGate.on_dtmf`): an
unauthenticated entry is only ever interpreted as a login attempt, so an unauthenticated `73#`
is a failed TOTP code and dies silently. That made ADR 0042's disconnect combo unusable in the
exact situation it exists for: the operator connects a net, sits back and listens, the session
times out after five idle minutes — and now *dropping* the link requires reading a fresh TOTP
code and logging back in first. Keying six digits plus the combo just to hang up is the wrong
shape for "I'm done listening."

## Decision

The **disconnect combo** (`mumble.disconnect_dtmf`, default `73#`) is the one RF command that
bypasses the TOTP gate. `Controller.step` intercepts it *before* `AuthGate.on_dtmf` and runs the
existing `_run_command` link-off branch directly: `on_link(None)`, the spoken confirmation
(station-ID-prepended when due), the `link {entry: None}` event.

Connect combos (`entry.dtmf`) remain gated exactly as ADR 0042 argued: connecting enables
Mumble voice to key the transmitter, so it sits behind the login like every capability-granting
command. Disconnecting only ever *removes* capability.

## Consequences and trade-offs accepted

- **Anyone on frequency can key `73#`** and drop the link (and trigger the short "Link off."
  confirmation over). Accepted: that actor is already on the air and could just as easily talk
  over the net; the command grants no TX-enabling capability, and the de-escalating failure mode
  (link drops) is the safe one. Guardrail 4 — auth strength matched to what a command can do —
  is the reasoning, applied in the permissive direction for once.
- **The session is untouched.** The intercept happens before the gate, so keying `73#` neither
  stamps `last_activity` (a disconnect never extends a session) nor burns a TOTP attempt. An
  authenticated operator keying `73#` gets identical behavior through the same path.
- **Matching precedence:** link-off is checked before TOTP verification. Exact-string matching
  keeps this safe — codes are six digits, combos are load-validated (`validate_link_digits`)
  against the keypad map and the connect combos.
- Deployments with no `[[mumble.servers]]` entries have an empty link-off set; nothing changes
  for them (`73#` unauthenticated stays a rejected login attempt).
- The web `POST /link` path is LAN-token-gated and TOTP-independent already — unaffected.
