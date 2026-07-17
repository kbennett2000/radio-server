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

## The live linking arc

Linking a repeater/base to a remote voice channel is done over **Mumble**, not the reverted M17 stack.
The arc is ADR **0041** (single link) → **0042** (multiple servers, DTMF-selectable, one active) →
**0043** (ungated disconnect) → **0045** (audio-correctness fixes) → **0049** (real-time DTMF mute +
Mumble→RF keying yield) → **0050** (the web UI doubles as a Mumble client). Config lives under
`[mumble]` / `[[mumble.servers]]`; see
[configuration.md](../configuration.md) and the annotated [`radio.toml.example`](../../radio.toml.example).
