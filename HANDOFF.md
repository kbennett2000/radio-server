# Handoff

## Current state

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
**core deps** (the API is the product's stated purpose), so the tests **run**, not skip. `uv run
pytest` → **149 total, all green** (131 + 18 API tests, 0 skips). This cycle is **independent of the
DTMF/piper/voice-ID stack (#7–#9, in review)** — it imports only `backends` + the new `api` package
and touches no `services/` file, so it composes with that stack additively when it merges.

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
  auth rejects missing/bad and accepts good; `load_api_token({})` raises. `uv run pytest` → **149
  total, all green**. New deps: `fastapi`, `uvicorn` (runtime), `httpx` (dev). See ADR 0011.

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

- **V71-only scan engine** (the headline post-API cycle). `CatRadio.scan(on)` exists on the
  backend; build the scan control/progress logic on top, fully mock-testable against
  `MockRadio`'s fake busy/status (drive `busy`/frequency and assert scan advances/stops). It is
  CAT-only, so gate it behind `Capability.SCAN` at the API exactly like the other CAT endpoints,
  and **publish `scan` progress events on the WebSocket `EventHub` established in cycle 10** (the
  `Event.type` field was left open for this). Baofeng mode simply does not expose it.
- **Session-lifecycle & scheduler wiring for ID over the API.** `StationId.begin_session()` /
  `check()` / `sign_off()` exist and are unit-tested but are not yet called from real events: the
  API/controller should call `begin_session` on `ACCEPTED`, run `check` on a periodic task
  (≤ interval), and `sign_off` on session close/inactivity — and can now surface those as
  `session` events on the WS stream (type reserved in cycle 10).
- **In review (parallel #7–#9 stack, land before/after this API PR):** DTMF decode
  (`multimon-ng -a DTMF` over `receive()` → `AuthGate.on_dtmf`), real piper TTS, and the
  `VoiceId` phonetic encoder with `RADIO_ID_MODE`. Independent of the API code; only `HANDOFF.md`
  overlaps (trivial prose merge).
- **Real hardware backends** (`SignaLinkV71`, `AiocBaofeng`) — the "plug it in, it keys up clean"
  empirical bring-up phase.
- **More services / auth strength per service (guardrail 4).** The time announce is read-only;
  guard anything that keys TX for real harder. `ServiceContext` is the place to thread per-service
  authority if needed.

## Open questions / blocked

(none)

## Notes for the cycle runner

- Single-use `consumed` state is in-memory per process; a restart mid-window or a
  multi-process deployment would need it shared/persisted. Out of scope now; noted in
  ADR 0003.
- There is no GitHub instruction issue in this repo — cycles have arrived via the
  prompt. The CLAUDE.md "comment PR URL / swap label on the issue" close step has no
  issue to act on; PRs are still opened for human merge as required.
