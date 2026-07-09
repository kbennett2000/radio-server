# 0012 — The software scan engine: a clock-driven loop over CAT, gated and streamed

Status: Accepted

## Context

radio-server's pitch includes "scan channels remotely like in person," and `scan` is one of the
five CAT-only capabilities (`Capability.SCAN`, ADR 0002), but nothing implemented the scan *loop*.
The backend's `CatRadio.scan(on)` is only a bare hardware start/stop toggle — it does not step
channels, poll busy, or act on activity. Cycle 10 deliberately left the WebSocket `EventHub` open
for this (`EVENT_TYPES` reserved room; ADR 0011 named "the V71-only scan engine (next cycle)" as
the consumer of a `"scan"` event type). This cycle builds that loop over the `CatRadio` surface —
tune, settle, poll `status().busy`, decide — fully testable against `MockRadio` with an injected
clock (no hardware, no real sleeps).

The engine is **software scanning**, distinct from the radio's built-in `scan(on)`: it tunes via
`set_frequency` and reads `status().busy`, the "scan like in person" behavior a remote operator
wants. It is V71/CAT-only; on an audio-only backend it is not advertised and its API endpoint
returns the same `501` as the other CAT endpoints (guardrail 3). PTT is never touched — scanning is
receive-only tuning (guardrail 2 is unaffected).

## Decision

- **A pure, clock-driven state machine with two drive surfaces.** `ScanEngine.tick(now)` is the
  full resume-mode machine: after each tune it waits a `settle` window before trusting the busy
  read, then dwells / resumes / holds / advances. Every timing decision is made against an injected
  `clock` (default `time.monotonic`), so tests drive it with the `FakeClock` and there are no real
  sleeps. `ScanEngine.sweep()` is a synchronous single pass that stops-and-holds at the first active
  channel; clear channels advance instantly (a sweep never dwells on time), so it needs no clock and
  runs with zero sleeps. Both share pure helpers (`_tune`, `_read_busy`, `_emit`, `_advance`), so the
  loop logic is one implementation, not two.

- **A small plan model addressed by frequency.** `ScanPlan` is an ordered tuple of frequencies (Hz)
  plus a `lockout` frozenset (skipped channels) and an optional `priority` frequency (re-checked
  between steps). `from_frequencies(...)` and `from_range(start, stop, step)` build it;
  `active_channels()` is the scanned order with lockouts removed. Plans address by **frequency**
  because a range+step is naturally in Hz and per-channel busy is keyed on frequency; channel-*number*
  plans are out of scope this cycle.

- **Three resume modes, carrier the marked default.** `ResumeMode.CARRIER` dwells while the channel
  stays busy and resumes when the carrier drops (the classic "listen until they stop talking");
  `TIMED` dwells a fixed `dwell` seconds then moves on even if still busy; `HOLD` stops the scan
  entirely on the first activity. `RADIO_SCAN_MODE` selects it, defaulting to `carrier`.

- **Progress is emitted through an injected callback, so `scan` stays below the API.** The engine
  emits a frozen `ScanEvent(phase, frequency, channel)` — `phase` in `SCAN_PHASES`
  (`scanning` → `active` → `dwelling`, plus `resumed` when a dwell ends) — to an injected
  `on_event`, and imports only `radio_server.backends`. It never imports `EventHub`. The API adapts
  each `ScanEvent` to an `Event(type="scan", data={...})` on the shared hub. This keeps the
  dependency arrow `api → scan` (and `scan → nothing-above`); had the engine imported the API's hub,
  `api` (which imports `ScanEngine`) and `scan` would form a cycle. `"scan"` is now registered in
  `EVENT_TYPES` — the only touch to the hub module, exactly as ADR 0011 anticipated ("without
  touching the hub" — `EventHub` itself is unchanged).

- **Capability-gated at both the engine and the HTTP boundary.** `ScanEngine.__init__` raises
  `UnsupportedCapability(Capability.SCAN)` on a backend that does not advertise `SCAN` — a defensive
  guard mirroring the mock's `_require_cat`. `POST /scan` pre-checks `_require_cat(Capability.SCAN)`
  and re-maps any `UnsupportedCapability` to the same `501` body `{"error": ..., "capability":
  "scan"}` the other CAT endpoints use (guardrail 3) — never a silent no-op. The endpoint lives on
  the token-gated router, so it inherits the LAN bearer auth for free.

- **The HTTP `/scan` surface is a thin control that runs one synchronous sweep.** It builds a plan
  from the request (`frequencies`, or a `start/stop/step` range; exactly one form, else `422`), runs
  `engine.sweep()`, and returns `{"held": <freq|null>, "status": {...}}`, publishing `scan` events to
  the hub as it goes so a WebSocket client sees live progress. The live, real-time background pump
  (continuous `tick()` on the busy-poll cadence) is deferred to the controller-loop cycle, exactly as
  cycle 7 shipped the pure DTMF pieces and deferred the live `receive()` pump.

- **`MockRadio` gains scriptable per-frequency busy.** A `busy_frequencies` set (public, mutable)
  makes `status().busy` true while tuned to a listed frequency, on top of the existing flat `busy`
  flag. A test scripts "channel X is busy" with `MockRadio(busy_frequencies={X})` and drops the
  carrier mid-scan with `radio.busy_frequencies.discard(X)`. The flat `busy` bool is untouched, so
  every existing test still passes.

- **Timing defaults are guardrail-1 config, verify-on-hardware.** `DEFAULT_SCAN_SETTLE` (post-tune
  settle before the busy read is trusted) and `DEFAULT_SCAN_POLL` (the future pump's tick cadence)
  are marked "VERIFY AGAINST HARDWARE" on their constants — real settle time is the radio's PLL lock
  + squelch response, an empirical bring-up fact. `DEFAULT_SCAN_DWELL` is an operator preference. All
  loaders fail loud on a set non-numeric/non-positive value rather than papering it over.

## Consequences

- The "scan channels remotely" feature exists and is reachable: `POST /scan` sweeps and holds, and a
  WebSocket client watches `scan` progress live on the stream cycle 10 established. Full suite:
  **213 passed, 4 skipped** (was 187 on the merged stack; +26 model-free tests, all running — no
  skips added; the 4 skips remain the multimon + piper hardware/model gates).
- **Guardrail 3 now covers all five CAT capabilities** at the HTTP edge with one consistent 501
  body; `scan` is the fifth. No silent no-ops.
- **A third module now publishes to the cycle-10 hub without depending on it** — the injected-callback
  seam is the pattern any future producer (session lifecycle, busy) should copy.
- **Scope limits, deliberate:** the `/scan` endpoint runs one synchronous sweep (stop-and-hold at
  first activity) — the live real-time background pump that would exercise carrier/timed dwell over
  wall-clock is a later controller-loop cycle; plans address by frequency (Hz), not channel number;
  the priority channel is peeked with an immediate poll (no separate settle) between steps; and
  `tick()`'s continuous scanner wraps the plan (a real pump bounds it).
- **Verify-on-hardware (guardrail 1):** the settle time, busy-poll cadence, and therefore the real
  scan speed and squelch responsiveness are bring-up checks, not proven here — the mock's busy is
  instantaneous and scripted.
- **Numbering / branch note:** this ADR is 0012 by cycle order, cut from `master` at the cycle-9/10
  merge point (187 passed, 4 skipped). It builds only on `backends` + the cycle-10 `api` package;
  `services/` and `auth/` are untouched.
- **Still ahead before RF:** the controller/API pump loop (drive `DtmfInput.pump` and
  `ScanEngine.tick` + the ID session lifecycle on a live `receive()` loop) and the two real hardware
  backends (`SignaLinkV71`, `AiocBaofeng`) — the "plug it in, it keys up clean" phase.
