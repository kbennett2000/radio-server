# 0046 — Opening the OTA session from the web UI (the code chip is a button)

Status: Accepted

## Context

The masthead's OTA-code chip shows the current TOTP code so the operator can key it over DTMF.
When the operator is already at the browser, dialing the code back at themselves through the
radio is ceremony: the LAN token in their hand already keys the transmitter directly (`/ptt`,
`/services/{digit}` — the ADR 0034 control-operator posture). They asked for the obvious
shortcut: click the chip, get the session.

## Decision

`POST /auth/session` (token-gated) calls a new `Controller.open_session()`, which mirrors the
DTMF-accepted branch of `step()` — `AuthGate.open` flips the session, `station.begin_session`
arms the ID, the login announcement transmits, `auth_accepted` + `session_open` emit — with one
deliberate difference: **the TOTP gate is bypassed entirely and no code is burned.** The LAN
token is the credential (exactly `trigger()`'s rationale); consuming the displayed code would
lock an RF caller out of that 30-second window for no security gain. Opening an already-open
session transmits nothing and just stamps `last_activity` (a keep-alive), returning
`{"opened": false}`.

The chip (`TotpCard`) becomes a real `<button>`: same LCD face, `aria-label`, green-lit
`totp-chip-open` state driven by the existing WS `session` events. The endpoint 503s when no
controller is configured, like `/services`.

## Consequences and trade-offs accepted

- A UI open emits `auth_accepted` and lands an `auth_accepted` ledger record even though no code
  was verified. Accepted: a `session_open` with no auth record would be a hole in the audit
  trail, and the LAN token *is* an authentication. The adapter still records no code (ADR 0019).
- On-air behavior is identical to a DTMF login — the welcome over and station-ID obligations
  begin (guardrail 5) — so a chip click transmits. That is the point, but it means the click is
  not a silent action; the inactivity timeout closes the session as usual.
- Anyone holding the LAN token can open an RF session without TOTP. Not a new capability: the
  token already keys TX directly, and TOTP remains the gate for the *over-the-air* plane only.
