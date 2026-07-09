# 0015 — Activity detection: RMS + hysteresis + hang, CAT-busy vs audio-VAD behind one gate

Status: Accepted

## Context

Cycle 13 (ADR 0014) streams received audio out over `/audio/rx`, but every frame goes on the wire —
the `RxActivityGate` is a `pass_through_gate` no-op, the **seam** for real squelch. So a listener
hears a constant hiss of dead air, and the pump relays even when nobody is talking. This cycle fills
the seam with the real **activity detector**: decide "is this frame live audio worth streaming" vs
silence, so listeners get speech, not noise.

There are two independent sources of truth for "is there a signal," and which applies depends on the
backend — the same split the scan engine's busy-poll question raised (ADR 0012):

- **V71 (CAT):** the radio has a real hardware squelch. Read it over `status().busy` (rigctld). The
  radio already decided; the frame content is irrelevant.
- **Baofeng (AIOC):** no busy line at all. Software squelch on the audio level is the **only** option
  — measure the frame's energy and threshold it.

Both must live behind the **one** `(AudioFrame) -> bool` gate the pump already injects, so the
backend/config picks the implementation without the pump knowing. And the detector must be **reusable,
not welded to `rx/`**: the same activity signal should later feed scan's stop decision, so it belongs
below the RX transport, not inside it. Everything stays mock-only — pure signal processing on
canonical PCM (48k/s16le/mono) plus a clock-injected hang timer, no hardware, no real sleeps. The
threshold and hang **values** are exactly the bench-tuned facts the project doc warned about ("squelch
too low relays noise / stops scan immediately; too high clips speech"), so they ship as marked
verify-on-hardware defaults, not asserted constants.

## Decision

- **A new `radio_server/activity/` package, below `rx`.** It holds the detector and imports only
  `..audio` (RMS on `AudioFrame`) and `..backends` (the CAT busy read) — never `rx`. Its gates
  structurally satisfy the `RxActivityGate` protocol (`(AudioFrame) -> bool`) without importing it,
  so the dependency arrow stays `activity -> {audio, backends}` and `scan` can reuse the same
  primitives later without pulling the RX transport. `build_rx_gate`'s one `off` branch reaches for
  `rx`'s canonical `pass_through_gate` via a **local** import, keeping the module graph clean.

- **`frame_rms(frame)` — the pure, shared energy primitive.** RMS amplitude of a canonical (s16le)
  frame in int16 units via `np.frombuffer(..., "<i2")` (the `audio.tone`/`resample` idiom); empty or
  odd-trailing-byte frames read as `0.0` rather than raising, so a stray non-PCM frame can't crash the
  pump. This is the reusable signal both the VAD gate and (later) scan can share.

- **`AudioLevelGate` — software VAD with hysteresis and hang.** *Hysteresis:* the threshold a frame is
  compared against depends on the current state — the higher `on_threshold` to **open** a closed gate,
  the lower `off_threshold` to **hold** an open one. A level between the two neither opens nor closes,
  so a marginal signal on the boundary doesn't chatter. *Hang:* when the level drops below the
  off-threshold the gate stays open until `hang` seconds after the last above-threshold frame, so a
  gap between words doesn't clip the stream. Timed against an injected clock (`time.monotonic` default,
  as `ScanEngine` uses) so the hang is exactly testable with a `FakeClock` — no real sleeps.
  Construction fails loud if `on_threshold <= off_threshold` (hysteresis is not optional).

- **`CatBusyGate` — the hardware squelch behind the same gate.** `__call__` **ignores the frame** and
  returns `radio.status().busy`, exactly as the scan engine reads it (`_read_busy`). The design tension
  the one interface papers over, named plainly: unlike the audio gate, this one needs the *radio* at
  construction, not just the frame. That's acceptable — the gate is a closure over its source of truth.

- **Config-selected, not hardcoded to a backend.** `build_rx_gate(env, radio)` picks via `RADIO_SQUELCH`
  (`off` | `audio` | `cat`, fail-loud on anything else): `off` → `pass_through_gate`, `audio` →
  `AudioLevelGate` from the `RADIO_VAD_*` thresholds/hang, `cat` → `CatBusyGate`. The intended per-backend
  mapping (V71 → `cat`, Baofeng → `audio`) is **documented, not wired as a silent default** — the
  guardrail's "note, don't hardcode a choice." Auto-deriving the mode from the backend's capabilities is
  a later refinement.

- **`create_app` gains an optional `rx_gate`, defaulting to `pass_through_gate`.** It flows straight into
  `RxPump(radio, hub, gate=...)`. The default keeps the DI seam and every existing test unchanged;
  `build_app` computes the gate from the environment via `build_rx_gate`. With `RADIO_SQUELCH` unset the
  default is `off`, so the wired default is byte-for-byte the cycle-13 relay-everything behavior.

## Consequences

- Dead air stops leaving the box when a deployment opts in: with `RADIO_SQUELCH=audio` a scripted mock's
  silent frames are suppressed and only live frames reach `/audio/rx` (proven end-to-end through the real
  WS), while the default `off` preserves the cycle-13 stream exactly. The V71 path gates off the radio's
  own squelch with no audio analysis.
- **The activity signal is now a reusable primitive, not welded to the pump.** `frame_rms` /
  `AudioLevelGate` live in `activity/` below `rx`, so cycle-N scan can gate its stop decision off the same
  code path — the "one interface, two implementations" shape mirroring ADR 0012's CAT-busy-vs-hardware-scan
  framing.
- Full suite: **257 passed, 4 skipped** (was 238; +19 activity tests, all model-free; the 4 skips remain
  the multimon + piper hardware/model gates).
- **Scope limits, deliberate:** scan is **not** rewired this cycle — the detector is merely made importable
  by it (its busy read is unchanged); TX audio ingest is the other direction (cycle 15); Opus/compression is
  still noted, not built; and the mode is config-selected, not yet auto-derived from backend capabilities.
- **Verify-on-hardware (guardrail 1):** `DEFAULT_VAD_ON_RMS`, `DEFAULT_VAD_OFF_RMS`, and `DEFAULT_VAD_HANG`
  are marked bring-up facts — the real noise floor, the interface gain, and the speech-gap timing are the
  exact "too low relays noise / too high clips speech" values the doc warned about, tuned on the bench. The
  mock proves the *logic* (open/close/hysteresis/hang), never the *values*.
- **Numbering / branch note:** ADR 0015 by cycle order, cut from `master` at **238 passed, 4 skipped** (the
  cycle-13 RX-audio merge point `0be21cd`). It adds a new `activity` package and touches only the cycle-10
  `api` (the new `rx_gate` param + `build_app` wiring); `rx/`, `scan/`, and the backends are untouched.
- **Still ahead before RF:** TX ingest (cycle 15), the two real hardware backends (`SignaLinkV71`,
  `AiocBaofeng`) with real capture + on-bench threshold tuning, feeding scan's stop decision off this gate,
  and the single-capture-reader consolidation.
