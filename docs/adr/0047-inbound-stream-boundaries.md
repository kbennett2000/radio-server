# 0047 — Inbound stream boundaries: `Link.receive()` → `AudioFrame | StreamEdge | None`

Status: Accepted

## Context

Direction three — `link.receive()` → `radio.transmit()`, a peer on the network keying the local
transmitter — is the next cycle and the dangerous one. This cycle removes one protocol ambiguity that
cycle would otherwise have to *guess* through, and does nothing else. Nothing here calls
`radio.transmit()`, `ptt()`, `TxSession`, `TxSlot`, the arbiter, or `TxLimiter`.

Cycle 49 (ADR 0044) solved the symmetric problem **outbound**: a gate open/close is a *stream boundary*,
`transmit(AudioFrame)` alone cannot express it, and inferring boundaries from frame gaps is guesswork —
so `Link.stream(on)` was added (the network mirror of `Radio.ptt`) and `StreamEdge.START/END` was born,
recorded as an amendment to ADR 0041.

**Inbound had the same problem and no answer.** `Link.receive()` returned `AudioFrame | None`, and `None`
is ambiguous between two states that demand *opposite* transmitter actions:

- **"no data this poll"** — jitter, packet loss, a mid-stream gap → the transmitter must **HOLD PTT**.
- **"the peer stopped talking"** — an M17 EOT → the transmitter must **UNKEY NOW**.

A backend that infers the difference from frame gaps is guessing, and the cost of guessing wrong is
concrete: too eager and every dropped packet chops the transmission; too lazy and every over ends with a
`tx.idle_timeout` tail. M17 signals this explicitly — an LSF at the start of a transmission, an EOT at
the end. Throwing that signal away at the `Link` boundary and re-deriving it downstream would be a
self-inflicted wound. So this cycle carries it up through the protocol, reusing the vocabulary cycle 49
already created rather than inventing a second one.

## Decision

- **`receive()` widens to `AudioFrame | StreamEdge | None`.** One call, four possible returns:
  - `AudioFrame` — a frame of stream audio.
  - `StreamEdge.START` — a peer began transmitting (an M17 LSF).
  - `StreamEdge.END` — the peer stopped (an M17 EOT).
  - `None` — nothing right now. It says **nothing** about stream state (jitter, loss, a mid-stream gap,
    or a quiet channel); it is **not** a boundary. Only `StreamEdge.END` ends a stream.

- **Reuse `StreamEdge` — it is now the shared vocabulary in both directions.** Outbound it is the marker
  `stream()` writes into `MockLink.tx_log`; inbound it is a `receive()` return. `START`/`END` mean the
  same thing (open/LSF, close/EOT) on both sides. No second type — the symmetry is the point.

- **The contract the next cycle depends on** (pinned here, while free — the transmit cycle is written
  against exactly this):
  - **Edges are the backend's job.** A backend with a native boundary signal (M17's LSF/EOT) forwards it;
    a backend without one **synthesises** the edges itself and says so in its ADR. The ambiguity is
    resolved at the backend and is **never** pushed up to the consumer.
  - **`START`..`END` brackets a stream.** Frames arriving outside a bracket are a backend bug, not a case
    the consumer must handle.
  - **An unpaired `START` is real and must be survived.** A peer can vanish and a connection can drop, so
    the backend does **not** promise a matching `END`. The consumer must not deadlock waiting for one:
    `tx.idle_timeout` is the backstop for exactly this, and the transmit cycle wires it (key on `START`,
    unkey on `END` *or* on idle-timeout).

- **`LinkPump` ignores edges this cycle.** The inbound *listening* tier (ADR 0043) fans `receive()`
  frames to `/audio/link` browsers; it does not key a transmitter and so needs no boundaries. It now
  publishes only `AudioFrame` frames and drops both `None` and `StreamEdge` — an `isinstance(frame,
  AudioFrame)` guard, replacing the old `frame is not None` check that would have raised on an edge (a
  `StreamEdge` has no `.samples`). No second endpoint and no boundary event: the browser tier does not
  need stream edges, and adding a channel for them now would be speculative.

- **`MockLink` scripts edges and gaps with no network.** Its RX queue widens from frames to
  `AudioFrame | StreamEdge | None`, so a test can script the exact sequence `receive()` returns:
  `START`/frames/`END`, an unpaired `START` (no `END`), frames with no leading `START`, and a mid-stream
  `None` gap. An explicit scripted `None` (a jitter/loss poll) is distinct from the drained-queue
  `canned_rx` idle fallback — the non-empty-queue check returns the scripted `None` as-is.

## Consequences

- The transmit cycle can drive PTT from an unambiguous signal: key on `START`, hold across `None`, unkey
  on `END`, with `tx.idle_timeout` covering an `END` that never comes. No frame-gap inference anywhere on
  the inbound path.
- `StreamEdge` is now bidirectional; ADR 0041 is amended (not superseded) to record the inbound use
  alongside the outbound `stream()`.
- **Scope limits, deliberate:** no call to `radio.transmit()`, `ptt()`, `TxSession`, `TxSlot`, the
  arbiter, or `TxLimiter` (direction three, next cycle); no real M17/mrefd socket or Codec2; no UI. No
  new config key (no settings-canary bump, no `radio.toml.example` change).
- **Still ahead:** direction three — drive `TxLimiter` (ADR 0045) from the `link.receive()` →
  `radio.transmit()` path, keying/unkeying on these edges with the idle-timeout backstop — then the real
  M17/mrefd network backend behind `create_link`. Each its own ADR + PR.
