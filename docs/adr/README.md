# Architecture Decision Records

This directory holds the ADRs for radio-server. Each ADR is a short, dated record of one significant
decision and its context (ADR-first is the project convention — see [CLAUDE.md](../../CLAUDE.md)). The
table below is the index: what each ADR decided, its current status, and any supersession. It is
maintained by hand — add a row when you add an ADR.

## Read this first — two things the numbering can't tell you

- **`0001` is used twice.** [`0001-cycle-model.md`](0001-cycle-model.md) is a *process/meta* ADR (the
  headless-cycle working model, no `Status` line); [`0001-two-backend-radio-abstraction.md`](0001-two-backend-radio-abstraction.md)
  is the first *technical* ADR. They are kept as-is rather than renumbered, so existing references stay
  valid; a future tidy could renumber the process ADR to `0000`.
- **`0035` and `0036` are missing, and an earlier `0049`–`0053` never existed on disk.** Those numbers
  belonged to a reverted **M17 / mrefd / Codec2** linking arc that was rolled back wholesale (commit
  `f39a750`, "Revert cycles 41-58"). That arc is **dead**. Its live successor is the **Mumble/Murmur
  link** (ADRs 0041→0049). The current `0049` below is a completely different, live ADR that happens to
  reuse the number. **Anything in the tree referencing M17, mrefd, Codec2, libcodec2, or reflectors is
  stale and does not describe the shipped system.**

## Index

| ADR | Title | Status |
|----|----|----|
| [0001](0001-cycle-model.md) | Cycle model (headless working process) | Accepted (process ADR) |
| [0001](0001-two-backend-radio-abstraction.md) | Two-backend radio abstraction, mock-first build | Accepted |
| [0002](0002-radio-protocol-shape.md) | Radio protocol shape (shared vs CAT-only split) | Accepted |
| [0003](0003-session-auth-state-machine.md) | Session / auth state machine | Accepted |
| [0004](0004-dispatch-service-tts.md) | Command dispatch, service interface, TTS shape | Accepted |
| [0005](0005-station-id-scheduling.md) | Station ID scheduling model | Accepted |
| [0006](0006-canonical-audio-format.md) | Canonical audio format, fail-loud frames | Accepted |
| [0007](0007-cw-id-encoder.md) | CW station-ID encoder (Morse, PARIS timing) | Accepted |
| [0008](0008-dtmf-decode-and-framing.md) | DTMF decode + framing grammar | Accepted |
| [0009](0009-piper-tts-engine.md) | PiperTts engine | Accepted |
| [0010](0010-voice-id.md) | VoiceId (phonetic spoken ID) + CW/voice mode | Accepted |
| [0011](0011-api-layer.md) | HTTP/WebSocket API layer, capability gating | Accepted |
| [0012](0012-scan-engine.md) | Software scan engine (clock-driven CAT loop) | Accepted |
| [0013](0013-controller-loop.md) | Controller loop: pure `step()` over `receive()` | Accepted |
| [0014](0014-rx-audio-streaming.md) | RX audio streaming (binary WS, demand pump) | Accepted |
| [0015](0015-activity-detection.md) | Activity detection (RMS + hysteresis + hang) | Accepted |
| [0016](0016-tx-audio-ingest.md) | TX audio ingest (binary WS in, single-talker) | Accepted |
| [0017](0017-duplex-arbiter.md) | Duplex arbiter (half-duplex TX priority) | Accepted |
| [0018](0018-event-log.md) | Event log (subscriber ledger over JSONL) | Accepted |
| [0019](0019-deferred-event-instrumentation.md) | Deferred-event instrumentation | Accepted |
| [0020](0020-audio-recording.md) | Audio recording (RX to WAV, passive tap) | Accepted |
| [0021](0021-recording-safety-and-tx.md) | Recording safety rails + TX recording | Accepted |
| [0022](0022-web-ui-architecture.md) | Web UI architecture (React SPA, same-origin) | Accepted |
| [0023](0023-rx-playback.md) | Live RX playback in the browser | Accepted |
| [0024](0024-tx-mic-capture.md) | Browser TX mic capture (getUserMedia) | Accepted |
| [0025](0025-config-system.md) | Config system (schema-driven TOML + secrets) | Accepted |
| [0026](0026-settings-api.md) | Settings REST API + secret rotation | Accepted |
| [0027](0027-settings-ui.md) | Web UI settings screen | Accepted |
| [0028](0028-async-scan-runner.md) | Async scan runner | Accepted |
| [0029](0029-aioc-baofeng-bringup.md) | AIOC/Baofeng backend bring-up (audio-only PTT) | Accepted |
| [0030](0030-controller-dtmf-buffering.md) | Buffer RX audio before DTMF decode | **Superseded in part by 0038** (buffered survives as `dtmf.decode_mode="buffered"`) |
| [0031](0031-single-capture-reader.md) | One capture reader for browser + controller | Accepted |
| [0032](0032-tx-lead-in.md) | TX lead-in (silence after key-up) | Accepted |
| [0033](0033-fetch-voice-services.md) | LAN-fetch voice services (quote/battery/bible) | Accepted |
| [0034](0034-pluggable-voice-services.md) | Pluggable voice-service architecture | Accepted |
| *0035* | *(gap — reverted M17 arc)* | — |
| *0036* | *(gap — reverted M17 arc)* | — |
| [0037](0037-web-ui-simplification.md) | Web UI simplification & approachability | Accepted |
| [0038](0038-streaming-dtmf-decode.md) | Streaming DTMF decode (one persistent multimon-ng) | Accepted (**supersedes 0030's default**) |
| [0039](0039-https-secure-context.md) | HTTPS secure context (phone live audio) | Accepted |
| [0040](0040-nonblocking-dtmf-feed.md) | Non-blocking DTMF feed off the RX loop | Accepted |
| [0041](0041-mumble-link.md) | Mumble/Murmur link (bridge RF to a channel) | Accepted |
| [0042](0042-multi-mumble-servers.md) | Multiple Mumble servers, DTMF-selectable | Accepted |
| [0043](0043-ungated-link-off.md) | Link-off combo works without a login | Accepted |
| [0044](0044-retro-ham-visual-refresh.md) | Retro-ham visual refresh (Day/Night themes) | Accepted |
| [0045](0045-link-audio-correctness.md) | Link audio correctness (DTMF mute; no busy-latch) | Accepted; **DTMF-mute mechanism superseded by 0049** (busy-latch fix live) |
| [0046](0046-web-session-open.md) | Open the OTA session from the web UI | Accepted |
| [0047](0047-web-restart.md) | Restart the server from the settings screen | Accepted |
| [0048](0048-cockpit-theme-meter-scale-totp-toggle.md) | Cockpit theme, meter dB scale, TOTP toggle | Accepted (extends 0044) |
| [0049](0049-realtime-dtmf-mute-and-yield.md) | Real-time DTMF mute + Mumble→RF keying yield | Accepted (**supersedes 0045's DTMF-mute mechanism**) |
| [0050](0050-web-mumble-client.md) | Web UI as a Mumble client (browser monitor/talk on the linked channel) | Accepted |
| [0051](0051-local-service-plugins.md) | Slim the shipped service set; local service plugins | Accepted (**supersedes 0034's in-tree-only scope**) |
| [0052](0052-freetext-mumble-names-demo-server.md) | Free-text Mumble names, per-entry password, shipped demo server | Accepted (amends 0042) |
| [0053](0053-bootstrap-installer.md) | One-command bootstrap installer (`scripts/install.sh` / `.ps1`) | Accepted |
| [0054](0054-native-dtmf-decode.md) | Native in-process DTMF decode (Goertzel), additive `native` mode | Accepted |
| [0055](0055-auto-decode-mode.md) | Auto-resolve DTMF decode mode by multimon-ng availability (`auto` default) | Accepted |
| [0056](0056-mumble-on-windows.md) | Mumble link on native Windows (tarball pin + vendored `opus.dll`) | Accepted |
| [0057](0057-installer-ships-the-link.md) | Installer ships the Mumble link by default; libopus via a bundled-wheel carrier (retires the vendored DLL) | Accepted |
| [0058](0058-posix-install-script.md) | `install.sh` is POSIX sh so `curl … \| sh` runs on dash; docs↔script contract test | Accepted |
| [0059](0059-plugin-migrations-and-examples.md) | Removed services ship as `examples/`; named migration errors for `[plugins.*]` and `local_services/` ids | Accepted |
| [0060](0060-native-is-the-default.md) | Resolve `auto` to `native` unconditionally (bench-verified); multimon-ng becomes optional | Accepted (**flips 0055's preference; closes 0054's A/B for decode**) |
| [0061](0061-kv4p-uart-backend.md) | kv4p HT UART/KISS backend shape (state reconciler, first real `CatRadio`, real busy line); pure wire codec only this cycle | Accepted |
| [0062](0062-kv4p-transport-handshake.md) | kv4p serial transport: connect by syncing `DeviceState.appliedSequence` (no HELLO dependency); hold DTR/RTS low on open (no reset-to-get-HELLO) | Accepted |
| [0063](0063-kv4p-backend-capabilities-and-units.md) | kv4p `Kv4pHt` backend: complete-state reconcile; advertise `SCAN` (software sweep) but omit `SET_CHANNEL`; unit mapping (Hz↔MHz, CTCSS Hz↔index, mode↔bandwidth) fails loud | Accepted |
| [0064](0064-kv4p-firmware-repin-shipped.md) | Re-pin the kv4p firmware reference to shipped v2.0.0.1 (`3f0e809`); the audio command ID (`0x07` vs `0x0C`), not `FIRMWARE_VER`, discriminates the two v17 builds | Accepted |
| [0065](0065-kv4p-opus-codec.md) | kv4p audio edge is Opus (48 kHz, 40 ms frames), replacing the dead IMA-ADPCM path | Accepted |
| [0066](0066-kv4p-connect-running-board.md) | kv4p `connect()` re-founded on shipped firmware: passive-first → elicit-with-retransmit → restore; de-clobber the NVS (no zero-write on connect/close) | Accepted |
| [0067](0067-extras-taxonomy.md) | Extras taxonomy: factor leaves (`serial`/`soundcard`/`opus`) and compose backends (`hardware`/`kv4p`/`mumble`) so a node installs only what it uses; `opuslib` named explicitly | Accepted |
| [0068](0068-kv4p-bringup-detections-and-docs.md) | kv4p bring-up: doctor detections for pre-KISS firmware (`de ad be ef` sniff) and band-mismatch (HELLO vs `kv4p.module_type`), shipped with the user docs (new `kv4p-setup.md`, fork by radio) | Accepted |
| [0069](0069-kv4p-tx-bringup.md) | kv4p TX bring-up: doctor TX telemetry rig (`TxStats`, key-up latency, `--tx-lead` sweep); fixes doctor to read `radio.toml`; first bench numbers (`tx_lead` 0.2→**0.5**, ~230 B/frame, window blocks are backpressure) | Accepted |
| [0070](0070-kv4p-rx-sample-rate-correction.md) | kv4p RX sample-rate correction: shipped firmware clocks the RX ADC ~2% fast (`rxAudio.h` `*1.02`) but labels it 48 kHz — broke DTMF and drifts every continuous consumer. Resample the true device rate → 48 kHz at the decode edge (soxr HQ); `kv4p.sample_rate_correction` knob + doctor `--rx-level` rate readout; connect timeout 2→10 s | Accepted |
| [0071](0071-kv4p-rx-dtmf-capture-analysis.md) | kv4p DTMF still fails after 0070: stop analysing, capture it. doctor `--rx-capture` (RX→WAV) + `--analyze-wav` read the DTMF tones straight out of the audio via FFT (independent of GoertzelStream) and return a verdict — clipping (firmware 16× gain) vs off-frequency vs clean-so-decode-wiring; tightens the `--rx-level` correction verdict to 0.2% | Accepted |
| [0072](0072-kv4p-dtmf-energy-floor.md) | kv4p DTMF cause found by reproducing the real capture through the live decoder: the native `NATIVE_ENERGY_FLOOR` (0.02) is ~10× too high for received audio (measured low-tone power ~0.012), so every block read as silence. Lower it to **0.002** (talk-off stays with the scale-invariant ratio gates); adds received-level + frame-size-invariance + talk-off regressions; realigns the 0070 offset regression to a received level | Accepted |
| [0073](0073-radio-holder-seam.md) | A `RadioHolder` seam (api/holder.py): one object owns the active radio (`.radio`, built via the extracted `build_radio(settings)`) and the lifecycle of the radio-bound pipeline — `start()` constructs the `RxPump`/`ScanRunner` against it, `stop()` tears them down (drop PTT if the arbiter holds TX, stop scan, halt pump, reap controller, close radio) idempotently. Pure behaviour-preserving refactor; the keystone for later in-app backend switching (`stop(); rebuild; start()`) | Accepted |
| [0074](0074-multi-backend-config.md) | radio.toml describes more than one backend: presence-based configured set (`[<backend>]` block present + active `server.backend`), each validated at load so a broken *inactive* switch target fails at startup, not at select time. New light `api/backend_config.py` (pure `validate_backend_config`/`validate_configured_backends`/`backend_kwargs`/`configured_backends` enumeration for the next cycle's select endpoint + UI); `doctor` validates the selected backend; no schema change (example/canary unmoved). No switching yet | Accepted |
| [0075](0075-configurable-dtmf-reverse-twist.md) | Make the native DTMF decoder's reverse-twist tolerance configurable via `audio.dtmf_reverse_twist_db` (default 4.0 = today's `NATIVE_REVERSE_TWIST_DB`, threaded into `GoertzelStream`). Bench finding: the UV-5R Mini sends its low group ~6.4 dB hotter than the high, tripping the −4 dB gate so it decodes nothing while a UV-5R decodes fine. Opt-in (bump to ~10) so compliant radios keep the tight, talk-off-safe default; dominance + second-harmonic gates carry talk-off, so the wider gate stays talk-off-clean. Reverse twist only, global (not per-backend) | Accepted |
| [0076](0076-live-backend-switch.md) | The live backend switch: `RadioHolder.rebuild(new_settings)` (atomic under a lock; stop → construct via `radio_factory` → start, rebuilding the controller via a `controller_factory`; **rolls back** to the previous working backend if the target fails to open, so a switch never bricks the server) + `POST /radio/select` (validates against `configured_backends`, 409 unconfigured, 503 on rollback) + `GET /radio/backends` (current + configured list). The select handler `nonlocal`-rebinds the route closures' `radio`/`rx_pump`/`scan_runner`/`controller` (the ADR 0073-deferred "routes follow the live radio" step), re-honors RX demand, re-emits a new `capabilities` event + a status snapshot, and persists `server.backend` through the schema. Endpoint + API only; the UI dropdown is the next cycle. No schema change | Accepted |
| [0077](0077-backend-selector-ui.md) | The backend selector in the web control panel: a `BackendPanel` dropdown over `GET /radio/backends` + `POST /radio/select` (self-hides with <2 backends; tracks the live active radio so a 503 snaps back and names the radio you're still on; "Switching…"/disabled in flight; warns switching drops PTT mid-transmit). The payoff — **reactive re-greying without a reconnect**: `reduceStatus` folds the re-emitted `capabilities` event into `state.caps` and `ControlPanel` prefers it over the one-shot login prop, so the CAT tuning/scan cards appear for the kv4p and vanish for the AIOC live; the additive `disabledCaps` clears on a switch. Bootstraps Vitest + Testing Library (the frontend had no JS test runner). No server/endpoint change | Accepted |

## The live linking arc

Linking a repeater/base to a remote voice channel is done over **Mumble**, not the reverted M17 stack.
The arc is ADR **0041** (single link) → **0042** (multiple servers, DTMF-selectable, one active) →
**0043** (ungated disconnect) → **0045** (audio-correctness fixes) → **0049** (real-time DTMF mute +
Mumble→RF keying yield) → **0050** (the web UI doubles as a Mumble client). Config lives under
`[mumble]` / `[[mumble.servers]]`; see
[configuration.md](../configuration.md) and the annotated [`radio.toml.example`](../../radio.toml.example).
