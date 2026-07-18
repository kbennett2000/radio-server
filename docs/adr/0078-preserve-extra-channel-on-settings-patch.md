# 0078 — Preserve the local-plugin (extra) channel across a settings patch

Status: Accepted

## Context

ADR 0076 shipped the live backend switch: `POST /radio/select` tears the pipeline down and rebuilds
it against the newly-selected radio. On the operator's two-radio box, switching AIOC ↔ kv4p made every
local-plugin service (weather, astronomy, quote, battery, bible) **vanish** from the web UI. A full
server restart brought them back; the next switch dropped them again. Confirmed on hardware and in the
code.

### Root cause

The select handler rebuilds the settings object from **schema keys only** and revalidates them:

```python
# api/app.py, POST /radio/select
base = {spec.key: current.get(spec.key) for spec in SETTINGS if current.is_set(spec.key)}
new_settings = resolve_settings({**base, "server.backend": target})   # no extra=
```

The `[plugins.*]` blocks are not schema settings — they ride the **extra channel** (ADR 0051),
carried on `Settings._extra` and read via `settings.extra("<name>.base_url")`. `resolve_settings(raw,
extra=None)` defaults `extra` to `None`, so `new_settings` has an **empty** extra channel. The handler
then:

- `app.state.settings = new_settings` — propagating the stripped settings app-wide; and
- `holder.rebuild(new_settings)` — which rebuilds the controller via `controller_factory(new_settings,
  radio)` → `build_controller(new_settings, …)`. Each local plugin gates `enabled(settings)` on
  `settings.extra("<name>.base_url")`, which now returns `""`, so every one is filtered out of the
  registry and drops out of `controller.service_catalog` (what `GET /services` returns).

The failure is **runtime-only**. `save_settings` is a tomlkit round-trip that rewrites only schema
keys and leaves the on-disk `[plugins.*]` tables untouched (ADR 0051), so a clean restart re-reads the
file and restores the services. But every live switch re-strips them until the next restart.

The web UI was **not** at fault: the panel faithfully rendered a catalog that had genuinely shrunk.

### The audit — this is a class of bug, not one line

The switch rebuild was audited for other schema-only assumptions ("a switch must preserve everything a
fresh boot would load"):

- `load_service_bindings` (`[services]`), `load_mumble_servers` (`[[mumble.servers]]`),
  `configured_backends`, `validate_configured_backends`, and `backend_kwargs` all read from disk or use
  only schema keys — **switch-safe**. The `[services]`/`[[mumble.servers]]` deps are captured once in
  `build_app` and closed over in `controller_factory`, so they survive a switch intact.
- Only the extra channel rides on the `Settings` object, and it is the only channel the switch
  reconstructs from a schema-only projection — hence the only one dropped.

But the **identical idiom** — the "patch_settings idiom" (ADR 0026) — also lives in `PATCH /settings`
(`api/settings.py`): the same schema-only `base`, the same `resolve_settings(…)` with no `extra=`. So
any settings save also strips the live plugins channel off `app.state.settings` until a restart. Both
sites share one root cause and one fix.

## Decision

Round-trip the extra channel unchanged across the patch.

### A public whole-channel accessor on `Settings`

`Settings` exposed only the private `_extra` and the per-key `extra(key, default)` getter — no way to
read the whole channel back for a round-trip without reaching into the private field from a route. Add
a clean public accessor:

```python
def extras(self) -> dict[str, Any]:
    """The whole [plugins.*] extra channel (ADR 0051) as a copy."""
    return dict(self._extra)
```

It returns a **copy**, so `Settings` stays immutable — a caller cannot mutate the stored channel.

### Thread `extra=` through both patch sites

Both call sites pass `current`'s extra channel through the revalidation:

- `POST /radio/select`: `resolve_settings({**base, "server.backend": target}, extra=current.extras())`
- `PATCH /settings`: `resolve_settings({**base, **patch}, extra=current.extras())`

Nothing else changes. `holder.rebuild` → `controller_factory` → `build_controller` already flow
`new_settings` end-to-end, so the restored extra channel reaches every plugin's `enabled()` gate and
the catalog rebuilds intact. `save_settings` is unchanged (it already preserves on-disk `[plugins.*]`).

## Consequences

- A live backend switch preserves the local-plugin services: they stay in `controller.service_catalog`
  and on the UI across AIOC ↔ kv4p and back, with no restart.
- `PATCH /settings` no longer strips the live plugins channel off `app.state.settings` — an audited
  sibling fixed by the same one-line change.
- New tests:
  - `test_config.py` — the new `extras()` accessor (whole channel, returns a copy) and the patch-idiom
    round-trip (schema-only `base` + `extra=current.extras()` preserves a plugin key).
  - `test_backend_select.py` — a switch preserves the extra channel on the live `app.state.settings`,
    and (end-to-end, with a controller wired to an extra-gated local plugin) the plugin's service stays
    in `GET /services` across a switch **and the switch back**.
  - `test_settings_api.py` — `PATCH /settings` preserves the extra channel.
- No schema change: `radio.toml.example` byte-identical, settings-count canary unmoved. The existing
  switch tests (swap, rollback, capabilities re-emit, persistence) stay green.

## Non-goals

No UI change (the panel was correct — it showed a catalog that really had shrunk). No new backends, no
per-backend DTMF twist (ADR 0075 noted it for this arc — still global). No change to `save_settings`
or the on-disk format. Docs limited to this ADR + HANDOFF + the ADR index row.
