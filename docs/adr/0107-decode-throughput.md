# 0107 — Decode keeps up with the stream (non-blocking drain + honest loss counters)

Status: Accepted

## Context

With ADR 0106 live (0 cuts, 0 relatches — structurally verified on the bench), a ~48 s test still
degraded from clear to unintelligible: `rx_seq_lost=331` of ~2425 frames (~14%) while module B's
dstarrepeaterd log showed the SAME reflector stream at **0.0% network loss** over 1063 packets.
The loss is radio-server's own: the bounded `rx_queue` (64 ≈ 1.3 s) drop-oldest fires because the
decode path sustains only ~43 frames/s against the stream's 50/s. The queue absorbs the deficit
for ~10 s ("starts clear"), then sheds ~14% steadily ("unintelligible by the end").

Where the ~23 ms/frame goes, from the transport's own arithmetic:

- Wire: one decode frame writes ~372 B (AMBE config + dummy audio packet) and reads ~372 B (AMBE
  echo + PCM) at 230400 baud ≈ **16.1 ms/frame each direction** (overlapped, full duplex) — tight
  (81% utilization) but inside the 20 ms budget.
- The killer: past the priming window, `_DvDongleDecodeStream.decode` **blocks for at least one
  decoded frame per call** (`_drain_decoded(block=True)`), and the reader delivers replies in
  ~27.5 ms batches (`serial.read(512)` returns on 512 bytes or the 0.1 s timeout; replies accrue
  at ~18.6 kB/s). The drain loop therefore locks to the reader's batch cadence (~20+ ms/frame),
  and the per-frame executor hop + a busy event loop (uvicorn, WebSocket audio fan-out) add the
  rest.

Also discovered: `rx_seq_lost` (ADR 0106) was counted **after** the queue, so our own drop-oldest
was booked as "upstream loss" — the counter meant to separate network from local conflated them.
And the per-over INFO log lines never reached the journal: the entrypoint never configures the
root logger, so module logs below WARNING are dropped.

## Decision

1. **The decode stream stops blocking mid-over.** `decode()` drains opportunistically
   (`block=False` always): writes go to the OS buffer (fast), decoded PCM is collected whenever
   the reader has delivered it. The wire itself paces the pipeline — input arrives at 50/s, the
   wire drains ~62/s, so the write side can never run away. `flush()` still blocks (bounded) to
   collect the tail. Mid-over stall detection is NOT lost, it moves to the layers built for it:
   a wedged write still raises (1 s write timeout → `_rx_decode_failing` → idle unkey, ADR 0106),
   and a chip that goes silent while frames flow is reaped by the `dead_air` bound.
2. **Loss counters are split at the honest boundary.**
   - `rx_seq_lost` moves to the ENQUEUE side (`_enqueue_rx`, before the queue can drop): DSRP seq
     discontinuities per stream id — true upstream loss only.
   - `rx_queue_drops` (new) counts drop-oldest events — our own backpressure, named as such.
   The post-dequeue `_track_seq` is deleted. `/dstar/status` shows both.
3. **A permanent lightweight throughput probe:** the bridge accumulates decode-call wall time and
   logs one INFO line per 500 decodes — average ms/frame and current queue depth — so the bench
   reads sustained throughput directly from the journal.
4. **The entrypoint configures root logging** (`logging.basicConfig(level=INFO)`) so module INFO
   — the ADR 0106 per-over lines, the probe, recovery notices — actually reaches journald.

## Consequences

- Sustained decode throughput is bounded by the wire (~62 frames/s), not the reader's batch
  cadence — clears 50/s with headroom. Acceptance on the bench: a 60 s continuous over with
  `rx_queue_drops == 0` and audio clean to the end.
- If the bench still shows drops, the probe names the residual cost per frame, and batching
  multiple queued frames per executor hop is the held-in-reserve next step (noted, not built —
  smallest load-bearing change first).
- `rx_seq_lost` high with `rx_queue_drops == 0` now UNAMBIGUOUSLY means upstream/network loss —
  the reflector-path investigation trigger, never a self-inflicted artifact.
- Emission becomes slightly burstier (reader batches ~1–2 frames); the AIOC pacer (2 s bound) and
  the browser hub absorb it.
