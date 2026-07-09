# 0019 — Deferred-event instrumentation: completing the ledger taxonomy at the producers

Status: Accepted

## Context

Cycle 17 (ADR 0018) shipped the **full** ledger taxonomy behind a passive `EventLog` subscriber and
unit-tested every record shape — but roughly **half of it was dead in production**. The mapper
branches for `auth`, `command`, and `arbiter`, and the `callsign`/`mode` fields on `station_id`, are
pure functions of events that **nothing publishes to the `EventHub`**. So a station running the
software recorded `ptt`/`scan`/`session` and nothing else: no auth trail, no record of which service
a caller ran, no keyed-mode transitions, and an ID record that couldn't say what identified the
station. The mapper was ready; the producers were silent. ADR 0018 named exactly this as the
deferred work.

The one non-obvious constraint is architectural. Every leaf package — `auth`, `services`,
`controller`, `arbiter`, `tx` — **deliberately does not import `EventHub`**; the only `hub.publish`
sites in the tree are in `radio_server/api/app.py`. Leaves emit their own **domain events through
injected callbacks** (`controller.on_event`, `scan on_event`) and the API composition root **adapts**
each to `hub.publish(Event(...))`, keeping `api → controller/auth/scan/arbiter/tx` acyclic (stated in
the `ControllerEvent` docstring and the arbiter leaf-purity note). So "add a `hub.publish` in the
producer's package" is not literally possible without breaking that guarantee. The faithful reading —
and what "don't centralize" means here — is that **each producer surfaces its own distinct signal at
its own site** (no central event-sniffer), routed through a callback the API turns into a hub event.

## Decision

- **Five producer emissions, all via the callback → API-adapter pattern.** No new `hub.publish` site
  is added to any leaf; every new `Event(...)` is minted in `radio_server/api/app.py`, exactly like
  the existing `_publish_scan` / `_publish_session` adapters.

  1. **`auth_accepted` and `auth_rejected`** — the `Controller.step` entry loop
     (`controller/engine.py`) already observes each `AuthGate.on_dtmf` outcome. It now emits
     `auth_accepted` (alongside the existing `session_open`) on `ACCEPTED` and `auth_rejected` on
     `REJECTED`, through the controller's existing `on_event` channel.
  2. **`command_dispatched`** — the same loop emits `command` with the dispatched `service` name on a
     `COMMAND` outcome, **only when it actually transmitted** (a registry miss is a graceful no-op,
     not a dispatch).
  3. **`station_id` enrichment** — the periodic-ID emit now carries `{callsign, mode}`. `StationId`
     gained read-only `callsign`/`mode` properties; `mode` (`"cw"`/`"voice"`) is threaded in from
     `load_id_mode(env)` at `build_controller` (it was computed transiently in `build_id_encoder` and
     retained nowhere).
  4. **`arbiter_mode`** — `RadioArbiter` gained an optional injected `on_change` callback fired **only
     on an actual derived-mode change** (a latch flip that leaves the TX-priority mode unchanged fires
     nothing). It stays leaf-pure: a `Callable`, no `radio_server` import. The composition root wires
     it to publish `Event(type="arbiter", data={"mode": str(mode)})`.
  5. **Streaming-TX `ptt`** — `TxSession` gained an optional `on_key` callback fired at its two
     key edges, wired in the `/audio/tx` endpoint to publish the same `ptt` on/off events REST `/ptt`
     already does. Streaming keying now lands in the ledger as `tx_key_up`/`tx_key_down` **with
     duration**, closing a real Part 97 gap (REST was already complete).

  The API adapter `_publish_controller` (renamed from `_publish_session`) fans the controller's one
  `on_event` channel out by phase: auth phases → `Event(type="auth", {"result": ...})`, `command` →
  `Event(type="command", {"service": ...})`, everything else → the unchanged `session` lifecycle.

- **No secrets at the source, still whitelisted downstream.** The `auth_rejected`/`auth_accepted`
  signals carry **no `data` at all** — the fact and the time, never a digit. This is belt-and-braces
  with the cycle-17 mapper whitelist: the payload is clean at the producer *and* the mapper still
  refuses unknown keys. A controller test asserts the rejected signal contains no code material, and
  the end-to-end test asserts no code/secret/token appears anywhere in the written file now that auth
  is a live producer.

- **Publish stays fire-and-forget (confirmed, not regressed).** `EventHub.publish` is `put_nowait`
  onto **unbounded** queues — non-blocking and non-raising — so these emissions, which fire
  synchronously inside `step()` / `TxSession.feed()` / the arbiter mutators, cannot break auth,
  dispatch, keying, or the arbiter. The audio path remains structurally quarantined from the ledger.

- **The correction: `session_open` ≠ `auth_accepted`.** The cycle brief said "auth_accepted already
  flows"; it did not — the accept path emitted only `session_open` (a session-lifecycle record). Both
  are now emitted as distinct taxonomy entries, which also lets the end-to-end acceptance test prove
  *every* type is present.

## Consequences

- **The log is no longer half-blind.** An end-to-end test drives a real bad-code → login → command →
  forced-ID → streaming-TX round-trip through `create_app` with a live `EventLog(JsonlSink(...))` and
  asserts the JSONL file contains **every** taxonomy type — `auth_rejected`, `auth_accepted`,
  `session_open`, `command_dispatched`, `station_id` (with `callsign`+`mode`), `tx_key_up`,
  `tx_key_down`, `arbiter_mode` — with the right fields, deterministic ordering invariants, and no
  secret material. A manual `create_app` smoke to a real file confirmed the same.
- **`radio_server/eventlog/` is untouched.** The mappers written in cycle 17 changed by zero lines;
  they simply light up now that the events flow. This is the payoff of building the taxonomy ahead of
  the producers.
- **Leaf acyclicity preserved.** `auth`, `services`, `controller`, `arbiter`, and `tx` still import no
  `EventHub`; every publish is in `api/app.py`. The arbiter and `TxSession` gained only injected
  `Callable` seams (defaulting to `None`), so isolated construction is behaviorally unchanged.
- **Existing tests: three assertions updated, none weakened.** The controller lifecycle/WS-order tests
  now expect the richer stream (`auth_accepted` before `session_open`, a `command` event, the auth
  event ahead of the session events on the socket). All other prior tests pass unchanged —
  `create_app`/constructor signatures only gained keyword-default callbacks.
- Full suite: **323 passed, 4 skipped** (was 311; +12 — controller auth/command/ID-enrichment emits,
  arbiter `on_change` + dedupe + StrEnum serialization, `TxSession` `on_key` edges, `StationId`
  `callsign`/`mode`, and the end-to-end full-taxonomy + no-secrets proof). The 4 skips remain the
  multimon + piper hardware/model gates.
- **Deferred, on purpose:** the SQLite `LogSink` swap; log rotation / retention; a query/`GET` API;
  audio recording (the next cycle); the web UI (the sequence after). The taxonomy itself is now
  complete end-to-end.
- **Numbering / branch note:** ADR 0019 by cycle order, cut from the cycle-17 merge point (`d03f934`,
  ADR 0018) at **311 passed, 4 skipped**. It touches five producers plus the `api` composition root
  and adds no new package.
