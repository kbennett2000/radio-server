# 0073 — A radio-holder seam for a swappable active radio

Status: Accepted

## Context

The app is single-radio to the bone. `build_app` builds one `radio = create_radio(...)` and threads that
one instance into `RxPump`, every `TxSession`, the DTMF `Controller`, the `ScanEngine` factory, and the
station-ID paths; the FastAPI lifespan tears those pieces down inline. There is no one object that owns the
radio *and* its pipeline, so **live backend switching is impossible** — you cannot stop the pipeline, build a
different backend, and restart against it, because the wiring and teardown are scattered across the
composition root.

This cycle introduces the seam that makes the swap possible — and nothing else. It is a **pure,
behaviour-preserving refactor**: no switching, no second backend, no select endpoint, no config, no UI. The
whole existing suite stays green with behaviour identical; that is the primary proof.

## Decision

Introduce **`radio_server/api/holder.py`** with:

- **`build_radio(settings) -> Radio`** — the `server.backend` switch (settings→kwargs per backend) plus the
  two fail-loud squelch guards (baofeng + `audio.squelch=cat`; kv4p + `audio.squelch=cat` at squelch level 0),
  extracted verbatim from `build_app`. It calls the existing `create_radio(...)`. It lives at the composition
  root (api/), **not** in `backends/factory.py`: the backend classes are deliberately Settings-free (the
  composition root owns the settings→kwargs mapping), and the switch carries config-layer policy. `api/` is
  the top import layer, so reading Settings/activity here introduces no cycle. This is the one place the swap
  cycle calls to build a different backend.

- **`class RadioHolder`** — owns the active radio and the lifecycle of the radio-bound pipeline. Constructed
  with the active `radio` plus the stable, radio-*independent* collaborators the pipeline binds against
  (`hub`, `audio_hub`, `arbiter`, `gate`, `recorder`, `controller`, `scan_settings`, `scan_poll`). Exposes
  **`.radio`** — the single reference the app owns the active radio through (`app.state.holder`).
  - **`start()`** (sync, idempotent) — constructs the pieces bound to the radio: the single capture reader
    `RxPump` and the `ScanRunner` (engine factory over `scan_settings`/`radio`/`arbiter`), with their two
    hub-publish adapters (`on_activity`→`rx` event, scan progress→`scan` event) owned here since each only
    needs `hub`. It starts **no** task — the pump is demand-started (`_acquire_rx`) and a scan is plan-started
    (`scan_runner.start(plan)`); `start()` only builds them so those on-demand starts have something to drive.
    The name matches the holder's swap contract (`stop(); …; start()`), not the pieces' own task-`start`.
  - **`stop()`** (async, idempotent, fail-safe) — teardown in the proven lifespan order, each step
    independently guarded so it is safe when a piece was never started (and as the first half of a swap):
    drop PTT *if the arbiter holds the transmitter* → stop a running scan → halt the pump → reap the
    controller's DTMF decoder → close the radio device (`getattr(radio, "close", None)`).

`build_app` now calls `radio = build_radio(settings)`. `create_app` builds the stable collaborators as before,
constructs the holder, calls `holder.start()`, and **rebinds its locals** `rx_pump = holder.rx_pump` /
`scan_runner = holder.scan_runner` (the demand-counter, the Mumble bridge's `rx_active`, and the scan routes
all close over those locals). `app.state.{holder,radio,rx_pump,scan_runner}` point at the holder's instances.
The lifespan's radio-bound teardown block becomes `await app_.state.holder.stop()`, with `link.disconnect()`
kept **before** it (it releases the link's rx demand first) and the recorder/event-log teardown kept after.

The swap cycle then reduces to `await holder.stop(); <build a new radio + rebind the pieces>; holder.start()`.

## Findings surfaced (pieces that aren't cleanly stoppable / aren't singletons)

Recording these rather than papering over them, per the cycle's brief:

- **(a) There is no single app-level "is keyed" flag.** PTT-drop is fragmented across per-connection
  `TxSession.close()`, the direct `POST /ptt` path (which keys `radio.ptt` directly), and the Mumble bridge's
  own session. The closest global truth is `arbiter.transmitting` (half-duplex ownership), so `stop()` keys
  down **conditionally on it** — an arbiter-holding session (a real keyed over) can never leave the transmitter
  latched across a swap, while a quiescent teardown adds no spurious `ptt(False)` (which is what keeps behaviour
  identical — the keying-contract tests assert the exact `ptt_log`). It cannot cover a `POST /ptt`-keyed radio,
  which bypasses the arbiter; that residual gap is the reason a future app-owned keyed-state owner is still
  worth having. (An *unconditional* drop was tried first and rejected: it changed the observable teardown
  keying and broke five keying-contract tests.)
- **(b) The `Radio` protocol has no `close()`.** Mock/aioc/kv4p implement it; `SignaLinkV71` has none — hence
  the `getattr` guard (the same pattern `Controller.close` uses for its decoder). Calling `radio.close()` on
  teardown is a deliberate, safe addition (a no-op on MockRadio; releases the serial device on the real
  backends at shutdown; load-bearing for the swap). Promoting `close()` to the protocol is a candidate for a
  later cycle.
- **(c) The station-ID "scheduler" is not a stoppable object.** Periodic ID is clock-driven inline inside
  `Controller.step` and `TxSession.feed` — there is no scheduler task to move into or halt from the holder.
  Nothing to own here this cycle.
- **(d) The DTMF `Controller` has no self-owned task.** It is driven by the shared `RxPump` feeding `step`
  (ADR 0031); the `ControllerRunner` / the `runner` param are vestigial. A clean stop is "halt the pump, then
  `controller.close()`" — exactly what `stop()` does. Removing `ControllerRunner` is out of scope.

## Considered and deferred (the swap cycle, not this one)

- **Relocating the routes' direct `radio` references through `holder.radio`.** Dozens of route closures capture
  the local `radio` param (== `holder.radio`, the same object today). Rewriting them is zero-behaviour-change
  churn whose only value is a *live* swap (so a route reads the new radio). Deferred with the swap logic.
- **`RadioHolder.rebuild(new_settings)` / a select endpoint / multi-backend config / the web UI.** The whole
  point of the seam, but each is its own reviewable cycle. No swap code ships here (no `swap()`/`rebuild()`
  wired to anything).

## Consequences

- One object (`RadioHolder`, `app.state.holder`) now owns the active radio and its pipeline lifecycle; teardown
  is centralized in `holder.stop()` instead of scattered across the lifespan. `build_radio` is the single
  config→radio path.
- Behaviour is unchanged: `create_app`'s signature, the `app.state.*` names tests read, and every route's
  `radio` binding are all identical. `uv run pytest` → **1015 passed, 5 skipped** (the prior 1008 plus seven
  new holder tests). The `test_backend_wiring` suite now monkeypatches `create_radio` where `build_radio` looks
  it up (the switch moved modules; the wiring it asserts is identical).
- New hardware-free `tests/test_radio_holder.py` locks the seam the swap cycle builds on: `start()` constructs
  the pipeline (and starts no task) and is idempotent; `stop()` is idempotent and safe before `start()`;
  `stop()` keys down when the arbiter holds TX and — the behaviour-preservation guard — does **not** key down
  at a quiescent teardown; `stop()` halts a running scan.

## Non-goals

No second backend; no select endpoint; no swap/rebuild logic; no multi-backend config; no UI; no route-level
`holder.radio` indirection; no `ControllerRunner` removal; no `close()` on the `Radio` protocol; the network
`LinkManager` teardown stays in the lifespan (before `holder.stop()`), not in the holder.
