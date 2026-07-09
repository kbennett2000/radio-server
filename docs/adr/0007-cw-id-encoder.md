# 0007 — CW station-ID encoder: Morse table, PARIS timing, sidetone/WPM config

Status: Accepted

## Context

Cycle 4 (ADR 0005) built the station-ID *scheduler* `StationId` and the one-method
`IdEncoder` protocol, transmitting through a deterministic placeholder `StubId` that emits a
symbolic `AudioFrame(b"<id:AE9S>")` so `tx_log` stays exactly assertable. Cycle 5 (ADR 0006)
pinned the canonical audio format and shipped `synth_tone` — genuine click-free PCM —
explicitly as "the substrate CW ID (cycle 6) keys on and off." Both ADRs named the real CW
encoder as one of the last gates before hardware.

This cycle fills that gate: `CwId`, the first **real transmission content** the server
produces. It implements the existing `IdEncoder` so the swap is drop-in — `StationId`,
`Dispatcher`, and the config loaders are untouched, per ADR 0005's promise that "nothing above
this layer changes when a real encoder lands." Per guardrail 5 (Part 97), automatic station ID
is required controller behavior; this makes that ID audible Morse instead of a stub.

Guardrail 1 (verify hardware facts empirically) applies to the audio *content* parameters:
CW speed and sidetone pitch are operator preferences, safe as marked-default config — but
whether the keyed CW is actually *readable* over the air is an empirical bring-up check, not
something this software cycle can prove.

## Decision

- **New module `radio_server/services/cw.py`, beside `StubId`/`IdEncoder`.** CW ID is a
  service-layer encoder that happens to consume an audio primitive, so it lives with the
  `IdEncoder` contract and the `load_*` config convention it follows, not in the audio layer.
  It imports `synth_tone` from `..audio`. `station_id.py` stays focused on scheduling.

- **Morse table: A–Z, 0–9, and `/`.** `MORSE` maps each character to a dit/dah string.
  Callsigns are alphanumeric, so the alnum set is the required minimum; `/` (portable/rover
  indicator, `-..-.`) is cheap and common on IDs, so it is included. Lookup normalizes to
  uppercase (Morse is case-insensitive). A space separates words (see timing) rather than being
  a table entry.

- **Unknown character fails loud.** `cw_timeline` (via `_morse_for`) raises `ValueError` naming
  the offending character and callsign rather than silently skipping it. A callsign that keys
  out *wrong* — or drops a character — is a worse Part 97 outcome than a loud failure that
  stops an obviously-misconfigured station from transmitting a bad ID.

- **PARIS timing model, pure and isolated.** `unit_ms(wpm) = 1200 / wpm` is the dot-unit
  length. `cw_timeline(text, wpm)` decomposes a callsign into an ordered list of
  `(on, duration_ms)` segments using the standard element timing — dit = 1 unit, dah = 3,
  intra-character gap = 1, inter-character gap = 3, inter-word gap = 7 — with **no leading or
  trailing gap** (gaps appear only *between* elements/characters/words). Keeping the timing as a
  pure function over a symbolic on/off list means PARIS conformance and a known callsign's
  dit/dah/gap sequence are asserted exactly, with no PCM decoding.

- **`CwId.encode(callsign, format=CANONICAL_FORMAT)` keys `synth_tone` along the timeline.**
  Each `on` segment renders `synth_tone(tone_hz, duration_ms, format, amplitude=…)`; its
  raised-cosine on/off envelope (ADR 0006) is what keeps every element from clicking. Each
  `off` segment renders a **canonical-zero silence frame** (`bytes(n · frame_bytes)`, same
  rounding as `synth_tone`), so concatenation via `AudioFrame.__add__` stays format-identical —
  no `AudioFormatMismatch`, and total duration is exactly the sum of the per-segment sample
  counts. Output is deterministic (no RNG), so it is exactly assertable and `StationId`'s
  `id + audio` prepend is safe by construction.

- **The `format` parameter reconciles the cycle-6 shape with the one-arg protocol.** The
  instruction specifies `encode(callsign, format)`, but the shipped `IdEncoder.encode` is
  single-argument and `StationId` calls it with one argument (and must not change). Resolution:
  `format` is an **optional** parameter defaulting to `CANONICAL_FORMAT`, so `StationId`'s
  one-arg call is unaffected and `isinstance(CwId(...), IdEncoder)` still holds
  (`runtime_checkable` checks method presence, not arity). The protocol itself is not widened.

- **WPM and sidetone are marked-default config (guardrail 1).** `RADIO_CW_WPM` (default `20`)
  and `RADIO_CW_TONE_HZ` (default `600`) follow the established `*_ENV_VAR` + `load_*(env)`
  convention, sharing `load_id_interval`'s policy: return the default when unset, but fail loud
  on a *set* value that is non-numeric or non-positive. Unlike the callsign (legally required,
  no default), these are operator preferences — safe defaults, not confirmed hardware facts. WPM
  and sidetone are injected into `CwId` at construction, not passed per call.

- **On-air readability is explicitly empirical.** 20 WPM at a 600 Hz sidetone are reasonable
  software defaults, but whether the keyed CW is *copyable* through a real SignaLink/AIOC audio
  path and radio — envelope shaping, level, actual keying speed on air — is a hardware bring-up
  observation. This cycle proves the encoding is correct (PARIS timing, right elements, right
  format); it does not and cannot prove the RF result is readable.

## Consequences

- `StubId` → `CwId` is a genuine drop-in: no change to `StationId`, `Dispatcher`, or any config
  loader. The end-to-end tests show an authed `"1"` now prepends real keyed CW to the time
  announcement, with cycle-4 scheduler behavior (first-over ID, no within-interval repeat)
  unchanged. Full suite green (131 tests; +21 for `test_cw.py`).
- The pure timing layer (`unit_ms`, `cw_timeline`) is public and reusable — a later `VoiceId`
  or a CW-decode/self-test path can assert against the same on/off model.
- No new runtime dependencies: `CwId` builds entirely on cycle 5's `synth_tone` and `numpy`.
- **Scope limits, deliberate:** the Morse table is the callsign alphabet (alnum + `/`), not full
  ITU punctuation; sidetone is a single sine (no prosign concatenation, no Farnsworth spacing).
  A later cycle can widen these intentionally rather than by accident.
- **Still ahead before RF:** `VoiceId` (real piper TTS), wiring `begin_session`/`check`/
  `sign_off` to the real session lifecycle and a periodic task (ADR 0005), DTMF decode, and the
  empirical hardware bring-up where the marked-default WPM/sidetone are confirmed on air.
