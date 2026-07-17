# 0054 — Native in-process DTMF decode (Goertzel), additive third mode

Status: Accepted

## Context

DTMF decode is the audio → digits seam that carries over-the-air login and every keyed service. Two
modes exist today (ADR 0008 → 0030 → 0038), both built on **`multimon-ng`**, an external binary:

- `streaming` (default): one persistent `multimon-ng -a DTMF -t raw -` process fed the continuous RX
  stream (ADR 0038).
- `buffered`: a fixed ~0.5 s accumulation window over a per-window `multimon-ng` invocation (ADR 0030).

`multimon-ng` has been load-bearing but costly:

- **It has no official Windows build.** That single fact is why real-radio use on Windows is routed
  through WSL2 (install.md; ADR 0053's Windows posture). It is the last hard blocker on a native
  Windows install.
- **It is a subprocess with a pipe**, and that shape has cost three ADRs of workaround: **0030** added
  0.5 s buffering because multimon needs ~40–200 ms of continuous tone to lock; **0038** switched to a
  *persistent* process because per-window re-decode + de-dup could not tell one held tone from two
  presses (`99#` logged out as `9#`); **0040** moved the blocking `stdin.write`/`flush` off the event
  loop after it stalled the RX task and cut browser audio.
- **It cannot be exercised in CI here.** Every real-decode test is gated
  `@pytest.mark.skipif(shutil.which("multimon-ng") is None, ...)`, so the decode path itself is only
  covered on a machine that happens to have the binary installed.

Constraints found in the tree:

- **The decode seam is already pluggable.** `DtmfStream` (`radio_server/audio/dtmf.py`) is a
  three-method protocol (`write(pcm)` / `read()` / `close()`); `StreamingDtmfInput` composes *any*
  `DtmfStream` with the pure `DtmfFramer`. A second `DtmfStream` implementation slots in with no change
  to framing, auth, sessions, or dispatch.
- **`write()` receives 22050 Hz s16le mono.** `StreamingDtmfInput.pump` resamples the canonical 48 kHz
  frame to `MULTIMON_RATE` (`to_multimon`, ADR 0006) *before* `write()`. A new stream inherits that
  rate for free.
- **The tone table is a fixed telephony standard**, already asserted as `DTMF_FREQS` (697–1633 Hz).
- **`soxr` is a core dependency** (ADR 0006), so an in-process resampler needs no new package.

## Decision

Add a **third** decode mode, `DECODE_MODE_NATIVE = "native"`, backed by a new in-process
`GoertzelStream` — a second `DtmfStream`, no subprocess and no pipe. **`streaming` stays the default on
every platform; nothing is removed.** This is additive.

- **`GoertzelStream` (`radio_server/audio/dtmf.py`).** Fully in-process and synchronous — a per-block
  Goertzel/DFT tone detector is cheap enough (one 16×205 complex matmul per ~26 ms block, ≈39
  blocks/s) to run inline in `write()` without a thread, so ADR 0040's "never block the event loop"
  rule is met by being fast rather than by offloading. No pipe means none of multimon's window/latency
  workarounds apply.
  1. **Decimate 22050 → 8000 Hz with a *stateful* `soxr.ResampleStream`.** 8 kHz + **N = 205** is the
     canonical DTMF block where the eight tones land on clean bins. The resampler is carried across
     `write()` calls (never re-windowed), so a digit spanning a block boundary is never split.
     **Quality is `HQ`, not the `to_multimon` edge's `VHQ`** — deliberately. `VHQ`'s long filter
     buffers ~150 ms before emitting any output for a short chunk, which would delay the terminating
     `#` of a code unacceptably in real time; `HQ` holds back <15 ms (under one block) while its
     anti-alias filter still protects the 697–1633 Hz band from the 4–11 kHz fold. `VHQ` remains
     correct for `to_multimon`, a one-shot whole-frame edge where latency is irrelevant.
  2. **Per-block classification (a Q.24-style validity gauntlet).** Max-magnitude bin per group
     (low/high); accept only if forward twist ≤ 8 dB and reverse twist ≤ 4 dB, the winning bin
     dominates the rest of its group, second-harmonic energy is rejected, and both tones clear an
     absolute energy floor (which also floors talk-off — full-scale noise decodes to nothing).
  3. **Onset/gap state machine → held tones emit once, genuine repeats emit twice** (the property ADR
     0038 bought with a persistent subprocess). A digit emits after a short *onset* run of stable
     blocks and cannot re-emit until a *drop-out* run (silence or a different digit) re-arms it. State
     is carried across `write()`.
  4. **`write()`** resamples + decodes inline and appends recognized keys to an internal buffer;
     **`read()`** drains that buffer (mirroring `MultimonStream.read`); **`close()`** is a no-op.

- **The detector parameters are marked, tunable defaults — not asserted facts (guardrail 1.)** Rate,
  block size, `HQ`, the energy floor, the twist/dominance/second-harmonic ratios, and the onset/gap
  block-counts are all module constants carrying a VERIFY-AGAINST-HARDWARE note. They were calibrated
  in software to reproduce ADR 0038's empirical multimon table (below), which is the oracle for this
  cycle — not derived from RF.
  - The table's **"two 9s @ 30 ms gap → 99"** row is what pins the onset/drop-out counts to **one**
    block each: a 30 ms gap is sub-block (block ≈ 25.6 ms), so a ≥2-block re-arm could not see the gap
    and would collapse the pair to `9`. Reproducing multimon here means single-block onset/release,
    with the per-block validity gauntlet (not onset counting) carrying noise immunity.

- **Wiring — explicit three-way dispatch, kept in lockstep.** Both selection sites (the live
  controller `build_controller`, and the `doctor --dtmf` diagnostic) branched `!= buffered → streaming`;
  each becomes an explicit mode branch so `native → StreamingDtmfInput(GoertzelStream(), framer)`.
  `native` is added to the `DECODE_MODES` tuple the config coercer validates against.

### Alternative considered — keep the persistent multimon subprocess

Rejected as the *only* option, not on its merits: multimon is a mature decoder with real-RF tuning this
software cycle cannot match, so it stays the default and stays shipped. But it cannot run on native
Windows and cannot be tested in CI, and those are exactly the gaps `native` fills. Making `native`
additive keeps multimon's field-proven behavior available while a software-testable path exists
alongside it.

### Alternative considered — run Goertzel directly at 22050 Hz

Rejected to keep the canonical 8 kHz / N = 205 bin math where all eight tones sit on clean bins; at
22050 Hz the same ~26 ms block gives coarser resolution (≈38 Hz bins vs the 73 Hz min tone spacing),
eroding the group-dominance test. The `HQ` decimation costs <15 ms of latency, which the terminating-`#`
timing tolerates.

## Consequences

- **A native decode path with no external binary**, covered by `tests/test_native_dtmf.py` with **no
  `skipif` gate** — the first DTMF decode tests that run unconditionally in CI. They reproduce ADR
  0038's table exactly (held `9` → `9` at 500 ms and 1500 ms; two `9`s → `99` at 30 ms and 80 ms gaps;
  `9 9 #` → `99#`; `1 5 5 #` → `155#`), round-trip all 16 keys, prove a boundary-straddling digit isn't
  split, and prove full-scale white noise emits nothing.
- **Verify-on-hardware, per guardrail 1 (open item).** Talk-off on real voice and weak-signal /
  HT-flutter robustness versus multimon on real RF are **unproven** here — synthetic tones and noise
  are not RF. The absolute energy floor in particular trades weak-signal sensitivity against the
  sub-block gap detection above, and its right value is a level/AGC fact of the installed cable. The
  intended settlement is to replay recorded RF audio (ADR 0020) through both `native` and `streaming`
  and compare; the marked constants are the tuning surface.
- **The default does not change.** `native` is opt-in via `dtmf.decode_mode` /
  `RADIO_DTMF_DECODE_MODE`. Flipping the default to `native` (and, eventually, retiring the multimon
  dependency and its WSL2-on-Windows routing) is a **later cycle**, gated on that A/B holding.
- **Out of scope, recorded so it stays small:** no multimon removal, no default flip, and no Windows
  packaging (opus.dll, device enumeration, serial DTR-on-open) — each a separate cycle. Docs-facing
  mention of `decode_mode = native` belongs to the docs cycle, not this one.
