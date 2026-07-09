# Handoff

## Current state

Cycle 4 complete: automatic station ID (guardrail 5, Part 97). The transmit path is now
**legality-clean** — every service transmission carries the station ID, there is a
forced-periodic ID timer, and a sign-off ID at session end. `StationId` is the single seam
through which all audio reaches the radio, so no transmission can go out un-ID'd. ID audio
is a deterministic stub (scheduling logic only — real CW/voice synthesis is deferred to
after the audio-format ADR). See ADR 0005. **Still blocked on the audio-format ADR + real
encoders before any hardware.**

Cycle 3 (merged, PR #3): command dispatch + the first voice service (announce-the-time),
the first thing the server transmits. Authenticated digit `"1"` → time announcement
rendered through a stub TTS → `MockRadio.tx_log`. Still fully mock/hardware-free;
unit-tested with the injected fake clock. See ADR 0004.

Cycle 2 (merged, PR #2): a DTMF-gated TOTP auth layer + session state machine, fed digit
strings directly (no audio/DTMF decode yet), unit-tested with an injected fake clock.
See ADR 0003.

Cycle 1 (merged, PR #1): the `Radio` protocol surface + full `MockRadio`, hardware
backends stubbed and wired into a factory. See ADR 0002.

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

- **Audio-format ADR** before any real audio I/O: pin rate/width/channels/endianness.
  Audio is still opaque `bytes` (`AudioFrame`); `StubTts` and `StubId` output are
  placeholder shapes. **This + real encoders are the remaining gate on hardware.**
- **Real ID/TTS encoders** (`CwId`/`VoiceId`, piper TTS) implementing the existing
  one-method `IdEncoder`/`TtsEngine` protocols — land after the audio-format ADR.
- **Session-lifecycle & scheduler wiring for ID.** `StationId.begin_session()` /
  `check()` / `sign_off()` exist and are unit-tested but are not yet called from real
  events: a controller/API cycle should call `begin_session` on `ACCEPTED`, run `check`
  on a periodic task (≤ interval), and `sign_off` on session close/inactivity.
- **DTMF decode** (`multimon-ng -a DTMF`) over `MockRadio.receive()` → digit strings
  that feed `AuthGate.on_dtmf`. This is the piece that connects audio to auth.
- **More services / auth strength per service (guardrail 4).** The time announce is
  read-only; guard anything that keys TX for real harder. `ServiceContext` is the place
  to thread per-service authority if needed.

## Open questions / blocked

(none)

## Notes for the cycle runner

- Single-use `consumed` state is in-memory per process; a restart mid-window or a
  multi-process deployment would need it shared/persisted. Out of scope now; noted in
  ADR 0003.
- There is no GitHub instruction issue in this repo — cycles have arrived via the
  prompt. The CLAUDE.md "comment PR URL / swap label on the issue" close step has no
  issue to act on; PRs are still opened for human merge as required.
