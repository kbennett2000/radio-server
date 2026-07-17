# 0059 — Removed services ship as examples; shape changes ship a named migration error

Status: Accepted

## Context

ADR 0051/0052 (commit `d2ff286`) made three breaking changes to a live `radio.toml` and shipped a
migration error for **none** of them. Nothing re-reads `radio.toml` until a restart, so a deployment
hit all three in sequence months later — on the first restart after an upgrade. Each error was loud,
correct, and useless.

Constraints found in the tree (walking the actual failure the deployment saw):

- **Plugin settings moved to the `[plugins.*]` channel.** A config still carrying a flat `[weather]`
  table gets the generic `resolve_settings` error — `radio_server/config/settings.py:127`,
  `"unknown setting(s): weather.base_url, …; not in the config schema"`. It never says the keys are fine,
  just misplaced, nor where they belong.
- **weather/astronomy/quote/battery/bible left the tree for `local_services/`.** A `[services]` binding
  still naming one raises at `radio_server/services/plugin.py:152`,
  `"[services] '03' = 'weather': unknown service or command"`. It never names `local_services/` — the
  folder the id must now come from — and `discover_local_plugins` returns `()` silently on a missing
  folder (`radio_server/services/local.py:41-42`), so an operator whose folder doesn't exist gets no
  hint that it's even a thing.
- **The five plugin files were deleted outright.** Recovering them meant `git show d2ff286^:…` and a
  hand-written `sed` script — an upgrade path of "reconstruct from a commit that no longer exists in a
  shallow clone." ADR 0051's reasoning (the in-tree `PLUGINS` *registry* should hold only what works
  everywhere) was sound, but it deleted shipped, running *features*, not just registry entries.

The precedent for doing better is in the same file: `_LEGACY_MUMBLE_KEYS`
(`radio_server/config/settings.py:56-58`) gives a leftover flat `[mumble]` key a tailored migration
error in `_flatten` instead of the generic unknown-key message. This ADR extends that habit and closes
the archaeology gap.

## Decision

**When a cycle changes a shape `radio.toml` can hold — or an interface local plugins bind to — it ships
a named migration error. And anything removed that operators were running ships as an example, not as a
git-archaeology exercise.** Concretely, for the three 0051/0052 breakages:

- **The five services get a committed home.** `examples/local_services/` ships all five, already ported
  (absolute imports; `settings.extra(...)` for their own keys; astronomy's bare
  `from weather_service import …`, since the folder joins `sys.path`). They are **not** in `PLUGINS` and
  **not** imported by the app — they are files to copy. `cp examples/local_services/*.py
  local_services/` is now the whole upgrade path, and the "add your own services" docs get five working
  references instead of prose. `.gitignore`'s `/local_services/` is anchored, so the examples commit
  normally while the operator's own folder stays ignored.
- **One import test carries the weight.** `tests/test_examples_local_services.py` imports every module
  under `examples/local_services/` through the real `discover_local_plugins` and asserts each exposes a
  valid `PLUGIN`. That is the point: it catches interface drift in `Fetcher` / `ServiceContext` /
  `Service` / `ServicePlugin` in CI, before a station hits it at restart. The five per-service unit
  tests `d2ff286` deleted are **not** restored — the one import test is the load-bearing unit.
- **`resolve_settings` names the `[plugins.*]` home.** An unknown key whose namespace isn't a schema
  group (`weather.base_url` → no `weather` group) is almost always plugin settings left out of
  `[plugins.*]`. It now raises `"unknown config table(s): [weather] (weather.base_url) ->
  [plugins.weather]. … only the TOML nesting moves. See examples/local_services/."` A real typo whose
  namespace *is* a schema group (`server.prot`) still gets the generic `"not in the config schema"`
  message — the split is by namespace, derived from `{s.key.split(".",1)[0] for s in SETTINGS}` (there
  is no namespace constant; the schema groups *are* the namespaces).
- **`resolve_bindings` names `local_services/` and the example file.** The existing
  `"unknown service or command; known ids are […]"` prefix is preserved (tests and muscle memory match
  it); appended is where ids come from (`./local_services/`), and — if the id is one of the five
  0051 removals (`_REMOVED_IN_0051`) — the exact file to copy
  (`examples/local_services/weather_service.py`). When the folder is absent it says so. The
  `DEFAULT_LOCAL_SERVICES_DIR` constant is lazy-imported from `.local` inside the function to avoid the
  `local`↔`plugin` import cycle.

**Precedence when both a stray plugin table and a real typo are present:** the plugin-migration hint
wins (it's the station-down case); the operator fixes it, re-runs, and then sees any residual typo. A
bare top-level scalar (no `.`) falls to the plugin-hint branch harmlessly — it still points at a
`[plugins.<name>]` home.

### Alternative considered — raise the plugin hint in `_flatten`, beside `_LEGACY_MUMBLE_KEYS`

`_flatten` is where the mumble migration lives, so it's the tempting spot. But `_flatten` sees the
nested `(table, leaf)` shape one table at a time, while "is this namespace a schema group?" is a
whole-schema question that `resolve_settings` already answers when it computes `unknown`. Putting the
check there reuses the existing unknown-key set, keeps `_flatten` a pure shape transform, and naturally
yields the "typo vs migration" split. `_LEGACY_MUMBLE_KEYS` stays in `_flatten` (it's a fixed legacy-key
set, not a namespace question) and its behaviour is unchanged.

### Alternative considered — restore the five services in-tree

That would undo ADR 0051's deliberate slimming (the in-tree registry ships only what works everywhere)
and re-introduce five network-coupled services and their tests into the default install. Examples keep
0051's boundary intact — nothing auto-loads — while still handing operators a working file to copy.

## Consequences

- Upgrading from a pre-0051 config is now self-describing: the flat-table error names its
  `[plugins.…]` home, the unknown-service error names the example file, and the file exists to copy.
  No git spelunking.
- `examples/local_services/` is a maintenance surface: the new import test is what keeps it from
  silently rotting when the plugin interfaces evolve — a port that breaks `ServiceContext`/`Fetcher`
  fails in CI, not on a station.
- The rule generalises: future cycles that move a config shape or a plugin-facing interface owe a named
  migration error and (for removed running features) an example, the same way they already owe an ADR.
- `PLUGINS` is untouched (`("time",)`); the app does not import `examples/`. The examples are
  copy-source only.
