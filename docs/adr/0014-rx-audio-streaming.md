# 0014 — RX audio streaming: a binary WebSocket transport, a demand-driven pump, and the activity-gate seam

Status: Accepted

## Context

The full software tower runs live end-to-end on the mock (cycle 12), but received audio never
leaves the box: `radio.receive()` is consumed only internally, by the controller loop, to decode
DTMF. The product's purpose is a **two-way voice relay** over the LAN, and neither direction of
audio exists yet. This cycle builds the **first half** — streaming received audio out to LAN
listeners — and stays mock-only: hardware is in transit, so this is the transport + pump, not real
capture. `MockRadio.receive()` serves scripted frames instantly; the TX ingest direction
(client → radio) and real backend capture are later cycles.

Two things must not be over-built here. Relaying *dead air* forever is wrong, but real software
squelch / VAD is cycle 14 — so this cycle lays only the **seam** (an injectable activity
predicate), not the detector. And a continuous ~96 KB/s audio stream to a slow listener must not be
allowed to blow up memory or stall the producer — so backpressure is load-bearing from the start,
unlike the cycle-10 `EventHub` whose unbounded queues are fine only because control events are rare.

## Decision

- **A binary WebSocket `GET /audio/rx`, separate from the `/events` JSON stream.** It sends raw
  canonical PCM (48k/s16le/mono) as **binary** frames via `send_bytes` — `frame.samples` on the
  wire, unmodified. It reuses the `/events` auth plane exactly (guardrail 4): read `?token=` and
  `token_matches` it *before* `accept()`, else `close(WS_1008_POLICY_VIOLATION)` — browsers cannot
  set headers on a WS handshake. It is a **distinct socket** from `/events`; audio and control are
  not multiplexed onto one connection.

- **Raw PCM on the wire; compression noted, not built.** At 48k/s16le/mono the stream is ~96 KB/s —
  trivial on a LAN — so raw PCM keeps the transport dead simple and the frames byte-for-byte
  assertable. Opus / compression is recorded here as a later option for constrained links, not
  implemented.

- **A standalone, demand-driven `RxPump` fanning out through a bounded `AudioHub`.** `RxPump` is a
  thin async loop over the synchronous `receive()` — the `ControllerRunner` shape (ADR 0013) — that
  publishes each live frame's PCM to `AudioHub`, which fans it to every subscriber. The pump owns
  its task and is **demand-driven**: `start()` on the first `/audio/rx` subscriber, `stop()` on the
  last disconnect. It is deliberately **independent of the controller**: tying it there would couple
  audio to the DTMF/auth loop being active and, worse, create a *second* independent `receive()`
  reader. `start()` is idempotent; `stop()` nulls its task reference **before** awaiting the cancel,
  so a listener reconnecting mid-teardown starts a fresh pump instead of observing a dying one. A
  **lifespan shutdown handler** also stops the pump — the real no-leaked-task guarantee, since a
  per-connection `finally` runs inside a cancelled scope and cannot reliably join.

- **Backpressure is a bounded, drop-oldest queue per subscriber.** Each subscriber's queue is
  bounded (`DEFAULT_AUDIO_QUEUE_MAXSIZE`); on a full queue `publish` **evicts the oldest frame** and
  enqueues the new one. Drop-oldest keeps a live stream near-real-time (a slow listener hears a
  glitch, not ever-growing latency); drop-newest would preserve contiguity at unbounded delay —
  wrong for live audio. `publish` is synchronous and non-blocking, so a slow or stuck listener drops
  frames **without ever blocking the pump or any other listener**. This is the deliberate
  divergence from `EventHub`'s unbounded queues.

- **An injectable `RxActivityGate` seam — the squelch hook, not the squelch.** `RxPump` takes a
  predicate `(AudioFrame) -> bool` deciding whether a frame is "live" and worth relaying; the
  default `pass_through_gate` relays everything. Real software squelch / VAD implements this same
  shape in cycle 14 without touching the pump. Distinct from the gate, the pump also **skips empty
  (0-byte) frames** as a transport sanity rule — a frame with no audio never goes on the wire, which
  is what makes an unscripted `MockRadio` (empty `canned_rx`) produce no traffic.

- **`MockRadio` gains a scriptable RX sequence.** A public FIFO frame queue (constructor `rx_frames`
  plus `script_rx(*frames)`) that `receive()` drains before falling back to the static `canned_rx` —
  the RX mirror of the `tx_log` / `busy_frequencies` public-mutable-attribute convention. This lets
  a test drive a deterministic received-audio sequence into the pump. Backward compatible: an empty
  queue is exactly the prior behavior.

- **RX cadence and buffering are guardrail-1 config, verify-on-hardware.** `DEFAULT_RX_POLL` is
  marked "VERIFY AGAINST HARDWARE" and kept **> 0** so a silent radio (empties skipped) does not
  hot-spin the event loop; the true cadence is bounded by how long `receive()` blocks and the audio
  chunk size — empirical bring-up facts. `DEFAULT_AUDIO_QUEUE_MAXSIZE` (the live-buffer depth) is the
  same kind of marked default. On the mock, `receive()` returns instantly, so neither value affects
  the tested transport logic.

## Consequences

- Received audio leaves the box for the first time: a token'd LAN client connects to `/audio/rx`
  and receives the scripted PCM frames in order, as binary; the pump fans out to many listeners with
  a slow one degrading only itself. The voice relay's receive half is in place on the mock.
- **A third fan-out lives beside `EventHub`, and the first one that needed real backpressure** — the
  bounded, drop-oldest `AudioHub` sets the pattern for any continuous stream, as distinct from the
  unbounded control-event hub.
- Full suite: **238 passed, 4 skipped** (was 227; +11 RX-audio tests, all model-free; the 4 skips
  remain the multimon + piper hardware/model gates).
- **Scope limits, deliberate:** the activity gate is a seam with a pass-through default — no real
  squelch/VAD (cycle 14); TX audio ingest from a client is the other direction (cycle 15);
  Opus/compression is noted, not built; and the pump is a *second* `receive()` reader whose
  consolidation with the controller's reader is deferred. `/audio/rx`'s handler blocks on
  `queue.get()` and detects a disconnect only on the next `send_bytes` — a mock/edge stall that
  real continuous PCM (silence is non-empty) does not hit, and the same shape `/events` accepts.
- **Verify-on-hardware (guardrail 1):** the real `receive()` chunk size, capture latency, and device
  buffering — and therefore `DEFAULT_RX_POLL` and the queue depth — are bring-up checks; the mock
  delivers scripted frames instantly. Whether the single hardware capture feeds both the controller
  and this pump from one reader (rather than two independent `receive()` loops) is a bring-up
  decision, flagged not resolved.
- **Numbering / branch note:** ADR 0014 by cycle order, cut from `master` at **227 passed, 4
  skipped** (the cycle-12 controller merge point). It adds a new `rx` package and touches only the
  cycle-10 `api` (the new WS endpoint + lifespan) and `MockRadio` (additive scripted-RX) below it.
- **Still ahead before RF:** software squelch/VAD (cycle 14), TX ingest (cycle 15), the two real
  hardware backends (`SignaLinkV71`, `AiocBaofeng`), and the single-capture-reader consolidation.
