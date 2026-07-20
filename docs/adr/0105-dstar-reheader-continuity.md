# 0105 — Same-stream re-headers must not cut the over (the 12-fragment shredder)

Status: Accepted

## Context

With the full fix stack deployed (ADR 0101 registration, 0102 pacer, 0103 re-auth, 0104 stop
budget), the crossband still failed on the bench in a new, sharply-measured way: **one** ~5-10 s
key-up on the D-STAR radio arrived at module A as **12 overs totalling 186 frames** (`rx_overs`
increments only when a HEADER packet is accepted, so this is proof of ~12 headers for one voice
stream). The same stream heard via the other DVAP (B↔C) was clean and continuous, and `/dstar/status`
showed `rx_dropped_busy: 0` with zero decode errors logged — nothing "failed"; the bridge's state
machine did exactly what it was told.

What it was told is wrong: the g4klx gateway **re-sends the stream header periodically mid-stream**
(late-join resync — a normal HB/DSRP behavior), and the bridge's HEADER handler unconditionally
closed any open over (`_end_rx()`) and opened a new one. Every re-header therefore:

- closed the `TxSession` → **unkeyed the radio** → the ADR 0102 pacer (correctly) discarded its
  queued audio;
- re-keyed → enqueued a fresh **0.5 s TX lead-in** — longer than the ~0.7 s fragment itself, so the
  FM carrier transmitted **almost pure lead-in silence** (bench: "FM keys and transmits but nothing
  comes through", crisp unkey);
- closed the decode stream via the no-flush path, stranding the pipeline's ~`DECODE_LATENCY` tail
  frames per cut — 12 cuts shredded the browser audio into "chopped syllables" (186 of ~400+ frames
  emitted).

A USB-bus-contention theory was tested first and disproven (the DV Dongle was moved off the AIOC's
hub; no change — the move is kept as hygiene).

## Decision

1. **Track the inbound stream identity.** DSRP packets carry a session id; the bridge records it
   when an over opens (`_rx_stream_id`).
2. **A HEADER with the same session id while an over is open is a re-header: absorb it.** No
   `_end_rx`, no re-key, no new decode stream — the session, pacer queue, and pipeline continue
   untouched. Counted as `rx_reheaders` in `tx_stats()`/`/dstar/status` so the cadence is visible.
3. **A HEADER with a different session id ends the old over with the tail flushed** —
   `_flush_and_end_rx()` instead of the bare `_end_rx()` — so a genuine talker change no longer
   strands the previous stream's last frames. (`_flush_and_end_rx` no-ops when nothing is open,
   so the idle-path behavior is unchanged.)
4. `_end_rx` clears the stream id: a later header of the same id after an idle/end cut legitimately
   re-latches as a fresh over.

## Consequences

- One key-up = one over: a single key-cycle, a single lead-in, continuous decode — the fragmentation
  mechanism observed on the bench is structurally impossible.
- Talker changes on a busy reflector still hand over promptly (different id → flush + new over),
  now without losing the old stream's tail.
- A pathological gateway that reused a session id for a *new* stream without an end-bit would be
  absorbed into the old over — the idle watchdog/over-cap (ADR 0091/0097) still bound it, and the
  end-bit path is unchanged. Judged acceptable against the observed, repeatable damage.
- `rx_reheaders` gives the bench a direct measurement of the gateway's re-header cadence
  (guardrail 1: verified against the live gateway rather than assumed).
