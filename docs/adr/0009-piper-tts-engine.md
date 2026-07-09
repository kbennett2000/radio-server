# 0009 — PiperTts engine: voice-config-driven rate, the to_canonical playback edge, model-config fail-loud

Status: Accepted

## Context

Every service has spoken through `StubTts` (cycle 3): its `render(text)` returns a
deterministic `AudioFrame(b"<audio:…>")` that embeds the text, so `tx_log` is exactly
assertable and the whole auth → dispatch → CW-ID path stays byte-checkable. Cycle 5 (ADR
0006) pinned the canonical 48k format and built **two** resample edges at the tolerant
software boundaries: `to_multimon` (48k→22050, the decode edge, first exercised by cycle 7's
DTMF) and its symmetric mirror `to_canonical` (native→48k, the *playback* edge). Until now
`to_canonical` had **no consumer** — nothing produced non-canonical audio to bring back up.

This cycle ships `PiperTts`, the first real spoken audio, behind the existing cycle-3
`TtsEngine` protocol. Its load-bearing role beyond swapping the stub is that it is the first
consumer of `to_canonical`: piper emits PCM at the voice's native rate, which `render`
resamples up to canonical — so this cycle *proves the playback edge*, closing the pair with
cycle 7's decode edge.

Guardrail 1 (verify hardware facts empirically) governs the whole engine. Neither piper nor
`onnxruntime` nor a voice model is present in the software-cycle environment, exactly as
`multimon-ng` was absent in cycle 7. So the piper build is treated the same way: the real
engine is isolated behind a seam a synthetic fake can satisfy, its tests are `skipif`-gated,
and its output is property-asserted rather than byte-asserted.

## Decision

- **`PiperTts` implements the existing `render` contract — a genuine drop-in.** The shipped
  `TtsEngine` protocol is `render(text) -> AudioFrame`, and the time service calls
  `ctx.tts.render(text)`. `PiperTts.render` implements exactly that, so swapping `StubTts`
  for `PiperTts` changes nothing above the TTS layer — the dispatcher, `StationId`, and
  `CwId` are untouched. The cycle spec named the method "synthesize"; it maps onto the
  protocol's `render` here, the same reconciliation ADR 0007 made when the instruction's
  `encode(callsign, format)` met the one-arg `IdEncoder`. The protocol is not widened.

- **The voice's native rate is read from its `.json` sidecar, never hardcoded.** piper
  voices ship a `<model>.onnx` plus a `<model>.onnx.json` sidecar whose `audio.sample_rate`
  is the model's output rate — and voices vary (some 22050, some 16000). `PiperTts` reads
  that rate at construction (`_read_voice_rate`) and wraps piper's raw int16 output as
  `AudioFrame(raw, AudioFormat(rate, …, 1))` before `to_canonical`. Assuming 22050 would
  silently pitch-shift a 16000 Hz voice; reading it fails loud on a malformed/absent rate
  instead.

- **`to_canonical` is the playback edge, and it is the only rate math.** `render` is just
  `to_canonical(AudioFrame(piper_raw, native_format))` — the ADR 0006 `soxr` VHQ resampler
  brings native-rate speech up to canonical 48k so nothing downstream ever sees another
  rate. This is the symmetric mirror of cycle 7's `to_multimon` downsample.

- **One installed-build seam: `_synthesize_raw`.** The *only* code that imports piper or
  calls its API lives in `_synthesize_raw`, carrying a **VERIFY-AGAINST-INSTALLED-BUILD
  (guardrail 1)** marker: the piper package version and the exact
  `PiperVoice.load(...)`/`synthesize(...)` shape are installed-build facts, not asserted
  here. Everything else — path/sidecar validation, the rate read, the `to_canonical`
  assembly — is model-free, so a test subclass overrides that one method to drive the real
  `render` path with a synthetic voice buffer at a chosen rate, no piper and no model
  required. The import is lazy (piper is heavy and optional); a missing `piper`/`onnxruntime`
  raises a `RuntimeError` with an install hint, mirroring `MultimonDtmfDecoder`'s fail-loud
  on an absent binary.

- **Model config fails loud — no baked-in default voice.** `RADIO_TTS_VOICE` names the
  `.onnx` path and has **no default**, following `load_totp_secret` (a required fact, not a
  marked-default preference like CW WPM): `load_tts_voice` raises when it is unset/empty, and
  `PiperTts.__init__` raises when the `.onnx` or its sidecar is missing or the sidecar has no
  valid rate. A TTS engine configured without a real model must refuse to start, never
  silently emit nothing. Because validation runs before any piper import, these fail-loud
  paths are testable with no piper installed.

- **piper is an optional extra, not a core dependency.** `[project.optional-dependencies]
  tts = ["piper-tts", "onnxruntime"]` declares the dependency (discoverable, installable via
  `radio-server[tts]`) while keeping the core install and CI lean and model-free — the same
  posture as `multimon-ng` being an out-of-band system binary. piper's exact version is left
  unpinned per guardrail 1.

- **Neural output is property-asserted, never byte-asserted; `StubTts` is retained.** Real
  speech is not a deterministic function of its input the way the stub is, so the real-engine
  tests assert *properties* — canonical format, plausible nonzero duration, and (wired into
  the time service) a single canonical over with the CW ID prepended ahead of the speech —
  not exact bytes. `StubTts` stays exactly as-is so the cycle-7 end-to-end keeps its precise
  `tx_log` assertion; `PiperTts` is purely additive.

## Consequences

- The `to_canonical` playback edge now has a real consumer and is proven end-to-end: a
  synthetic 16000 Hz (and 22050 Hz) voice buffer resamples to a `CANONICAL_FORMAT` 48k frame
  of the expected length, driven with no model. Together with cycle 7's `to_multimon`, both
  resample edges from ADR 0006 are now exercised. Full suite green (152 passed, 3 skipped —
  the two real-engine `PiperTts` tests plus cycle 7's real-decode test).
- Swapping `StubTts` → `PiperTts` at the composition root is a one-line change with nothing
  else touched; the time service, dispatcher, and station-ID path are unchanged.
- No new **core** runtime dependencies: `PiperTts`'s model-free paths use only stdlib
  (`json`, `pathlib`) and the existing `to_canonical`. piper/onnxruntime arrive only via the
  optional `tts` extra.
- **Scope limits, deliberate:** the ID stays CW this cycle — `PiperTts` speaks *service*
  audio (the time), not the station ID. There is no streaming/chunked playback (piper output
  is joined into one frame), no voice caching across processes, and no phonetic spelling.
- **Still ahead before RF, and empirical:** `VoiceId` — a second `IdEncoder` that speaks the
  callsign through this engine, with the phonetic/"niner" spelling map and `StationId`
  encoder-selection (CW vs voice) — is the next cycle. Then the FastAPI layer, the scan
  engine, and the two real hardware backends. Whether piper speech is *intelligible* through
  a real SignaLink/AIOC path and radio, and the exact installed piper version/API, are
  hardware/installed-build bring-up checks this software cycle cannot prove.
