# 0079 — Persist the over-RF auth session across a backend switch

Status: Accepted

## Context

ADR 0076 shipped the live backend switch: `POST /radio/select` tears the radio pipeline down and
rebuilds the controller against the newly-selected radio via `holder.rebuild(new_settings)` →
`controller_factory(new_settings, radio)` → `build_controller(…)`. ADR 0078 then closed a class of
"a switch must preserve everything a fresh boot would load" bug (the local-plugin extra channel).
This ADR closes another instance of the same class — but for **runtime session state**, not config.

`build_controller` constructed a **fresh `Session()`** on every call
(`controller/engine.py`), so each rebuild silently replaced the live session with a new,
unauthenticated one. An operator who had authenticated over the air (via a DTMF TOTP code or the web
UI's OTA-code chip) was **logged out the instant the station switched radios**.

That is a lifetime bug, not a design flaw in the session machine. The whole session state is tiny —
`Session` is just `state` + `last_activity` — and `AuthGate` is stateless about it by design ("one
gate serves many sessions"). `Controller` already takes the `Session` by injection and exposes it via
the public `Controller.session` property. The auth session is a property of **the operator at the
station**, not of the per-radio controller, so it must outlive a controller rebuild.

The single `build_controller` call site is the `controller_factory` closure in `build_app`
(`api/app.py`), which already captures the file-derived deps that are stable across a switch
(`service_bindings`, `mumble_entries`, `service_plugins`). It is invoked for both the initial build
and every rebuild — the natural place to own one long-lived session.

## Decision

Hoist the `Session` to the composition root and reuse it across rebuilds.

- **`build_controller` gains `session: Session | None = None`** (`controller/engine.py`): use the
  passed one, else mint a fresh `Session()`. Back-compat — every existing caller/test that passes no
  session still gets its own, so a plain `build_controller` stands alone.
- **`build_app` constructs one `Session` and captures it in the factory closure** (`api/app.py`):
  the same object flows into the initial build **and** every `holder.rebuild`. A switch therefore
  injects the same session — the operator's authenticated state and inactivity clock survive it. The
  `AuthGate` is still rebuilt fresh each time (correct — it is stateless re: the session and re-wires
  to the new controller's dispatcher/station ID); it merely operates on the persistent `Session`.

Nothing else changes. This is not the REST/Bearer auth plane (stateless, already carries), and it
does not change the session's cardinality (still one session).

### Behaviour that falls out

- **The inactivity clock is neither reset nor extended by a switch.** The same `Session` persists, so
  `last_activity` is real: a session near timeout stays near timeout across a switch and still
  expires on schedule via the controller's `expire_if_idle` poll. Resetting it would keep a stale
  operator alive forever; extending it would be equally wrong. Neither happens.
- **DTMF mid-entry accumulation is discarded on a switch — the correct split.** The `DtmfFramer` /
  decoder buffer is built fresh inside `build_controller` and is tied to the audio stream, which
  changed. Only the authenticated `Session` persists: a half-keyed code is dropped, but an
  already-open session stays open.
- **Part 97 / station ID re-arms correctly, erring toward ID-ing (legal).** The rebuilt controller
  builds a fresh `StationId` (`_last_id = None`, `_transmitted_this_session = False`). For a session
  carried open across the switch, `_due()` is `True` while `_last_id is None`, so the **first** over
  on the new radio always carries the ID; the periodic-ID safety net (`check`, run each step while the
  session is authenticated) never fires spuriously because nothing has transmitted on the new radio
  yet. The new radio, now the station, honours its own ID obligation and errs toward identifying,
  never toward skipping. No `StationId` change was needed — this behaviour was confirmed, not weakened.
- **Persisting across a LOCAL switch is fine.** Auth over RF is "gated, not secure" (guardrail 4 —
  everything is in the clear). It is the same operator at the same station switching their own radio,
  so carrying the session is correct, not a security regression.

## Consequences

- An operator authenticated over the air stays logged in when the station switches radios; the
  inactivity timeout keeps ticking from real activity, unshifted by the switch.
- New tests (`tests/test_backend_select.py`):
  - a controller rebuild that reuses the `Session` keeps it authenticated (`c2.session is c1.session`,
    still authenticated), while the controller/DTMF pipeline is genuinely rebuilt (`c2 is not c1`);
  - back-compat — `build_controller` with no `session=` mints a distinct fresh, unauthenticated one;
  - end-to-end — `POST /auth/session` opens a session, `POST /radio/select` switches, and
    `GET /status` still reports `session_open: True`;
  - the inactivity clock carries — a session near timeout stays open across a switch and expires on
    schedule (not reset, not extended);
  - a mid-entry DTMF accumulation does not survive the switch (the DTMF input is a new instance while
    the `Session` is carried).
- No schema change: `radio.toml.example` byte-identical, settings-count canary unmoved. The existing
  switch, session, auth, and controller suites stay green.

## Non-goals

Not the REST/Bearer auth (stateless, already carries). Not multi-caller RF sessions — still one
session; this cycle changes its lifetime, not its cardinality. No new backends, no UI change, no
per-backend DTMF twist (ADR 0075 noted it for this arc — still global). Docs limited to this ADR +
HANDOFF + the ADR index row.
