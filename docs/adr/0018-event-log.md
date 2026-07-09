# 0018 — Event log: a subscriber-not-instrumenter station ledger over a JSONL sink

Status: Accepted

## Context

The station emits a live event stream but keeps **no durable record** of what it did. `EventHub`
(ADR 0011, `radio_server/api/events.py`) fans events out to `/events` WebSocket subscribers in real
time and then forgets them. Part 97 makes the licensee responsible for every transmission the
station makes; a defensible operating log — TX key-downs with timestamps and durations,
station-ID-sent records, session lifecycle — is required controller behavior, not a nicety.

The events needed for such a log **already flow** through the hub: the pump publishes them and any
number of subscribers can consume without the producer knowing or caring. So the ledger is not new
instrumentation — it is **another subscriber** of the existing flow that happens to write its
consumption to disk. This cycle adds that subscriber, mock-only and hardware-free (guardrail 1). It
deliberately does **not** add new `hub.publish` sites anywhere in the tree.

## Decision

- **A new pure-leaf `radio_server/eventlog/` package** holding the ledger. It imports only stdlib
  and `..api.events.Event` (the record it consumes), so it adds no dependency cycle. One instance is
  created at the composition root (`build_app`) and injected into `create_app` via a safe default
  (`event_log=None`), the same DI-with-default discipline the RX `gate` (ADR 0015) and `arbiter`
  (ADR 0017) seams use — so every existing `create_app` test is behaviorally unchanged.

- **Subscriber, not instrumenter (the load-bearing stance).** `EventLog` hangs off the shared
  `EventHub` exactly like the `/events` WebSocket: a lifespan-managed background task drains its own
  `hub.subscribe()` queue and calls `EventLog.handle(event)`. It adds **zero** emissions to `auth`,
  `arbiter`, `tx`, or `controller`. It records what already flows today — `ptt` (REST `/ptt`
  key-up/down), `scan` (phases incl. `active`+frequency), and `session` (`session_open` / `id` /
  `session_close`). Because publish is synchronous onto an unbounded queue, the ledger never blocks
  the pump or any other subscriber.

- **A `LogSink` protocol with a default `JsonlSink`** (`radio_server/eventlog/sink.py`). The sink is
  the storage seam: `write(record)` / `close()`. The default writes **append-only JSONL — one JSON
  object per line** — greppable, `tail -f`-able, and the same file-backed shape the project favours
  over a database (it is the project's first persistence). A **SQLite sink is the notable future
  swap** behind this protocol and is deliberately **not** built here. The output path is
  configuration, `RADIO_LOG_PATH` with a marked default (`radio-server.jsonl`), mirroring
  `services/time_service.load_timezone`. A *set-but-unwritable* path **fails loud at construction**
  (`JsonlSink.__init__` opens in append mode, so `OSError` surfaces immediately at the composition
  root) — an operating log that silently isn't being written is worse than none.

- **A flat, clock-stamped record taxonomy built by a whitelisting mapper**
  (`EventLog._record_for`). Every record is `{"ts": <clock float>, "type": <str>, ...fields}`.
  `ts` comes from the injected `Clock` (`Callable[[], float]`, default `time.time`, `FakeClock` in
  tests) — the same clock seam every time-sensitive object shares. `tx_key_up` remembers its
  timestamp so the paired `tx_key_down` records the keyed **duration** (the Part 97 value); every
  other record is a pure function of the event. `scan` `active` carries the hit frequency; `session`
  maps to `session_open` / `station_id` / `session_close`.

- **No secrets, ever (hard rule).** The mapper **whitelists** the fields each record type emits — it
  **never spreads `event.data` wholesale**. Even if an upstream event carried a TOTP code, the
  shared secret, or the API token, none can reach the ledger: the record simply does not copy
  unrecognized keys. A rejected-auth record states *that* auth failed and *when*, never the digits.
  This is proven by a test that feeds a rejected-auth event carrying fake `code`/`secret` fields and
  asserts they are absent from the written record.

- **Failure isolation from the audio/event path.** `EventLog.handle` wraps translate-and-write in a
  catch-all and **drops** the record on any error — a slow disk, a full filesystem, or a bug in a
  builder can never propagate back into the event pump or a transmission. Structurally, the audio
  path never even reaches here: `/audio/rx` (`AudioHub`) and `/audio/tx` (`TxSession`) do not flow
  through `EventHub`, so the ledger is quarantined from keying by construction. On graceful
  shutdown the lifespan drains any still-queued events before closing the sink, so a clean stop
  loses no entries.

- **Forward-compatible types the mapper already handles.** `auth` (accepted/rejected), `command`
  (dispatched service), and `arbiter` (mode change) records are implemented and tested, but nothing
  publishes those `event.type`s to the hub **yet** (see Deferred). The mapper is ready so a future
  instrumentation cycle need only add the `hub.publish` — the records then appear with no ledger
  change.

## Consequences

- **The station now has a durable operating log.** A REST `/ptt` round-trip lands as `tx_key_up`
  then `tx_key_down` with a measured duration; scan hits and session lifecycle are recorded; the
  file is valid line-delimited JSON, safe to grep or tail while the server runs — proven end-to-end
  through `TestClient` and by a manual `build_app` smoke run.
- **Every prior test passes unchanged.** `create_app`'s `event_log` defaults to `None` (no
  subscriber, no file), so the whole existing suite is untouched; `build_app` — which wires the
  always-on sink — is exercised by no test, so the default log path touches nothing that runs today.
- **Secrets cannot reach the ledger.** The whitelist is a checked property, not a hope: the
  no-code-material test asserts a rejected-auth record built from data containing a fake code/secret
  contains neither.
- **Untouched, deliberately:** `auth/`, `arbiter/`, `tx/`, `controller/`, and `api/events.py` — no
  new emissions were added (the subscriber-not-instrumenter stance). `backends/mock.py`,
  `audio/`, and every other package are unchanged. The only edit outside the new package is the
  `create_app`/`build_app` wiring in `radio_server/api/app.py`.
- Full suite: **311 passed, 4 skipped** (was 293; +18 event-log tests — taxonomy per type, the
  no-secrets rule, FakeClock key-down duration, JSONL one-object-per-line, fail-loud construction,
  no-propagation on sink error, and the end-to-end app wiring; the 4 skips remain the multimon +
  piper hardware/model gates).
- **Deferred, on purpose:** the SQLite sink (a future `LogSink` swap); log rotation / retention; a
  query API (the UI reads the file or a simple `GET` later); audio recording (cycle 18); the web UI
  (cycle 19+); **and the live emission of `auth_rejected` / `command_dispatched` / `arbiter_mode`
  and ID `callsign`+`mode` fields — the mapper is ready, but publishing those to the hub is a future
  instrumentation cycle** (it would add `hub.publish` sites to `auth`/`controller`/`arbiter`/`tx`,
  which this cycle's subscriber-not-instrumenter stance deliberately avoids). Until then the live
  ledger captures `ptt`/`scan`/`session` — which already covers the Part 97 essentials (TX
  key-downs with duration via REST `/ptt`, and ID-sent via the controller's `session` events).
- **Numbering / branch note:** ADR 0018 by cycle order, cut from the cycle-16 merge point
  (`14a8369`, ADR 0017) at **293 passed, 4 skipped**. It adds the `eventlog` package and touches
  only the `api` composition root.
