# 0027 — Web UI settings screen (schema-driven rendering, dirty-tracking PATCH, atomic-error UX, restart-to-apply banner, write-only secret rotation with once-shown token + QR re-enroll)

Status: Accepted

## Context

The config arc spans three cycles. Cycle 25 made `radio.toml` a schema-driven config with a
`SettingSpec` registry as the single source of truth. Cycle 26 exposed that registry over HTTP: a
token-gated `GET /settings` (schema + current values, secrets as presence-only), `PATCH /settings`
(atomic, schema-validated write that round-trips the file preserving comments), and two write-only
secret-rotation endpoints (`api-token/rotate`, `totp/enroll`). Until now the operator still edited
`radio.toml` by hand. This cycle adds the **browser settings screen** so the operator reads and
edits config — and *understands* each setting from the schema's human `description` — without
touching a file or the docs. It closes the arc.

It is a **pure client feature** over the cycle-26 endpoints — the mirror of the RX/TX audio cycles
(22–24): a browser screen over an **unchanged backend**. Apply semantics stay
**restart-to-apply (v1)**: writes persist to `radio.toml` but do not hot-reload the running server.

**Verified, not assumed.** Before building, the cycle-26 contract was read against
`tests/test_settings_api.py` and confirmed sufficient for the UI: `GET /settings` returns
`{key, group, type, choices?, default, value, required, description}` per setting plus a
presence-only `secrets` block; `PATCH`'s atomic `400` returns `{"detail": "<string naming the bad
key>"}` (surfaced verbatim by the existing `readDetail` in `api.js`); the two rotate/enroll
endpoints accept a **bodyless POST** and return the fresh secret exactly once. **No backend change
was needed.** The standing rule (as in the audio cycles) held: had a real gap surfaced in the
browser walkthrough, the fix would be a minimal backend change **plus a pytest** — never a
client-side workaround.

## Decision

### Schema-driven rendering — the form builds itself from `GET /settings`

`SettingsView` fetches `GET /settings` once and renders **entirely from the response** — there is no
hardcoded field list anywhere in the UI. Settings are grouped by their `group` (in the order the
schema presents them) into `.card` sections (Station/Identity, Audio/Squelch, Recording, TTS, Time,
TX, Logging, Server/Web — whatever groups the registry carries). Each field is rendered by
`SettingsField` **by `type`**:

- `string` → text input; `integer`/`number` → number input (step `1` / `any`), parsed back to a
  Number; `boolean` → a toggle switch (styled checkbox); `enum` → a `<select>` over `choices`.

Every field shows its label (the last dotted segment, title-cased, with the full dotted `key` shown
muted for unambiguity), its current value, and — the point of the whole arc — the schema
**`description` as always-visible inline help** (not hover-only), so the operator understands each
setting without the docs. **Adding a setting to the registry later needs zero UI change.**

### Required + required-unset are flagged

A `required` setting gets a "required" tag. A required setting served with `value: null` (e.g.
`station.callsign` before it is set) is flagged **"needs setting"** in amber so the operator
immediately sees the one field that must be filled — the UI face of the config's `UNSET_REQUIRED`
sentinel.

### Dirty-tracking PATCH — only changed keys are sent

`SettingsView` tracks an `edited` map of `key → newValue` for fields changed from their served value;
reverting a field to its original clears it. Save PATCHes **only** `edited` (`{values: edited}`),
never the whole form. Save is disabled when nothing is dirty.

### Atomic-error UX — the 400 names the key, and edits are preserved

On a successful PATCH the UI shows a **restart-to-apply banner** built from the response's
`restart_required` list, then re-fetches `GET /settings` and clears `edited` (the form now shows the
persisted values). On the atomic `400`, the rejected key is parsed out of the error detail and shown
as an inline error on that field; the form **keeps all the operator's edits** (nothing is wiped, and
nothing was written server-side — the whole patch was rejected). This makes the cycle-26 atomicity
visible and non-destructive.

### Honest restart-to-apply

Every write path says changes are not live until restart. The banner after a settings save, and the
note after a secret rotation, both state this plainly — matching the backend's `"apply": "restart"`.

### Write-only secret rotation — once-shown token (+ copy), QR re-enroll

`SecretsPanel` shows `api_token` and `totp_secret` as **present / absent** only, never a value.
- **Rotate API token** reveals the returned token **once** in a highlighted box with a copy button
  and a warning. Honest wording: the new token becomes active **after the server restarts**; the
  current session keeps working until then (restart-to-apply — the running server still accepts the
  old token). A "Return to token gate" action lets the operator re-enter the new token when ready.
- **Re-enroll TOTP** renders the returned `otpauth://` provisioning URI as a **QR code** for phone
  re-scan, shown **once**, with the URI also shown as copyable text (fallback + accessibility). The
  endpoint only ever returns a freshly generated secret, never the existing one.

### One navigation change + the QR dependency

There is no router today, and Settings is a full-width multi-section screen too large for the 2-col
control grid. `ControlPanel` gains a minimal topbar **view toggle** (Control ⇄ Settings) that swaps
the main body; the single `/events` subscription and all existing controls are untouched.

QR rendering uses **`qrcode.react`** — a **zero-runtime-dependency**, MIT React component that emits
an SVG. It is the only new dependency, consistent with the project's minimal-deps ethos (`qrcode`
was rejected: it drags in yargs/pngjs). `web/dist` is gitignored, so the commit carries source +
`package.json`/`package-lock.json` only; the reviewer runs `npm install && npm run build`.

## Consequences

- **The operator edits config in the browser.** The settings screen renders every group and field
  from the schema with descriptions visible, saves only changed keys, and persists to `radio.toml`.
- **Backend unchanged.** No Python edit — the cycle-26 API was verified sufficient. `uv run pytest`
  stays 426 passed / 4 skipped. All new code is `web/src/` (`SettingsView`, `SettingsField`,
  `SecretsPanel`, `QrCode`), four `api.js` client methods, the `/settings` dev-proxy path, the
  `ControlPanel` view toggle, the `App` re-auth wiring, CSS, and the `qrcode.react` dependency.
- **Atomicity is visible.** An invalid value shows the rejected key inline and preserves the form —
  the cycle-26 all-or-nothing PATCH, surfaced honestly.
- **Restart-to-apply is honest, not hidden.** Every write says it needs a restart; rotation is clear
  that the current session survives until then.
- **Acceptance is a browser walkthrough** against the mock server (not pytest), per the brief:
  schema-driven render with descriptions; save PATCHes only changed fields and persists; the atomic
  400 shows the key and keeps edits; the restart banner appears; secrets show present/absent never a
  value; rotate shows the token once + re-auth prompt; TOTP re-enroll renders a scannable QR once.
- **Deferred, on purpose:** live hot-reload (restart-to-apply stays v1); server-side scan-stop (the
  standing, unrelated backend gap); the hardware backends.
