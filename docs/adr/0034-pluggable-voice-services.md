# 0034 — Pluggable voice-service architecture

Status: Accepted

## Context

By cycle 39 there are six registry voice services — time (1#), weather (2#), astronomy (3#), quote
(5#), battery (6#), bible (7#) — plus two controller built-ins (4# play station ID, 99# logout).
The six were grown one ADR at a time (0004, 0033) and by now share one nearly-identical shape: a
pure `format_spoken_*`, a construction-time `<svc>_service(...)` factory, a `register(registry, ...)`,
a `<svc>.base_url` config gate, and a `<svc>_DIGIT`/`_NAME`/`_DESCRIPTION` constant triple.

The regularity is real, but two frictions remain:

1. **Adding a service is a surgical edit to one imperative block.** `build_controller` hand-wires
   every service (import, `load_*`, `if <url>:` guard, `register_*`, manual catalog entry). Adding
   the seventh service touched that block; so will the eighth.
2. **The digit is a hardcoded module constant.** An operator cannot re-lay-out their keypad without
   editing code — yet the services are, by the operator's own framing, "custom to me and my network;
   other people may want other options."

This ADR formalizes the existing `ServiceRegistry`/`Service`/`ServiceContext` seam (unchanged in
shape since 0004) into a small **plugin contract**, retrofits the six services onto it, and makes the
digit→service map **operator-assignable** in `radio.toml`.

## Decision

### A `ServicePlugin` contract, one declarative `PLUGINS` list

`radio_server/services/plugin.py` defines:

- `ServicePlugin` — a `Protocol` (structural, so a service module needs no import to conform):
  `id: str`, `description: str`, `enabled(settings) -> bool`, `build(ctx) -> Service`.
- `PluginBuildContext` — carries the resolved `Settings` and a **lazily-built, memoized shared
  `Fetcher`** (`fetcher()`), injectable for tests. The lazy build reproduces ADR 0033's "construct
  one `UrllibFetcher` on the first enabled fetch service" rule for free: only a fetch-backed plugin
  ever calls `ctx.fetcher()`, and the first call builds the single instance the rest reuse.
- `PLUGINS: tuple[ServicePlugin, ...]` — the in-tree registry, one entry per service module. This is
  the single list a new in-tree service is added to.
- `build_registry(plugins, bindings, ctx) -> ServiceRegistry` — for each bound `(digit, id)`, if the
  plugin is `enabled`, register it. A bound-but-disabled service (its URL unset) is simply not
  registered: the digit stays a **graceful miss**, exactly as before.

Each service keeps its existing pure formatter and `<svc>_service(...)` factory **verbatim**; the
plugin is a thin adapter (`enabled` reads the `base_url`, `build` calls the factory). The `_DIGIT`
constants stop being the runtime source of truth (the digit now comes from bindings) and are retired
into `DEFAULT_BINDINGS`; `_NAME` becomes the plugin `id`.

### Operator-assigned digits via a separate `[services]` config channel

`radio.toml` gains an optional table mapping digit → plugin id:

```toml
[services]
"1" = "time"
"8" = "quote"     # remapped from the default 5
```

- Absent → `DEFAULT_BINDINGS` (`{"1":"time","2":"weather","3":"astronomy","5":"quote","6":"battery",
  "7":"bible"}`), so a deployment with no `[services]` table behaves exactly as today.
- `resolve_bindings` **fails loud** on an unknown plugin id, a non-DTMF digit, or a reserved digit.

This is a **separate config channel, like secrets** (ADR 0025 precedent), *not* a `SettingSpec`: the
table has arbitrary digit keys that the fixed one-spec-per-key schema cannot model, and routing it
through `resolve_settings` would trip its unknown-key rejection. `load_settings` peels the top-level
`services` table off before schema resolution; `load_service_bindings` reads it back. The scalar
service settings (`weather.base_url`, `bible.translation`, …) stay in the `SettingSpec` schema
unchanged — so the settings canary stays 47 and no services↔config import cycle is introduced (config
already imports service `DEFAULT_*` constants).

### The built-ins 4# / 99# stay controller-owned, and their digits are reserved

Play-station-ID and force-logout need `StationId`/`Session` authority that `ServiceContext`
deliberately withholds (a service must not key TX directly or end a session — ADR 0004, guardrails 2
& 4). They remain handled in `Controller._run_command`, keep their manually-appended catalog entries,
and `resolve_bindings` rejects any attempt to bind a plugin to `4` or `99`.

### Scope: in-tree only

`PLUGINS` is a hand-maintained in-tree tuple — **no** pip/entry-point auto-discovery. Auto-running an
externally-installed plugin that keys the licensee's transmitter is a Part-97 / guardrail-4 trust
decision and is out of scope here. The contract is intentionally shaped so external discovery could
be added later behind an explicit operator opt-in without reopening it.

## Consequences

- **Adding an in-tree service** becomes: write the module (formatter + factory + a small plugin),
  add its scalar settings to the schema, append it to `PLUGINS` and (optionally) `DEFAULT_BINDINGS`.
  `build_controller` is no longer touched — the imperative registration block is gone.
- **Operators own their keypad.** Any operator can remap digits, or point two digits at one service,
  from `radio.toml`; a typo (unknown service, reserved digit) fails loud at startup, not silently.
- **Behavior is preserved** for existing deployments: with no `[services]` table and the same
  `base_url`s set, the registered digits are identical to pre-refactor. Every per-service
  formatter/factory test passes unchanged, because those functions are untouched.
- The `[services]` table survives the settings-write API (`save_settings` only rewrites schema keys
  via tomlkit and never removes other tables) — verified by test.
- Supersedes the "adding a service = write a `Service` and `register` it in `build_controller`"
  consequence of ADR 0004 and the per-service `register()` mold of ADR 0033: the `register()` free
  functions are replaced by plugin objects; the pure formatters and factories they wrapped remain.
