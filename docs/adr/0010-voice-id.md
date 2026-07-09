# 0010 — VoiceId: a phonetic spoken-ID encoder over the TTS engine, and CW-vs-voice ID-mode selection

Status: Accepted

## Context

Station identification has been CW since cycle 6 (`CwId`, ADR 0007): a single `IdEncoder`
that `StationId` (cycle 4, ADR 0005) schedules through on the first, overdue, and sign-off
overs. Cycle 8 (`PiperTts`, ADR 0009) shipped real neural speech behind the one-method
`TtsEngine.render` contract and proved the `to_canonical` playback edge — but it speaks only
*service* content (the time), not the ID; ADR 0009 recorded "no phonetic spelling" as a
deliberate scope limit and named this cycle its sequel: a second `IdEncoder` that speaks the
callsign through that engine, with the phonetic/"niner" map and `StationId` encoder-selection.

This cycle ships `VoiceId` and a `RADIO_ID_MODE` (`cw` | `voice`) selector, completing the
audio-content tower: both ID paths now exist and an operator picks between them by config.
Like `CwId` before it, `VoiceId` satisfies the same one-argument `IdEncoder` protocol
(`encode(callsign) -> AudioFrame`), so **the cycle-4 scheduler is untouched** — the inclusion
rule, the forced ID, and the session sign-off ID are all identical; only which encoder
`StationId` holds changes.

Guardrail 1 governs the real path the same way it governs `PiperTts`: piper, `onnxruntime`,
and a voice model are absent in this software-cycle environment, so the real-engine test is
`skipif`-gated and property-asserted, and whether the spoken ID is *intelligible* keyed
through a real radio is an empirical bring-up check, not something this cycle proves.

## Decision

- **The phonetic map is pure, module-level, and separated from synthesis.** `PHONETIC` is a
  `dict[str, str]` (A→"alpha" … Z→"zulu", digits with the ham convention **9→"niner"**), and
  `spell_callsign(callsign)` is a pure builder that upper-cases, maps each character, and
  joins with spaces — structurally mirroring `MORSE` + `_morse_for` in `cw.py`. Keeping the
  spelled string separate from the TTS call makes the map **exactly assertable with no engine**
  (`spell_callsign("AE9S") == "alpha echo niner sierra"`), the same layering discipline that
  keeps `cw_timeline` testable without PCM.

- **Unknown characters fail loud with `ValueError`, and the accepted set matches `CwId`'s.**
  `spell_callsign` raises on any character outside `PHONETIC`, exactly as `_morse_for` does —
  a callsign the ID cannot speak is a misconfiguration to surface, not to drop. The accepted
  set is A-Z, 0-9 and `/` (the portable indicator, spoken "slash"), identical to `CwId`'s
  `MORSE` coverage, so switching ID mode never changes which callsigns are encodable. `/`→
  "slash" (rather than "stroke") is a small judgment call noted for review; the spec mandated
  only 9→"niner", so the remaining digits keep their plain English words.

- **`VoiceId` takes the `TtsEngine` at construction (dependency injection), not per call.**
  `VoiceId(tts).encode(callsign, format=CANONICAL_FORMAT)` spells the callsign and renders it
  through the injected engine. Injecting at construction makes it exact-testable on `StubTts`
  (deterministic, byte-exact) and production-ready on `PiperTts`, and lets one voice back both
  the ID and the service announcements. The optional `format` parameter honors the
  `encode(callsign, format)` shape `CwId` established (ADR 0007) while defaulting to canonical,
  so `isinstance(VoiceId(stub), IdEncoder)` holds and `StationId`'s one-argument call is
  unaffected; a `TtsEngine` always renders `CANONICAL_FORMAT`, so the engine's output is
  authoritative — the same reconciliation ADR 0009 made for the "synthesize"↔`render` name.

- **`RADIO_ID_MODE` is a marked-default selector, with CW as the safe default.**
  `load_id_mode` returns `DEFAULT_ID_MODE = "cw"` when unset/empty and raises `RuntimeError`
  on any value outside `{"cw", "voice"}` — the marked-default flavor (like `load_id_interval`),
  not the no-default flavor (like `load_callsign`/`load_tts_voice`). CW is the default because
  it has **no model dependency and always works**, so an unconfigured or model-less station
  still identifies legally. `build_id_encoder` is the ID composition root: `cw` → `CwId` with
  the configured WPM/tone, `voice` → `VoiceId` over the injected engine or a fresh
  `PiperTts(load_tts_voice(env))`.

- **Voice mode fails loud, never silently degrades to CW.** With `RADIO_ID_MODE=voice` and no
  configured voice, `build_id_encoder` (no injected engine) calls `load_tts_voice`/`PiperTts`,
  which already raise — surfacing the existing model-config failure at construction. There is
  no `try/except` CW fallback anywhere: a station asked to identify by voice must refuse to
  start rather than quietly switch modes, the same fail-loud posture as a missing model in
  ADR 0009. The optional `tts` injection on `build_id_encoder` is what lets tests select voice
  mode deterministically on `StubTts` with no model present.

- **Real speech is property-asserted; the stub path is byte-asserted.** `VoiceId` on `StubTts`
  is a pure function of the callsign, so the encoder, its protocol conformance, and the
  end-to-end auth → dispatch → voice-ID over are all **byte-exact** — `tx_log` asserts the
  spelled-callsign ID prepended to the spoken time precisely. The real-piper test asserts only
  *properties* (canonical format, nonzero, plausible duration) and is `skipif`-gated, mirroring
  the `PiperTts` and multimon tests.

## Consequences

- Both station-ID paths now exist and are config-selectable; the audio-content tower — CW and
  voice ID, DTMF decode, TOTP auth, dispatch, TTS — is complete and green on the mock. Full
  suite: **169 passed, 4 skipped** (the one real-`VoiceId` test joins the two real-`PiperTts`
  tests and the real-decode test).
- The cycle-4 `StationId` scheduler and the cycle-6 `CwId` encoder are **unchanged** — `VoiceId`
  is purely additive at the encoder seam, and `RADIO_ID_MODE` defaults to `cw`, so existing
  behavior and its exact-assert tests are untouched.
- **No new dependencies.** `VoiceId` and the phonetic map use only the stdlib and the existing
  `TtsEngine`; voice mode reaches piper only through the optional `tts` extra already declared
  in cycle 8. `build_id_encoder` is the first real composition root in the tree, wiring the
  cycle-6/8 loaders (`load_cw_wpm`/`load_cw_tone_hz`/`load_tts_voice`) together.
- **Scope limits, deliberate:** the phonetic map is fixed English NATO/ITU (no per-locale
  variants), `/`→"slash" is the only non-alnum character supported, and `VoiceId` renders one
  frame per call (no streaming) — it inherits `PiperTts`'s posture directly. Mode selection is
  by environment only; there is no runtime API to flip it (that arrives with the API layer).
- **Still ahead before RF, and empirical:** the FastAPI REST/WebSocket API (with the
  capability split, guardrail 3), the V71-only scan engine, and the two real hardware backends
  (`SignaLinkV71`, `AiocBaofeng`) — the "plug it in, it keys up clean" phase. Whether the
  spoken ID is intelligible through a real SignaLink/AIOC path, and the exact installed piper
  version/API, remain hardware/installed-build bring-up checks.
