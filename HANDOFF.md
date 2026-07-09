# Handoff

## Current state

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

- **`VoiceId`** (headline) — a second `IdEncoder` that speaks the callsign through the
  cycle-8 `PiperTts` engine, with a phonetic/"niner" spelling map (so "AE9S" is spoken
  "Alpha Echo Niner Sierra") and `StationId` encoder-selection (CW vs voice ID). `PiperTts`
  already produces canonical frames via `to_canonical`, so this is additive at the encoder
  seam — nothing above `StationId` changes.
- **Session-lifecycle & scheduler wiring for ID.** `StationId.begin_session()` /
  `check()` / `sign_off()` exist and are unit-tested but are not yet called from real
  events: a controller/API cycle should call `begin_session` on `ACCEPTED`, run `check`
  on a periodic task (≤ interval), and `sign_off` on session close/inactivity.
- **Controller/API loop to drive the decode seam.** Cycle 7 shipped `DtmfDecoder`/
  `DtmfFramer`/`DtmfInput` and proved them end-to-end with a fake decoder, but nothing yet
  pumps a live `MockRadio.receive()` on a loop into `DtmfInput.pump` → `on_dtmf`, nor wires
  the ID lifecycle (`begin_session`/`check`/`sign_off`) to real events. That loop (or the
  FastAPI layer) is the natural next connective cycle. Confirm the installed multimon-ng's
  actual input rate/flags on hardware (guardrail 1).
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
