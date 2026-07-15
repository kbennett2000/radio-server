# 0031 — One capture reader feeds both the browser and the DTMF controller

Status: Accepted

## Context

On real hardware (the AIOC USB sound card) the capture is **single-open and single-reader**: each
received audio block is consumed exactly once by whoever calls `receive()` first. The server had **two
independent `receive()` loops** on the same radio:

- `RxPump` (ADR 0014) — reads `receive()` (poll 0.02 s) and fans frames to the browser `/audio/rx`
  hub and the recorder. Demand-driven by listeners.
- `ControllerRunner` (ADR 0013) — reads `receive()` then `await asyncio.sleep(controller.poll)`, with
  `controller.poll` defaulting to **0.5 s**, and runs `Controller.step` (DTMF decode → auth → dispatch
  → station-ID).

This produced two compounding failures that made **over-RF DTMF login impossible on hardware**, even
after ADR 0030 gave the controller a buffering decoder:

1. **Under-sampling.** `AiocBaofeng.receive()` returns exactly one ~20 ms block. The controller read
   one block, slept 0.5 s, and discarded the ~480 ms in between — a ~4 % duty cycle. `BufferedDtmfInput`
   then concatenated temporally-scattered 20 ms slivers into its window, so multimon never saw a
   contiguous held tone. (`doctor --dtmf` works precisely because it reads `receive()` back-to-back.)
2. **Contention.** With a browser listening, `RxPump` (~50 reads/s) and `ControllerRunner` (~2 reads/s)
   raced for blocks on the one capture; the pump won almost all of them, starving the controller
   further. Turning on "Listen" made DTMF strictly worse.

Both `rx/pump.py` and `controller/engine.py` explicitly named the missing consolidation ("one
`receive()` feeding both `controller.step` and this pump… not made here") as a deferred hardware
decision. This ADR makes it.

**Testing gap that hid this:** the ADR-0030 controller test fed a `FakeDtmfDecoder` returning whole
pre-formed entries, so it never exercised `receive()` cadence, real accumulation, real multimon, or
contention. A MockRadio emitting real `synth_dtmf` audio in 20 ms blocks through the live path with
real multimon fails on the old design and is now a permanent regression test.

## Decision

**One capture reader.** `RxPump` becomes the single reader of `receive()` and fans each frame out:

- **To the DTMF controller first, on the raw frame.** `RxPump` takes an optional `controller`; in its
  loop it calls `controller.step(clock(), frame)` on every received frame **before** the activity
  gate — so decode sees the full contiguous capture, exactly like `doctor --dtmf` and independent of
  the browser squelch (a keyed DTMF tone is full-quieting; never let the VAD swallow a code). The call
  is guarded so a controller fault can't kill the shared capture task.
- **To the browser hub + recorder, gated, unchanged.**

Because the reader is back-to-back (the small `poll` only keeps the mock from hot-spinning; on hardware
the blocking read paces it), a 0.5 s decode window fills from 25 contiguous 20 ms blocks in 0.5 s of
real time — the condition multimon needs.

**`ControllerRunner`'s independent receive loop is retired from the live path.** The class remains
(unit-tested as a standalone driver), but `build_app` no longer creates one; `controller.poll` no
longer affects DTMF.

**Lifecycle: reference-counted demand.** The reader runs while there is any demand for received audio —
a connected `/audio/rx` listener **or** an active controller. `create_app` counts both: `POST
/controller {on:true}` adds a demand (so DTMF decodes even with no browser listening), `/audio/rx`
connect/disconnect adds/drops one. The reader starts on the first demand and stops on the last. During
TX the reader already stands down via the arbiter (ADR 0017) — correct for DTMF too (can't receive
while keyed).

## Consequences

- **Over-RF DTMF login works on hardware**, with browser Listen off *or* on (one reader, no
  contention). Proven end-to-end in the mock with real multimon (`tests/test_controller_rx_e2e.py`).
- `Controller.step` now runs at the ~20 ms frame cadence instead of every 0.5 s; its ID/idle
  housekeeping is clock-driven and cheap, so more frequent ticks are harmless.
- `controller.poll` is now vestigial for DTMF (kept as config; may be removed later).
- The controller's housekeeping pauses during TX (the reader stands down) rather than ticking through
  a keyed over — acceptable and arguably more correct.
- Single source of truth: the live server and `doctor --dtmf` now share one decode path
  (`BufferedDtmfInput` + `MultimonDtmfDecoder`), so the bench tool faithfully predicts the server.
