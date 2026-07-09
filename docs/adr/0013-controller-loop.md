# 0013 — The controller loop: a pure step() over a live receive(), wiring session → station-ID

Status: Accepted

## Context

Every layer of the software tower now exists but nothing runs them together in real time. Cycle 7
built `DtmfInput.pump` (audio → completed DTMF entries) and deferred the live pump; the auth layer
has `AuthGate.on_dtmf` + `Session`; cycle 3/4 built the `Dispatcher` and `StationId` with
`begin_session`/`check`/`sign_off`; cycle 11 built `ScanEngine.tick`. But `StationId`'s
session-lifecycle methods were **never called outside tests**, no driver polled `radio.receive()`,
and there was no production composition root assembling the stack (unlike `build_app` /
`build_scan_engine`). This is the last all-mock cycle: it builds that driver so the full tower runs
live end-to-end on `MockRadio`. Only the two hardware backends remain.

The load-bearing subtlety: `AuthGate` evaluates inactivity **lazily**, inside `on_dtmf` — it has no
standalone tick and emits no open/close signal. A session that goes idle with *no further DTMF* is
therefore never actually closed, so its `StationId.sign_off` would never fire. The controller must
detect that transition itself.

## Decision

- **A pure, clock-injected `Controller.step(now, rx_audio)` core, and a thin async
  `ControllerRunner.run()` shell** — the same two-surface split the scan engine uses (ADR 0012).
  `step` is one iteration: decode the received frame to DTMF entries (`DtmfInput.pump`), route each
  through `AuthGate.on_dtmf` (whose dispatch hook transmits any service audio, auto-ID'd via its
  `StationId`), drive the station ID off the resulting transitions, then tick any attached scan.
  Every timing decision is made against an injected clock, so tests drive it with the `FakeClock`
  and there are no real sleeps. `run()` loops `radio.receive()` → `step(clock(), audio)` on a poll
  cadence and holds **no logic** that isn't exercised through `step` — a shell so the tested core
  runs live.

- **Session → station-ID wiring, off real transitions.** An `ACCEPTED` outcome opens a session and
  arms the ID (`begin_session`); the periodic-ID safety net (`check`) forces an ID-only over when
  overdue mid-session (Part 97, guardrail 5); an inactivity close signs off (`sign_off`). A session
  opened *this* step is never idle (its `last_activity` is `now`), so the close check cannot fire a
  false sign-off in the same iteration. The one `StationId` built by the composition root is
  **shared** with the `Dispatcher`, so service transmissions and the lifecycle IDs draw on one ID
  state.

- **`AuthGate.expire_if_idle(session, now)` surfaces the lazy demotion as a polling seam.** The
  inactivity check that `on_dtmf` did inline is extracted into a method returning whether it closed
  the session; `on_dtmf` now calls it (behavior identical — existing tests unchanged). This is the
  seam the controller calls each step, mirroring `DtmfFramer.tick` ("apply the timeout without
  feeding a key, for a real polling loop"). Without it the idle→closed transition is invisible to
  the loop and the closing ID never sends.

- **Progress is emitted through an injected callback, so `controller` stays below the API.** The
  controller emits a frozen `ControllerEvent(phase, data)` — phase in `CONTROLLER_PHASES`
  (`session_open`, `id`, `session_close`) — to an injected `on_event`, and imports only `audio`,
  `auth`, `services`, `scan`, `backends`. It never imports `EventHub`. The API adapts each event to
  an `Event(type="session", data={...})` on the shared hub — the `_publish_scan` pattern one layer
  up — keeping the arrow `api → controller` with no cycle. `on_event` is a public, reassignable
  attribute because the hub does not exist at `build_controller` time; `create_app` rebinds it after
  construction. `"session"` was already reserved in `EVENT_TYPES` (cycle 10 anticipated this); the
  hub module is otherwise untouched.

- **A `build_controller(env, *, radio, decoder=None, tts=None, clock=None)` composition root.**
  Env-first, mirroring `build_scan_engine` / `build_id_encoder`: it assembles the encoder
  (`build_id_encoder`), a `StationId`, the `ServiceRegistry` + time service, the `Dispatcher`, a
  `TotpVerifier` + `AuthGate`, and a `DtmfInput`, from marked-default fail-loud loaders. `decoder`
  and `tts` are injectable so tests wire a `FakeDtmfDecoder` + `StubTts` (no multimon/piper);
  production defaults to `MultimonDtmfDecoder` + `PiperTts`. It fails loud (`load_totp_secret` /
  `load_callsign`) rather than serving open or un-ID'd.

- **The API starts/stops the loop and surfaces its live state.** `POST /controller {on}` (token-
  gated) starts an `asyncio` task running `runner.run()` or stops and cancels it; a clear **503**
  (not a silent no-op) when no controller was configured — the fail-loud posture of the CAT gate.
  `/status` gains a `controller` block (`{running, session_open}`, or `null` when unconfigured).
  `build_app` wires a controller only when `RADIO_TOTP_SECRET` is set, so the prior no-hardware
  `build_app` contract is preserved; full production wiring (real multimon/piper) lands with the
  hardware bring-up.

- **Loop timing is guardrail-1 config, verify-on-hardware.** `DEFAULT_CONTROLLER_POLL` is marked
  "VERIFY AGAINST HARDWARE" — the real cadence is bounded by how long `receive()` blocks, the audio
  chunk size, and loop timing, all empirical bring-up facts. `DEFAULT_SESSION_TIMEOUT` is an
  operator preference (guardrail 4 keeps sessions short) feeding both `AuthGate(timeout=…)` and the
  controller's idle detection, so there is one source of truth. On the mock, `receive()` returns
  instantly, so this value does not affect the tested `step()` logic at all.

## Consequences

- The full software tower runs live end-to-end on the mock for the first time: received audio →
  DTMF → TOTP auth → dispatch → a CW-ID'd transmission, with automatic periodic and sign-off ID and
  an optional live scan, all pumped by one clock-injected loop. `StationId.begin_session`/`check`/
  `sign_off` are finally wired to real events, closing the cycle-4 deferral.
- **A fourth module now publishes to the cycle-10 hub without depending on it** — the injected-
  callback seam (scan established it) is confirmed as the pattern for any producer below the API.
- Full suite: **227 passed, 4 skipped** (was 213; +14 model-free tests, all running — no skips
  added; the 4 skips remain the multimon + piper hardware/model gates).
- **Scope limits, deliberate:** the async driver is a thin shell — its live behavior beyond "loops
  and stops" is proven by the pure `step()` tests and a bounded `run()` test, not by long-running
  wall-clock integration; the controller ticks an *attached* scan but starting a live scan through
  the API is left to a later cycle (the synchronous `/scan` sweep stays); and running `receive()`
  directly in the event loop rather than a thread executor is a hardware-bring-up decision.
- **Verify-on-hardware (guardrail 1):** the real `receive()` cadence, audio buffer/chunk size, and
  therefore the true loop timing are bring-up checks — the mock delivers scripted audio instantly.
- **Numbering / branch note:** ADR 0013 by cycle order, cut from `master` at **213 passed, 4
  skipped** (the cycle-11 scan merge point). It builds on `audio` + `auth` + `services` + `scan` +
  the cycle-10 `api`; the only non-additive touch below the API is `AuthGate.expire_if_idle`
  (behavior-preserving refactor).
- **Still ahead before RF:** the two real hardware backends (`SignaLinkV71`, `AiocBaofeng`) — the
  "plug it in, it keys up clean" empirical phase — plus per-service auth strength (guardrail 4) and
  live-scan-through-the-controller if wanted.
