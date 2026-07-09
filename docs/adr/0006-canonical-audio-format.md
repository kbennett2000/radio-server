# 0006 — Canonical audio format, fail-loud frames, resample at the edges

Status: Accepted

## Context

Cycles 1–5 modeled audio as an opaque `AudioFrame = bytes` alias. That was deliberate — it
kept the protocol dependency-free while the stack above the radio (auth, sessions, dispatch,
TTS, station ID) was built against mock backends — but ADRs 0002/0004/0005 all deferred the
real rate/width/channels/endianness "to its own ADR before real audio I/O," and HANDOFF names
the audio-format ADR + real encoders as the last gate before hardware. Cycle 1 flagged the
concrete risk: opaque `bytes` "silently papers over a format mismatch until you hear garbage."

Everything downstream — DTMF decode (`multimon-ng`), real TTS (piper), real CW station ID —
needs a pinned format to target. This ADR pins it and makes it load-bearing, and adds the
first producer of *real* samples (`synth_tone`) so the type is proven by a real consumer, not
just asserted. Tone synthesis is also the substrate CW ID (cycle 6) gates on and off, so it
belongs to this foundation.

Guardrail 1 (verify hardware facts empirically): device sample rates and `multimon-ng`'s
input rate are kept as config with a marked default and a "verify against hardware" note —
never hardcoded as confirmed.

## Decision

- **Canonical internal format: 48000 Hz, signed 16-bit little-endian, mono.**
  `CANONICAL_FORMAT = AudioFormat(48000, 2, 1)` in `radio_server/audio/format.py`. Rationale:
  match the real-time sound-card edge — USB audio codecs (SignaLink, AIOC) are 48k-native —
  and resample only at the tolerant software edges. This is **pinned architecture**: it is
  independent of any specific device. A device that only does 44.1k becomes an edge-resample
  **config** change, not an architecture change. Little-endianness is expressed everywhere via
  the numpy dtype `'<i2'`, so it is host-endian independent.

- **`AudioFrame` carries its format and fails loud.** `AudioFrame` is now a frozen dataclass
  `(samples: bytes, format: AudioFormat = CANONICAL_FORMAT)`, replacing the `= bytes` alias.
  `AudioFormat` is a frozen `(rate, width, channels)` with a `frame_bytes` property. Because
  the frame is frozen it has value equality and is hashable, so existing `tx_log == [...]`
  assertions keep working unchanged. The type still lives in the lowest layer
  (`radio_server.audio`) and is re-exported from `radio_server.backends`, so the `Radio`
  protocol and its consumers keep importing it from `..backends`.

- **The fail-loud contract is format identity, on concat and on transmit.**
  - `AudioFrame.__add__` raises `AudioFormatMismatch` if the other frame's format differs (or
    it is raw `bytes`, the exact old silent-coercion trap), else returns the concatenation.
    This is what makes `StationId`'s `id + audio` prepend safe **by construction**: two frames
    can only join if they are genuinely the same format.
  - `MockRadio` gains a `format` (default canonical) and `transmit` raises
    `AudioFormatMismatch` on a frame of any other format — the mock enforces the same contract
    a real sound card imposes. This is the "mismatched transmit raises" seam.

- **The guard is format identity, deliberately NOT PCM-length divisibility.** The deterministic
  stubs (`StubTts`, `StubId`) carry a *symbolic* payload (`b"<id:AE9S>"`, odd length) in a
  canonical-format frame so `tx_log` stays exactly assertable. Enforcing "len(samples) % frame_bytes
  == 0" would break those stubs for no safety gain — the real risk cycle 1 named is combining
  *different formats*, which the identity check catches. Real encoders will carry real PCM in
  the same canonical format, so nothing above the encoder changes when they land.

- **Resample only at the edges, with a quality resampler.** `radio_server/audio/resample.py`
  wraps `soxr` (SoX resampler, VHQ). `resample(frame, target_rate)` decodes `'<i2'` → float →
  `soxr.resample` → clip → `'<i2'`, returning a new frame at the target rate. Named edges:
  `to_multimon` (48k → decode rate) and `to_canonical` (e.g. TTS-native → 48k). **Not** naive
  decimation: a downsample's anti-alias filter is what keeps out-of-band energy from folding
  down into the 697–1633 Hz DTMF tones and corrupting detection. Mono 16-bit only for now
  (raises otherwise) — that is the canonical format; wider support is a later concern.

- **`synth_tone` — the proving consumer and CW substrate.** `radio_server/audio/tone.py`'s
  `synth_tone(freq_hz, duration_ms, format=CANONICAL_FORMAT, *, amplitude=0.5, ramp_ms=5.0)`
  produces genuine canonical PCM: `round(rate·duration_ms/1000)` samples of a sine at
  `freq_hz`, with a **raised-cosine on/off envelope** (~5 ms rise/fall, auto-shrunk so the
  ramps never overlap) to avoid key clicks. Deterministic (no RNG), so exactly assertable. CW
  ID (cycle 6) keys this primitive on and off; the anti-click envelope is defined here.

- **Verify-on-hardware, per guardrail 1.** `MULTIMON_RATE = 22050` is `multimon-ng`'s
  documented input rate for raw `s16le` mono — a marked default carrying a "verify against the
  installed multimon-ng build" note, pinned as the decode-edge *target*, not asserted as a
  hardware fact. The SignaLink / AIOC device sample rates (assumed 48000, **unconfirmed**) are
  documented as verify-on-hardware; no code hardcodes them as confirmed, and a differing device
  rate is handled by edge resampling.

## Consequences

- The audio format is now **pinned and load-bearing**. A format mismatch raises
  `AudioFormatMismatch` at the concat/transmit seam instead of silently producing garbage —
  the cycle-1 risk is closed by construction, and `StationId`'s prepend is safe.
- The type change rippled into the existing stubs and their tests: `StubTts`/`StubId` now wrap
  their symbolic payload in a canonical `AudioFrame`, and `MockRadio` records/serves frames.
  All prior suites were updated and stay green (110 tests).
- `numpy` and `soxr` are the first runtime dependencies beyond `pyotp`. Both ship manylinux
  wheels (no system libraries), so `uv sync` remains hands-off.
- **Rates split into pinned-architecture vs verify-on-hardware**: 48k canonical is pinned; the
  device rates and `MULTIMON_RATE` are marked defaults to confirm against real hardware.
- **Still ahead before RF**: real encoders — `CwId` on the `synth_tone` substrate (cycle 6),
  real piper TTS — plus DTMF decode and the session-lifecycle/scheduler wiring from ADR 0005.
  All are now unblocked by this format decision; the remaining gate is those encoders and the
  empirical hardware bring-up.
- Mono-only resampling and the format-identity (not length) guard are intentional scope limits,
  documented so a later cycle can widen them deliberately rather than by accident.
