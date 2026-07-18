# 0083 — A fixed over-RF login code option (and a collapsible Mumble panel)

Status: Accepted

## Context

Two operator-requested changes to the browser **Settings** screen (ADR 0027).

1. **The Mumble servers panel didn't fold like the other settings.** The settings screen renders each
   scalar setting group as a native collapsible `<details className="settings-group">`
   (`SettingsView` `GroupPanel`), but `MumbleServersPanel` — a bespoke editor, because
   `[[mumble.servers]]` is a list the flat schema form can't render — was a plain non-collapsing
   `<section className="card">` bolted on at the bottom, standing out from everything above it.

2. **The over-RF login code was TOTP-only.** The auth plane (ADR 0003/0048, guardrail 4) verifies a
   rotating TOTP code as DTMF (`TotpVerifier`, single-use burn, ±1 window). Some operators would
   rather set a **fixed** code once and key it every time, without an authenticator app. A fixed code
   is fundamentally weaker — it never changes, so it can be overheard and replayed, and the
   single-use burn cannot apply — so it must be **opt-in**, non-default, and clearly warned.

## Decision

### Mumble panel (cosmetic)

Wrap `MumbleServersPanel` in the same `<details className="settings-group">` / `<summary>` /
`.settings-group-body` shape the schema groups use (with a "N servers" count chip), so it folds like
the rest. Native `<details>`, no new state; the existing marker/rotate CSS applies. Its load/save
lifecycle and endpoints are unchanged.

### Fixed login code

Model auth as a derived mode — **off / TOTP / fixed** — from two non-secret settings, keeping the
existing one untouched (non-breaking):

- `auth.totp_enabled` (unchanged): the master "require a login code" gate.
- **`auth.fixed_code`** (new, bool, default `false`): use a fixed operator-set code instead of
  rotating TOTP. Effective only when `auth.totp_enabled` is on and a code is set.

The code itself is a **credential**, so it lives on the secrets channel (`fixed_code` /
`RADIO_FIXED_CODE`), never in `radio.toml` or the `GET /settings` payload (ADR 0025) — set write-only
from the UI, exactly like Mumble private passwords.

- **`FixedCodeVerifier`** (`radio_server/auth/fixed.py`) presents the same `verify_and_burn(code,
  now)` surface `AuthGate` consumes, so it drops in wherever `TotpVerifier` would with **no** change
  to the gate or session machine. It constant-time-compares against the fixed code and **never
  burns** — the same code authenticates every login (the documented downgrade).
- **`build_controller`** takes `fixed_code` and selects the verifier by `auth.fixed_code`;
  `Controller.auth_method` (`"totp"`/`"fixed"`) records the running scheme. `build_app` extends its
  controller-build gate so the controller also builds in fixed mode when a code is set (and is
  byte-identical to before when `auth.fixed_code` is off).
- **`GET /auth/totp`** reports `{"enforced": true, "fixed": true}` in fixed mode and **never** echoes
  the write-only code (`503` if fixed is selected but unset); the masthead card shows a locked
  "fixed code" chip instead of a rotating code. **`POST /settings/secrets/fixed-code`** sets the code
  (write-only, validated to exactly 6 digits, restart-to-apply).
- The UI surfaces the warning twice: the `auth.fixed_code` toggle's schema description (rendered in
  the settings grid) and a prominent inline caution on the Secrets "Fixed login code" control.

## Consequences

- A fixed code is strictly weaker than TOTP: no rotation, no window, no single-use burn — replayable
  by anyone who overhears it. That is the point of the warnings and the non-default. It remains
  "gated, not secure" (guardrail 4), just more so; automatic station ID (guardrail 5) is unaffected.
- No breaking config change: `auth.totp_enabled` keeps its meaning; existing deployments (with
  `auth.fixed_code` defaulting off) behave identically and still use rotating TOTP. One new schema
  setting (settings-count canary 64 → 65; `radio.toml.example` regenerated). One new secret name
  (`fixed_code`), which the existing generic secrets loader/saver handle.
- The endpoint reports the **running** scheme (honest under restart-to-apply), matching how
  `enforced` already behaves; when no controller is wired it falls back to the persisted setting.

Cross-refs: ADR 0003/0048 (the auth plane and its enforce toggle), ADR 0025 (secrets never in the
settings surface), ADR 0027 (the settings screen), ADR 0042/0052 (the write-only Mumble password
channel this mirrors).
