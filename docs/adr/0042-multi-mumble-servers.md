# 0042 — Multiple Mumble servers/channels, DTMF-selectable, one active link

Status: Accepted

## Context

ADR 0041 delivered exactly **one** Mumble link: a single `[mumble]` settings block names one
server + channel, `build_app` constructs one `PyMumbleClient` and one `MumbleBridge`, and
`POST /link {on}` toggles it. The operator wants the impromptu-net workflow that motivated the
link in the first place: **several** Murmur servers and/or several channels on one server, defined
once, then hopped between —

1. from the **web settings UI**, in a dedicated place (multiple named entries with all their
   connection details);
2. **over RF via DTMF** — each entry may be assigned a combo (e.g. `13#`, `1234#`) that connects
   it, so a licensed operator in the field can move the station between nets from an HT;
3. from the **control screen**, with per-entry Connect/Disconnect.

Constraints discovered in the code:

- The settings schema (ADR 0025) is a deliberately **closed, flat registry** — one `SettingSpec`
  per `group.leaf` scalar. It cannot model a list of entries, and the schema-driven settings form
  renders only scalars. The codebase already has exactly one escape hatch for config the schema
  cannot model: the `[services]` DTMF-binding table, skipped by `_flatten` and read by its own
  loader (`load_service_bindings`). A list of Mumble servers is the same shape of problem.
- DTMF combos are the exact string submitted before `#` (`DtmfFramer`: `#` submits, `*` clears).
  Side-effecting commands (`station-id`, `logout`) are **controller built-ins** run by
  `Controller._run_command` under the authenticated TOTP session — services proper are pure
  audio-returning functions and cannot touch app state. Connecting a link is a side effect that
  enables Mumble voice to key the transmitter, so it must sit at least as deep behind auth as any
  built-in (guardrail 4).
- `MumbleBridge` is a pure-DI state machine over one `MumbleClient`; nothing in it assumes it is
  the only bridge ever built. The single-link assumption lives entirely in the composition root.

## Decision

1. **Config: a `[[mumble.servers]]` array-of-tables, outside the spec registry** (the `[services]`
   precedent). Each entry: `name` (slug `[a-z0-9_]{1,32}`, unique — TOML-key-, URL- and
   env-var-safe), `host` (required), `port`, `username`, `channel`, `dtmf` (optional combo),
   `tx_to_rf`, `autoconnect` (at most one entry). `_flatten` skips the reserved `servers` leaf; a
   new `load_mumble_servers()` returns the raw list and `resolve_mumble_entries()` validates it
   into frozen `MumbleEntry` dataclasses, mirroring the `load_service_bindings` /
   `resolve_bindings` split. Defaults stay the marked `DEFAULT_MUMBLE_*` constants (guardrail 1).

2. **The flat `[mumble]` connection settings are removed; migration fails loud.** The six specs
   `mumble.enabled/host/port/username/channel/tx_to_rf` leave the registry (`mumble.tx_hang`
   remains as the global hang timer). A leftover flat block raises at load with a tailored message
   showing the `[[mumble.servers]]` replacement. One operator, one file, one minute of editing —
   silently rewriting the operator's TOML would be more code and more surprise than the feature.

3. **One active link at a time — switch semantics** (the operator's stated choice). A new
   `LinkManager` owns the boot-time entry tuple and at most one live `MumbleBridge`;
   `connect(name)` disconnects the current entry first. One radio, one talker slot: N simultaneous
   bridges would double every conference onto the air and multiply the Part-97 exposure for zero
   workflow gain. A **fresh client + fresh bridge is built per connect** (injected
   `client_factory`/`bridge_factory`; `MockMumbleClient` in tests) — pymumble's connection thread
   is not designed for reuse, and per-connect construction isolates a wedged old thread.

4. **Per-entry passwords are dynamic secrets**: `mumble_password_<name>` in `radio-secrets.toml`
   or `RADIO_MUMBLE_PASSWORD_<NAME>` env. The secrets channel gains a prefix predicate beside its
   fixed known-name set; the legacy `mumble_password` name is dropped (unknown file keys were
   always ignored, so old secrets files still load). Posture unchanged: never in `radio.toml`,
   presence-only in any GET, write-only set endpoint.

5. **DTMF link commands are controller built-ins resolved from the entry list** (not the
   `[services]` table — the combo belongs to the entry the operator defined it on). A combo match
   in `_run_command` fires a rebindable `controller.on_link(name)` callback (the `on_event`
   pattern, keeping `controller` free of `api`/`link` imports) and transmits a pre-rendered TTS
   confirmation ("linked to <name>" / "link off"). Disconnect gets one new scalar spec,
   `mumble.disconnect_dtmf` (default `"73"` — best regards). Combos run only inside an
   authenticated TOTP session, like every command; `Controller.trigger` (the LAN-token seam) can
   also fire them, intentionally.

   **Collision rules, fail-loud at startup and at save:** combos use `0123456789ABCD` only (`#`
   submits and `*` clears, so neither can appear in a matchable combo); unique across entries; no
   combo equals `disconnect_dtmf` or any resolved `[services]` binding digit (built-ins included).
   Exact-string comparison only — `"13"` and `"1"` do not collide, because the framer submits the
   whole buffered string.

6. **API reshape (breaking, accepted — the only client was our own UI).** `POST /link` becomes
   `{entry?, on}`; `GET /link/status` and the `/status` link block become
   `{active, entries: [...]}`. Entry CRUD is a whole-list `PUT /settings/mumble-servers`,
   **restart-applied** like every other setting — the entry list is immutable per process, which
   keeps the DTMF map, the manager, and the validation story static. Consequence: a freshly added
   server needs a restart before it can be connected; runtime connect/disconnect stays `/link`.

7. **A `link` hub event** is published on every manager transition so the web card and the event
   ledger see switches regardless of origin (browser, DTMF, autoconnect). The card keeps a light
   poll while a link is running as belt-and-suspenders for Mumble-side state (peers, drops).

## Consequences

- The Part 97 posture is unchanged per-bridge: whichever entry is active keys TX through the same
  `TxSession` + shared `StreamingId`, and per-entry `tx_to_rf = false` still yields a
  receive-only monitor. The switch itself transmits a spoken confirmation, which is itself covered
  by the station-ID seam.
- Switching drops the current channel mid-conversation by design — that is the feature.
- The settings UI gains its first non-schema editor (a bespoke list panel); the schema form stays
  scalar-only.
- Old flat `[mumble]` configs and the old `POST /link {on}` contract break loudly; both are
  one-line fixes for this deployment and are called out in the PR.

## Numbering / branch note

ADR numbering continues at 0042. Branch `multi-mumble-servers` cut from a freshly-pulled
`origin/master` at the PR #76 merge; one PR against `master` (ADR first commit, implementation
follows).
