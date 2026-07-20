# 0102 — AIOC playback pacer: no blocking audio writes on the event loop

Status: Accepted

## Context

The D-STAR crossband decode was A/B-proven perfect with `backend="mock"` and unintelligible
("clicking", fragmented, long FM tail after unkey) with `backend="baofeng"` — same DV Dongle, same
decode pipeline (ADR 0098), same gateway/reflector/stream, on a bench where every other suspect had
been eliminated live (cold-booted dongle, healthy DVAPs, stable DCS link, 0% packet loss).

The mechanism is structural. `AiocBaofeng.transmit` writes to a `sounddevice.RawOutputStream` —
a **blocking** call that back-pressures at the playback rate — and it is called **synchronously on
the event-loop thread**: `bridge._reflector_to_rf` → `_play_ambe` → `_emit_rx_pcm` →
`TxSession.feed` → `radio.transmit`. `_key_on` additionally blocks the caller for the whole 0.5 s
TX lead-in write. Parking the loop at playback rate:

- starves the bridge's `_rx_queue` (bounded drop-oldest), discarding inbound AMBE frames — the
  exact-zero dropout holes ADR 0098 characterized as the clicking;
- paces the DV Dongle decode FIFO to the audio clock instead of the chip's own cadence;
- delays the browser hub publish (same loop), so the "listen" path clicks identically — which is
  what made the fault masquerade as a decode/dongle problem;
- **freezes every watchdog that lives on the loop** (the ADR 0092 `_rx_watchdog`, the idle checks),
  re-opening the stuck-key class those ADRs closed;
- leaves queued device audio draining after the over ends — the drawn-out FM tail.

The repo already contains the cure for this class: the kv4p backend's `_TxPacer` (ADR 0082,
`backends/kv4p/pacer.py`) exists precisely because "the pre-pacer direct write could stall the
event loop". The AIOC backend never got the same treatment.

## Decision

Give the AIOC backend a TX pacer of its own: **all `RawOutputStream` writes move to a per-keying
daemon writer thread; producers enqueue and never block.**

1. **`_AiocTxPacer`** (private to `backends/aioc_baofeng.py`): a bounded FIFO of PCM chunks plus a
   daemon writer thread that pops one chunk at a time and performs the blocking `stream.write`.
   Unlike kv4p's pacer it needs no slot timer and no silence synthesis — the AIOC's continuous
   output stream already emits silence when idle and the blocking write itself provides the pacing;
   the thread simply lets the device clock the drain. A **parallel implementation, not a shared
   abstraction**: the kv4p pacer is an Opus-encoder-owning slot timer, this is a blocking-write
   drain — forcing one shape over both would obscure each. Chunk boundaries are preserved
   (deque of `bytes`, drop-oldest by whole chunks under a byte bound, `dropped_bytes` telemetry),
   so device writes keep the caller's frame shape.
2. **Streaming `transmit()` becomes non-blocking** — `pacer.enqueue(samples)` and return. The
   event loop is never parked by playback again.
3. **Key-up stops blocking too**: `_key_on` opens the stream, starts the pacer, asserts the line,
   then *enqueues* the 0.5 s lead-in (previously a blocking write). The atomic key-up guard
   (ADR 0093) narrows to the line-assert itself; the lead-in enqueue cannot raise.
4. **Write failure = unkey** (the ADR 0093 stranded-key guard moves with the write): if the writer
   thread's `stream.write` raises, the pacer stops, discards its queue, and invokes the backend's
   key-off — line dropped first, stream torn down best-effort. A dying audio device can never hold
   the transmitter keyed.
5. **One-shot `transmit()` keeps its blocking contract** (station ID, TTS, `/transmit` callers rely
   on "returns when the clip has been played"): enqueue the clip, `wait_drained(clip duration +
   margin)`, then key-off. Same observable semantics as before, single write owner throughout.
6. **`_key_off` keeps drop-line-FIRST** (ADR 0093), then stops the pacer **discarding** queued
   audio, then closes the stream — killing the long FM tail: unkey now silences the transmitter
   immediately instead of letting buffered audio drain.
7. **Bridge hardening, same failure family** (`dstar/bridge.py`): `_emit_rx_pcm`'s `session.feed`
   is now also guarded for `ArbiterStateError` — a stuck/contended arbiter drops the frame (counted
   in a new `rx_arbiter_conflicts` stat) instead of poisoning the drain loop with an unhandled
   raise; when the arbiter frees, the next frame keys normally (feed re-acquires per over).
   `tx_stats()` additionally reports the **arbiter's** mode alongside the bridge's own `mode`,
   closing the observability gap where `/dstar/status` said `idle` while the arbiter ledger said
   `transmitting`.

## Consequences

- The crossband decode is no longer coupled to playback pacing: the drain loop runs at network/chip
  cadence with mock and real backends alike — the A/B delta this ADR exists to erase. The browser
  monitor hears the same stream either way.
- Unkey is immediate (queued audio discarded), and the on-loop watchdogs actually run while the
  radio plays — both regressions of the blocking design.
- New failure surface: the writer thread. Bounded by design — daemon thread, bounded queue,
  write-failure → unkey, bounded `stop()` join.
- Tests model the previously untestable case: a clock/event-paced blocking `FakeOutputStream`
  proves streaming `transmit()` does not block, unkey discards, write failure unkeys, and chunk
  order/boundaries are preserved. Existing lead-in/ordering tests are updated to drain the pacer
  before asserting (`wait_drained` is part of the test surface via the one-shot path).
- The `receive()` side still blocks ~20 ms on the loop (ADR 0029 known limitation) — out of scope
  here, unchanged.
