# 0030 — Buffer received audio before decoding DTMF in the live controller

Status: Accepted

## Context

The live controller loop (ADR 0013) decodes DTMF one `receive()` frame at a time:
`Controller.step(now, rx_audio)` calls `DtmfInput.pump(rx_audio, now)`, which runs the decoder over
exactly that one frame. On the mock this is fine — a test hands `step()` a frame already carrying a
full `synth_dtmf` tone. On real hardware it is not: the AIOC delivers ~20 ms audio blocks
(`blocksize=960` at 48 kHz), and `multimon-ng` needs ~40–200 ms of continuous tone to lock onto a
DTMF pair. So a keyed digit is spread across ~2–10 short frames, each too short to decode, and the
live controller decodes **nothing** — over-the-air TOTP auth cannot work even with a secret and
callsign configured.

This was flagged as a known limitation during the cycle-30 DTMF bring-up. The diagnostic tool
`python -m radio_server.doctor --dtmf` already sidesteps it: its `collect_dtmf` **accumulates ~0.5 s
of received audio into one chunk before decoding**, and de-duplicates a held tone (multimon re-emits
a digit for as long as the key is held, and a tone can straddle a chunk boundary). That buffering was
bench-confirmed by the operator to decode real keyed digits off the UV-5R. The controller needs the
same treatment; the logic already exists and is proven, it is just trapped inside the doctor script.

## Decision

- **Introduce `BufferedDtmfInput`** (in `radio_server/audio/dtmf.py`) with the same public surface as
  `DtmfInput` — `pump(frame, now) -> list[str]` — but stateful: it accumulates frame bytes into a
  buffer and only runs the decoder when the buffer reaches a **fixed ~0.5 s window** (`window_bytes`).
  It applies **held-tone de-dup**: consecutive identical decoded digits within/across windows are
  collapsed, and a **silent window resets the run** (a gap = the next same key is a fresh press). It
  exposes `flush(now)` to decode a partial tail. This is a straight lift of `collect_dtmf`'s loop body
  into a reusable class — no new behavior.
- **The controller uses it unchanged.** `build_controller` constructs a `BufferedDtmfInput` in place
  of the raw `DtmfInput`; `Controller.step`'s `self._dtmf.pump(rx_audio, now)` call is identical.
  Because `step()` still runs every poll, the station-ID periodic check and the inactivity-timeout
  close keep ticking regardless of whether a decode window filled — only the *decode* is buffered.
- **`doctor --dtmf` is refactored onto the same core**, so there is exactly one accumulate-and-dedup
  implementation, exercised by both the controller-auth tests and the existing `collect_dtmf` tests.
- **The window is config** (`dtmf.buffer_seconds`, marked default `0.5`, "verify against hardware" —
  guardrail 1), converted to `window_bytes` via `CANONICAL_FORMAT`.

### Alternative considered — a persistent streaming multimon process

Piping the continuous RX stream into one long-lived `multimon-ng -a DTMF` process would be more robust
to tones split across window boundaries (multimon keeps its own detector state). It was **deferred**:
it means managing a long-running subprocess (spawn, backpressure, drain, restart on death) inside the
loop, a materially larger change, when the fixed-window accumulator is simple and already
bench-proven. If boundary splits prove troublesome on the air, this is the upgrade path.

## Consequences

- **~0.5 s of added decode latency per digit.** This is well inside the 3 s inter-digit framer
  timeout (`dtmf.timeout`), so a hand-keyed code does not fragment into multiple entries. Auth is not
  latency-sensitive.
- **Repeated adjacent digits need a brief pause.** Because a held tone collapses to one keypress, two
  genuinely-repeated digits in a code (e.g. `...55...`, common in 6-digit TOTP codes) only register as
  two presses if a silent window separates them. Documented as an operator note in the bring-up guide.
- **A rare boundary split fails safe.** If a tone straddles a window edge and decodes as the wrong /
  duplicated digit, the assembled TOTP code simply fails `verify_and_burn` — a **rejected** auth,
  never a false accept (the code is wrong, and burning is unaffected). The caller re-keys. Fails safe
  in the direction that matters (guardrail 4).
- **One source of truth.** The doctor and the controller now share `BufferedDtmfInput`, so the tool
  the operator uses to validate decode is literally the same code path the live server runs.
