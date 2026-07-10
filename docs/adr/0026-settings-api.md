# 0026 — Settings REST API + secret rotation: the schema served over HTTP, atomic validated writes, write-only secret endpoints

Status: Accepted

## Context

Cycle 25 (ADR 0025) made the configuration a schema-driven TOML file: the `SettingSpec` registry
(`radio_server/config/spec.py`) is the single source of truth, `resolve_settings`/`save_settings`
read and round-trip `radio.toml` (preserving comments via tomlkit), and the two secrets live on a
separate 0600 channel with `save_secret`/`rotate` helpers built but not yet exposed.

Cycle 27 will build a web **settings screen** — an operator editing config from the browser. It
needs to *read* the config (with enough metadata to render a form) and *write* it back over the
token-gated API first. This cycle is that HTTP surface, and nothing more: **no new config logic**.

**Verified, not assumed.** The whole substrate already exists and is tested: the schema registry and
`BY_KEY`/`KNOWN_SECRETS`, `resolve_settings` (validates every value, raises naming the bad key),
`save_settings` (round-trips, skips required-unset, never writes secrets), `save_secret`/`rotate`
(write 0600, fail loud on a group/world-readable file), and `TotpVerifier.provisioning_uri`. The
endpoints are a thin serialize/validate/persist layer over these; the one real wiring change is
threading the config/secrets **file paths** to the app so writes know where to land.

## Decision

### The schema is served, not hand-coded
`GET /settings` serializes every `SettingSpec`: `key`, `group`, `type` (+`choices` for enums),
`default`, current `value`, `required`, and the human `description` (the text cycle 27 renders).
Adding a setting to the registry adds it to the API for free — there is no per-setting endpoint
code. `type` is **derived in the API layer** so the config package stays untouched: `bool→"boolean"`,
an `Enum` default→`"enum"` with `choices` off the enum class, `station.id_mode→"enum"` via its
`ID_MODES` coercer, `int`/`float` defaults→`"integer"`/`"number"`, else `"string"` (checking `bool`
before `int`, since `bool` subclasses `int`). A generic pass over the schema, not per-key logic.

### Secrets are presence-only on the read path
Secrets are not in `SETTINGS`, so they are structurally absent from the serialized settings — a
secret value can never leak through `GET /settings`. The response carries a separate `secrets` block
that reports **only** `{"set": bool}` per secret. This preserves the cycle-25 security split at the
API boundary: the config surface renders and round-trips settings; it never renders or returns a
secret.

### PATCH is atomic and schema-validated
`PATCH /settings` takes a partial `{key: value}` map. It rejects secret keys (pointing at the
rotation endpoints) and unknown keys up front, then validates the **whole** patch by resolving
`{current values} | patch` through `resolve_settings` — which coerces and range-checks every value
and raises, naming the offending key, **before any file write**. A single bad value rejects the
entire patch with a 400; the file is never partially written. Only on full success does
`save_settings` persist the round-tripped TOML. This makes "no partial writes" a property of reusing
the cycle-25 validator, not of new bookkeeping.

### Secret rotation is write-only
`POST /settings/secrets/api-token/rotate` and `POST /settings/secrets/totp/enroll` **generate** a new
secret (or accept a provided API token), `save_secret` it 0600, and return it **once** — the API
token in the body, the TOTP secret as an `otpauth://` provisioning URI to re-enroll an authenticator.
They never read an existing secret back; there is no GET that returns one. This is the only way a
secret leaves the server, and only a freshly-minted one, only once.

### Restart-to-apply (v1), signaled explicitly
Consistent with ADR 0025, writes persist to file but do **not** hot-reload the running server: the
composed components (and the API token `require_token` closes over, and the scan route's startup
settings) keep their startup values until the process restarts. Every write response says so —
PATCH returns `restart_required` (v1: every changed key), and the rotation endpoints note the
operator must re-authenticate / re-enroll after a restart. The cycle-27 UI shows a "restart to apply"
banner off this. `app.state.settings` is updated after a PATCH so `GET` reflects the persisted file,
but that display-state update is deliberately decoupled from the running behavior. Live hot-reload
remains a future cycle.

### Wiring
`build_app`/`create_app` gain the config and secrets **file paths** (defaulting to `radio.toml` /
`radio-secrets.toml`), stored on `app.state` alongside the resolved `Settings`/`Secrets`, so the
write endpoints know their targets; `python -m radio_server --config/--secrets` threads the CLI
paths through. The routes hang off the existing token-gated `api` router, so every settings endpoint
is closed by default exactly like the rest of the API.

## Consequences

- **The config is fully addressable over HTTP** — read with render metadata, written atomically —
  so cycle 27 can build the settings screen against a stable, schema-driven contract.
- **Secrets stay write-only and presence-only.** No endpoint returns an existing secret; rotation
  reveals a new one once. The read path cannot leak a secret because secrets are not in the schema.
- **No new config logic.** Validation is `resolve_settings`, persistence is `save_settings`/
  `save_secret`/`rotate` — the endpoints only serialize the schema and marshal errors to 400s.
- **Restart-to-apply is explicit in every write response**, keeping the honest v1 contract while the
  UI (27) and hot-reload (deferred) build on top.
- **Deferred, on purpose:** the web settings screen (cycle 27); live hot-reload; rendering the
  provisioning URI as a QR image (the `otpauth://` string is authenticator-ready as-is).
