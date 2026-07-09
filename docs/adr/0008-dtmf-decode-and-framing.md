# 0008 — DTMF decode + framing grammar: decoder protocol seam, terminator/clear/timeout, synth fixtures

Status: Accepted

## Context

Since cycle 2 the auth layer has been fed **already-decoded digit strings**: ADR 0003's
`AuthGate.on_dtmf(digits, session, now)` takes `"123456"`, not audio — "the piece that
connects audio to auth" was named as future work in every handoff since. Cycle 5 (ADR 0006)
pinned the canonical audio format and built `to_multimon`, the resample edge to
`MULTIMON_RATE`, whose `soxr` VHQ anti-alias filter was explicitly chosen so a downsample
"can't fold high-frequency energy down into the 697–1633 Hz DTMF band." Cycle 6 (ADR 0007)
made the *outbound* content real (CW ID). This cycle closes the **audio-in** half: received
`AudioFrame` audio → decoded DTMF digits → framed entries → the existing, unchanged
`on_dtmf`. After it, the whole use case runs on the mock end-to-end.

Nothing in auth, session, dispatch, or `station_id` changes — this is purely the new
audio-in → digits seam plugged in front of `on_dtmf`.

Guardrail 1 (verify hardware facts empirically) is central here. `multimon-ng`'s exact
invocation flags and its raw-input sample rate are installed-build facts; the binary is not
even present in the software-cycle environment. So the real decoder is wrapped behind a
protocol seam a fake can satisfy, the real-decode test is gated on the binary, and the flags
are marked config — not asserted.

## Decision

- **New module `radio_server/audio/dtmf.py`, two concerns kept distinct.** Decode (audio →
  digit chars) and framing (digit chars → entries) are separate classes, as the cycle
  requires, but co-located because both are the DTMF seam. The module imports **no** auth
  code — it defines a local `Clock = Callable[[], float]` alias rather than importing auth's —
  so the dependency arrow keeps pointing audio → nothing-above-it. `__init__.py` re-exports the
  public surface following the existing alphabetized `__all__`.

- **`DtmfDecoder` is a one-method `runtime_checkable` protocol** (`decode(frame) -> str`,
  mirroring `IdEncoder`). This is the seam: `MultimonDtmfDecoder` is the real subprocess
  wrapper; a `FakeDtmfDecoder` drives tests (and the end-to-end) without the binary. The
  return is the digit characters found in the frame, in order — possibly empty, possibly
  several.

- **`MultimonDtmfDecoder` shells out over stdin, no temp files.** `decode` runs
  `to_multimon(frame)` (the ADR 0006 anti-aliased edge) and pipes the raw `s16le` PCM to
  `multimon-ng -a DTMF -t raw -`, parsing the `DTMF: <key>` lines from stdout. Because
  multimon-ng reads **raw PCM on stdin**, no WAV container is needed anywhere in the pipeline —
  fixtures need no on-disk assets. A missing binary raises a `RuntimeError` with an install
  hint (fail loud, not a silent empty decode).

- **The multimon invocation is marked config, not an asserted fact (guardrail 1).**
  `MULTIMON_ARGS`, `DEFAULT_MULTIMON_BIN` (`RADIO_MULTIMON_BIN`), and the existing
  `MULTIMON_RATE = 22050` all carry a "verify against the installed build" note. The real
  decode is proven by a single test that is `skipif`-gated on `multimon-ng` being on `PATH`:
  it runs where the binary is installed and skips cleanly where it is not.

- **Framing grammar: `#` submits, `*` clears, inter-digit timeout discards a partial.**
  `DtmfFramer` is pure and clock-injected. A run of digits between terminators is one entry;
  `#` emits the accumulated buffer as one `on_dtmf` payload (an empty buffer emits nothing);
  `*` cancels a partial. The inter-digit timeout is the automatic counterpart to `*`: if the
  gap since the last key reaches `timeout`, the partial is **abandoned** before the next key is
  handled. "Clears" (manual) and "closes" (timeout) both mean discard — a half-entered TOTP
  code is useless, so dropping it beats auto-submitting a guaranteed `REJECTED`. Timeout is
  applied lazily on `feed`, with a `tick(now)` for a future real polling loop; both are exactly
  testable with a fake clock.

- **`DtmfInput` composes decoder + framer and stays auth-free.** `pump(frame, now)` decodes a
  frame, feeds each char through the framer, and returns the entries that completed. The
  consumer feeds those to `on_dtmf` — so the gate call, and the entire auth/session/dispatch
  layer, is untouched. This preserves the "auth unchanged" invariant and the layering.

- **Synth fixtures are the deterministic baseline.** `synth_dtmf(digit, …)` sums two
  `synth_tone` frames at the key's standard low/high pair (`DTMF_FREQS`, the fixed telephony
  standard). `synth_tone`'s raised-cosine ramp (ADR 0006) keeps the mix click-free; a small
  `_mix` sums the int16 PCM as int32 and clips to full scale. Default per-tone amplitude `0.4`
  keeps the sum inside full scale for a clean spectrum. Output is deterministic, so a fixture's
  two tones are asserted directly by FFT — no binary, no external assets. An unknown key fails
  loud (mirrors `cw._morse_for`).

- **DTMF-tuning config follows the marked-default convention.** `RADIO_DTMF_TIMEOUT`
  (default `3.0` s) and `RADIO_MULTIMON_BIN` (default `multimon-ng`) use the established
  `*_ENV_VAR` + `load_*(env)` pattern; the timeout loader shares `load_id_interval`'s policy
  (default when unset, fail loud on a set non-numeric/non-positive value). These are UX/build
  preferences, not confirmed hardware facts.

## Consequences

- The stack now runs end-to-end on the mock: a fake decoder drives fixture audio → framed
  digits → TOTP `ACCEPTED` → authed `"1"` `COMMAND` → a genuinely **CW-ID'd** time
  announcement in `mock.tx_log`. This is the first full audio-in → answer path, and `on_dtmf`,
  the dispatcher, `StationId`, and `CwId` are all reused unchanged. Full suite green (143
  passed, 1 skipped — the real-decode test — for `test_dtmf.py`).
- The decode is behind a protocol seam, so the FastAPI/controller cycle can swap
  `MultimonDtmfDecoder` for the fake and vice-versa without touching call sites; the framer is
  reusable for any single-key stream.
- No new runtime dependencies: `synth_dtmf`/`_mix` build on `numpy` and cycle 5's `synth_tone`;
  the decoder uses stdlib `subprocess`. `multimon-ng` is an external system binary, not a
  Python dep.
- **Scope limits, deliberate:** no real recorded-WAV fixtures (synth is the asset-free
  baseline; a real recording can't be verified without the binary anyway); the framer treats
  `A`–`D` as ordinary data keys; a partial that never sees another key or a `tick` is not
  discarded until one arrives (no background timer in the mock).
- **Still ahead before RF, and empirical:** real weak-signal / HT-flutter DTMF decode
  robustness and the exact multimon-ng flags/rate are hardware bring-up checks, confirmed only
  with the binary installed and a radio in hand; `VoiceId` (real piper TTS); and wiring the
  decode+framer into a real controller/API loop that pumps `radio.receive()` and calls
  `on_dtmf` (this cycle stops at returning framed entries).
