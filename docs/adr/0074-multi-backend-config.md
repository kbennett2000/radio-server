# 0074 — radio.toml describes more than one backend

Status: Accepted

## Context

ADR 0073 gave the app a `RadioHolder` seam so the active radio + its pipeline can be stopped and
rebuilt — the keystone for live backend switching. But the config is still single-backend to the
bone: `server.backend` names one backend, `build_radio(settings)` builds *that* one, and the other
`[<backend>]` block is inert. The two hardware backends' per-key settings are already coerced at load
for every block (they are plain `SettingSpec`s), but the **cross-field** validation that would reject
a broken block only runs for the *active* backend:

- the two `audio.squelch=cat` guards (`build_radio`, holder.py) — cat squelch is invalid for baofeng
  (no busy line) and needs a non-zero `kv4p.squelch`;
- the kv4p frequency band check — an out-of-band `kv4p.frequency` fails loud, but only inside
  `Kv4pHt.set_frequency` at construction.

So a config could name `[kv4p]` and `[baofeng]` both, pick one, and carry a broken *other* block that
nothing notices until someone selects it live. ADR 0051 is the cautionary tale: a latent config
problem (a plugin table in the wrong place) that only surfaced on a restart months later. The whole
point of this cycle is to **move that failure to load time**: a switch target you cannot select
because its config is broken is worse than a loud startup error.

This cycle is the config model + its validation + doctor understanding both. It does **not** switch,
add a select endpoint, or touch the UI — those are later cycles (ADR 0073's deferred list).

## Decision

### 1. `server.backend` is the initial selection; presence declares the configured set

A backend is **configured** if its `[<backend>]` block is present in `radio.toml` (any `baofeng.*` /
`kv4p.*` key set), plus the active `server.backend` is always configured (it boots from defaults even
with no block). No new setting — the presence of the block *is* the declaration. This matches today's
config shape exactly and keeps a single-block config unchanged: a file with only `[baofeng]` boots to
baofeng and never validates or enumerates kv4p.

Presence is captured during resolution (it cannot be recovered afterward — every backend key has a
default, so a resolved `Settings` cannot tell a written block from an absent one). `resolve_settings`
computes the configured set from the raw dotted keys and stores it on `Settings`, exposed as
**`Settings.configured_backend_names() -> frozenset[str]`**. The backend-block group names are derived,
not hardcoded: `{spec group} ∩ available_backends()` = `{baofeng, kv4p}` (mock/v71 have no block).

### 2. Every configured backend is validated at load — inactive ones included

New light module **`radio_server/api/backend_config.py`** (pure config→backend policy, no pipeline
imports, so `doctor` can import it cheaply):

- **`validate_backend_config(settings, backend, *, include_construction_checks)`** — the pure
  cross-field checks for one backend, without constructing it (constructing a hardware backend opens a
  serial port / handshakes, and v71 raises — so validation must be pure). It always runs the checks a
  backend *constructor never does* (the two squelch guards, messages verbatim from the old
  `build_radio`). When `include_construction_checks=True` it also runs the checks the constructor
  *would* do but that we skip because we are not constructing this backend — today that is the kv4p
  frequency band check, against the module-type **default** band (the documented no-HELLO fallback —
  there is no device at load to report a real range).
- **`validate_configured_backends(settings)`** — validates each configured backend **except the
  active one** (`include_construction_checks=True`, since inactive blocks are never constructed). The
  active backend is left exactly as today: its squelch guard runs in `build_radio`, and its frequency
  is checked when the real backend is constructed (HELLO-aware). This is the split that keeps active
  behaviour byte-identical while adding load-time coverage for the switch targets.
- **`backend_kwargs(settings, backend) -> dict`** — the `settings→constructor kwargs` mapping,
  extracted verbatim from `build_radio`'s switch so the enumeration (and the swap cycle) can read a
  backend's resolved settings without duplicating the mapping.

`build_radio` stays in `holder.py` and now reads
`validate_backend_config(active, include_construction_checks=False)` + `create_radio(active,
**backend_kwargs(...))` — behaviour identical (it still looks `create_radio` up locally, so the
existing wiring tests' monkeypatch is unchanged). `build_app` calls `validate_configured_backends`
right before `build_radio`.

**This is deliberately stricter.** A config that names both blocks and sets `audio.squelch=cat` while
the *inactive* block is baofeng now fails at load (baofeng + cat is invalid) — where before the stray
block was ignored. That is the intended move: the block is a declared switch target, so its config
must be selectable. The blast radius is bounded by presence — a single-backend config is untouched.

### 3. Enumeration surface for the next cycle

**`configured_backends(settings) -> tuple[BackendChoice, ...]`** returns the configured backends,
active first, each a frozen `BackendChoice(name, active, settings)` where `settings` is that backend's
resolved `backend_kwargs`. This is what the swap cycle's **select endpoint + UI dropdown** consume —
"the backends this node is configured for, and how to build each." It has **no caller yet**; the shape
is defined now so the endpoint cycle is a thin HTTP wrapper. Live per-backend *capabilities* are not
included: they require constructing the backend (touching hardware), so they stay a property of the
built radio (`GET /capabilities`) — a note for the endpoint cycle. v71 is naturally excluded: it has
no config block, and an active v71 already fails at construction.

### 4. doctor validates the selected backend, without regressing the load

`doctor` resolves one backend (`--backend` override, else `server.backend`) and its per-backend config
builders already read only that backend's block. It now calls `validate_backend_config(settings,
backend, include_construction_checks=True)` up front (reading the real `radio.toml` via the ADR 0069
`_doctor_settings` path), printing a clear `FAIL` on a broken block instead of silently falling back to
defaults or only failing at device construction. The multi-backend validation stays **out of**
`resolve_settings`/`load_settings` on purpose: doctor wraps every settings read in `try/except`, so a
raising loader would be swallowed and regress the ADR 0069 "read the real file" fix. Validation is a
separate, explicit step; the load stays non-raising.

## Consequences

- Both `[baofeng]` and `[kv4p]` blocks can coexist and are each validated at load; an invalid inactive
  block fails the server loudly, naming the block and why. Single-backend configs boot unchanged.
- No schema change: no new `SettingSpec`, so `radio.toml.example` is byte-identical (no regen) and the
  settings-count canary is unmoved. The example already renders both blocks.
- Active-backend behaviour is byte-identical (the wiring/squelch tests stay green); the new coverage is
  purely additive for inactive present blocks.
- A stricter failure for the rare config that carries a stray second block plus `audio.squelch=cat` —
  called out in the PR as a (correct) behaviour change.

## Non-goals

No select endpoint, no swap/rebuild logic, no holder switching, no UI, no multi-backend *runtime*
(the holder still builds one backend at startup from `server.backend`). Docs limited to this ADR +
HANDOFF. The enumeration surface ships with no caller.
