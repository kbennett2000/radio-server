# 0051 — Slim the shipped service set; local service plugins

Status: Accepted

## Context

The repo ships six registry voice services (ADR 0034): time, weather, astronomy, quote, battery,
bible. The last four fetch from LAN services that exist only on the maintainer's network (ADR 0033),
and weather/astronomy share the same private dependency. Meanwhile the project's center of gravity
has moved to the Mumble link (ADRs 0041→0050): radio-server is now, first, a hotspot/browser UI for
talking on a Mumble server. The operator wants the repo template to reflect that:

- **Ship only what works everywhere**: station ID (`01#`), time (`02#`), the Mumble demo link
  (`10#`, ADR 0052), link-off (`98#`, ADR 0052), logout (`99#`).
- **The network-specific services leave the repo** — but must keep working on the maintainer's own
  deployment, unchanged in behavior and keypad layout.

The five are *code* (plugin modules), not config, so "keep them locally" needs a supported way to run
service plugins that are not in the tree. ADR 0034 scoped `PLUGINS` to in-tree only — no
pip/entry-point auto-discovery, because auto-running externally *installed* code that keys the
licensee's transmitter is a Part-97 trust decision — but explicitly shaped the contract so external
discovery "could be added later behind an explicit operator opt-in."

A second constraint: the settings schema **fails loud on unknown keys** (`resolve_settings`,
ADR 0025). Deleting the `[weather]`/`[quote]`/`[battery]`/`[bible]` specs would make any config still
carrying those tables refuse to boot — so out-of-tree plugins also need a sanctioned place for their
config.

## Decision

### 1. The shipped set shrinks to time + the built-ins; defaults renumber

`PLUGINS = (TIME_PLUGIN,)`. The weather/astro/quote/battery/bible modules, their scalar specs, and
their tests leave the repo. `DEFAULT_BINDINGS` becomes `{"01": "station-id", "02": "time",
"99": "logout"}` — two-digit codes, matching the link combos `10#`/`98#` (ADR 0052) in width so the
shipped keypad reads as one consistent scheme. Digits are exact strings end-to-end (framer, bindings,
registry), so leading zeros are safe. Existing deployments with an explicit `[services]` table are
unaffected; a deployment relying on the old implicit 1/2/…/7 layout loses only services that no
longer exist in-tree, and `time` moves 1#→02# (called out in the PR).

The shared-fetch plumbing (`fetch.py`: `Fetcher`, `UrllibFetcher`, `StubFetcher`) **stays and remains
exported** — it is the supported HTTP client for out-of-tree plugins. `PluginBuildContext.fetcher()`
now binds `DEFAULT_FETCH_TIMEOUT` (3.0 s) instead of the retired `weather.timeout`; a plugin that
needs a different timeout builds its own `UrllibFetcher` from its own config.

### 2. A `local_services/` folder, discovered at startup

`discover_local_plugins(directory)` (new `radio_server/services/local.py`) loads every top-level
`*.py` in `./local_services/` (CWD-relative, like `radio.toml`; gitignored) whose name doesn't start
with `_`, in sorted order, and collects each module's `PLUGIN` attribute where present. A module
without `PLUGIN` is a helper — importable by the others, because the folder goes on `sys.path` and
modules are loaded with ordinary `importlib.import_module`, so intra-folder imports
(`from weather_service import ...`) just work.

Fail-loud rules, all at startup: an import error propagates; a `PLUGIN` that doesn't satisfy the
`ServicePlugin` protocol raises; a duplicate id (against in-tree plugins, built-ins, or another local
plugin) raises. A missing or empty folder yields `()` — zero cost for everyone else.

`build_controller` gains a `plugins` parameter (default `PLUGINS`); `build_app` performs the one
discovery call and passes `PLUGINS + discover_local_plugins(...)` to the controller and to
`app.state.service_plugins` (used by the settings API's binding validation). `resolve_bindings` is
untouched — local ids resolve because the plugin set is what it validates against. DTMF binding is
unchanged: the operator maps digits to local plugin ids in the same `[services]` table.

**Part 97 posture (supersedes ADR 0034 §Scope):** this is not pip auto-discovery. Nothing lands in
`local_services/` unless the licensee puts it there; creating the folder and copying a file into it
*is* the explicit operator opt-in ADR 0034 reserved room for. The trust boundary — "code in this
deployment's working directory runs as the station" — is the same one `radio.toml` itself sits
behind.

### 3. A `[plugins.*]` config namespace, deliberately unvalidated

`load_settings` peels the top-level `plugins` table off before schema resolution (the `[services]` /
`[[mumble.servers]]` precedent), flattens its sub-tables to `group.leaf` keys **without** the
`plugins.` prefix, and carries them on the resolved `Settings` as a second, unvalidated mapping read
via `Settings.extra(key, default=None)`.

- A migrated plugin changes `settings.get("weather.base_url")` to
  `settings.extra("weather.base_url", "")` — nothing else.
- The schema stays strict: a stray top-level `[weather]` table still fails loud. Only `[plugins.*]`
  is the sanctioned free channel, and it is documented as such in `radio.toml.example`.
- `enabled(settings)` keeps its `Settings`-only signature — zero protocol change for existing
  plugins.
- The settings-write path (tomlkit parse-and-rewrite) already preserves unknown tables, so
  `[plugins]` survives every save — same guarantee `[services]` has (ADR 0034, verified by test).

## Consequences

- A fresh checkout keys exactly five things: `01#` ID, `02#` time, `10#` demo link, `98#` link off,
  `99#` logout — all of which work with no LAN services and no private infrastructure.
- The maintainer's deployment migrates by copying the five modules into `local_services/`, switching
  their relative imports to absolute (`radio_server.services.…`) and their config reads to
  `settings.extra(...)`, and renaming `[weather]`→`[plugins.weather]` (etc.) in `radio.toml`. The
  existing `[services]` table (01…07, 99) keeps the keypad identical.
- "Add your own spoken service" becomes a first-class operator story: drop a file in
  `local_services/`, bind a digit in `[services]`, restart.
- `local_services/` shares `sys.path` with the app: a module named like a stdlib/dependency module
  could shadow it. Documented in the example-file comment; discovery does not try to outsmart it.
- The settings-spec canary in `test_config.py` shrinks by the retired specs; `radio.toml.example`
  regenerates (it is asserted byte-equal to the generator).
