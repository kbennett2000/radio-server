# Handoff

## Current state

Cycle 12 complete: the **controller loop** (ADR 0013) — the **full software tower now runs live
end-to-end on the mock**. One clock-injected driver pumps everything on a `receive()` loop:
received audio → DTMF → TOTP auth → dispatch → a CW-ID'd transmission, with automatic periodic and
sign-off ID and an optional live scan. `Controller.step(now, rx_audio)` is the **pure, testable
core** (one iteration); `ControllerRunner.run()` is a **thin async shell** looping `radio.receive()`
→ `step()` on a poll cadence with no logic of its own. This is the cycle where `StationId`'s
session-lifecycle methods finally connect to real events (built cycle 4, deferred since): an
`ACCEPTED` outcome **opens a session and arms the ID** (`begin_session`); the periodic-ID safety net
(`check`) **forces an ID when overdue mid-session** (Part 97); an inactivity close **signs off**
(`sign_off`). Because `AuthGate` only demotes an idle session *lazily* inside `on_dtmf`, the
transition was surfaced as **`AuthGate.expire_if_idle(session, now)`** (a behavior-preserving
refactor mirroring `DtmfFramer.tick`) so the loop can detect and act on it. Lifecycle is emitted as
`ControllerEvent(phase, data)` (`session_open`/`id`/`session_close`) through an **injected callback**
— the controller never imports `EventHub`, so `api → controller` has no cycle; the API adapts each to
a **`"session"` event** on the cycle-10 `EventHub`. `build_controller(env, *, radio, decoder, tts,
clock)` is the composition root (fail-loud on the TOTP secret / callsign; `decoder`/`tts` injectable
so tests use `FakeDtmfDecoder` + `StubTts`). The API gained **`POST /controller {on}`** (token-gated,
**503** when unconfigured — never a silent no-op) and a **`controller` block in `/status`**
(`{running, session_open}`, `null` when unwired). Loop cadence is guardrail-1 **verify-on-hardware**
config. `uv run pytest` → **227 passed, 4 skipped** (+14 model-free tests; the 4 skips are unchanged).

Cycle 11 complete: the **software scan engine** (ADR 0012) — "scan channels remotely like in
person." A V71/CAT-only scan *loop* over the `CatRadio` surface (distinct from the radio's built-in
`scan(on)` toggle): it steps a `ScanPlan` of frequencies, tunes each (`set_frequency`), lets the
reading **settle**, polls `status().busy`, and acts on activity. Two drive surfaces share one set of
pure helpers: `ScanEngine.tick(now)` is the **clock-driven resume-mode machine** (carrier = dwell
while busy, resume on drop — the marked default; timed = dwell N s then move on; hold = stop on first
activity), and `ScanEngine.sweep()` is a **synchronous single pass** that stops-and-holds at the
first active channel (clear channels advance instantly — no clock, no sleeps). Lockout skips
channels; a **priority** frequency is re-checked between steps. Progress is emitted as
`ScanEvent(phase, frequency, channel)` (`scanning`→`active`→`dwelling`, plus `resumed`) through an
**injected callback**, so `scan` stays *below* the API (no `scan↔api` cycle); the API adapts it to a
`"scan"` event on the **cycle-10 `EventHub`** (now registered in `EVENT_TYPES`), so a WebSocket
client watches scan progress live. **Capability-gated** exactly like the other CAT endpoints:
`POST /scan` runs one sweep on a CAT backend and returns **`501` naming `"scan"`** (never a no-op) on
an audio-only one, where it is not advertised. `MockRadio` gained scriptable **`busy_frequencies`**
so a test can script per-channel activity and drop a carrier mid-scan — fully deterministic, no
hardware, no real sleeps. Timing (settle, poll cadence) is guardrail-1 **verify-on-hardware** config.
`uv run pytest` → **213 passed, 4 skipped** (+26 model-free tests; the 4 skips are unchanged).

Cycle 10 complete: the **FastAPI HTTP/WebSocket API layer** (ADR 0011) — the stack is reachable
over the network for the first time, and **guardrail 3 (the capability split) is enforced at the
HTTP boundary**. A thin, honest surface over the injected `Radio`: shared endpoints (`GET /status`,
`GET /capabilities`, `POST /ptt`, `POST /transmit`) always live; the CAT endpoints
(`POST /frequency` `/channel` `/tone` `/mode`) check `Capability` membership and, on an audio-only
backend, return **`501` with the missing capability named in the body** (`{"capability":
"set_frequency"}`) — never a silent no-op, so the web UI can grey out exactly the right control. A
**second, separate auth plane** lands here: a LAN-facing static **bearer token** (constant-time
compare, closed by default, `401`/WS-`1008` on missing/bad), kept deliberately distinct from the
over-RF TOTP/DTMF plane (different threat model — no replay window/burn). A `type`-discriminated
WebSocket `EventHub` pushes a `status` snapshot on connect and further events on control calls;
its shape is left **open for the scan engine's `scan` events next cycle**. `FastAPI`/`uvicorn` are
**core deps** (the API is the product's stated purpose), so the tests **run**, not skip. The API is
**independent of the DTMF/piper/voice-ID stack (#7–#9)** — it imports only `backends` + the new
`api` package and touches no `services/` file — so the two compose additively, as they now do on
`master`: with #7–#9 merged alongside, `uv run pytest` → **187 passed, 4 skipped** (cycle 10 added
18 API tests; cycles 7–9 added 38, with 4 hardware/model `skipif` gates).

Cycle 9 complete: **`VoiceId` + configurable ID mode** (ADR 0010) — the **audio-content
tower is now complete**. `VoiceId` is the second `IdEncoder` (after `CwId`): it speaks the
callsign as NATO/ITU phonetics (**9→"niner"**, so "AE9S" → "alpha echo niner sierra")
through an injected `TtsEngine` — `StubTts` in tests (byte-exact), `PiperTts` in production.
It satisfies the same one-arg `encode(callsign)` contract, so the **cycle-4 `StationId`
scheduler is untouched** — swapping CW for voice is an encoder swap, not a scheduler change.
The phonetic map (`PHONETIC`, `spell_callsign`) is **pure and separated from synthesis**, so
it is exactly assertable with no engine; unknown chars **fail loud** (`ValueError`), and the
accepted set matches `CwId`'s (A-Z, 0-9, `/`→"slash"). `RADIO_ID_MODE` (`cw` | `voice`)
selects the encoder via `build_id_encoder` (the first real composition root); **CW is the
marked default** (no model dependency, always works). Voice mode with no `RADIO_TTS_VOICE`
**fails loud** at construction — it never silently degrades to CW. **Guardrail 1:** the one
real-piper `VoiceId` test is `skipif`-gated (skips here) and property-asserted; on-air
intelligibility is a bring-up check. `uv run pytest` → **169 passed, 4 skipped** (+17
model-free tests in `test_voice_id.py`; the 4th skip is the new real-`VoiceId` test).

Cycle 8 complete: **real piper TTS** (`PiperTts`; ADR 0009) — the first real spoken audio,
behind the existing cycle-3 `TtsEngine` protocol. `render(text)` runs piper at the voice's
native rate and resamples up to canonical 48k, so `PiperTts` is the **first consumer of
`to_canonical`** — this cycle *proves the playback edge*, the symmetric mirror of cycle 7's
`to_multimon` decode edge (both ADR 0006 edges are now exercised). It is a **drop-in for
`StubTts`**: same one-method `render` contract, so the time service, dispatcher, `StationId`,
and `CwId` are untouched, and `StubTts` is **retained unchanged** as the deterministic
exact-assert baseline. The voice's native rate is **read from its `.json` sidecar**
(`audio.sample_rate`), never hardcoded to 22050 (voices vary; some are 16000). Model config
**fails loud**: `RADIO_TTS_VOICE` names the `.onnx` and has **no default** (like the TOTP
secret) — `load_tts_voice` raises when unset, and `PiperTts.__init__` raises on a missing
`.onnx`/sidecar/rate, *before* any piper import. **Guardrail 1:** piper + `onnxruntime` are
**not installed** here (declared as an optional `tts` extra, not a core dep), so the two
real-engine tests are `skipif`-gated (skip here, run where a model is present); the exact
piper version/API is isolated in `_synthesize_raw` and marked verify-against-build; neural
output is **property-asserted, never byte-asserted**; RF intelligibility is a bring-up check.
The `to_canonical` edge itself is proven **model-free** — a synthetic 16000/22050 Hz voice
buffer resamples to a canonical 48k frame of the expected length. `uv run pytest` →
**152 passed, 3 skipped** (+9 model-free tests in `test_tts.py`; the 3 skips are the 2 real
piper tests + cycle 7's real-decode test).

Cycle 7 complete: **DTMF decode + framing** (`radio_server/audio/dtmf.py`; ADR 0008) — the
audio-in → digits seam, and the **first full end-to-end on the mock**. Received `AudioFrame`
audio now drives the auth gate: `DtmfDecoder` (protocol seam; real `MultimonDtmfDecoder`
shells out to `multimon-ng -a DTMF -t raw -` over stdin, a `FakeDtmfDecoder` drives tests) →
`DtmfFramer` (pure, clock-injected grammar: `#` submit, `*` clear, inter-digit timeout
**discards** a stalled partial) → `DtmfInput.pump(frame)` returns completed entries → the
**unchanged** `AuthGate.on_dtmf`. Nothing in auth/session/dispatch/`station_id`/`CwId` changed
— the module is even **auth-free** (local `Clock` alias), so the layering arrow stays
audio → nothing-above. Fixtures are deterministic `synth_dtmf` dual-tones (sum two
`synth_tone` frames at the standard `DTMF_FREQS`), asserted by FFT — no on-disk WAVs
(multimon reads raw PCM on stdin). Config: `RADIO_DTMF_TIMEOUT` (default 3.0s) /
`RADIO_MULTIMON_BIN` (default `multimon-ng`), marked defaults. **Guardrail 1:** `multimon-ng`
is **not installed** in this environment, so the one real-decode test is `skipif`-gated on the
binary (skips here, runs where installed); the exact multimon flags/rate are marked
verify-against-build, and real weak-signal / HT-flutter decode robustness is a hardware
bring-up check, not proven here. `uv run pytest` → **143 passed, 1 skipped** (+13 tests in
`test_dtmf.py`). The headline: fixture audio (fake-decoded) → framed digits → TOTP `ACCEPTED`
→ authed `"1"` `COMMAND` → a real CW-ID'd time announcement in `mock.tx_log`.

Cycle 6 complete: **real CW station ID** (`CwId`; ADR 0007) — the first real transmission
content the server produces. `CwId` implements the existing one-method `IdEncoder`, so it is
a **drop-in for `StubId`**: `StationId`, `Dispatcher`, and every config loader are untouched,
and an authed `"1"` now prepends genuine keyed Morse to the time announcement. A pure PARIS
timing layer (`unit_ms`, `cw_timeline` → `(on, duration_ms)` segments) is isolated from PCM so
element/gap timing is exactly assertable; `encode` keys `synth_tone` on/off along it, with
canonical-zero silence for gaps (so concat stays format-identical). Unknown chars **fail loud**
(a wrong ID is worse than a loud failure). WPM/sidetone are **marked-default** config
(`RADIO_CW_WPM`=20, `RADIO_CW_TONE_HZ`=600, guardrail 1) — safe operator prefs, but **on-air CW
readability is an empirical bring-up check, not proven here.** `uv run pytest` → **131 total,
all green**. Still deferred: `VoiceId`, session-lifecycle wiring.

Cycle 5 complete: the **audio format is pinned and load-bearing** (guardrail 1; ADR 0006).
The opaque `AudioFrame = bytes` alias is gone — `AudioFrame` now carries its `AudioFormat`
(rate/width/channels) and **fails loud** (`AudioFormatMismatch`) on a mismatched concat or
transmit, closing the cycle-1 "bytes silently papers over a mismatch" risk by construction.
Canonical internal format is **48000 Hz / s16le / mono**; resampling happens only at the
tolerant software edges via `soxr` (VHQ, anti-aliased so a downsample can't corrupt DTMF). A
real `synth_tone` primitive (sine + raised-cosine anti-click envelope) proves the type with
real PCM and is the CW-ID substrate for cycle 6. **The remaining gate before hardware is now
just the real encoders (CW/voice ID, piper TTS) + empirical bring-up — the format no longer
blocks anything.**

Cycle 4 (merged, PR #4): automatic station ID (guardrail 5, Part 97). The transmit path is
**legality-clean** — every service transmission carries the station ID, there is a
forced-periodic ID timer, and a sign-off ID at session end. `StationId` is the single seam
through which all audio reaches the radio, so no transmission can go out un-ID'd. ID audio
is a deterministic stub (scheduling logic only). See ADR 0005.

Cycle 3 (merged, PR #3): command dispatch + the first voice service (announce-the-time),
the first thing the server transmits. Authenticated digit `"1"` → time announcement
rendered through a stub TTS → `MockRadio.tx_log`. Still fully mock/hardware-free;
unit-tested with the injected fake clock. See ADR 0004.

Cycle 2 (merged, PR #2): a DTMF-gated TOTP auth layer + session state machine, fed digit
strings directly (no audio/DTMF decode yet), unit-tested with an injected fake clock.
See ADR 0003.

Cycle 1 (merged, PR #1): the `Radio` protocol surface + full `MockRadio`, hardware
backends stubbed and wired into a factory. See ADR 0002.

### Controller loop (cycle 12)

- `radio_server/controller/` (new package). `engine.py` — the pure core, thin driver, and root:
  - `Controller.step(now, rx_audio) -> StepResult` — one loop iteration: `DtmfInput.pump` → for each
    entry `AuthGate.on_dtmf` (an `ACCEPTED` → `station.begin_session` + emit `session_open`); then
    `gate.expire_if_idle` (True → `station.sign_off` + emit `session_close`), else if authenticated
    `station.check(now)` (True → emit `id`); then tick an attached `scan`. `StepResult(entries,
    outcomes, session_open, id_sent, signed_off, scanning)`. Order is load-bearing (a session opened
    this step is not idle, so no false close). `on_event` + `scan` are public/reassignable.
  - `ControllerRunner.run()` — `while running: step(clock(), radio.receive()); await sleep(poll)`;
    `stop()` flips the flag. Thin shell, no logic not covered by `step`. Guardrail-1 poll cadence.
  - `ControllerEvent(phase, data)` with `CONTROLLER_PHASES = ("session_open","id","session_close")`.
  - Config: `load_controller_poll` / `load_session_timeout` (`_load_positive_float` shape,
    verify-on-hardware on the poll constant); `build_controller(env, *, radio, decoder, tts, clock)`
    assembles encoder→`StationId`→registry/time-service→`Dispatcher`→verifier/`AuthGate`→`DtmfInput`,
    sharing the **one** `StationId` with the dispatcher. Fail-loud on the TOTP secret / callsign.
- **Layering:** imports only `..audio/auth/services/scan/backends` (all below `api`), emits via the
  injected `on_event` — never imports `EventHub`. `api/app.py` adapts each `ControllerEvent` to
  `Event("session", {"phase":…, …})`, so the arrow stays `api → controller`.
- `auth/session.py` — extracted `AuthGate.expire_if_idle(session, now) -> bool` (returns whether it
  closed an idle authed session); `on_dtmf` now calls it. **Behavior identical** — the seam a polling
  loop needs, since `on_dtmf`'s inactivity demotion is otherwise only reachable by feeding a key.
- `api/app.py` — `create_app(radio, *, api_token, controller=None, runner=None)` rebinds
  `controller.on_event` to the hub adapter and stores both on `app.state`. `POST /controller {on}`
  (token-gated) starts/stops an `asyncio` task running `runner.run()`; **503** when unconfigured.
  `/status` merges a `controller` block (`{running, session_open}` or `null`). `build_app` wires a
  controller only when `RADIO_TOTP_SECRET` is set (prior no-hardware contract preserved).
  `api/events.py` docstring/`EVENT_TYPES` comment updated for the now-live `"session"` type
  (`EventHub` unchanged).
- Tests: `tests/test_controller.py` (12 new) — login opens+arms; authed `"1"` lands a CW-ID'd time
  announcement in `tx_log`; forced periodic ID at the interval; inactivity timeout closes + signs
  off; an attached scan ticks each step and holds on scripted busy; lifecycle events in order; a
  bounded `run()` pumps `step` each iteration; `POST /controller` flips `/status.running` + needs a
  token; `503`/null when unconfigured; `session` events over the WS in order. Plus
  `tests/test_session.py` `expire_if_idle` cases. `uv run pytest` → **227 passed, 4 skipped**. See
  ADR 0013.
- **Deferred (next):** the two hardware backends; optionally starting a *live* scan through the
  controller (the synchronous `/scan` sweep stays); running `receive()` in a thread executor.

### Software scan engine (cycle 11)

- `radio_server/scan/` (new package). `engine.py` — the pure engine + plan + config:
  - `ScanPlan` (frozen): `channels: tuple[int, ...]` (Hz), `lockout: frozenset[int]`,
    `priority: int | None`; `from_frequencies(...)` / `from_range(start, stop, step)`;
    `active_channels()` = order minus lockout. Addresses by **frequency**, not channel number.
  - `ResumeMode` (`carrier` default | `timed` | `hold`); `ScanEvent(phase, frequency, channel)` with
    `SCAN_PHASES = ("scanning", "active", "dwelling", "resumed")`.
  - `ScanEngine(radio, plan, *, on_event, mode, dwell, settle, clock)` — raises
    `UnsupportedCapability(Capability.SCAN)` on an audio-only backend. `tick(now)` is the clock-driven
    machine (settle → poll `status().busy` → dwell/resume/hold/advance, wraps); `sweep()` is the
    synchronous stop-and-hold pass the API uses (no clock, no sleeps). Pure helpers shared by both.
  - Config (guardrail-1 marked, verify-on-hardware on the constant): `load_scan_settle` /
    `load_scan_poll` / `load_scan_dwell` (`_load_positive_float` shape) + `load_scan_mode` (enum,
    fail-loud on unknown); `build_scan_engine(env, *, radio, plan, on_event, clock)` composition root.
- **Layering:** the engine imports only `..backends` and emits via the injected `on_event` — it does
  **not** import `EventHub`. `api/app.py` adapts each `ScanEvent` to `Event("scan", {...})` on the
  hub, so the arrow stays `api → scan`. `api/events.py` only gained `"scan"` in `EVENT_TYPES`
  (`EventHub` itself unchanged, as ADR 0011 promised).
- `api/app.py` — `POST /scan` on the token-gated router: `_require_cat(Capability.SCAN)` → `501`
  naming `"scan"` on audio-only (same body as the other CAT endpoints); else build a plan from
  `frequencies` **or** a `start/stop/step` range (exactly one, else `422`), run `engine.sweep()`,
  publish `scan` events, return `{"held", "status"}`. Live real-time pump **deferred** to the
  controller-loop cycle (like cycle 7's DTMF pump).
- `backends/mock.py` — `MockRadio` gained `busy_frequencies` (public mutable set): `status().busy`
  is true while tuned to a listed freq, on top of the flat `busy` flag (back-compat kept). This is
  the hook that scripts "channel X busy" and drops a carrier mid-scan (`.discard(x)`).
- Tests: `tests/test_scan.py` (26 new) — plan/config; capability gate; sweep holds first active /
  all-clear → None / lockout skips / priority peeked-and-held; tick carrier-resume, timed-move-on,
  hold-stops, settle-gates-the-poll; events in phase order; and the API (`/scan` sweeps on CAT,
  publishes `scan` over WS in order, `501`-naming-`scan` + unadvertised on audio-only, `422` on a bad
  body, `401` without a token). Plus `tests/test_mock_radio.py` busy_frequencies cases. `uv run
  pytest` → **213 passed, 4 skipped**. See ADR 0012.
- **Deferred (next):** the controller/API pump loop that ticks `ScanEngine` + `DtmfInput.pump` + the
  ID session lifecycle on a live `receive()` loop; then the two hardware backends.

### FastAPI API layer (cycle 10)

- `radio_server/api/` (new package). `app.py` — `create_app(radio, *, api_token) -> FastAPI` (the
  DI seam tests drive against `MockRadio`) and `build_app(env)` (the project's first top-level
  composition root: `create_radio(env["RADIO_BACKEND"] or "mock")` + `load_api_token(env)`, mirrors
  `build_id_encoder`). REST routes live on an `APIRouter` gated by a bearer-token dependency; CAT
  routes call `_require_cat(Capability.…)` before dispatching → `501` `{"error":…, "capability":…}`
  when absent (also catches `UnsupportedCapability` to the same body). `POST /transmit` wraps the
  raw request body in a canonical `AudioFrame` → `radio.tx_log`.
- `api/auth.py` — the LAN plane, **separate from `radio_server.auth`**. `RADIO_API_TOKEN` +
  `load_api_token` (fail-loud no-default, mirrors `load_totp_secret`); `token_matches`
  (`hmac.compare_digest`, constant-time); `bearer_token` (parses `Authorization: Bearer …`);
  `make_require_token(expected)` (the FastAPI 401 dependency). No `TotpVerifier`/`Session` reuse —
  static secret, no window/burn.
- `api/events.py` — `Event(type, data)` (`type` ∈ `status|ptt|busy|session`, `scan` reserved),
  `EventHub` (in-process asyncio fan-out: `subscribe`/`publish`/`unsubscribe`), `status_event(radio)`.
  WS `/events?token=…` accepts, sends an initial `status` snapshot, then streams published events;
  bad token → close `1008`.
- Decisions (see ADR 0011): `501` over `409` for gated CAT (permanent not-implemented, not a state
  conflict); token via `?token=` on the WS because browsers can't set WS handshake headers;
  FastAPI/uvicorn **core** (tests run) with httpx in the dev group (TestClient only).
- Tests: `tests/test_api.py` (18 new, `TestClient` over `MockRadio`, both `supports_cat` values) —
  `/status` mirrors state; `/capabilities` tracks `supports_cat`; a CAT route works on a CAT backend
  and returns a `501` naming the capability **with backend state unchanged** on an audio-only one;
  ptt/transmit reach the mock; WS emits a `status` event on connect and a `ptt` event on control;
  auth rejects missing/bad and accepts good; `load_api_token({})` raises. See ADR 0011.
- **Deferred (next):** the V71-only scan engine, which publishes `scan` progress on this
  `EventHub`, plus session-lifecycle wiring surfaced as `session` events on the same stream.

### VoiceId + configurable ID mode (cycle 9)

- `radio_server/services/voice_id.py` (new):
  - `PHONETIC: dict[str, str]` — NATO/ITU A-Z, digits 0-9 with the ham **9→"niner"**, and
    `/`→"slash". Accepted set matches `CwId`'s `MORSE`, so ID mode never changes which
    callsigns encode.
  - `spell_callsign(callsign) -> str` — pure; upper-cases, maps each char, joins with spaces.
    **`ValueError`** on any char outside `PHONETIC` (mirrors `CwId._morse_for`). Engine-free,
    so the map is exactly assertable.
  - `VoiceId` — `__init__(tts)` (DI at construction); `encode(callsign, format=CANONICAL)` →
    `tts.render(spell_callsign(callsign))`. Optional `format` honors the `CwId` shape so
    `isinstance(VoiceId(stub), IdEncoder)` holds and `StationId`'s one-arg call is unaffected.
  - `load_id_mode(env)` / `RADIO_ID_MODE_ENV_VAR` / `DEFAULT_ID_MODE="cw"` — marked-default
    (like `load_id_interval`); a set value outside `{cw,voice}` fails loud.
  - `build_id_encoder(env, *, tts=None)` — the ID composition root. `cw` → `CwId(wpm/tone from
    loaders)`; `voice` → `VoiceId(tts or PiperTts(load_tts_voice(env)))`. Voice mode with no
    voice **raises** (no CW fallback). The `tts` injection lets tests pick voice on `StubTts`.
- `radio_server/services/__init__.py` re-exports `VoiceId`, `spell_callsign`, `PHONETIC`,
  `RADIO_ID_MODE_ENV_VAR`, `DEFAULT_ID_MODE`, `ID_MODES`, `load_id_mode`, `build_id_encoder`.
  No new deps (voice mode reaches piper only via the cycle-8 optional `tts` extra).
- `tests/test_voice_id.py` (17 new) — phonetic map (spell, upper-case, slash, unknown→raise);
  `VoiceId` on `StubTts` byte-exact + canonical + protocol; `RADIO_ID_MODE` selection (default
  cw, reads voice, case-insensitive, unknown→raise); `build_id_encoder` cw/voice + voice-
  without-voice fail-loud-no-fallback; end-to-end authed `"1"` → voice-ID + time in `tx_log`
  (exact); 1 `skipif`-gated real-piper test (property-asserted). `uv run pytest` →
  **169 passed, 4 skipped**. See ADR 0010.
- **Deferred (next):** the FastAPI API layer, the V71-only scan engine, and the two real
  hardware backends. The audio-content tower is done.

### Real piper TTS (cycle 8)

- `radio_server/services/tts.py` (modified) — `PiperTts` added beside the **unchanged**
  `TtsEngine` protocol and `StubTts`:
  - `__init__(voice_path, *, config_path=None)` — default sidecar `<voice>.onnx.json` (piper
    convention, marked verify-against-build). Validates the `.onnx` + sidecar exist and reads
    `audio.sample_rate` into `self._rate`, all fail-loud, **without importing piper**.
  - `render(text) -> AudioFrame` — `to_canonical(AudioFrame(raw, AudioFormat(self._rate,
    2, 1)))`. Canonical 48k out regardless of the voice's native rate.
  - `_synthesize_raw(text)` — the **only** piper-touching seam (lazy import, marked
    VERIFY-AGAINST-INSTALLED-BUILD; missing piper/onnxruntime → fail-loud RuntimeError). A
    test subclass overrides it to drive `render` with a synthetic buffer, no model needed.
  - `load_tts_voice(env)` / `RADIO_TTS_VOICE_ENV_VAR` — fail-loud, **no default** (modeled on
    `load_totp_secret`).
- `radio_server/services/__init__.py` re-exports `PiperTts`, `load_tts_voice`,
  `RADIO_TTS_VOICE_ENV_VAR`. `pyproject.toml` gains an optional `tts` extra
  (`piper-tts`, `onnxruntime`) — declared, not core; piper unpinned (guardrail 1).
- `tests/test_tts.py` — the 5 existing StubTts baseline tests kept; +9 model-free PiperTts
  tests (config fail-loud ×4, rate read from sidecar, non-22050→48k and 22050→48k resample
  edge, protocol conformance) + 2 `skipif`-gated real-engine tests (canonical/nonzero/
  plausible-duration speech; wired into the time service → one canonical over with the CW ID
  prepended, structure asserted). `uv run pytest` → **152 passed, 3 skipped**. No new core
  deps. See ADR 0009.
- **Deferred (next):** `VoiceId` — a second `IdEncoder` speaking the callsign through this
  engine, with the phonetic/"niner" spelling map and `StationId` CW-vs-voice encoder
  selection. ID stays CW this cycle.

### DTMF decode + framing (cycle 7)

- `radio_server/audio/dtmf.py` (new) — two deliberately-distinct concerns plus fixtures:
  - **Decode:** `DtmfDecoder` (one-method `runtime_checkable` protocol, `decode(frame) -> str`,
    mirrors `IdEncoder`) and `MultimonDtmfDecoder` — `to_multimon(frame)` (ADR 0006 anti-alias
    edge) → pipe raw PCM to `multimon-ng` on stdin → parse `DTMF: <key>` lines. Missing binary
    fails loud with an install hint. `MULTIMON_ARGS`/`MULTIMON_RATE`/`RADIO_MULTIMON_BIN` are
    marked verify-against-build (guardrail 1).
  - **Framing:** `DtmfFramer` (pure, clock-injected). `feed(digit, now) -> str | None`: `#`
    emits the buffered run as one entry (empty buffer → nothing), `*` clears, any other key
    appends; inter-digit timeout discards a stalled partial (lazy on `feed`; `tick(now)` for a
    future real loop). Local `Clock` alias — the module imports no auth code.
  - **`DtmfInput`** composes decoder+framer: `pump(frame) -> list[str]` of completed entries.
    Auth-free; the caller feeds entries to `on_dtmf`.
  - **Fixtures:** `synth_dtmf(digit, …)` sums two `synth_tone` frames at `DTMF_FREQS` (standard
    697–1633 Hz pairs), `_mix` sums int16 as int32 + clips. Deterministic, FFT-assertable, no
    external assets. Unknown key fails loud.
  - **Config:** `load_dtmf_timeout` (`RADIO_DTMF_TIMEOUT`, default 3.0s, fail-loud on bad set
    value) and `load_multimon_bin` (`RADIO_MULTIMON_BIN`, default `multimon-ng`).
- `radio_server/audio/__init__.py` re-exports the new surface.
- `tests/test_dtmf.py` (13 new) — synth-fixture FFT (both tones present)/format/determinism/
  fail-loud; `skipif`-gated real multimon decode; framing (full run frames one entry, `*`
  clears, timeout discards partial via `FakeClock`, lone `#` no-op, `tick`); and **the**
  end-to-end (fake decoder → framed TOTP → `ACCEPTED` → authed `"1"` → CW-ID'd time in
  `tx_log`). `uv run pytest` → **143 passed, 1 skipped**. No new deps. See ADR 0008.
- **Deferred (empirical/next):** real recorded-WAV fixtures; a controller/API loop that pumps
  `radio.receive()` and calls `on_dtmf`; weak-signal/HT-flutter robustness + exact multimon
  flags (hardware bring-up); `VoiceId`.

### Real CW station ID (cycle 6)

- `radio_server/services/cw.py` (new) — `CwId` implements `IdEncoder`
  (`encode(callsign, format=CANONICAL_FORMAT) -> AudioFrame`). Built lowest-to-highest so the
  timing is pure: `MORSE` table (A–Z, 0–9, `/`); `unit_ms(wpm) = 1200/wpm`;
  `cw_timeline(text, wpm)` → ordered `(on, duration_ms)` segments using PARIS units
  (dit 1 / dah 3 / intra-char 1 / inter-char 3 / inter-word 7), **no leading/trailing gap**;
  `_silence` builds canonical-zero gap frames. `encode` keys `synth_tone` for each on-segment
  (its raised-cosine ramp kills per-element clicks) and concatenates via `AudioFrame.__add__`.
- **Encoder signature note:** the protocol is one-arg (`encode(callsign)`) and `StationId`
  calls it that way; the cycle-6 `encode(callsign, format)` shape is honored by an **optional**
  `format` param defaulting to canonical, so nothing above the seam changes and
  `isinstance(CwId(), IdEncoder)` still holds.
- Config: `load_cw_wpm`/`load_cw_tone_hz` follow the `load_id_interval` pattern —
  `RADIO_CW_WPM` (default 20) / `RADIO_CW_TONE_HZ` (default 600), marked defaults that still
  **fail loud** on a set non-numeric/non-positive value. WPM/tone injected into `CwId` at
  construction. Guardrail 1: safe operator prefs, not confirmed hardware facts.
- Swap point: `StubId()` → `CwId(...)` at the (still-to-be-written) composition root; nothing
  else changes.
- Tests: `tests/test_cw.py` (21 new) — PARIS `unit_ms`, exact `cw_timeline("AE9S", …)`
  dit/dah/gap sequence, total-duration = timing math, per-segment tone-energy/exact-zero-gap
  render check, sidetone FFT, unknown-char raises, canonical + concat, config loaders, and
  end-to-end via `StationId`/auth gate (authed `"1"` prepends real CW, no within-interval
  repeat — cycle-4 scheduler behavior unchanged). No new deps. See ADR 0007.

### Audio format + resample + tone (cycle 5)

- `radio_server/audio/` (new lowest layer). `format.py` — `AudioFormat(rate,width,channels)`
  and the frozen, format-carrying `AudioFrame(samples, format=CANONICAL_FORMAT)`; `__add__`
  and `MockRadio.transmit` raise `AudioFormatMismatch` on a format mismatch. Canonical =
  `AudioFormat(48000, 2, 1)`. The guard is **format identity, not PCM-length divisibility**,
  so the symbolic stubs (`b"<id:AE9S>"`) stay valid frames and `tx_log` stays assertable.
- `audio/resample.py` — `resample(frame, target_rate)` over `soxr` VHQ (anti-aliased),
  plus `to_multimon` / `to_canonical`. `MULTIMON_RATE = 22050` is a **verify-on-hardware**
  marked default (guardrail 1). Mono 16-bit only for now (raises otherwise).
- `audio/tone.py` — `synth_tone(freq_hz, duration_ms, format=CANONICAL_FORMAT, *,
  amplitude=0.5, ramp_ms=5.0)`: real sine PCM with a raised-cosine on/off envelope (no key
  clicks). Deterministic. This is the substrate CW ID (cycle 6) gates on/off.
- `AudioFrame` moved from `backends/base.py` to `audio/format.py`; `backends` re-exports it,
  so `from ..backends import AudioFrame` still works everywhere. `MockRadio` gained a
  `format` and a transmit guard; `StubTts`/`StubId` now wrap their symbolic payload in a
  canonical frame. New deps: `numpy`, `soxr` (first runtime deps beyond `pyotp`; wheels only).
- Tests: `test_audio_format.py`, `test_resample.py` (in-band survives + no aliasing into the
  DTMF band), `test_tone.py`; existing suites updated for the new frame type. `uv run pytest`
  → **110 total, all green**. See ADR 0006.

### Station ID scheduler (cycle 4)

- `radio_server/services/station_id.py` — `StationId(radio, encoder, callsign, *,
  interval=600, clock)` is the sole `radio.transmit` seam. `transmit(audio)` prepends the ID
  into the same over when *due* (due = first over of the session, i.e. `last_id is None`, OR
  `now - last_id >= interval`); within-interval overs do not repeat it. `check(now)` forces
  an ID-only over when the session is overdue (safety net for a real scheduler task).
  `sign_off(now)` sends a closing ID iff the station transmitted, then resets.
  `begin_session(now)` resets per-session state (for the inactivity-timeout path). The timer
  is measured from `last_id`, not the last transmission — the Part 97 invariant is "≤10 min
  since the last ID."
- Config mirrors the auth pattern: `load_callsign()` reads `RADIO_CALLSIGN` and **fails loud
  (no default)** — a station cannot legally transmit without a callsign (Kris sets `AE9S`).
  `load_id_interval()` reads `RADIO_ID_INTERVAL` (default 600) and **rejects** any value
  > 600 (legal max 10 min), non-numeric, or non-positive.
- `IdEncoder` protocol (`encode(callsign) -> AudioFrame`) + `StubId` (deterministic
  `b"<id:AE9S>"`, so `tx_log` is assertable). Real `CwId`/`VoiceId` are later cycles.
- `radio_server/services/dispatch.py` — `Dispatcher` now holds a `StationId` (`transmitter`)
  instead of a raw `Radio`, so no service transmission can bypass ID by construction.
- `tests/test_station_id.py` (23 new tests) + updated `tests/test_dispatch.py` (first over
  now asserts the ID prefix). `uv run pytest` → **88 total, all green**. No new deps.

### Dispatch + services (cycle 3)

- `radio_server/services/dispatch.py` — `Service = Callable[[Session, ServiceContext],
  AudioFrame]` (handlers *produce* audio, no radio I/O). `ServiceContext(clock, tts)` is
  radio-free. `ServiceRegistry` maps digit → `(name, Service)`. `Dispatcher(radio, ctx,
  registry)` is *callable* matching the auth layer's `Dispatch` contract, so it drops into
  `AuthGate(verifier, ..., dispatch=dispatcher)`; it owns the radio and is the single
  `transmit` seam. Returns `DispatchResult(digits, service, transmitted)` (unknown digit →
  `transmitted=False`, nothing sent — graceful, `Outcome.kind` stays `COMMAND`).
- `radio_server/services/tts.py` — `TtsEngine` protocol (`render(text) -> AudioFrame`) +
  `StubTts` (deterministic `b"<audio:...>"`, so `tx_log` is assertable). Piper is later.
- `radio_server/services/time_service.py` — `format_spoken_time(now, tz)` (pure, 24-hour
  local, isolated from dispatch); `load_timezone()` reads `RADIO_TZ` (IANA name) with a
  marked `UTC` default (bad zone → raises); `time_service(tz)`/`register(registry, tz)`
  bind digit `"1"`. Reads the SAME injected clock as the session timeout.
- `radio_server/services/__init__.py` — public surface re-exports.
- `tests/test_tts.py`, `tests/test_time_service.py`, `tests/test_dispatch.py` — 16 new
  tests (incl. full enroll→auth→`"1"`→exact `tx_log` on a fake clock). `uv run pytest` →
  65 total, all green. No new dependencies (stdlib `zoneinfo`/`datetime`).

### Auth layer (cycle 2)

- `radio_server/auth/totp.py` — `TotpVerifier`. `verify_and_burn(code, now=None)`:
  ±1-step windowed (== pyotp `valid_window=1`), constant-time compare, **single-use**
  (burns each consumed `(code, time_step)`; a replay inside the window is refused).
  Burn set is pruned each call so it stays bounded. `provisioning_uri()` emits the
  `otpauth://` enrollment URI. `load_totp_secret()` reads `RADIO_TOTP_SECRET` (env,
  never hardcoded) and raises if unset. `Clock = Callable[[], float]` alias, injectable.
- `radio_server/auth/session.py` — two-state machine (`SessionState`:
  UNAUTHENTICATED ⇄ AUTHENTICATED). `AuthGate.on_dtmf(digits, session, now=None)` is
  the single entry point → `Outcome(kind, detail)` where `OutcomeKind` ∈
  {ACCEPTED, REJECTED, COMMAND}. Inactivity `timeout` (injectable) drops the session.
  Unauth → TOTP verify; authed → injected `dispatch` hook (stubbed; cycle 3).
- `radio_server/auth/__init__.py` — public surface re-exports.
- `tests/conftest.py` — `FakeClock`, shared `TEST_SECRET`/`verifier`/`code_for`.
- `tests/test_totp.py`, `tests/test_session.py` — 22 new tests. `uv run pytest` → 49
  total, all green.
- ADR 0003 records the state machine, single-use burn strategy, and clock injection.
- `pyproject.toml` now depends on `pyotp>=2.9` (see `uv.lock`).

## Next up

The **entire software tower is now built and runs live end-to-end on the mock.** What remains needs
the box (or is optional polish):

- **Real hardware backends** (`SignaLinkV71`, `AiocBaofeng`) — the last thing that needs hardware,
  and the "plug it in, it keys up clean" empirical bring-up phase. This is where the marked
  verify-on-hardware facts get confirmed: the Hamlib rig model + serial speed (V71 CAT), the AIOC's
  PTT line (RTS vs DTR), multimon-ng's exact input rate/flags, the piper voice, and the controller's
  real `receive()` cadence / audio chunk size / loop timing (guardrail 1). PTT stays off the DATA
  port / AIOC serial line, never CAT `TX` (guardrail 2).
- **Live scan through the controller (optional).** `Controller` ticks an *attached* `ScanEngine`, but
  nothing starts one over the API yet; the synchronous `/scan` sweep still stands. A later cycle could
  add start/stop-scan control that installs a live engine on the running controller and streams
  carrier/timed dwell over wall-clock.
- **`build_app` production wiring / the real entrypoint.** `build_app` wires the controller only when
  `RADIO_TOTP_SECRET` is set, and full wiring needs real multimon + a piper voice — that comes online
  with the hardware phase. No `uvicorn` entrypoint binds a server yet.
- **More services / auth strength per service (guardrail 4).** The time announce is read-only; guard
  anything that keys TX for real harder. `ServiceContext` is the place to thread per-service
  authority if needed.
- **Runtime hardening for the async driver.** On hardware, `receive()` blocks — run it in a thread
  executor rather than directly in the event loop; and the single-use TOTP `consumed` set is
  per-process in-memory (noted in ADR 0003).

## Open questions / blocked

(none)

## Notes for the cycle runner

- Single-use `consumed` state is in-memory per process; a restart mid-window or a
  multi-process deployment would need it shared/persisted. Out of scope now; noted in
  ADR 0003.
- There is no GitHub instruction issue in this repo — cycles have arrived via the
  prompt. The CLAUDE.md "comment PR URL / swap label on the issue" close step has no
  issue to act on; PRs are still opened for human merge as required.
