# 0072 — kv4p DTMF: the native decoder's energy floor is ~10× too high for received audio

Status: Accepted

## Context

DTMF still did not decode on the kv4p backend after ADR 0070 (sample-rate correction) and ADR 0071
(capture-and-analyse). ADR 0071 ended with a VERDICT-3 capture — the received tones were **clean and
on-frequency** — and pointed the next cycle at "decode-path wiring." The task for this cycle was
explicit: **stop analysing, reproduce it in a test, then fix from what the test shows.**

The lead going in was frame size: kv4p delivers ~1882/1920-sample RX frames, the AIOC delivers 960, and
`GoertzelStream` classifies in fixed 205-sample blocks — so maybe 40 ms frames misalign the block grid.
**That was wrong.** Reproduced against the operator's real bench capture (`cap.wav`, the 12 s WAV the
ADR-0071 `--rx-capture` wrote), the cause is a **level-calibration bug in the native decoder**, not
frame size and not kv4p-specific.

### What the reproduction showed (real numbers)

Feeding `cap.wav` (48 kHz, the corrected stream `receive()` returns) through the two paths:

1. **The analyzer reads the tones; the live decoder reads nothing.** `analyze_dtmf_windows(cap)` →
   `…1122203334444####…` (clean `1234#` repeats). The live path — `StreamingDtmfInput(GoertzelStream())`
   fed via `pump`, exactly as `doctor --dtmf`'s `_drive_dtmf` does — returns `''`.
2. **Not frame size.** Synthesized clean `1234#` decodes through `pump` at 960 / 1920 / 1882 / 705 / 441
   sample frames alike. The real `cap.wav` decodes `''` at every frame size **and** as a single
   whole-stream `write` (ruling out the per-frame stateless `to_multimon` resample seams too).
3. **Which gate fails: the energy floor, on every block.** Instrumenting `_classify` over the real
   audio: **468/468** blocks fail `NATIVE_ENERGY_FLOOR` (0.02); zero reach the dominance / twist /
   harmonic gates. In the strongest tone block the low tone (**≈937 Hz**, 941 grid) has Goertzel power
   **0.0123** and the high tone (**≈1483 Hz**, 1477 grid) **0.0281** — the low tone sits *below* the
   0.02 floor, so the block is silence and no digit emits.
4. **It is purely level.** Scaling `cap.wav` up before decode: ×1.0 → `''`, ×1.5 → `'444#…'`, ×2.0 →
   `'234#1234#1234#…'`, ×3.0 → the same clean `1234#` repeats. The audio is clean and on-frequency
   (ADR 0071 was right); it is just **quieter, relative to full scale, than the floor expects.** The
   same UV-5R decodes into the AIOC backend because that cable delivers hotter line audio that clears
   the floor.

### Root cause

`NATIVE_ENERGY_FLOOR = 0.02` is an **absolute** normalized-power threshold, calibrated to the
0.4-amplitude `synth_dtmf` test fixtures (per-tone power ≈ 0.039). Real received DTMF lands an order of
magnitude lower (measured low-tone power ≈ 0.012). Every existing DTMF test used those 0.4-amplitude
fixtures, so none could catch it — the **level** analogue of the exact-frequency blind spot ADR 0070
closed. The floor's own doc comment already flagged it as *"the single most level/AGC-dependent constant
here, so verify on hardware first"* (guardrail 1); ADR 0060 and `HANDOFF.md` both named it as the open
marked-tuning surface / the exact thing to check against RX level. The bench capture is that hardware
verification: **0.02 is ~10× too high for receiver audio.**

## Decision

Lower **`NATIVE_ENERGY_FLOOR` from `0.02` to `0.002`** (one constant, decoder-wide), and record the
empirical basis in its marked comment.

- Real received low-tone Goertzel power ≈ 0.012 → 0.002 gives ~6× headroom below real tones.
- The floor's only job is rejecting digital silence. **Talk-off / non-tone rejection is done by the
  scale-invariant ratio gates** (group-dominance 4×, twist, 2nd-harmonic 4×), which are unchanged.
  Full-scale white noise stays rejected down to 0.001 (verified across 12 seeds) because *dominance*,
  not the floor, kills broadband energy — so 0.002 keeps a 2× guard above that leak point.
- Still guardrail-1 marked (verify on hardware; the value is now backed by a real capture, not a guess).

This honours the task's framing — *"make decode independent of the backend; a kv4p-only patch papers
over a decoder that shouldn't care."* The fix is in the **decoder**; the kv4p RX path is untouched (no
host-side gain).

### Interaction with ADR 0070 (worth recording)

Lowering the floor exposed that the ADR 0070 firmware-offset regression asserted more than was true. At
the loud **0.4** fixture level, a ~2%-offset (scalloped, off-bin) tone still clears the *new* floor for
some digits (1, 4), so "uncorrected never decodes" no longer holds there. The truth: on **real received
audio — quiet *and* offset — the correction is genuinely required.** The regression now synths at a
received-level amplitude (`_RECEIVED_AMPLITUDE = 0.15`), where the offset breaks decode for all of
`1234#` and the correction restores them. Both fixes remain load-bearing; neither is redundant.

### Considered and deferred

Per-block normalization / a relative floor would make detection fully level-invariant, but it changes
the talk-off characterization (needs voice-corpus validation, not just white noise) and is not needed to
clear the real bench numbers by a wide margin. Recalibrating the marked floor is the smallest
load-bearing fix; normalization can follow if a still-quieter node ever demands it.

## Consequences

- `doctor --backend kv4p --dtmf` should now decode `1234#` off the air — the offline reproduction over
  the real capture predicts it (that same audio decodes once the floor is lowered). Operator confirms.
- Every backend benefits (the decoder was level-brittle, not kv4p-specific); the AIOC path is unaffected
  (its audio already cleared the old floor).
- New hardware-free regressions lock the two blind spots: a **received-level decode** test (quiet clean
  `1234#` must decode; would have failed at the old floor), a **frame-size-invariance** test
  (960/1920/1882/441/705 all decode the same), and a **12-seed talk-off** guard at the lower floor.
- This closes the ADR 0060 / `HANDOFF` "RX level vs `NATIVE_ENERGY_FLOOR`" item and generalizes ADR
  0070's exact-frequency blind spot to **level**.

## Bench acceptance (operator, RX only — the tool never keys)

`uv run python -m radio_server.doctor --backend kv4p --dtmf`, key `1234#` from a handheld → the digits
decode. This is the last item between a fresh board and a working node. Record the decoded digits in the
PR.

## Dominant frequencies found (recorded per the task)

From `cap.wav`: strongest tone block ≈ **937 Hz** (low group, 941 grid) + ≈ **1483 Hz** (high group,
1477 grid); the analyzer read `1234#` repeats across the 12 s capture. Per-block: 468/468 below the old
0.02 floor; strongest-block powers low 0.0123 / high 0.0281. Gain sweep ×1 → nothing, ×2 → `1234#`.
Floor sweep 0.006 / 0.004 / 0.002 all decode the capture; talk-off clean to 0.001.
