# 0028 — Async scan runner (background task, start/stop lifecycle, single-scan guard, clean stop at tick boundary, stop-while-TX-suspended, shutdown cancellation)

Status: Accepted

## Context

Since cycle 11 (ADR 0012) `POST /scan` ran one **synchronous** `ScanEngine.sweep()` — a single pass
that blocks the request until it stops-and-holds at the first active channel, with no way to stop it
and no persistent scan state. So the cycle-21 web UI could only offer a "Scan" button with **no stop
button** and a live phase line — the deferred backend gap noted in every HANDOFF since.

The engine already had the harder half built. `ScanEngine.tick(now)` is a clock-driven, resume-mode
(carrier/timed/hold) state machine, and ADR 0017 threaded the half-duplex arbiter through it so a TX
key-up pauses the scan in place (`tick()` early-returns while `arbiter.transmitting`, and all
positional state survives on the instance). But no live path drove `tick()` — only `sweep()` was
wired. This cycle adds the **async driver + control surface** around that existing machine, mirroring
how `RxPump` drives `receive()`. The engine's tick/sweep logic and arbiter behavior are unchanged.

Mock-only, no hardware. `MockRadio`'s scriptable `busy_frequencies` drives the tests; timing paths use
the `FakeClock` and `poll=0` (no real sleeps), per the suite convention.

## Decision

- **`ScanRunner` — a background task mirroring `RxPump`** (new `radio_server/scan/runner.py`). It owns
  a single `asyncio.Task` that steps `ScanEngine.tick()` on the `scan.poll` cadence. `start(plan)` is a
  **single-scan guard** — it builds the engine via an injected `engine_factory` and returns `False` if a
  scan is already running (never stacked). `stop()` clears its task reference **before** awaiting the
  cancel (the `RxPump` discipline) and is idempotent. The runner stays below the API: it emits progress
  (and the `stopped` lifecycle event) through the same injected `on_event` callback the engine uses, so
  it never imports `EventHub` — the API's `_publish_scan` adapts to `Event(type="scan", …)` as before.
- **`POST /scan` is now non-blocking.** It stays capability-gated (**501** naming `"scan"` on an
  audio-only backend, unchanged) and validates the plan (**422** on an ambiguous body, unchanged), then
  `scan_runner.start(plan)`; a start while already scanning is a **409 Conflict** (a clear conflict, not
  a silent stack). It returns `{"scanning": true, "status": …}` immediately. The old synchronous `held`
  return is gone — held/dwell is now a live concept (the `held` value from `sweep()` had no async
  equivalent); `sweep()` itself is retained on the engine but no longer on the live path.
- **`POST /scan/stop` — signal stop, clean at the tick boundary.** Capability-gated like `/scan` (501 on
  audio-only, so the endpoint doesn't exist in that mode — guardrail 3). It `await`s `scan_runner.stop()`,
  which cancels the task; because `tick()` is **fully synchronous** (no `await` inside), the cancel can
  only land at the loop's `await asyncio.sleep(poll)` — **never mid-tune**, so the in-progress tick always
  completes. The runner then emits a `stopped` event and drops to idle. **Idempotent**: a stop when
  nothing is scanning is a clean no-op ack (`{"scanning": false, "stopped": false}`), not an error.
- **Stop while TX-suspended cannot wedge.** While `arbiter.transmitting`, the engine's `tick()`
  early-returns, so the runner loop keeps *polling* (spinning cheaply) rather than blocking on a resume
  that may never come. A stop therefore cancels cleanly even while the scan is paused for TX — proven at
  both the unit and endpoint level.
- **Lifecycle tied to the app.** One `ScanRunner` is created in `create_app` (`app.state.scan_runner`),
  and the lifespan teardown `await app_.state.scan_runner.stop()` right after the `rx_pump.stop()` line —
  same discipline, so a scan still running at shutdown is cancelled with no leaked task.
- **Live state in `/status` and `/events`.** `/status` gains a `scan` block (`{running, frequency}`,
  mirroring the `controller` block) and the `/events` stream carries the scan phases including the new
  `stopped`, so the UI reflects running/stopped and enables/disables the stop button.
- **UI: a real Start/Stop pair.** `ScanControl.jsx` replaces the lone "Scan" button with a Start/Stop
  pair modeled on `ControllerControl`, tracking `running` (optimistically from the POST responses and
  from live `scan` events, so a scan started/stopped elsewhere — or torn down at shutdown — is reflected
  too). `api.js` gains `scanStop()`. This closes the cycle-21 "Scan + live phase, no stop" into real
  start/stop.

## Consequences

- **Scan is stoppable.** A background scan runs after `POST /scan` returns, streams its phases to
  `/events`, and ends cleanly on `POST /scan/stop` at a tick boundary — verified end-to-end against a
  real bound server (uvicorn + websockets) and in a headless browser (Start → live phase + Stop enabled
  → Stop → idle).
- **`uv run pytest` → 436 passed, 4 skipped** (+10; the 4 skips unchanged). New `tests/test_scan_runner.py`
  (async unit tests: background start emits `scanning`, single-scan guard, clean stop emits `stopped` with
  no leaked task, idle-stop no-op, stop-while-TX-suspended). `tests/test_scan.py`'s endpoint tests were
  rewritten for the async contract (non-blocking ack, first `scanning`/`stopped` over the WS, 409 on a
  second start, 501 on both endpoints for audio-only, shutdown cancels the task, stop-while-TX-suspended).
- **The `held` response field is gone** from `POST /scan` (now `{scanning, status}`) — a deliberate
  contract change; the UI consumed `held` and is updated to read live phase instead.
- **Testing note:** a background task spawned during a request is cancelled by `TestClient` when that
  request ends unless the client is driven as `with TestClient(app) as client:` (one persistent loop) —
  the endpoint tests that need the scan to live across requests use that form.
- **Deferred, on purpose:** live hot-reload; Opus/compression; the hardware backends' real tune/busy
  timing (guardrail 1 — `scan.poll`/settle/dwell stay verify-on-hardware config).

## Numbering / branch note

Cut from `origin/master` at `86bb000` (cycle 27, ADR 0027 merged via #29). Branch
`cycle-28-async-scan`, PR against `master`. ADR numbering continues cleanly at 0028 (the known
duplicate `0001` from cycle 24 is untouched).
