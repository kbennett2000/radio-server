# 0108 — Decode-stream starvation alarm + the bench end-to-end self-test

Date: 2026-07-20
Status: accepted

## Context

ADR 0107 made the streaming decode drain non-blocking (`_drain_decoded(block=False)`) to unlock
the drain loop from the reader's ~27 ms batch cadence. What that change silently removed was a
safety property nobody had written down: with the old blocking drain, a decode pipeline that ate
input and returned **nothing** raised `VocoderTimeout` on the first post-priming call, latched the
stream's wedge, and failed the over loudly — and on the next over `open_decode_stream` healed the
dongle or the bridge fell back to the legacy per-frame decode. Audio could not silently vanish.

With `block=False`, that same dead pipeline returns `[]` forever: no timeout, no wedge, no log,
no audio. The bridge's other bounds don't cover it — dead-air (ADR 0106) only unkeys PTT after
10 s and does nothing within a short over, and the write path keeps "succeeding" because the OS
serial buffer accepts input regardless. The failure mode is a full over of pure nothing with
every counter looking healthy (frames arrived, 0 lost, 0 drops).

The 2026-07-20 bench hunt also exposed a process failure: three consecutive "fixed" claims were
made on layer-level evidence (routing repaired, links stable, counters clean) without an
end-to-end audio proof, while the actual dead stage moved between layers (gateway DExtra
routing → suspected vocoder → panel/UI). The missing tool was a machine-runnable end-to-end
verification that doesn't need a human keying an HT.

## Decision

1. **Starvation alarm** (`_DvDongleDecodeStream.decode`): keep the non-blocking drain, but stamp
   a progress clock whenever a drain yields audio (or the stream is still priming). If the stream
   keeps feeding past the priming window and nothing has emerged for a full `reply_timeout`,
   raise `VocoderTimeout` and latch the wedge — restoring exactly the pre-0107 loud-failure
   contract without restoring the block.

2. **Bench self-test** (`scripts/bench/dstar_decode_selftest.py`): inject a synthetic DSRP over
   into the gateway socket (any-source UDP) while counting decoded PCM on `/audio/dstar/rx`
   (published pre-gate, so silence must appear). `real` mode adds superframe re-headers and
   pseudo-random AMBE — which the AMBE2000 decodes to full-scale noise, opening the content gate
   and keying the FM crossband (`/status` shows `transmitting: true` during the run). One command
   proves reflector→decode→browser-hub→gate→FM-keying end to end, no HT, no RF receiver.

## Consequences

- A silently-dead decode pipeline now fails the over within `reply_timeout` (default well under
  a second) instead of playing nothing for its whole duration; recovery then happens at the next
  over boundary as designed (ADR 0099).
- Deploys touching `radio_server/dstar/` or `radio_server/vocoder/` get a mandatory bench step:
  run the self-test before handover ("fixed" requires an end-to-end PASS, not clean layer
  counters).
- The ADR 0107 throughput measurements should be re-read with suspicion: the old blocking
  path's failure→fallback was silent, so historical decode-cost numbers may have profiled the
  legacy per-frame fallback rather than the stream. The ADR 0107 probe (journal INFO every 500
  decodes) now answers that directly on live overs.
