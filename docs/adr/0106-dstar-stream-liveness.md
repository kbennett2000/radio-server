# 0106 — Over liveness follows the stream, not the silence (the phrase-chopper)

Status: Accepted

## Context

With ADR 0105 deployed the crossband became intelligible for the first time — and its counters
measured what was still wrong. One bench session: **2136 rx_frames (~43 s of speech) split into
9 overs (~4.7 s each), with 27 `rx_dropped_busy` despite zero TX**, heard as "very choppy on both
outputs, chops are longer bits than before". Two structural causes in `_reflector_to_rf`:

1. **Over liveness followed decoded content (ADR 0097).** Only frames passing the `rx_gate`
   (deployed VAD: on 500 / off 300 RMS, 0.5 s hang) fed the `TxSession`, so a talker's pause —
   or quiet decoded audio flapping the VAD — stopped refreshing the idle deadline, and after
   `tx_hang` (deployed 2.0 s) the over was cut *mid-stream, while DATA frames were still
   arriving*. Every phrase boundary became a cut.
2. **After a cut, arriving DATA frames were discarded** (`mode != "rx"` → `rx_dropped_busy`,
   the mystery 27) until the gateway's next ~0.5 s re-header re-latched — so each cut cost the
   decode pipeline's stranded tail (the idle/timeout sites used the bare no-flush `_end_rx`),
   up to ~0.5 s of binned voice, and a fresh 0.5 s FM lead-in eating the next phrase's start.

Also missing: any visibility into upstream loss. DSRP DATA carries a sequence (0..0x14
superframe wrap) that was never checked, so reflector-path packet loss (Starlink) was
indistinguishable from local cutting.

The safety architecture this must not weaken (the stuck-key incident): stream death must unkey
promptly (ADR 0091), a wedged decode must be watchdogged (ADR 0092), and content that never ends
must be bounded (ADR 0097).

## Decision

1. **Liveness = frame arrival.** `TxSession.touch()` (new; stamps `_last_active` iff keyed) is
   called on every accepted DATA frame. A pause keeps the carrier up with silence — normal
   repeater behavior. `idle_elapsed()` now means "frames stopped arriving, or the drain loop is
   parked in a wedged decode" (touches happen in the loop, so a parked loop starves the deadline
   and the ADR 0092 watchdog still fires). The gate still governs *keying/feeding*: a session
   only opens on the first gate-passing frame, so dead air never keys a fresh over.
2. **Content silence gets its own, longer bound.** The bridge stamps the last gate-pass; a keyed
   over with no gate-passing decode for `dstar.dead_air_seconds` (default 10 s, tunable,
   0 disables) is cut. With `max_over` (60 s) and the TOT above it, the lost-end-bit/garbage
   cases stay bounded — just never at speech-pause timescale.
3. **A still-flowing stream re-latches from its next DATA frame.** `_last_rx_stream_id`
   survives mid-stream cuts (idle / stream-quiet / dead-air / watchdog); a DATA frame with that
   id while idle reopens the over immediately (`rx_relatches`) instead of waiting for a
   re-header. The id is cleared on a genuine end-bit and on teardown, so trailing frames of a
   finished stream can never ghost-key a new over.
4. **Every live-path cut flushes the decode tail.** The loop-top idle/over-cap/dead-air cuts and
   the queue-timeout cut use `_flush_and_end_rx` (it no-ops when nothing is open). The sync
   safety paths (watchdog, teardown, `_force_unkey`) keep the bare `_end_rx`.
5. **The cut ledger + sequence continuity land in `/dstar/status`:** `rx_idle_cuts`,
   `rx_stream_cuts`, `rx_dead_air_cuts`, `rx_relatches`, and `rx_seq_lost` (missing frames from
   DSRP seq discontinuities, end frame excluded, continuity kept across re-latches). One INFO
   line per over close: cause, frames, seconds, reheaders, seq-lost. Any remaining chop is now
   localizable numerically: `seq_lost` high ⇒ frames never arrived (network); cuts/relatches
   climbing ⇒ the bridge fragmented a live stream; all-zero with one over per key-up ⇒ look
   downstream of the bridge.

## Consequences

- One key-up = one over even with natural pauses; no more discarded inter-phrase frames; the FM
  lead-in happens once per key-up.
- A keyed-but-silent talker now holds the FM carrier (with silence) until the end-bit, dead-air
  bound, or stream death — the deliberate trade back toward normal repeater semantics; the
  stuck-key protections remain as layered bounds (tx_hang stream death → dead_air content
  silence → max_over ceiling → TOT).
- A cut caused by TX takeover can re-latch after TX ends if the inbound stream is still flowing
  (same id, frames arriving) — the desired behavior; a finished stream cannot (end-bit clears
  the id).
- `rx_seq_lost` finally separates the reflector/network question from the local one (guardrail
  1: measured on the live gateway, not assumed).
