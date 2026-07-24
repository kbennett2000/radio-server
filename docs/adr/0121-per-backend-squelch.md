# 0121 — Per-backend squelch mode (unbreak the mixed-radio box)

Status: Accepted

## Context

`audio.squelch` was a single **global** RX-gate mode (`off` / `audio` / `cat`), and ADR 0074's
`validate_backend_config` checks *every configured backend* against it. That coupling made a
legitimate config unstartable: `audio.squelch=cat` — which the docked UV-K6 needs post-F3 (ADR
0120: dock entry force-opens the AF path, so the radio hisses continuously; software `audio` VAD
can't gate that and `off` never segments, leaving only the radio's RSSI busy line, `cat`) — plus
**any** configured audio-only block (`[baofeng]`) fails at load: `validate_configured_backends`
validates the inactive `[baofeng]` block against the global `cat`, hits the "UV-5R has no busy line"
guard (ADR 0015), and boot aborts.

This bit the bench: a box with a stale `[baofeng]` block, running uvk5. A box that genuinely runs a
UV-5R **and** a K6 is a valid setup and was impossible. The global squelch is the wrong granularity
— squelch is a property of the *radio*, not of the whole node.

## Decision 1 — a per-backend `squelch_mode`, global as fallback (the `uvk5.tot` pattern)

uvk5 and baofeng get their own key; a backend without one falls back to the global `audio.squelch`.
`resolve_squelch_mode(settings, backend)` (in `activity/gate.py`, beside `load_squelch_mode`) is the
single source of truth, shared by the gate and the validator, and mirrors
`holder.resolve_tot`/`tx.tot` exactly:

- `uvk5`    → `uvk5.squelch_mode`    (backend-declared default `cat`)
- `baofeng` → `baofeng.squelch_mode` (backend-declared default `audio`)
- everything else (`kv4p`, `mock`, `v71`) → global `audio.squelch` (default `off`)

**Backend-declared defaults.** Each backend owns a module-level `DEFAULT_SQUELCH_MODE` constant
(`uvk5/radio.py` = `"cat"`, `aioc_baofeng.py` = `"audio"`), which `config/spec.py` imports and wraps
in `SquelchMode(...)` — the same shape as `DEFAULT_TOT` → `DEFAULT_UVK5_TOT`, and the same wrapping
`audio.squelch`'s own default string already uses. A plain string keeps the backend from importing
the `activity` layer (no cycle). uvk5 defaults to `cat` (per the F3 finding above); baofeng to
`audio` (the UV-5R has no busy line, so software VAD is the only real gate). kv4p gets **no** new key
— it "keeps current behavior" literally, reading the global exactly as before.

### Naming — `<backend>.squelch_mode`, not `<backend>.squelch`

- `[kv4p]` already owns `squelch` — an SA818 hardware *level* 0-8. A `squelch` enum under `[uvk5]`/
  `[baofeng]` next to kv4p's integer `squelch` invites exactly the level-vs-mode confusion we're
  trying to avoid. `squelch_mode` is unambiguous, and `uvk5.squelch_threshold` already sets the
  precedent of namespacing a squelch concept per-backend.
- Not an `[audio.<backend>]` table: the config registry is *flat* — one `SettingSpec` per dotted
  key, `group` = the first segment. A three-segment nested key isn't modeled by the spec/example
  machinery, and it would separate the key from the `[baofeng]`/`[uvk5]` block where every other
  backend knob lives.

### Back-compat

Realistic single-global configs resolve unchanged: uvk5 + `audio.squelch=cat` → `cat` (matches the
new uvk5 default), baofeng + `audio.squelch=audio` → `audio` (matches), any kv4p/mock config → the
global, unchanged. The one **documented divergence**: a uvk5/baofeng box that leaned on the global
for a *non-default* mode (e.g. baofeng + explicit `audio.squelch=off`) now uses its backend default;
to keep the old mode it sets the per-backend key (`baofeng.squelch_mode=off`). This is inherent to
the cited `uvk5.tot` pattern (which likewise ignores `tx.tot` for uvk5), and for uvk5 the changed
default (`off`→`cat`) fixes the very setup that was broken in dock mode.

## Decision 2 — validate each backend against ITS effective mode

`validate_backend_config` now reads `resolve_squelch_mode(settings, backend)` instead of the raw
global. The stale-`[baofeng]`-blocks-`cat` failure disappears by construction: an inactive
`[baofeng]` block with no `baofeng.squelch_mode` resolves to `audio`, so the "no busy line" guard
never fires from a global `cat`. The guard still bites when the `[baofeng]` section *explicitly* sets
`squelch_mode=cat`.

The misleading messages are reworded (they must name the **section/key** being validated, never
imply the active `server.backend`, since an inactive switch target is also validated here): the
baofeng guard now cites `baofeng.squelch_mode=cat` and "the [baofeng] section"; the uvk5 guard cites
`uvk5.squelch_mode`/`uvk5.squelch_threshold`. The kv4p guard keeps `audio.squelch` (kv4p genuinely
reads the global) and was already section-scoped.

## Decision 3 — the gate is re-selected per backend on a live switch

The RX gate was built once at `build_app` and *reused* across `holder.rebuild`, so a live backend
swap neither re-selected the gate type nor re-pointed a `CatBusyGate` at the new radio — a latent
stale-radio bug (the old gate kept polling the now-closed previous radio's `status().busy`).
`build_rx_gate` now resolves the mode for the active `server.backend`, and `RadioHolder.rebuild`
(and the rollback `_restore`) rebuild `self._gate = build_rx_gate(settings, radio=self._radio)` after
the new radio is built. No parallel state — the gate stays owned by the holder, refreshed on the
existing rebuild path. Requirement met and the stale-radio bug closed in one move.

## Consequences

- **New config:** `uvk5.squelch_mode` + `baofeng.squelch_mode` (both Advanced, `coerce_enum(
  SquelchMode)` reused — no new coercer). Settings canary 90→92; `radio.toml.example` regenerated.
- **Touched:** `config/spec.py` (2 specs + 2 imports), `activity/gate.py` (`resolve_squelch_mode`;
  `build_rx_gate` resolves the active backend), `backends/uvk5/radio.py` + `backends/aioc_baofeng.py`
  (`DEFAULT_SQUELCH_MODE`), `api/backend_config.py` (effective-mode validation + reword),
  `api/holder.py` (gate rebuild on swap), `api/app.py` (the recording-`off` safety rail reads the
  effective mode too).
- **Tests:** new `tests/test_squelch_per_backend.py` (defaults, override, invalid-value, resolution,
  the headline mixed/stale-block validation, gate selection, back-compat) + a holder
  gate-rebuild-on-switch test; `test_multi_backend`'s old fail-at-load test inverted to the new
  boots-fine behavior (plus an explicit-cat-still-fails companion); `test_backend_wiring` /
  `test_recording` / `test_settings_api` messages + canary updated. `uv run pytest` green.
- Not a backend construction arg — `squelch_mode` stays a composition-root/gate concern (like `tot`),
  so `backend_kwargs` is untouched.

## Out of scope

The F3 bench loose ends (boot-race tolerance for the audio force-open, shutdown `CancelledError`
tidy, doctor stopwatch, RSSI readout) — the next cycle. No firmware change. D-STAR's independent
per-link `AudioLevelGate` (ADR 0091) stays independent of `audio.squelch`.
