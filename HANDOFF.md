# Handoff

## Current state

Cycle 40: **pluggable voice-service architecture** (ADR 0034). Formalized the existing
`ServiceRegistry`/`Service`/`ServiceContext` seam into a `ServicePlugin` contract and retrofitted all
six services (time/weather/astro/quote/battery/bible) onto it — **behavior-preserving** (every
per-service formatter/factory test unchanged; the settings canary stays 47). New
`radio_server/services/plugin.py`: `ServicePlugin` Protocol (`id`, `description`, `enabled(settings)`,
`build(ctx) -> Service`), `PluginBuildContext` (carries `Settings` + a **lazily-built, memoized shared
`Fetcher`** — reproduces ADR 0033's "one fetcher on first enabled fetch service"), the in-tree
`PLUGINS` tuple, `DEFAULT_BINDINGS`, `RESERVED_DIGITS` (`{"4","99"}`), `resolve_bindings` (fails loud on
reserved/unknown/non-DTMF), and `build_registry`. Each `*_service.py` gained a small `PLUGIN` singleton
wrapping its **unchanged** factory; the `register()` free functions were **removed** (5 helper tests +
engine updated to register via the factory / plugins). **Operator-assigned digits:** a new `[services]`
TOML table maps digit→service id — a **separate config channel** (like secrets; arbitrary digit keys
don't fit the `SettingSpec` schema). `config/settings.py` peels `[services]` off before schema
resolution (`_flatten`) and reads it via new `load_service_bindings`; `save_settings` leaves the table
intact (only rewrites schema keys); `render_example` emits a documented `[services]` block
(`radio.toml.example` regenerated). `build_controller` gained `service_bindings=None` (defaults to
`DEFAULT_BINDINGS`) and **replaced the imperative registration block** with
`build_registry(PLUGINS, resolve_bindings(...), PluginBuildContext(settings, fetcher))`; `build_app`
loads bindings via `load_service_bindings(config_path)`. New tests: `test_service_plugin.py`,
`test_service_bindings.py`; `test_services_catalog.py` gained remap/reserved/unknown cases.
`uv run pytest` → **582 passed, 3 skipped**. Verified end-to-end through the real composition root: a
remapped keypad (time→8#, weather→9#) transmits correctly; an unbound digit is a graceful miss; `4`/`99`
stay controller built-ins. **Adding an in-tree service** is now: write the module + plugin, append to
`PLUGINS`, add its default digit + scalar settings — `build_controller` is untouched. Scope is in-tree
(no pip/entry-point discovery — a Part-97/guardrail-4 trust decision left for later behind an explicit
opt-in). Branch `cycle-40-pluggable-voice-services` from freshly-pulled `origin/master` (`08143f2`), PR
against `master`.

Cycle 34: **weather (2#) + astronomy (3#) DTMF voice services** reading a LAN weather station, plus a
`/services` catalog. New **HTTP fetch seam** `radio_server/services/fetch.py`: a `Fetcher` protocol
(`fetch_json(url) -> Mapping`, mirrors `TtsEngine`), a real `UrllibFetcher(timeout)` over stdlib urllib
(**no new dependency**; the single network-dependent path, wraps every failure as `FetchError`), and a
`StubFetcher` (canned JSON) for tests. Two services mirroring `time_service`
(`radio_server/services/weather_service.py` `2#`, `astro_service.py` `3#`): pure formatters
`format_spoken_weather` (`sensors.outdoor.derived.{temperature_f, feels_like_f,
absolute_humidity_g_m3}` → *"Outdoor temperature 78 degrees. Feels like 78. Absolute humidity 8.1 grams
per cubic meter."*) and `format_spoken_astro` (`astronomy.sun.{sunrise,sunset}` +
`astronomy.moon.{phase_name,moonrise,moonset}`, ISO→local 12-hour, null moon → "not available" →
*"Sunrise 5:43 AM, sunset 8:26 PM. Moon phase New Moon. Moonrise 7:03 AM, moonset 9:10 PM."*). Each
service factory (`weather_service(base_url, fetcher)`) binds URL+fetcher at construction and catches
`FetchError`/`KeyError` → speaks an "unavailable" line (a dead station never crashes the loop; the GET
runs in the controller loop so `weather.timeout` defaults to a short **3 s**). **Config:** new `weather`
group — `weather.base_url` (`RADIO_WEATHER_URL`, optional, default `""`) and `weather.timeout` (default
3.0); settings-API canary 37→39; `radio.toml.example` regenerated; `save.py` banner. **Registration:**
`build_controller` gains an injectable `fetcher=None` and registers weather+astro **only when
`weather.base_url` is set** (else the digits are graceful misses). **`ServiceRegistry.register` gained a
`description`; `catalog() -> [{digit,name,description}]`** surfaced on `Controller.service_catalog` and
a new **`GET /services`** endpoint (token-gated; `[]` when no controller) — drives the web UI panel
(PR B) + the README table. **`uv run pytest` → 503 passed, 3 skipped** (+ test_fetch, test_weather_service,
test_astro_service, test_services_catalog). **Verified live against the real station** (192.168.1.62):
both formatters + the real `UrllibFetcher` produce the natural-language lines above. Docs: README "DTMF
voice services" table. Also set the operator's local `radio.toml`: `[time] tz="America/Denver"` (1# now
speaks 24-hour local time, was UTC) + `[weather] base_url`. Cut from freshly-pulled `origin/master`
(cycle 33 / PR #35 merged, `63cdc6d`); branch `cycle-34-weather-astro-services`, PR against `master`.
**Follow-ups queued:** PR A2 — announce on successful auth + on session timeout/de-auth (controller
voice); PR B — web UI hide-unsupported-controls + services panel; PR C — RX activity in the event log.

Cycle 33: **single capture reader — one `receive()` feeds both the browser and the DTMF controller**
(ADR 0031). Root-causes why over-RF DTMF login did **nothing** on the bench even after cycle 31: (1)
`ControllerRunner` read one ~20 ms AIOC block then slept `controller.poll` (**0.5 s**), sampling ~4% of
the audio into non-contiguous slivers that multimon can never lock; (2) `RxPump` (browser Listen) and
`ControllerRunner` were **two independent `receive()` loops on one single-open capture**, stealing each
other's blocks — Listen made it strictly worse. Both files had literally deferred "one `receive()`
feeding both `controller.step` and this pump" as a hardware decision. **Fix:** `RxPump` is now the
single reader — it reads back-to-back and, when a `controller` is set, calls `controller.step(now,
frame)` on the **raw** frame FIRST (guarded), then the gate→hub→recorder path (`radio_server/rx/pump.py`).
`build_app` no longer creates a `ControllerRunner` (class kept, retired from the live path);
`build_controller` still builds the controller. Lifecycle is **reference-counted demand** in
`create_app`: the reader runs while a `/audio/rx` listener is connected OR the controller is active —
`POST /controller {on}` and `/audio/rx` connect/disconnect each `_acquire_rx`/`_release_rx`
(`radio_server/api/app.py`); `_controller_state.running` now reports `controller_active`. `controller.poll`
is vestigial for DTMF. **Why the cycle-31 test missed it (operator's point — this WAS mockable):** it
fed a `FakeDtmfDecoder` returning whole pre-formed entries, never exercising `receive()` cadence /
real accumulation / real multimon / contention. **New `tests/test_controller_rx_e2e.py`** is the test
that would have caught it: a TOTP code rendered as **real `synth_dtmf` sliced into 20 ms blocks**
(0.5 s tone + 0.5 s silence per key) decoded by **REAL multimon** through the real `BufferedDtmfInput`
→ `session.authenticated` (fails on the old design); plus a proof that ONE `RxPump` feeds both a
`controller` and a hub subscriber from one `receive()`. **`uv run pytest` → 483 passed, 3 skipped.**
**Verified live:** the fixed server starts against the real AIOC, `POST /controller {on:true}` →
`running:true`, the reader pumps the card continuously with no errors (browser `/events` connected).
Docs: ADR 0031, `docs/hardware-bringup.md` DTMF note updated. **The last inch — a human keying a
DTMF code over RF — cannot be automated (no self-loopback on a half-duplex radio); the live decode path
is now byte-identical to `doctor --dtmf`, which already decodes real keyed tones on this hardware.**
Cut from freshly-pulled `origin/master` (cycle 32 / PR #34 merged, `0e62dfc`); branch
`cycle-33-single-rx-reader`, PR against `master`.

Cycle 32: **TOTP enroll CLI for Google Authenticator** (`python -m radio_server.enroll`) — the
companion to cycle 31: now that the live controller decodes over-RF DTMF, the operator needs an easy
way to get the TOTP secret onto their phone. Before this there was no CLI — minting meant the
authenticated REST endpoint `POST /settings/secrets/totp/enroll` or hand-running `pyotp.random_base32()`.
New **`radio_server/enroll.py`**: `enroll(secrets_path, account, *, force, out, env)` mints a fresh
secret via `rotate(path, "totp_secret")` (writes `radio-secrets.toml` `0600`), builds the `otpauth://`
URI via `TotpVerifier.provisioning_uri`, **always prints the base32 secret + URI**, and **renders a
scannable terminal QR** when the optional **`qrcode`** package is importable (soft import; falls back
to a "install the hardware extra" hint + the URI otherwise). Re-enrolling mints a NEW secret and
invalidates the phone's current one, so an existing secret is **refused without `--force`**. `env`
defaults to `os.environ` (respects an ambient `RADIO_TOTP_SECRET`); tests pass `env={}` to isolate.
`main([...])` wires argparse (`--secrets`/`--account`/`--force`). Nothing transmits or touches the
radio. **`qrcode>=7` added to the `hardware` optional extra** (kept optional — the CLI degrades
gracefully; consistent with ADR 0003's no-required-image-dep stance). Docs: `docs/hardware-bringup.md`
gained an "Enrolling Google Authenticator (DTMF login)" section (run enroll → scan QR → set callsign +
voice → restart → key `<code>#` then `1#`), README secrets section points to it. **`uv run pytest` →
480 passed, 4 skipped** (+5 `tests/test_enroll.py`: mints+persists a base32 secret at `0600` with the
URI+account shown, refuses-without-force, `--force` replaces, qrcode-absent URI fallback, qrcode-present
QR render via `importorskip`, `main` writes the named file; the 4th skip is the qrcode QR test when the
dep is absent). **Verified live:** `uv run --with qrcode python -m radio_server.enroll` mints, writes
`0600`, and renders a clean scannable QR + secret + URI. Cut from freshly-pulled `origin/master` **after
PR #33 (cycle 31) merged** (`cd788b5`); branch `cycle-32-totp-enroll-cli`, PR against `master`. NOT
stacked on cycle 31 — it was rebased onto the merged master. **Deferred:** QR is best-effort terminal
rendering (invert=True for dark terminals; the URI/secret always print as the reliable fallback).

Cycle 31: **buffer DTMF audio in the live controller** (ADR 0030) — closes the cycle-30 flagged
limitation so **over-RF TOTP auth actually decodes**. Root cause: `Controller.step` decoded one
`receive()` frame at a time (~20 ms on the AIOC), far too short for multimon to lock a tone (~40–200
ms), so keyed codes never decoded on the live server even with a secret + callsign configured. The
fix promotes the accumulate-and-dedup logic the operator already bench-proved in `doctor --dtmf` into
a shared **`BufferedDtmfInput`** (`radio_server/audio/dtmf.py`, same `pump(frame, now) -> list[str]`
surface as `DtmfInput`): it buffers frame bytes until a **`dtmf.buffer_seconds` window** (default 0.5
s, `dtmf_window_bytes`) then decodes the chunk, **de-dups held tones** (consecutive identical digits
collapsed; a **silent window resets** the run, so a genuinely-repeated key needs a brief pause),
feeds the framer, and returns completed entries; `flush(now)` drains the tail; an optional `on_digit`
hook surfaces each key for live display. **`doctor.py`'s `collect_dtmf` is refactored onto the same
core** (behavior identical — the existing collect_dtmf tests, incl. the real-multimon round-trip,
pass unchanged), so the tool and the live controller share ONE decode path. `build_controller` now
wires `BufferedDtmfInput` (window from `load_dtmf_buffer_seconds`); `Controller.step`'s
`self._dtmf.pump(...)` call is unchanged, and the station-ID/idle checks still tick every poll (only
the *decode* buffers). **`dedup` is a `build_controller` test seam** (default True for production):
the existing controller tests feed a `FakeDtmfDecoder` that returns whole pre-formed entries per
call, which would fold a code's repeated digits, so `build_ctrl` (and the event-log-wiring test) pass
`dedup=False` + a tiny `dtmf.buffer_seconds=0.02` window to keep the per-over cadence. New config
`dtmf.buffer_seconds` (spec.py, `RADIO_DTMF_BUFFER_SECONDS`, positive-float, verify-on-hardware) →
settings-API canary 36→37, `radio.toml.example` regenerated. **`uv run pytest` → 475 passed, 3
skipped** (+ new `tests/test_buffered_dtmf.py`: accumulate-until-window, cross-window framing,
held-tone dedup + silent reset, dedup-off, flush tail, real-multimon round-trip, window-bytes math;
+ `test_controller.py::test_login_accumulates_from_short_frames_over_the_buffered_loop` proving a
code arriving in ~20 ms frames authenticates via the buffered loop with dedup on). Docs:
`docs/hardware-bringup.md` "Testing DTMF decode" note rewritten (over-RF auth now decodes; pause
between repeated code digits; `dtmf.buffer_seconds` knob), ADR 0030. Cut from freshly-pulled
`origin/master` (cycle 30 / PR #32 merged, `e1e11ab`); branch `cycle-31-controller-dtmf-buffering`,
PR against `master`. **FOLLOW-UP (separate branch, not stacked):** PR B — a `python -m
radio_server.enroll` CLI to mint the TOTP secret + print the `otpauth://` URI + a terminal QR (soft
`qrcode` dep) so the operator can load Google Authenticator. **Deferred (noted in ADR 0030):** a
persistent streaming multimon process (more robust to tones split across a window boundary; the
fixed-window accumulator was chosen for simplicity and is already bench-proven — a boundary split
fails *safe*: a corrupted digit just rejects the code, never a false accept).

Cycle 30: **DTMF decode test tool** (`doctor --dtmf`) — the operator wanted to test DTMF decode on
the AIOC. Findings: `multimon-ng` (the decoder the server shells out to) wasn't installed (now is,
1.3.1), and there was no way to watch DTMF decode from the radio — the live DTMF path is gated on a
TOTP secret AND `controller.step()` decodes **one ~20 ms `receive()` frame at a time**, far too short
for multimon to lock onto a tone (needs ~40–200 ms). New **`radio_server/doctor.py --dtmf`** (read-
only, no keying): builds the AIOC backend and runs **`collect_dtmf(radio, decoder, framer, *, seconds,
chunk_bytes, clock, on_event)`** — a pure helper that **accumulates received audio into ~0.5 s chunks**
(`DEFAULT_DTMF_CHUNK_BYTES`) before each `MultimonDtmfDecoder.decode`, feeds digits to `DtmfFramer`
(`#` submits, `*` clears), and prints each digit + completed entry live. Reuses the existing
decoder/framer + the `--rx-level` scaffolding; handles multimon-missing and capture-busy with clean
messages. **Verified live (no hardware needed):** `collect_dtmf` over a `MockRadio` serving
`synth_dtmf("123#")` through REAL multimon decodes entry `123`. **`uv run pytest` → 465 passed, 3
skipped** (+4: `collect_dtmf` accumulate/frame with a fake decoder, silence, and a multimon round-trip
gated on the binary; the pre-existing `test_dtmf` real-decode test now RUNS since multimon is
installed — 4→3 skips). Docs: `docs/hardware-bringup.md` gained a "Testing DTMF decode" section
(install multimon → pytest self-test → `--dtmf` from the radio). **FLAGGED FOLLOW-UP (next cycle,
own ADR):** the live controller's per-frame DTMF decode almost certainly won't decode real over-RF
tones — fix is to buffer received audio into ~0.3–0.5 s windows (or stream a persistent multimon
process) in the controller; `--dtmf` is the tool that confirms the need. Cut from `origin/master`
(cycle 29 merged, `407958a`); branch `cycle-30-dtmf-diagnostic`, PR against `master`.

Cycle 29 (cont.): **AIOC audio-level diagnostics** — added to the same PR #31 after bench testing
showed keying works but audio doesn't audibly flow (unverified levels, guardrail 1). Root causes
confirmed in code: RX "Listen" is silent because `audio.squelch=audio` gates on a software VAD
(`vad_on_rms=500`) and the AIOC's received level (which follows the UV-5R volume knob + the card's
ALSA capture level) sits under it; TX "Talk" transmits the **computer mic** (not the radio) and the
local monitor mutes while keyed. Deliverables: **`python -m radio_server.doctor --rx-level`**
(read-only — reads `receive()` for N s, reports RMS/peak in int16+dBFS vs the VAD thresholds and
recommends `vad_on/off` values or flags "no audio arriving"; pure `measure_rx_levels(radio, seconds,
clock)` reused-`frame_rms` helper, MockRadio-testable) and **`--tx-tone`** (RF, same dummy-load
CONFIRM guard as `--key-test` — one-shot `transmit(synth_tone(...))` into a dummy load to prove TX
audio without the browser mic). Also: `web/src/useTxAudio.js` now requests the mic with
`echoCancellation/noiseSuppression/autoGainControl:false` (raw mic for radio, not call-DSP);
`docs/hardware-bringup.md` gained an "Audio levels & squelch" bring-up flow (squelch=off → alsamixer
+ UV-5R volume → `--rx-level` → set VAD → squelch=audio → `--tx-tone`). **Verified live on the
bench:** `--rx-level` reads real audio and correctly reports it as arriving-but-gated (~112 RMS vs
threshold 500); `--tx-tone`/`--key-test` refuse non-interactively (RF safety). **`uv run pytest` →
461 passed, 4 skipped** (+7: new `tests/test_doctor.py` — level summary, silence, the classify
branches, RF-refusal). Web build clean (51 modules). **AIOC bring-up COMPLETE — full talk-through
confirmed on the bench:** operator raised `alsamixer` + UV-5R volume (received signal then measured
~5675 RMS avg / 25837 peak-block via `--rx-level`), set `audio.vad_on_rms=1000`/`vad_off_rms=500`
(squelch=audio); browser **Listen** gates on real audio, `--tx-tone` was heard on a second radio, and
**Talk** (computer mic → radio) works. Also added a graceful "capture busy — stop the server" message
to `--rx-level` (the AIOC sound card is single-open; the doctor and server can't share it). The
tuned VAD values live in the operator's gitignored `radio.toml`. **AIOC/Baofeng is production-ready.**

Cycle 29 complete: **AIOC/Baofeng hardware backend bring-up** (ADR 0029) — the real `AiocBaofeng`
is implemented; it was a `NotImplementedError` stub. The AIOC cable is physically plugged in and was
**empirically confirmed** (guardrail 1): USB `1209:7388`, PTT serial `/dev/ttyACM0` (stable by-id
`usb-AIOC_All-In-One-Cable_da3441ac-if04`, group `dialout`, operator in `dialout`), ALSA card
`hw:CARD=AllInOneCable` (48 kHz-native capture+playback). **The backend** (`radio_server/backends/
aioc_baofeng.py`) is a pure DI object (Settings-free, like `MockRadio`): `sounddevice` `RawInput/
OutputStream` for TX/RX (48 kHz, no resample — bytes straight through), `pyserial` control line for
PTT. **Keying model:** `transmit()` self-keys only when the line isn't already held — a one-shot clip
(station ID / service TTS / REST `/transmit`, each one `transmit(whole_clip)` call) asserts→plays→
drains→drops; a stream (`TxSession`: `ptt(True)`…N×`transmit`…`ptt(False)`) holds the line across
frames and `transmit()` only plays (state `_keyed`). **PTT line is configurable** (`baofeng.ptt_line`,
`rts`/`dtr` enum, **default RTS — marked verify-on-hardware**). **RF-safety:** the port opens with
both lines pre-set **low** (kills the pulse-on-open footgun), `close()`+`atexit` always drop the line
(never exit keyed), and playback is `stop()`-drained before the line drops (no clipped tail).
`capabilities()`=`SHARED_CAPS` only (API 501 on CAT — guardrail 3); `status().busy` always False (no
COS line — ADR 0015 → use `audio.squelch=audio`; `build_app` **rejects** `squelch=cat` for baofeng).
**Config:** new `[baofeng]` group in `config/spec.py` (serial_port/ptt_line/input_device/
output_device/blocksize) + `save.py` banner; `radio.toml.example` regenerated. **Deps:** new
`hardware` optional extra (`pyserial`,`sounddevice`), lazily imported (CI stays hardware-free);
`sounddevice` also needs system `libportaudio2`. **Composition root** (`api/app.py`) passes the
baofeng kwargs. **New `radio_server/doctor.py`** (`python -m radio_server.doctor`): read-only pass/
fail table (enumerate the AIOC card @48 kHz, serial opens without keying, dialout access) + a guarded
**`--key-test`** (the ONLY RF path — refuses non-interactive/CI, demands typed `CONFIRM`, asserts the
line ~2 s, asks which line keyed) for the empirical RTS-vs-DTR answer. **`uv run pytest` → 452 passed,
5 skipped** (+16): new `tests/test_aioc_baofeng.py` (fake serial/audio seams — format-reject-before-
audio, one-shot self-key + drain-then-drop, streaming holds one stream across frames, ptt idempotency,
no-keying-on-construction parametrized RTS/DTR, lazy-import error, close/atexit line-drop); factory
test now builds baofeng (only `v71` still raises); settings-API canary 31→36 + asserts `ptt_line` enum
renders. 5th skip = the hardware-gated real-capture test (device present but this sandbox lacks
`libportaudio2`). **Bench-verified live this cycle:** doctor audio + serial all PASS against the
plugged-in AIOC; the sound card resolves as **`All-In-One-Cable: USB`** (sounddevice matches by
PortAudio-name substring / index, NOT a raw ALSA `hw:CARD=` string — a bare `All-In-One-Cable` is
ambiguous because PulseAudio also exposes the card; the `: USB` substring targets the raw ALSA
device) and **reads real 48 kHz audio** (the hardware-gated capture test now passes on the bench);
`--key-test` confirmed **DTR keys PTT** (RTS did not) → **default flipped RTS→DTR**. The backend also
constructs against the real `/dev/ttyACM0` holding both lines low (no keying) and closes clean.
**Only operator step left:** run `backend=baofeng`,`squelch=audio` with an API-token secret and
confirm full browser talk-through (TX keys, RX streams back, and — with TOTP+callsign+voice wired —
station ID fires). Docs: ADR 0029, `docs/hardware-bringup.md` rewritten (AIOC section real; V71
still pending), README status updated. **Deferred:** blocking `receive()` still inline on the event
loop (executor is a follow-up, fine at ~20 ms); a composition-root backend `close()` lifecycle hook
(atexit covers the safety-critical drop); `SignaLinkV71` still a stub (hardware not here). Next: the
bench acceptance above, then SignaLinkV71 when its box arrives, or recordings playback/download UI.

Cycle 28 complete: **async scan runner + `/scan/stop`** (ADR 0028), mock-only — makes scan
**stoppable**, closing the cycle-21 "Scan + live phase, no stop" gap that every HANDOFF since has
deferred. `POST /scan` used to run one **synchronous** `ScanEngine.sweep()` (blocks, no stop); it now
starts a **background async task** that steps the existing `ScanEngine.tick()` on the `scan.poll`
cadence — the async **driver** around the unchanged cycle-11 tick/sweep logic and cycle-16
arbiter/TX-suspend behavior, mirroring how `RxPump` drives `receive()`. New
**`radio_server/scan/runner.py`** holds **`ScanRunner`**: owns a single `asyncio.Task`, `start(plan)`
is a **single-scan guard** (builds the engine via an injected `engine_factory`, returns `False` if
already running), `stop()` clears its task ref **before** awaiting the cancel (RxPump discipline) and is
idempotent. It stays below the API — progress **and** the new `stopped` lifecycle event flow through the
same injected `on_event` (`_publish_scan`), so it never imports `EventHub`. **The clean-stop guarantee
is free from `tick()` being fully synchronous:** a `task.cancel()` can only land at the loop's
`await asyncio.sleep(poll)`, never mid-`tick`, so the in-progress tick always completes — no mid-tune
kill. **Stop-while-TX-suspended can't wedge** because while `arbiter.transmitting` the tick early-returns
and the loop keeps *polling* (spinning cheaply), never blocking on a resume that isn't coming. **API:**
`POST /scan` is now `async`, stays **501**-gated naming `"scan"` and **422** on an ambiguous plan, then
starts the runner and returns `{"scanning": true, "status"}` immediately; a start while running is a
**409**. New **`POST /scan/stop`** (capability-gated, idempotent) returns `{"scanning": false,
"stopped": <bool>}`. The old synchronous `held` return is **gone** (no async equivalent; `sweep()` is
retained on the engine but off the live path). `/status` gained a **`scan` block**
(`{running, frequency}`, mirroring `controller`) and `/events` carries the new `stopped` phase, so the
UI reflects running/stopped. **Lifecycle:** one `ScanRunner` in `create_app`
(`app.state.scan_runner`); the lifespan teardown `await app_.state.scan_runner.stop()` right after
`rx_pump.stop()` — a scan running at shutdown is cancelled with no leaked task. **UI (`web/src/`):**
`ScanControl.jsx` replaces the lone "Scan" button with a **Start/Stop pair** modeled on
`ControllerControl` (tracks `running` optimistically from the POST responses **and** from live `scan`
events, so a scan started/stopped elsewhere — or torn down at shutdown — is reflected; a `stopped` phase
means idle); `api.js` gained `scanStop()`. `web/dist` is gitignored (source + `package.json` committed;
`cd web && npm run build` rebuilds — verified clean, 51 modules). **`uv run pytest` → 436 passed, 4
skipped** (+10; 4 skips unchanged): new `tests/test_scan_runner.py` (async unit — background start emits
`scanning`, single-scan guard, clean stop emits `stopped` w/ no leaked task, idle-stop no-op,
stop-while-TX-suspended), and `tests/test_scan.py` endpoint tests rewritten for the async contract
(non-blocking ack, first `scanning`/`stopped` over the WS, 409 second start, 501 both endpoints on
audio-only, shutdown cancels the task, stop-while-TX-suspended). **Testing note:** a task spawned during
a request is cancelled by `TestClient` at request end unless driven as `with TestClient(app) as client:`
(one persistent loop) — the tests needing the scan to live across requests use that form. **Verified
end-to-end**: against a real bound server (uvicorn + `websockets`) the full lifecycle
(start→events→`/status` block→stop→`stopped` event→idempotent no-op→409) is green, and a headless
Chromium walkthrough confirmed Scan→(Scan disabled, Stop enabled, "Live: scanning @ …")→Stop→idle.
Docs: ADR 0028 + `docs/api.md` (async `/scan`, `/scan/stop`, the `scan` status block, the `stopped`
phase, 409). **Deferred, on purpose:** live hot-reload; Opus/compression; hardware backends (real
tune/busy timing — `scan.poll`/settle/dwell stay verify-on-hardware). Next: recordings
playback/download UI + a GET API for the JSONL ledger, or the hardware bring-up phase.

Cycle 27 complete: **Web UI — settings screen** (ADR 0027), mock-only — the browser face of the
cycle-26 endpoints and the close of the config arc. **Pure client feature; the backend is
unchanged** (`uv run pytest` stays **426 passed / 4 skipped**). The cycle-26 contract was verified
sufficient before building (endpoints + `test_settings_api.py`), so no Python edit was needed; the
standing rule (real gap → minimal backend fix + pytest) did not fire. The whole form **renders from
the schema** returned by `GET /settings` — no hardcoded field list — so a setting added to the
registry later needs zero UI change. New `web/src/components/`: **`SettingsView.jsx`** (fetches
`GET /settings`, groups fields by `group` into `.card` sections, dirty-tracks edits, Save PATCHes
**only changed keys**; on the atomic **400** it surfaces the named key inline and **keeps the
operator's edits**; on success shows a **restart-to-apply banner** off `restart_required` then
re-fetches), **`SettingsField.jsx`** (renders one setting **by `type`** — text/number/toggle/select
— with the schema **`description` always visible** as inline help, and required / required-unset
flagged), **`SecretsPanel.jsx`** (api-token + TOTP shown **present/absent only**; **Rotate API
token** reveals the new token **once** with copy + honest "active after restart; current session
still works" wording + a return-to-gate re-auth action; **Re-enroll TOTP** renders the returned
`otpauth://` URI as a **scannable QR** once via **`QrCode.jsx`**, with the URI as copyable text),
and the QR uses **`qrcode.react`** (the one new dep — zero-runtime-deps, MIT SVG). **Wiring:**
`api.js` gained four client methods (`settings`/`updateSettings`/`rotateApiToken`/`enrollTotp`);
`vite.config.js` proxies `/settings`; `ControlPanel.jsx` got a topbar **view toggle** (Control ⇄
Settings); `App.jsx` threads an `onReauth` (deliberate return-to-gate). **`web/dist` is gitignored** —
the commit carries source + `package.json`/`package-lock.json`; `cd web && npm install && npm run
build` rebuilds it (verified: 51 modules, clean). **Apply semantics: restart-to-apply (v1)** — the UI
says so on every write. **Acceptance is a browser walkthrough** against the mock server (see ADR 0027
/ the PR); the endpoint contract the UI consumes was also re-proven via a TestClient. **Deferred:**
live hot-reload; server-side scan-stop (standing, unrelated backend gap); hardware backends.

Cycle 26 complete: **Settings REST API + secret rotation** (ADR 0026), mock-only — a **thin,
token-gated HTTP surface over the cycle-25 config**, so the cycle-27 UI can read/edit settings. **No
new config logic:** endpoints serialize the `SettingSpec` registry and validate via `resolve_settings`
/ persist via `save_settings`/`save_secret`/`rotate`. New `radio_server/api/settings.py` with
`register_settings_routes(api, app)` (called from `create_app`, routes on the existing
`Depends(require_token)` router). **`GET /settings`** serializes every setting — `key, group, type
(+choices for enums), default, value, required, description` — with `type` **derived in the API
layer** (bool/enum/integer/number/string; `bool` checked before `int`; `station.id_mode` keyed off
its coercer) so `config/spec.py` stays untouched; a required-unset value serializes as `null` (no
raise); plus a `secrets` block that reports **presence only** (`{"set": bool}`) — a secret value can
never appear because secrets aren't in `SETTINGS`. **`PATCH /settings`** takes `{"values":{key:val}}`,
rejects secret + unknown keys up front, then validates the **whole** patch atomically by resolving
`{current values}|patch` (raises naming the bad key **before any write** → 400; file untouched), then
`save_settings` round-trips to `radio.toml` and updates `app.state.settings` for display; returns
`restart_required` (v1 = all changed keys). **`POST /settings/secrets/api-token/rotate`** and
**`POST /settings/secrets/totp/enroll`** are **write-only** — generate (or accept, for the API token)
a secret, `save_secret` it 0600, and return it **once** (the token in-body; the TOTP secret as an
`otpauth://` provisioning URI via `TotpVerifier.provisioning_uri`); they never read an existing
secret back. **Wiring:** the one real change was threading the config/secrets **file paths** to the
app — `build_app`/`create_app` gained `config_path`/`secrets_path` (+ the `Secrets` object) stored on
`app.state`; `--config`/`--secrets` flow through; `DEFAULT_CONFIG_PATH` moved into
`config/settings.py`. **Apply semantics: restart-to-apply (v1)** — writes persist but the running
server (the token `require_token` closes over, the scan route's startup settings) is **not**
hot-reloaded; every write response says so. `uv run pytest` **426 passed / 4 skipped** (412 + the new
`tests/test_settings_api.py`: schema+values with no secret leak, atomic-reject-naming-key with the
file byte-unchanged, unknown/secret-key rejection, token-gating, rotate persists+returns-once, enroll
fresh-URI-never-existing-secret). Docs: ADR 0026 + a `## Settings & secrets (ADR 0026)` section in
`docs/api.md`. **Deferred for cycle 27:** the web settings screen (renders off `GET /settings`, shows
a "restart to apply" banner off `restart_required`); live hot-reload; QR rendering of the URI.

Cycle 25 complete: **config foundation — a schema-driven `radio.toml` replaces the ~31 scattered
`RADIO_*` env reads** (ADR 0025), reversing the de-facto env-only decision. **Behavior-preserving
refactor:** with no config file, every default equals today's and the suite stays green (**412
passed / 4 skipped** — up from 386 because the config system added `tests/test_config.py`; five old
`load_api_token`/`load_totp_secret` env-reader tests were removed as those functions are gone). New
`radio_server/config/` package: **`spec.py`** (the `SettingSpec` registry — one source of truth for
key/default/coercion/description, 31 non-secret settings grouped into 11 TOML tables; every default
*references* the existing `DEFAULT_*` constant so there's no duplication), **`settings.py`**
(immutable `Settings` + `resolve_settings`/`load_settings` via stdlib `tomllib`), **`save.py`**
(`save_settings` round-trips via **tomlkit**, preserving hand-added comments; `render_example`
generates `radio.toml.example` from the registry), **`secrets.py`** (`load_secrets`/`save_secret`/
`rotate` — the two secrets on a separate 0600-enforced channel). **The load-bearing subtleties, all
verified against the old loaders:** empty-string handling is per-field (→default for floats, →fail
for callsign/tts, →False for record bools, →True for mock_cat); `time.tz` keeps its
`ZoneInfoNotFoundError`, the VAD `on>off` hysteresis stays a `ValueError` in `AudioLevelGate.__init__`
(a cross-field check, not in the schema); two bool coercers (strict fail-loud for `recording.*`,
permissive for `server.mock_cat`); **required-unset fails loud lazily on access** (so the default
mock app with no callsign/voice still starts — the invariant the whole refactor hinges on). Every
`load_*(env)` is now a thin `load_*(settings)` accessor; `build_app(settings, secrets)` /
`build_controller(settings, *, totp_secret=…)` thread the secret in explicitly (it is never a schema
setting). Bootstrap: `python -m radio_server --config PATH --secrets PATH` (argparse; `create_app`
gained an optional `settings=` only so the on-demand `/scan` route can read scan timing — otherwise
still the env-free DI seam). **Secrets split is the security-load-bearing part:** `RADIO_TOTP_SECRET`
/ `RADIO_API_TOKEN` are never in `radio.toml`, never in the `SETTINGS` schema, never serialized by
`save_settings` — so the future settings API/UI can't leak or clobber them. Also broke a latent
`eventlog↔api` import cycle (`eventlog/log.py`'s `Event` import is now `TYPE_CHECKING`-only) that the
new `config.spec` imports surfaced. **Apply semantics: restart-to-apply (v1)** — `save_settings`
persists but does not hot-reload; live reload is deferred on purpose. Docs swept off env vars to
`radio.toml` (README §Configuration, `docs/operating.md`, `docs/api.md`, `docs/architecture.md`,
`web/README.md`); ADRs left as historical record. **Deferred for cycles 26/27 (helpers built here):**
settings REST API + secret-rotation endpoints (26 — `save_secret`/`rotate` are built and tested),
the UI settings screen (27), and live hot-reload. `save_settings`/rotation have no endpoint yet.

Cycle 24 complete: **comprehensive documentation pass** (no ADR — docs cycle), **zero code change**.
The repo had 24 ADRs but no top-level user-facing docs (`README.md` was a 0-byte stub, `web/README.md`
was stale — it predated the cycle-22/23 audio work and still said "live audio arrives in later
cycles"). Wrote the user-facing set, every factual claim **verified against source, not memory**:
**`README.md`** (front door — the two modes, an honest mock-vs-hardware status block, the
two-auth-planes warning up top, quickstart, and a **complete 33-var `RADIO_*` env table** grouped by
concern with defaults + which 4 are fail-loud: `RADIO_API_TOKEN`/`RADIO_CALLSIGN`/`RADIO_TOTP_SECRET`/
`RADIO_TTS_VOICE`); **`docs/api.md`** (REST + WS reference — the 10 endpoints, the `501`
named-capability gate body `{"detail":{"error":...,"capability":...}}`, the three sockets with the
`{"status":"ready","format":{rate:48000,width:2,channels:1}}` handshake, the `/events` taxonomy
incl. `arbiter`/`auth`/`command`, and close codes `1008`/`1013`-with-the-accept-then-busy quirk/`1003`);
**`docs/architecture.md`** (the `Radio`/`CatRadio` protocol + capability split, the layer map, the
pure-leaf packages activity/arbiter/eventlog/recording, the duplex arbiter's TX-priority auto-resume,
and mock-first testability); **`docs/operating.md`** (Part 97 — the two auth planes, station ID
≤600s/forced/sign-off/cw|voice, the no-secrets whitelist log, security-reality, config guardrails);
**`web/README.md`** rewritten (RX/TX audio now ship, the static-mount-last serve path, the
AudioContext-gesture / mic-permission browser requirements, the dev proxy). Two deferred guides are
honest one-paragraph placeholders — **`docs/hardware-bringup.md`** and **`docs/deployment.md`** —
pointing to the pending bench bring-up; **no fabricated hardware specifics** (Hamlib model,
multimon flags, AIOC PTT line stay verify-on-hardware). ADRs are **linked, not duplicated**.
`uv run pytest` **386 passed, 4 skipped** (unchanged — proves zero behavior change); `git status`
shows only `.md` files. **Two doc/code discrepancies surfaced (flagged in PR #26 for a later cycle,
NOT fixed here):** (1) **duplicate ADR 0001** — both `0001-cycle-model.md` and
`0001-two-backend-radio-abstraction.md` exist; (2) **`api/events.py` is stale** — `EVENT_TYPES` still
lists `"busy"` (reserved/unused) and its docstring predates ADR 0019, omitting the `arbiter`/`auth`/
`command` types the app actually emits. (The suspected "web/dist committed despite gitignore" was a
non-issue — `web/dist/` is correctly gitignored/untracked, only a local build artifact.) **No
instruction issue exists in this repo** (`gh issue list` empty), so the CLAUDE.md end-of-cycle issue
comment/label step had no target — noted in the PR instead. Next: pick up either discrepancy as a
tiny code cycle, or the backend scan-stop / recordings playback-download UI deferred earlier.

Cycle 23 complete: **web UI — TX mic capture** (ADR 0024), mock-only. The browser operator can now
**talk through the gateway** — the mirror of cycle 22, an almost pure client feature over the cycle-15
`/audio/tx` socket. **Verified, not assumed:** the whole TX contract already exists — `?token=`→1008,
single-talker `TxSlot`→1013, the JSON format handshake
(`{"rate":48000,"width":2,"channels":1}`→`parse_tx_format`, non-canonical→1003), the
`{"status":"ready","format":…}` ack, whole-sample framing (odd→1003), PTT keyed on the first real
frame + dropped on close/idle (2 s), and `MockRadio.tx_log` (`list[AudioFrame]`, `.samples`==bytes
sent) — all covered by `tests/test_tx_audio.py`. **One minimal server change** surfaced in browser
verification (the "server gap gets a pytest" the brief anticipated): a browser **cannot see a
pre-accept WS close code** — a rejected handshake shows as generic **1006**, so the single-talker
**1013 was invisible**. Fixed by **accept-then-inform**: `api/app.py`'s busy path now `accept()`s,
sends an explicit **`{"status":"busy"}`** message the client reads, then closes 1013 (ordering is
load-bearing — the busy path returns before the `session`/`finally`, so it never releases the *other*
talker's slot). Token/1008 stays pre-accept (a browser 1008 is a rare rotated-token edge — token is
gate-validated first — surfacing as a generic error). The two second-talker tests now assert the busy
message then 1013. `uv run pytest` stays **386 passed, 4 skipped**. **The client** (new under
`web/src/`): **`txWorklet.js`** — a
`"tx-capture"` sink worklet (`numberOfOutputs:0`), the inverse of `rxWorklet.js`, forwarding each
captured Float32 quantum (a copy) to the main thread. **`useTxAudio.js`** — mirrors `useRxAudio`
(state/ref split, gesture gate, rAF meter of the *outgoing* audio): `startTalk()` (from the Talk click)
`getUserMedia({audio:{channelCount:1,…}})` (denial→clear "denied" state, no hang), builds
`MediaStreamSource → tx-capture` on a **default-rate** `AudioContext` (NOT forced 48k, so the
resampler is the real path), opens `/audio/tx?token=`, sends the canonical header, awaits the ready
ack, then streams. **The load-bearing piece: client-side resample** `ctx.sampleRate → 48000` (streaming
linear interpolation, carrying `prev` sample + fractional `pos` across quanta so it's click-free) +
Float32→Int16 LE, batched into ~20 ms (960-sample/1920-byte) frames — the exact inverse of cycle 22's
decode. **No auto-reconnect** (unlike RX): a keyed transmitter must never silently resurrect — close
codes map to states (1008→`onAuthError`/re-gate; **1013→"radio busy", no retry-hammer**;
1003→format-error). `stopTalk()` closes the WS (server `finally` drops PTT + frees the slot), **stops
the mic tracks** (clears the OS indicator), tears down. **`TalkControl.jsx`** — the TX pair to
ListenControl: a red `.ptt.keyed` toggle ("Talk"/"Stop talking"), an "on air" badge, a red mic level
meter (`.meter-tx`), and clear denied/busy states; reports its talking state up. **Half-duplex UX:**
because the RX **jitter buffer holds ~500 ms**, server-side RX suspension alone would let you *hear
yourself gate in/out* — so `ControlPanel` lifts the local `talking` state and passes `suspendedLocally`
→ `ListenControl` → a new **`forceMute`** input on `useRxAudio` (effective gain `=(muted||forceMute)?0:1`,
ramped live) that mutes the monitor **immediately** on local keying; gated on *our own* talk, not the
global `transmitting`, so a remote op's TX doesn't mute us. `PttControl` (REST `/ptt`) left untouched
(orthogonal manual key). Vite proxy already had `/audio/tx` (cycle 22); no `api.js` change (WS auth is
`?token=`). **Verified end-to-end in a real headless browser** (Chrome with a fake mic device): Talk
keys + streams canonical frames into `tx_log`; a forced-44.1k context still lands ~48k/s (resample
proven); release drops PTT + frees the slot; a second talker → "radio busy" no-retry; mic denial →
clear message, no hang; talking mutes the local RX monitor. Deferred, on purpose: recordings
playback/download UI, async scan + `/scan/stop` (noted backend gap), Opus/compression. Next: recordings
playback/download, or the backend scan-stop.

Cycle 22 complete: **web UI — live RX audio playback** (ADR 0023), mock-only. The browser now **plays
what the radio hears** — a pure client feature over the cycle-13 `/audio/rx` socket, plus **one minimal,
symmetric server change**. **Verified, not assumed** (the brief's caution): `/audio/rx` sent *no*
format header (just raw `send_bytes` after `accept()`), while `/audio/tx` has a declared-format
handshake — the "cycle-15 symmetry decision" was never actually implemented. Realized now: `/audio/rx`
sends **`{"status":"ready","format": asdict(CANONICAL_FORMAT)}` first**, mirroring TX's ready ack, then
the raw canonical PCM as before (the **only** Python edit — one `send_json` in `api/app.py`; the
demand-driven pump lifecycle is untouched). Three RX WS tests now read the header first
(`receive_json()`) and a new `test_audio_rx_sends_format_header` asserts it (reject-token tests
unaffected — they close `1008` before `accept()`). **The client** (all new under `web/src/`): an
**AudioWorklet ring-buffer player** (`rxWorklet.js`, processor `"rx-player"`) fed by **`port.postMessage`
Float32 — deliberately NOT SharedArrayBuffer, so no COOP/COEP headers and the cycle-21 same-origin
static mount stays intact**. A **jitter buffer** primes ~150 ms then drains and caps latency at ~500 ms
(drop-oldest, mirroring the server hub); an **underrun outputs silence + re-primes**, so *every* gap —
scripted RX silence, WS reconnect, and the arbiter suspending RX during TX (half-duplex, ADR 0017,
where `/audio/rx` just stops delivering frames) — is a **clean pause, no buzz, no crash**, resuming
cleanly when frames return. **`useRxAudio.js`** mirrors `useEvents` (`?token=` auth, backoff reconnect,
`1008` → `onAuthError` back to the gate) but `binaryType="arraybuffer"`; it **creates nothing until the
Listen gesture** (browsers start an `AudioContext` suspended — autoplay is impossible on load), then
builds the context at **48 kHz** (canonical PCM maps 1:1, no resample), loads the worklet, wires
`worklet → GainNode(mute) → destination`, reads the leading header (noted, but plays canonical
regardless → header-less older servers still work), and decodes each frame `Int16Array → Float32`
(`/32768`, LE). **`ListenControl.jsx`** (a `.card` in the left column): Listen/Stop, a mute (`GainNode`
0/1), a peak **level meter** (per-frame peak smoothed on `requestAnimationFrame`, reflects incoming
audio even when muted), a stream conn badge, and a **"receiving paused (transmitting)"** note driven off
the existing `/events` `transmitting`/`arbiter` state (no new server "suspended" marker). `vite.config.js`
gains `/audio/rx` (+ `/audio/tx`, reserved for 23) as `ws:true` dev-proxy entries. `uv run pytest` →
**386 passed, 4 skipped** (+1; the 4 hardware/model skips unchanged). **Verified end-to-end in a real
headless browser** (Chromium against the live mock seeded with an audible looping tone): Listen →
continuous audio + moving meter; a TX-suspend gap via a streaming `/audio/tx` client → paused
indicator, no buzz, clean resume; Stop → pump idle; autoplay confirmed impossible before the gesture.
Deferred, on purpose: **TX mic capture (cycle 23)**, recordings playback/download + a GET API for the
JSONL ledger, a distinct `/events` "suspended" marker, Opus/compression. Next: **TX mic capture.**

Cycle 21 complete: **web UI — control panel** (ADR 0022), mock-only. The first browser client and,
finally, a **real server entrypoint**. Control + visibility only; **live audio is deferred to cycles
22–23**. A **React + Vite SPA** under a new top-level `web/` (sources in `web/src/`, builds to
`web/dist/`; `node_modules/` + `web/dist/` gitignored, so `npm install && npm run build` is a
documented prerequisite — the chosen cost of a build toolchain in a uv-only repo). **Served
same-origin:** `create_app` gained a keyword-default `web_dir` that mounts the built bundle at `/`
via `StaticFiles(html=True)` **mounted last** (after the REST router + WS routes, so the token-gated
API always wins); an **unbuilt** `web_dir` serves a "run `npm run build`" placeholder instead of
crashing; `web_dir=None` (all prior tests) adds no `/` route → surface unchanged. `build_app` reads
`RADIO_WEB_DIR` (marked default → `web/dist`). **The missing entrypoint exists:** `python -m
radio_server` (`radio_server/__main__.py`) binds uvicorn to `RADIO_HOST` (default `127.0.0.1`) /
`RADIO_PORT` (default `8000`) around the env-composed app — thin; `build_app` still fails loud
without `RADIO_API_TOKEN`. **`websockets` is now a runtime dep** — plain `uvicorn` ships no WS
implementation, so a bound server 404s every `/events` upgrade (the TestClient masked this with its
own in-process WS); added explicitly, not `uvicorn[standard]`, to stay lean. **Mock CAT toggle:**
`RADIO_MOCK_CAT` (marked default `on`) — `off` yields an **audio-only mock** so guardrail 3's
control-greying is demonstrable in a browser without hardware. **The UI:** in-memory token (React
state only, never `localStorage`; `Authorization: Bearer` on REST, `?token=` on the WS); token gate
validates via `GET /capabilities` (bad token → clear error, not a hang); **capability-driven
greying** off `/capabilities` with a **defensive 501** backup (reads `detail.capability`, greys
exactly that control); **one `/events` WS** folds `status`/`ptt`/`scan`/`session`/`auth`/`command`/
`arbiter` frames into a live status panel + a **bounded (~500)** scrolling event log, **reconnects
with exponential backoff** on drop (a `1008` rejected-token close stops retrying → back to the gate).
Controls: tune (freq/channel/tone-with-clear/mode), PTT toggle, scan, controller start/stop. **Honest
about the API:** `/scan` is one synchronous sweep returning `held` with **no server-side stop**, so
there's a "Scan" button and live phase — no dead stop button; controller `503` "not configured"
renders as a disabled control with a message, not a dead button. **Backend behavior unchanged** — the
only Python edits are `api/app.py` (mount + env toggles), the new `__main__.py`, and the `websockets`
dep; every other package untouched. **Verified end-to-end in a real headless browser** (Chromium via
Playwright against the live server): token gate, CAT-vs-audio-only greying, each control hitting its
endpoint and reflecting result, `/events` driving status + log, controller 503, and **WS reconnect
after a server drop/restart** — all green. `uv run pytest` → **385 passed, 4 skipped** (+8 in
`tests/test_web.py`: static mount, unbuilt placeholder, static-never-shadows-gated-API, `web_dir=None`
unchanged surface, `RADIO_WEB_DIR` + `RADIO_MOCK_CAT` env wiring; SPA itself is browser-verified).
Deferred, on purpose: **live RX playback (cycle 22)**, TX mic capture (23), recordings
download/playback + a GET API for the JSONL ledger, a server-side scan-stop, an `/events` "suspended"
marker for arbiter RX-pause. Next: **live RX audio.**

Cycle 20 complete: **recording safety rails + TX recording** (ADR 0021), mock-only. Closes the three
cycle-19 footguns and folds in the deferred TX capture. **The backend is now genuinely complete and safe.**
Four pieces: **(A) Max-duration segment roll** — `Recorder` gained an **always-on** cap `max_seconds`
(`RADIO_RECORD_MAX_SECONDS`, default 3600, positive-or-fail-loud, **no disable sentinel**). `write()` checks
the injected clock **before** the lazy-open and, if the open segment has run `>= max_seconds`,
`end_segment()`s so the existing lazy-open rolls a fresh file — the triggering frame starts the new segment;
`_open_segment` stamps `_segment_started`, the `_wav is not None` guard makes a stale start after `_abort()`
harmless (no reset). So **no single WAV grows without bound even under `RADIO_SQUELCH=off`** — its endless-file
footgun is closed. FakeClock-deterministic (reuses the stamp clock). **(B) Squelch-off warning** — `build_app`
now logs a one-time `WARNING` (the repo's **first** `logging` use — a module `logger`, handler-free, `caplog`-
testable) when `RADIO_RECORD=on` and `RADIO_SQUELCH=off`, saying segmentation is time-based (the roll), not
activity-based. **It does not fail** — the roll makes it safe. **(C) Half-duplex split** — `RxPump.run`'s
existing `if self._arbiter.transmitting:` branch now also calls a **guarded** `self._recorder.end_segment()`
before sleeping, so a streaming-TX key-up mid-RX **finalizes the open RX segment** and resume lazy-opens a
fresh file — a recording reflects one continuous receive, no concatenation across the keyed gap. Idempotent →
no rising-edge flag. Correctly scoped to `arbiter.transmitting` (streaming TX only; REST `/ptt` keys directly
and never touches the arbiter, so it neither pauses nor splits — pre-existing behavior). **(D) TX recording** —
the same `Recorder` records transmitted audio, distinguished only by a **`tx-` filename prefix** (the
hardcoded `rx-` became a ctor `prefix` param). `TxSession` gained a `recorder` injection (a **local**
`TxRecorder` Protocol + `null_recorder` default mirroring `rx.pump.RxRecorder`, so `tx` **never imports**
`recording` — the arrow stays `tx -> {audio, backends}`); `feed()` writes each transmitted frame (guarded,
after `transmit`), `close()` finalizes. Opt-in via **`RADIO_RECORD_TX`** (default off, **independent** of
`RADIO_RECORD`); shares `RADIO_RECORD_PATH`, inherits `RADIO_RECORD_MAX_SECONDS`, ignores `RADIO_RECORD_MODE`
(gating is an RX concept). RX/TX are **separate `Recorder` instances** (own sequence counters) in the same dir,
disambiguated by prefix, both on `time.time` → filename stamps **timestamp-align** with the ledger's
`tx_key_up`/`tx_key_down`. **The sharpest failure-isolation call:** `close()`'s `end_segment` is placed **after**
the keying/arbiter-release work and **inside `if self._keyed`**, and **guarded** — the `/audio/tx` `finally`
runs `session.close()` **then** `tx_slot.release()`, so an exception escaping `close()` would skip the slot
release and **permanently wedge the single transmitter**; guard + ordering guarantee a disk fault can never
break keying or leak the slot. Concurrent isolation comes from `TxSlot` (a second talker is refused **before**
its `TxSession` is built), so the shared `tx_recorder` is only ever fed by one talker; sequential talkers share
it and get a continuous `tx-000001`, `tx-000002`… counter. **Wiring:** `create_app`/`build_app` gained a
`tx_recorder` param (keyword-default → existing callers unchanged), stored on `app.state.tx_recorder`, closed in
the lifespan teardown alongside `recorder`, and passed into each `/audio/tx` `TxSession`; `build_app` calls
`build_tx_recorder(env)`. `uv run pytest` → **377 passed, 4 skipped** (+25; the 4 multimon/piper skips
unchanged; all prior tests pass untouched — only keyword-default params were added). Deferred, on purpose:
Opus/compression, retention/cleanup, the playback/download API (the web UI), full-capture (pre-gate) mode
(seam only), decoupling recording from the demand-driven pump. Next: **the web UI.**

Cycle 19 complete: **audio recording — received audio → WAV** (ADR 0020), mock-only. The stack could
capture, stream, gate, and log audio but not **keep** it; this cycle adds a passive `Recorder` that
writes received audio to timestamped WAV files, one per RX activity session. **The load-bearing
design call:** the brief said tap the pump "as another sink alongside the WS listeners" (an
`AudioHub` subscriber), but the hub only ever carries **gate-open** frames — a subscriber can't see
the **gate-close** edge that bounds one session, so segmentation would need a wall-clock gap timeout
(not `FakeClock`-deterministic) or a racy second channel. So the recorder is a **`Protocol`-injected
sink the pump calls directly** (confirmed with the user): gate-open → `recorder.write(samples)`, a
non-empty frame the gate **rejects** → `recorder.end_segment()` (the close edge), empty frames reach
neither; the hub `publish` runs **first** so recording never adds stream latency. This sees exactly
the post-gate frames the hub streams, opens **no second capture reader**, is deterministically
testable, and gated-recording falls out for free. A new pure-leaf **`radio_server/recording/`**
package (sibling of `eventlog/`, imports only `..audio`) holds **`Recorder`**: WAV via the stdlib
**`wave`** module (no new dep; fixed canonical 48k/s16le/mono → deterministic header), lazy per-
segment open, filename `rx-{seq:06d}-{YYYYmmddTHHMMSSZ}.wav` (the **sequence counter** guarantees
uniqueness + lexical==chronological order; timestamp from an injected `Clock`). `rx/pump.py` gained
only a local **`RxRecorder`** Protocol + a **`null_recorder`** no-op default; `api/app.py` is the
only meeting point (`create_app(recorder=None)` → `app.state.recorder` → `RxPump`; shutdown
`close()`; `build_app` calls `build_recorder(env)`). **Config (opt-in):** `RADIO_RECORD` (default
**off** → no recorder, writes nothing), `RADIO_RECORD_PATH` (marked default `recordings`, validated
**fail-loud at construction** via makedirs+probe like `JsonlSink`), `RADIO_RECORD_MODE` (default
`gated`; `full`/pre-gate is a **reserved seam** — `build_recorder` raises `NotImplementedError`, not
silently gated). **Failure isolation (hard rule):** `Recorder` catches+drops internally **and** the
pump guards its calls (double guard — the pump is the single shared capture task whose death blinds
every listener; disk I/O is a broader fault surface than the non-raising leaves — the `EventLog.handle`
reasoning). **TX recording deferred** with a note (clean via the cycle-18 `on_key` edges + a `tx-`
prefix later; `feed` is the load-bearing keying path, its own cycle). **Documented, not fixed:** with
`RADIO_SQUELCH=off` there's no gate-close edge so all RX becomes one file (finalized on pump stop);
recording is coupled to the demand-driven pump (nothing records when nobody's listening); a
half-duplex TX pause concatenates across the keyed gap. `uv run pytest` → **352 passed, 4 skipped**
(+29; `create_app`/`RxPump` gained only keyword-default params, so all prior tests pass unchanged; 4
hardware skips unchanged). End-to-end smoke: scripted frames through `/audio/rx` produced both the
live stream and a valid on-disk WAV (canonical header, exact PCM). Deferred: Opus/compression,
retention/cleanup, playback/download API (the UI), full-capture mode (seam only), TX recording,
pump-decoupling. Next: the web UI.

Cycle 18 complete: **emit the deferred log events** (ADR 0019) — pure wiring, mock-only, **no new
record shapes**. Cycle 17 built the full ledger taxonomy but ~half was **dead in production**: the
`auth`/`command`/`arbiter` mapper branches and the `station_id` `callsign`/`mode` fields are pure
functions of events **nothing published**. This cycle connects the real producers. The load-bearing
constraint: every leaf (`auth`/`services`/`controller`/`arbiter`/`tx`) **deliberately does not import
`EventHub`** — the only `hub.publish` sites are in `api/app.py`; leaves emit domain events through
injected callbacks and the API adapts. So "publish in the site's own package" is impossible as
written; the faithful realization (and what "don't centralize" means) is **each producer surfaces its
own signal at its own site**, routed through a callback the API turns into a hub event. **Five
emissions, all via the callback → API-adapter pattern, zero `hub.publish` in any leaf:** (1)
`auth_accepted` + `auth_rejected` from the `Controller.step` outcome loop (auth signals carry **no
data** — never a code); (2) `command_dispatched {service}` on a **transmitted** dispatch only (a
registry miss is a graceful no-op, no record); (3) `station_id` enriched with `{callsign, mode}` —
`StationId` gained `callsign`/`mode` properties, `mode` threaded from `load_id_mode(env)` at
`build_controller`; (4) `arbiter_mode` via a new `RadioArbiter(on_change=...)` fired **only on a real
derived-mode change** (leaf-pure `Callable`, no import); (5) streaming-TX `ptt` via a new
`TxSession(on_key=...)` at both key edges — streaming keying now logs `tx_key_up`/`tx_key_down` with
duration like REST. The API adapter `_publish_controller` (renamed from `_publish_session`) fans the
controller's one `on_event` channel out by phase → `auth`/`command`/`session` hub types. **Correction
to the brief:** "auth_accepted already flows" was wrong — the accept path emitted only `session_open`;
both are now distinct records. **Fire-and-forget confirmed, not regressed:** `EventHub.publish` is
`put_nowait` onto unbounded queues (non-blocking, non-raising), so these synchronous emissions can't
break auth/dispatch/keying/arbiter. The cycle-17 `eventlog/` mappers changed **zero lines** — they
just light up. `uv run pytest` → **323 passed, 4 skipped** (+12; 3 existing controller assertions
updated for the richer stream, none weakened; 4 skips unchanged). End-to-end proof: a bad-code →
login → command → forced-ID → streaming-TX round-trip through `create_app` writes a JSONL file
containing **every** taxonomy type with no code/secret/token material. Deferred: SQLite sink, log
rotation/retention, query/`GET` API, audio recording (next cycle), web UI (the sequence after).

Cycle 17 complete: **event log / QSO ledger** (ADR 0018) — a durable, structured, timestamped
station log, mock-only and hardware-free. The events a log needs **already flow** through `EventHub`
(ADR 0011), so the ledger is **not new instrumentation** — it is **another SUBSCRIBER** of that flow
that writes durable records, adding **zero** `hub.publish` sites to `auth`/`arbiter`/`tx`/`controller`.
A new pure-leaf **`radio_server/eventlog/`** package (imports only stdlib + `..api.events.Event`)
holds it. **`LogSink`** is the storage protocol (`write`/`close`); the default **`JsonlSink`** writes
**append-only JSONL, one JSON object per line** (greppable, `tail -f`-able — the project's first
persistence). A **SQLite sink is the documented future swap**, not built. Path is `RADIO_LOG_PATH`
(marked default `radio-server.jsonl`, mirroring `time_service.load_timezone`); a **set-but-unwritable
path fails loud at construction** (`JsonlSink.__init__` opens in append mode → `OSError` at the
composition root). **`EventLog`** is the sync mapper: a lifespan-managed background task drains its
own `hub.subscribe()` queue and calls `EventLog.handle(event)` — the exact `/events` consumer shape,
passive, never blocks `publish` (unbounded queue). Records are flat `{"ts": <clock float>, "type",
...fields}`; `ts` from the injected **`Clock`** (`Callable[[], float]`, default `time.time`,
`FakeClock`-testable). `tx_key_up` remembers its timestamp so the paired `tx_key_down` records the
keyed **duration** (Part 97 value). **SECURITY (hard rule):** the mapper **whitelists** the fields
each record emits — it **never spreads `event.data`** — so a TOTP code/secret/API token can never
reach the ledger even if it appeared upstream; a rejected-auth record is just `{ts, type:auth_rejected}`
(tested with a fake `code`/`secret` payload → absent). **Failure isolation:** `EventLog.handle` catches
+ drops on any error (a logging fault never reaches the pump or a transmission), and the audio path
(`/audio/rx` `AudioHub`, `/audio/tx` `TxSession`) never flows through `EventHub` anyway; graceful
shutdown drains still-queued events before closing the sink (no lost entries). Live records today:
`ptt` (REST `/ptt` key-up/down), `scan` (phases incl. `active`+freq), `session` (open/id/close).
**Forward-compatible but NOT yet emitted to the hub** (mapper ready, `hub.publish` deferred to a
future instrumentation cycle): `auth_accepted`/`auth_rejected`, `command_dispatched`, `arbiter_mode`,
and ID `callsign`+`mode` fields. Wiring is confined to `create_app` (new `event_log=None` default →
existing tests unchanged) + `build_app` (opens the sink). `uv run pytest` → **311 passed, 4 skipped**
(+18; the 4 skips unchanged). Deferred: SQLite sink, log rotation/retention, a query/GET API, audio
recording (cycle 18), web UI (cycle 19+), and the live emissions above.

Cycle 16 complete: **RX/TX duplex conflict policy** (ADR 0017) — the **last pure-software cycle**,
mock-only. A half-duplex radio can't receive and transmit at once (keying blinds the receiver), so
this cycle adds the seam that enforces it: **TX takes the radio; the RX pump and any live scan stand
down while keyed and resume when TX drops.** A new pure-leaf **`radio_server/arbiter/`** package
(imports *nothing* from the rest of the tree, so `tx`/`rx`/`scan`/`api` all depend on it with no
cycles) holds **`RadioArbiter`** — "who has the radio right now" as **`RadioMode`** (`idle` /
`receiving` / `transmitting`), modeled as **two independent latches** (`_transmitting` set by TX,
`_receiving` set by the RX pump) with a **TX-priority derived mode** (`transmitting > receiving >
idle`). That beats preempt/restore bookkeeping: on `release_tx()` the RX latch is still set, so the
mode falls back to `receiving` on its own. **Coherence guard:** `acquire_tx()` raises
**`ArbiterStateError`** on a double-key (one transmitter, one talker); `release_tx()` is idempotent
(mirrors `TxSession.close()`). One shared arbiter is created in **`create_app`** (`app.state.arbiter`)
and injected into the RX pump and every per-connection `TxSession`. **TX (writer):** `TxSession.feed()`
calls `acquire_tx()` before `ptt(True)`, `close()` calls `release_tx()` after `ptt(False)` — the two
existing keying points. **RX (reader):** `RxPump.run()` asserts `begin_receive()`/`end_receive()` and,
while `arbiter.transmitting`, **does not pull `receive()` at all** (you can't read a blinded receiver);
listeners stay subscribed (`subscriber_count` unchanged) — only delivery pauses, then resumes to the
same queue. **Scan (reader):** `ScanEngine.tick()` early-returns while transmitting — no tune, no poll,
no advance; **resume needs only the flag** because all positional state (`_state`, `_i`, `_current_freq`,
`_tuned_at`, `_dwell_deadline`) already survives on the instance (noted wrinkle, not fixed: `_tuned_at`
is wall-clock, so a channel paused mid-settle polls one tick sooner after a long pause — harmless). The
`POST /scan` `sweep()` path is untouched (synchronous, can't interleave). Every consumer's arbiter param
**defaults to a private idle arbiter**, so standalone construction is behaviorally unchanged — all prior
tests pass untouched. `MockRadio`/`audio/format.py`/`activity/`/`controller/`/`auth/`/`events.py` are
untouched (tune + receive spies live in the test). `uv run pytest` → **293 passed, 4 skipped** (+10 — 6
arbiter unit, 2 RX-pump, 1 scan, 1 end-to-end; the 4 skips unchanged). Deferred: the optional `/events`
"suspended" marker (behavior delivered without it), Opus, and the real backends' audio I/O + on-bench
PTT-tail/turnaround timing (guardrail 1 — the arbiter models the *logical* exclusion, never the ms).

Cycle 15 complete: **TX audio ingest** (ADR 0016) — the **second half of "talk through the gateway,"**
mock-only, the mirror of cycle 13's RX path in the opposite direction. A binary WebSocket **`GET
/audio/tx`** accepts canonical PCM *in* from a LAN client and feeds it to `radio.transmit()`; it lands
in `MockRadio.tx_log`. Same `?token=` auth plane as `/audio/rx` (rejected pre-`accept()` with
`WS_1008`). A new **`radio_server/tx/`** package sits **below `api`** (imports only `..audio` +
`..backends`, never `rx`/`api`), mirroring the `activity` layering. **No hub, no pump** — TX is
**fan-in/serialized** (one radio, one talker), the opposite of RX's fan-out. **`TxSession`** is the
per-connection keying/ingest state machine (guardrail 2): `feed(data)` validates whole-sample framing
**first** (a bad frame raises before any `ptt()`, so it never keys), **skips empty `b""`** (mirrors
`RxPump`), keys **`ptt(True)` once** on the first real frame, `transmit`s each frame, stamps activity;
`close()` drops **`ptt(False)`** (idempotent) on any exit — PTT is keyed via `ptt()`, **never a CAT
TX**. **`TxSlot`** is the single-talker guard — a plain flag, **not** an `asyncio.Lock` (a Lock would
*queue* the second talker; we must *refuse* it): a second concurrent client is closed **`1013`** before
`accept()`, released in the endpoint's `finally`. Wire protocol: token → slot acquire → `accept()` →
**declared-format handshake** (`parse_tx_format` builds the client's declared `AudioFormat` and requires
`== CANONICAL_FORMAT`, else `AudioFormatMismatch` → **`1003`**; on success acks `{"status":"ready"}`) →
binary frame loop. **Idle timeout:** the endpoint wraps each receive in `asyncio.wait_for(...,
timeout=session.idle_timeout)`; on `TimeoutError`, `session.on_idle()` drops PTT — `wait_for` is only
the wakeup, the **decision** is the clock-injected `idle_elapsed()` (`FakeClock`-testable, no real
sleeps). Close codes: `1008` token · `1013` busy · `1003` bad format/frame · idle → normal `1000`.
`create_app` gained **`tx_idle_timeout=DEFAULT_TX_IDLE_TIMEOUT`** + an `app.state.tx_slot`; `build_app`
reads **`RADIO_TX_IDLE_TIMEOUT`** via `load_tx_idle_timeout`. `DEFAULT_TX_IDLE_TIMEOUT` is guardrail-1
**verify-on-hardware** (real PTT-tail/buffer/cadence). `MockRadio` and `audio/format.py` are
**untouched** — the `ptt` spy (`_PttSpyRadio`) lives in the test. `uv run pytest` → **283 passed, 4
skipped** (+26 tests — 12 WS-integration, 14 unit; the 4 skips are unchanged). Deferred: Opus, real
backend transmit + on-bench timing (hardware), and the **full-duplex RX-while-TX conflict policy**
(noted, not built).

Cycle 14 complete: **software squelch / activity detection** (ADR 0015) — the RX activity-gate seam
from cycle 13 is now filled with a real detector, mock-only. A new **`radio_server/activity/`**
package sits **below `rx`** (imports only `..audio` + `..backends`, never `rx`) so the same activity
signal is reusable — later it feeds scan's stop decision, not just the RX stream. **`frame_rms`** is
the pure, shared energy primitive (RMS of a canonical s16le frame via numpy; empty/odd-byte → `0.0`,
never raises). Two gates implement the one `(AudioFrame) -> bool` shape, picked by backend/config
(mirroring scan's busy-poll question): **`AudioLevelGate`** is software VAD with **hysteresis** (open
on the higher `on_threshold`, hold on the lower `off_threshold`, so a marginal signal doesn't chatter)
and **hang** (stay open `hang` s after the level drops so a speech gap doesn't clip — clock-injected,
`FakeClock`-testable, no real sleeps); construction fails loud if `on <= off`. **`CatBusyGate`** reads
the V71's hardware squelch over `status().busy` and **ignores the frame** (the noted interface tension:
it needs the radio at construction, not just the frame) — the only option for the busy-line-less
Baofeng is audio VAD. **`build_rx_gate(env, radio)`** selects via **`RADIO_SQUELCH`** (`off` | `audio`
| `cat`, fail-loud on anything else); **default `off`** returns the cycle-13 `pass_through_gate`
**unchanged** — the intended per-backend mapping (V71→`cat`, Baofeng→`audio`) is documented, not
hardcoded (auto-derive from capabilities is deferred). `create_app` gained an optional
**`rx_gate=pass_through_gate`** flowed into `RxPump`; `build_app` computes it from the env. VAD
thresholds/hang (`DEFAULT_VAD_ON_RMS`/`OFF_RMS`/`HANG`, env `RADIO_VAD_*`) are guardrail-1
**verify-on-hardware** — real noise floor and speech-gap timing are bench-tuned. `rx/`, `scan/`, and
the backends are untouched. `uv run pytest` → **257 passed, 4 skipped** (+19 model-free tests; the 4
skips are unchanged). Deferred: TX ingest (15), Opus, real capture + real threshold tuning (hardware),
scan rewire.

Cycle 13 complete: **RX audio streaming** (ADR 0014) — the **first half of the voice relay**, mock-
only. Received audio now leaves the box: a binary WebSocket **`GET /audio/rx`** streams raw canonical
PCM (48k/s16le/mono) via `send_bytes` — a **separate socket** from the cycle-10 `/events` JSON
stream, sharing only its `?token=` auth plane (rejected pre-`accept()` with `WS_1008`). A new
**`radio_server/rx/`** package holds the transport: **`AudioHub`** is the audio sibling of
`EventHub` but **bounded + drop-oldest** (each subscriber gets a bounded queue; on overflow `publish`
evicts the oldest frame so the live stream stays near-real-time) — a slow/stuck listener drops frames
without ever blocking the pump or other listeners. **`RxPump`** is a thin async loop over the
synchronous `receive()` (the `ControllerRunner` shape) that publishes each **live** frame's PCM; it
is **demand-driven** (`start()` on the first `/audio/rx` subscriber, `await stop()` on the last) and
**controller-independent**. It takes an injectable **`RxActivityGate`** predicate (default
`pass_through_gate`) — the **squelch seam only**; real software squelch/VAD is cycle 14. Distinct
from the gate, the pump **skips empty (0-byte) frames** (a transport sanity rule). `start()` sets
`running` synchronously and is idempotent; `stop()` nulls its task ref **before** awaiting the cancel
(a reconnect-during-teardown starts fresh, not stalled); a **lifespan shutdown handler** also stops
the pump — the real no-leaked-task guarantee. `MockRadio` gained a scriptable RX sequence
(**`rx_frames`** ctor arg + **`script_rx(*frames)`**, drained FIFO by `receive()` before falling back
to `canned_rx`) — the RX mirror of `tx_log`. RX cadence/buffering (`DEFAULT_RX_POLL` > 0,
`DEFAULT_AUDIO_QUEUE_MAXSIZE`) are guardrail-1 **verify-on-hardware** config. `uv run pytest` →
**238 passed, 4 skipped** (+11 model-free tests; the 4 skips are unchanged).

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

The **entire software tower is now built and runs live end-to-end on the mock**, both halves of the
voice relay stream — receive (cycle 13, squelched cycle 14) and transmit (cycle 15) — and the
**half-duplex conflict between them is now arbitrated** (cycle 16: TX takes the radio, RX + scan
stand down and resume). **Cycle 16 was the last pure-software cycle.** What remains needs the box
(or is optional polish):

- **Optional software polish (mock-testable).** The `/events` **"suspended" marker** — surfacing
  the arbiter's mode to listeners on `/events` when RX pauses/resumes — is a cheap observability add
  cycle 16 deferred (the behavior is delivered; the marker is a nicety). **Opus/compression** on
  `/audio/rx` and `/audio/tx` remains a noted-not-built option for constrained links. And the RX
  pump is still a **second** `receive()` reader — consolidating it with the controller's reader (one
  capture fanned to both) is a bring-up decision (and would let the arbiter also gate the controller
  reader, not just the pump).
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
