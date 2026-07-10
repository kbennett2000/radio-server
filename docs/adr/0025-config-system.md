# 0025 — Config system: a schema-driven TOML file replaces scattered env-var reads, with a separate secrets source

Status: Accepted

## Context

Until this cycle, configuration was **env-only and decentralized**. Each of 31 settings lived as a
`RADIO_*` environment variable, read by a module-local `load_*(env=os.environ)` function that owned
its own `DEFAULT_*` constant, its type coercion, and its validation. There was no single place that
enumerated what is configurable, no file-based config, and — critically for what comes next — no
machine-readable schema describing each setting.

That decentralization was the right call while the tower was being built cycle-by-cycle (each module
owning its own knob keeps cycles small and reviewable). But two upcoming cycles need a **single
source of truth**: cycle 26 wants a settings REST API + secret rotation, and cycle 27 wants a web
settings screen. Both need one object that lists every setting with its type, default, validation,
and a human-readable description. Threading a UI off ~29 scattered `load_*` functions is not viable.

So this cycle **reverses the de-facto env-only decision** and introduces a schema-driven config
system backed by a TOML file. It is a **behavior-preserving refactor**: with no config file present,
every default equals today's, and the full suite (386 passed / 4 skipped) stays green.

**Verified, not assumed.** Every setting, default, coercion, and validation rule below was read out
of the existing `load_*` functions, not recalled — including the load-bearing divergences that a
naive "one schema, one validator" design would have flattened and broken (empty-string handling,
exception types, two different boolean grammars). The 386-green invariant is the proof the refactor
preserved behavior.

## Decision

### Schema as the single source of truth (`radio_server/config/spec.py`)
A `SettingSpec` per setting — `key` (dotted `group.leaf`), `env` (the legacy `RADIO_*` name, now
metadata), `default` (a **reference** to the existing `DEFAULT_*` constant so the constant stays the
one source of truth), a `coerce` callable, and a **detailed human `description`** (this text feeds
cycle 27's UI, so it is a real sentence or two per setting, not a stub). The `SETTINGS` registry is
the tuple of all 31 non-secret specs, grouped: station, audio, dtmf, recording, tts, time, tx, scan,
controller, logging, server.

### All per-field logic lives in the spec's `coerce` — because the fields genuinely diverge
The temptation was one uniform "validate against type" rule. That would have broken real, tested
behavior. The divergences, each encoded per field:
- **Empty-string is not uniform.** `""` means *use default* for the floats/enums/paths, *fail loud*
  for `station.callsign` and `tts.voice`, *False* for `recording.enabled`/`.tx`, and *True* for
  `server.mock_cat`. It cannot be a global rule.
- **Exception types diverge.** `time.tz` lets `ZoneInfoNotFoundError` propagate; the
  `vad_on_rms <= vad_off_rms` hysteresis check is cross-field and raised as `ValueError` by
  `AudioLevelGate.__init__` (so it stays at the constructor, not in a spec); everything else raises
  `RuntimeError`. Standardizing on one exception would break `test_time_service` and `test_activity`.
- **Two boolean grammars.** `recording.enabled`/`.tx` are strict (truthy `{1,true,on,yes}`, falsey
  `{"",0,false,off,no}`, anything else fails loud); `server.mock_cat` is permissive (anything not in
  `{0,off,false,no,n}` is true, never fails). Two coercers, not one.
- **No I/O at load.** Path writability (`JsonlSink`/`Recorder`) and voice-file existence (`PiperTts`)
  stay at object construction, so config-load stays pure and *when* failures occur is unchanged.

The four byte-identical `_load_positive_float` copies (cw, activity, scan, controller) collapse into
one shared `coerce_positive_float`.

### Resolution + the lazy-required rule (`radio_server/config/settings.py`)
`load_settings(toml_path) → resolve_settings(raw) → Settings`: for each **present** key, coerce
(which validates) — invalid fails loud at load naming the key; each **missing optional** key takes
its spec default; each **missing required** key (callsign, tts.voice) is stored as an *unset
sentinel*, not failed. `Settings.get(key)` raises `RuntimeError` naming the key only when a
required-unset key is actually read — a **lazy, point-of-use** fail-loud.

This laziness is mandatory, not stylistic. Today `load_callsign`/`load_tts_voice` are reached only
inside `build_controller`/`build_id_encoder`, which `build_app` wires only when the TOTP secret is
present; the default mock app never reads them. Eagerly demanding required values at load would make
the default app refuse to start and break every default-mock test. So: **present-but-invalid →
load-time failure; missing-required → use-time failure.**

### The secrets split (`radio_server/config/secrets.py`)
`RADIO_TOTP_SECRET` and `RADIO_API_TOKEN` are **not** in `SETTINGS`, **not** in `radio.toml`, and are
never serialized by `save_settings`. They load from a separate `Secrets` source: a
`radio-secrets.toml` written `0600` (and **fail-loud if group/world-readable** — a secrets file a
neighbor can read is a misconfiguration, not a warning), or, as a documented deployment option, from
the environment. Secrets are the *only* thing still read from `os.environ`. `save_secret`/`rotate`
are write-only helpers built and unit-tested here for cycle 26's rotation endpoints (no endpoint yet).

Why keep secrets wholly out of the file and the schema: everything a UI renders or a settings file
round-trips is a place a secret could leak. Keeping them on a physically separate, permission-checked
channel means the settings surface (file + future REST + future UI) can never accidentally expose or
overwrite them.

### Round-trip writes (`radio_server/config/save.py`)
`save_settings(settings, path)` writes via **tomlkit** (not stdlib `tomllib`, which is read-only):
if the file exists it is loaded and updated in place so hand-added comments and formatting survive a
UI save. Required-unset keys are skipped (never emit `callsign = ""`); secrets are never written.
Reads use stdlib `tomllib`; only the writer needs the new `tomlkit` dependency.

### Bootstrap and rewire
Config path comes from a `--config PATH` flag (`python -m radio_server --config PATH`, default
`./radio.toml`) — the one pointer that cannot itself live in the file. Every `load_*(env)` becomes a
thin `load_*(settings)` accessor; `build_app(settings, secrets)` and the `build_*` helpers compose
from `Settings`; `create_app` is unchanged (already env-free). A shipped `radio.toml.example`
documents every non-secret setting with its default and description.

### Apply semantics: restart-to-apply (v1)
Settings are composed once at startup. `save_settings` persists to file; it does **not** hot-reload a
running server. Live hot-reload is a deliberate deferral — it needs safe re-wiring of live audio
threads, the controller loop, and open sockets, which is its own cycle. v1 is honest: change the
file, restart.

## Consequences

- **One source of truth.** Every setting is described once (type, default, validation, human text)
  in `SETTINGS`, ready to drive cycle 26's REST API and cycle 27's UI off the same schema that
  resolves the running config.
- **Behavior-preserving.** No config file → identical defaults; the full suite stays 386 passed / 4
  skipped. The 13 test files that injected `RADIO_*` dicts now build `Settings`/`Secrets` directly —
  cleaner, and part of the refactor.
- **Secrets are structurally isolated.** They live on a separate 0600-enforced channel, out of the
  file, the schema, and any rendered surface — so the config UI/API cannot leak or clobber them.
- **Env-var config is gone** (except the two secrets' documented env fallback). Deployments that set
  `RADIO_CALLSIGN` etc. move those into `radio.toml`; the docs were swept to match.
- **Deferred, on purpose:** the settings REST API (26), the UI settings screen (27), the
  secret-rotation endpoints (26 — the `save_secret`/`rotate` helpers are built and tested here), and
  live hot-reload.
