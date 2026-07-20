# 0098 — The reflector→RF decode must stream through the AMBE2000 pipeline in order (fix the garbage decode)

Status: Accepted

## Context

The first bench bring-up of the module-A crossband (2026-07-20) decoded real off-air D-STAR AMBE (an
Icom ID-51A relayed via DVAP → ircDDBGateway → DSRP) into **garbage, not voice** — in the browser
monitor AND on FM, so the fault is the **decode**, not the FM path (the stuck-key it also triggered is a
separate concern, fixed in ADR 0097).

It is **not** a bit-format problem. A source-cited review of the G4KLX reference (DummyRepeater
`DVDongleController.cpp` / `DV3000SerialController.cpp`, DStarRepeater `DVAPController.cpp`,
`DStarScrambler.cpp`, `AMBEFEC.cpp`) established that the DVAP firmware already de-scrambles /
de-interleaves / FEC-corrects, so the 9-byte AMBE off DSRP is raw codec form and is fed to the AMBE2000
**verbatim** — exactly as `radio_server/vocoder/frames.py` does. Adding a transform would *break* a
correct path.

The real cause is that **the AMBE2000 decode is pipelined and the per-frame driver mishandles it**:

- `DVDongleVocoder.decode(ambe_k)` (`vocoder/dvdongle.py`) resets **single-value** reply slots in
  `_exchange_once`, writes the decode pair, then blocks for the *next* PCM the reader dispatches. On a
  pipelined chip that PCM belongs to a frame ~L ticks earlier, so it returns `pcm_{k-L}`; and because the
  slot keeps only the latest value, a burst of pipeline replies between exchanges **drops frames**.
- `bridge._play_ambe` keys each returned frame immediately as frame k. `doctor.py` already documented the
  latency as session-variable (0–18 frames); the `--vocoder-loopback` self-test only passes because it
  decodes contiguously and lag-aligns *in the metric*, never keying per-frame.

## Measurement (bench, DV Dongle A602RQNI, decode-only, no keying — 2026-07-20)

A prime/marker/flush decode stream (`[30×NULL_AMBE][16×tone-AMBE][30×NULL_AMBE]`) recovered L as the
frame index at which the marker emerges, minus the prime length; repeated across idle gaps and after a
session reset. Findings:

- **Byte path correct:** `NULL_AMBE` decodes to RMS ~0 (silence) — the standard external frame is handled.
- **Latency L ≈ 5 frames (100 ms)**, ranging **4–6** across eight idle-gap runs (min 4, max 6, σ 0.83) —
  far tighter than the documented 0–18 worst case, but **not perfectly constant** (±1 frame).
- **Dominant fault = frame DROPOUTS:** the decoded tone showed exact-zero holes mid-tone
  (`…11580 11580 0 0 11580…`) — the single-value slots lose bursty pipeline replies. This irregular
  mid-stream frame loss (not the ~100 ms lag) is what scrambles a multi-second over.
- A control-only `REQ_STOP`/`REQ_START` reset yields L=5 but is **fragile** (it threw a reader-thread
  error); a full `_recover()` yields L=5 cleanly. So a per-over session reset is **not** worth its risk.

## Decision

Add an ordered, streaming decode path and route the crossband through it; keep L a measured bench fact.

1. **A streaming-decode seam on the `Vocoder`** (`vocoder/base.py`, `vocoder/dvdongle.py`):
   `open_decode_stream() -> DecodeStream` with `decode(ambe) -> list[AudioFrame]` (0..n **in-order**
   frames, empty while priming), `flush() -> list[AudioFrame]` (drain the tail at over end), and
   `close()`. Inside `DVDongleVocoder` the stream uses an **ordered FIFO** of decoded PCM (never a
   single-value slot), so no pipeline reply is dropped or reordered. The legacy per-frame `decode()` /
   `encode()` stay for `--vocoder-loopback` / `--dstar-echo`.
2. **Fixed prime/flush, no per-over reset.** Because the measurement showed L small and only ±1 variable,
   and dropouts (not lag) are the dominant fault, the stream discards a fixed prime and flushes a fixed
   tail of `decode_flush_frames` frames (marked tunable default ≈ 8, ≥ observed max L; guardrail 1) rather
   than a fragile `STOP/START` reset or a live marker-sync. The ±1 lag is an inaudible 20 ms, and the
   pre-over pipeline content is keepalive silence (harmless). Marker-sync stays a documented fallback if
   the bench shows boundary artifacts.
3. **Bridge wiring** (`dstar/bridge.py`): open a decode stream on the inbound HEADER, feed each AMBE
   through it in `_play_ambe` (then the existing `to_canonical` → `dstar_rx_hub.publish` → `_rx_gate`
   (ADR 0097 preserved) → `session.feed` path for each yielded frame), and drain `flush()` through the
   same path in `_end_rx`. A fresh stream per over neutralizes keepalive residue.
4. **L stays a measured bench fact, not a hardcoded one** (guardrail 1). This cycle measured it with a
   decode-only bench script (the prime/marker/flush method above; radio-server stopped so the dongle is
   free — never keys). Folding that script into a versioned `doctor --vocoder-latency` subcommand (+ a
   pure `latency_metrics` helper) for repeatable re-measurement is a documented fast-follow; the value
   `DEFAULT_DECODE_LATENCY_FRAMES = 8` is the measured max (6) plus margin and is a marked tunable.

## Consequences

- The crossband decodes in order with no dropped frames; a fixed ~160 ms flush drains the real tail so the
  end of each over is not truncated. Constant ~100 ms latency is acceptable for crossband.
- Fakes model the pipeline (a `PipelinedFakeVocoder` / pipelined `FakeDongle` with FIFO depth L) so the
  fix — N frames in → N frames out, in order, regardless of L — is unit-tested without hardware; the
  current zero-latency `FakeVocoder` could not catch this.
- **This does not re-enable the crossband.** It stays disabled on the live radios; re-enable is gated on a
  joint dummy-load re-proof (operator watching) per the ADR 0091–0094 / 0097 rules — now with an added
  no-keying step: confirm intelligible audio through the decode→`dstar_rx_hub` listen path before any TX.
- Scope: reflector→RF decode only. Encode/RF→reflector, the browser paths, and DVAP are untouched.
