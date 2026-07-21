# 0115 — Channel presets: server-side named tuning entries

Status: Accepted

## Context

Radio channels live **server-side**, not in any radio's memory. ADR 0111 settled this for the UV-K5
dock (no memory-channel select), and the `CatRadio` backends deliberately omit `SET_CHANNEL` — kv4p and
uvk5 advertise `SET_FREQUENCY`/`SET_TONE`/`SET_MODE`/`SCAN` but not `SET_CHANNEL`. A "channel" is
therefore a host-side **preset**: a `{frequency, tone?, mode}` triple the operator names in `radio.toml`
and applies through the existing tuning surface. The feature was named-and-deferred across the UV-K5 arc
(ADR 0111/0112, `backends/uvk5/__init__.py`). The desk goal it serves is **monitoring a repeater's
output from the browser** by applying a named simplex entry.

This cycle builds the model, config loading, the apply path (with interlocks), and the HTTP API. **The
web UI is a separate cycle**; `curl` is the acceptance interface here.

## Decision 1 — scope: simplex-only v1; split/offset is a named follow-on

A preset is a **simplex** entry (RX = TX). That exactly serves repeater-**output** monitoring from the
desk. Transmitting *through* a repeater needs split/offset (a TX frequency ≠ RX frequency), which **no
current `CatRadio` surface supports** — `set_frequency(hz)` is a single frequency; kv4p and uvk5 are both
simplex-only. Split/offset is recorded here as a follow-on arc that would touch the `CatRadio` interface
itself (a new `set_split`/offset concept every backend must implement), **not** something to smuggle into
a preset field now.

## Decision 2 — the `[[presets]]` channel + fail-loud `resolve_presets`

Presets are a **top-level `[[presets]]` array-of-tables** — a list of tables the flat one-spec-per-key
`SettingSpec` schema cannot model, so it lives outside the registry exactly like `[[mumble.servers]]`
(ADR 0042) and `[[dvap.modules]]` (ADR 0095). The recipe is the established one:
`config.settings.load_presets` reads the raw list, `_flatten` peels `presets` off the top level (beside
`[services]`/`[plugins]`) so schema resolution never trips on it, and **`presets.resolve_presets`**
validates it fail-loud into frozen `Preset` values — the `load_mumble_servers` / `resolve_mumble_entries`
split.

Validation uses the same units discipline the backends use: `name` required (1–64 chars, **unique
case-insensitively** — apply is by name), `frequency` a **positive int** (Hz; `bool`/float/str rejected),
`tone` (when present) a **standard EIA CTCSS tone** (the 38-tone set), `mode` one of `FM`/`NFM`
(upper-cased), unknown fields are typos. A bad preset **fails startup loudly**; it is never silently
skipped. An empty/absent list leaves the feature dormant — nothing changes anywhere. No `SettingSpec` is
added, so the settings-API spec canary does not move.

The **file is the source of truth (v1)** — there is no preset-editing API. `save_settings` round-trips via
tomlkit and only writes schema leaves, so a hand-authored `[[presets]]` block survives a settings save
untouched (like `[services]`). `render_example` ships **commented** examples (a preset's frequency is the
operator's local choice — nothing live, like `[[dvap.modules]]`).

### The CTCSS set is self-contained here

The canonical EIA 38-tone table (`CTCSS_TONES`) is defined in `radio_server/presets.py`, not imported
from a backend. The kv4p backend holds its own private copy (`backends/kv4p/radio.py`) for SA818 index
mapping; presets must not couple to a backend module. A future refactor could unify them into a shared
tones module, but that is out of scope — kv4p stays byte-untouched.

## Decision 3 — the apply path: capability-gated, with interlocks from existing discipline

`apply_preset(radio, preset)` applies a preset through the **existing** radio surface — `set_frequency`,
then `set_mode`, then `set_tone` (anchor-first) — **capability-gated per field**: mode/tone are applied
only when the active backend advertises `SET_MODE`/`SET_TONE`, and anything skipped is **reported, never
silent** (guardrail 3). `split_preset_fields` is the pure honoured/skipped split, in the machine-readable
`Capability` vocabulary the 501 body and the UI already speak; both `GET /presets` and the apply seam use
it. `apply_preset` takes `radio` as a parameter (no captured reference), so it composes with a live
backend switch (ADR 0076) and is testable with any `Radio`.

**Interlocks, derived from existing discipline (not invented):**

- **Mid-TX → refuse (409).** `set_frequency` is not guarded at the backend layer; the half-duplex
  interlock is the **arbiter** (ADR 0017). `RadioArbiter.transmitting` is "what the RX pump and scan
  engine check to pause." The apply route consults it and **refuses** — matching the arbiter's
  refuse-don't-key posture (`ArbiterStateError` on a double-key). Queueing was rejected: no
  deferred-action machinery exists anywhere, and a silent later tune is worse than a clear 409.
- **Mid-scan → stop the scan first.** The scan owns tuning while running (`ScanEngine._tune`). The
  stop-first precedent is `holder.stop()` → `await scan_runner.stop()` (ADR 0028). The apply route, if
  `scan_runner.running`, stops the scan cleanly (at a tick boundary) before tuning.
- **No parallel state store.** After applying, the route calls `hub.publish(status_event(radio))` — the
  exact reactive path the tuning routes use (ADR 0076/0077); the UI's FreqLcd/mode/tone react for free.

### The HTTP surface

- **`GET /presets`** (read-only, LAN-token auth like every control route): each preset with its fields
  plus the honoured/unsupported split for the **current** backend. Pure read, no state push.
- **`POST /presets/apply` `{name}`** (`async`, to await the scan stop): case-insensitive lookup → **404**
  on an unknown name; **`_require_cat(SET_FREQUENCY)`** → **501** on an audio-only backend, exactly like
  `POST /frequency` (a preset is fundamentally a tune; a radio that can't tune can't apply one, and
  `GET /presets` still lists them with an empty honoured set); **409** mid-TX; stop the scan first;
  apply; return `{applied, skipped, status}`. A backend `ValueError` — a pre-validated preset can still
  be outside the *active* radio's band (presets are backend-agnostic; a band is per-radio) — is wrapped
  in a clean **422** naming the preset, rather than surfacing as a 500.

## Consequences

- New hardware-free tests (`tests/test_presets.py`, `tests/test_config.py`): `resolve_presets`
  fail-loud (bad tone, duplicate name case-insensitively, malformed/missing/≤0 frequency, unknown field,
  bad mode) + dormant `()`; `split_preset_fields` full/partial/audio-only; `apply_preset` end-to-end
  including the per-field skip via a partial-capability stub (`SET_FREQUENCY`+`SET_MODE`, no `SET_TONE` —
  the gap no real backend has today); the API (list + honoured split, apply-changes-state + `status`
  push, 404, mid-TX 409, mid-scan stop-first, audio-only 501, backend-ValueError 422). Full suite green;
  the baofeng/kv4p/uvk5 backend suites are untouched.
- No new dependencies; extras closures (ADR 0067) unchanged — `uv.lock` byte-identical, deployed box
  untouched.

## Out of scope (named; built here: none)

- **The web UI** (next cycle) — this cycle is model/config/apply/API; `curl` is the acceptance interface.
- **Split/offset** — TX-through-a-repeater, a follow-on arc that would touch the `CatRadio` interface
  itself (no current backend has a split surface).
- **Preset editing via the API** — the config file is the source of truth in v1 (a
  `save_presets`/tomlkit writer is sketched only where the round-trip precedent lives).
- The stuck-key **watchdog/TOT** arc (ADR 0112) and any bench numbers.
