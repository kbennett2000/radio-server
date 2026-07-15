# 0048 — Direction three: `link.receive()` → `radio.transmit()` (a peer keys the transmitter)

Status: Accepted

## Context

This is the highest-risk wiring in the project: **a remote peer on the network keys the licensee's
transmitter.** Everything above the radio layer has been built toward exactly this, and every piece it
needs already exists and is reviewed — this cycle *wires them* and does nothing else:

- ADR 0041/0047 — `StreamEdge` in both directions. `link.receive()` returns `AudioFrame | StreamEdge |
  None`: `START` = a peer began (M17 LSF), `END` = the peer stopped (EOT), `None` = nothing-right-now
  (never a boundary). The keying decision is thus driven by the **protocol, not inferred from frame gaps**.
- ADR 0045 — `TxLimiter`, a pure policy oracle, built and unwired. It bounds the runaway `tx.idle_timeout`
  cannot see: **continuous** audio (a stuck VOX, a looped bridge) that never goes silent, so the idle
  timer never fires and the radio keys indefinitely.
- ADR 0016/0017 — `TxSession` (the model this mirrors), `TxSlot` (the single-talker occupancy guard the
  browser `/audio/tx` path already uses), and `RadioArbiter` (the half-duplex owner: claim TX, RX stands
  down).
- ADR 0046 — the open-gateway refusals are already enforced at `POST /link/enable` (refuse `squelch=off`;
  refuse `require_auth=false`), so a live link can only exist behind a real squelch and over-RF auth.

The listening direction (ADR 0043) and the outbound feeder (ADR 0044) are done; this closes the loop.

## Decision

### The keying lifecycle is driven by the stream edges, with two backstops

A new `LinkTxBridge` (package `radio_server/linktx/`) is the single reader of `link.receive()`. It mirrors
`TxSession` — a clock-injected, synchronous keying core wrapped in an async poll loop — and translates the
inbound protocol into keying:

- `StreamEdge.START` → **acquire** the shared `TxSlot`, `arbiter.acquire_tx()`, `radio.ptt(True)` (arbiter
  *before* PTT, exactly as `TxSession.feed`), `limiter.key_down`.
- `AudioFrame` → `radio.transmit(frame)` while keyed.
- `StreamEdge.END` → `radio.ptt(False)`, release the slot + arbiter, `limiter.key_up`.
- `None` → hold PTT; it says nothing about stream state (ADR 0047).

Two backstops cover the two ways a keyed stream can run away, and they are **different failures**:

- **Silence** — the peer vanished mid-stream and no `END` ever comes (an unpaired `START`, which ADR 0047
  pinned as real and survivable). `tx.idle_timeout` (ADR 0016) drops PTT after the inbound stream goes
  quiet. ADR 0047 promised this cycle wires it; it does.
- **Continuous audio** — a stuck-on peer that never goes silent, so `idle_timeout` never fires. `TxLimiter`
  force-unkeys at `link.max_tx_seconds` **mid-stream, without waiting for `END`**, then refuses to re-key
  for `link.tx_cooloff` (a `START` refused by cooloff is **dropped, not queued** — without the cooloff a
  stuck peer just re-keys instantly and you have built a square-wave generator). PTT is keyed via the
  audio/serial path only — guardrail 2 is not relaxed for the link.

### Contention: THE LOCAL OPERATOR OWNS THE STATION

This is the design call of the cycle. A link stream never preempts and is never queued behind local use:

- **Link `START` while the slot is held** (a browser Talk, a voice service, the ID) → the link stream is
  **dropped**. Not queued, not preempting. Its frames are still teed to the browser monitor, but nothing
  reaches the antenna.
- **Browser Talk while a link stream holds** → **refused** for the duration (the existing `TxSlot` refusal:
  `{"status":"busy"}` + `1013`). This is **not a bug** and is not to be "fixed": not keying over someone
  else's over is how a radio works. It is bounded by `link.max_tx_seconds` and the escapes are the limiter
  and `POST /link/disable`.

Both refusals are surfaced **by name**, never a silent no-op (guardrail 3): the browser refusal via the
existing busy close; the link-side drops via distinct ledger records (below). Contention is mediated by the
**one shared `TxSlot`** — the bridge and `/audio/tx` acquire the same instance — so the mechanism is
already built and proven; this cycle only routes the link through it.

### `POST /link/disable` is a hard unkey

Disable is the panic button and must work **while a stranger is keying your rig**. It is not "stop feeding":
it drops PTT **now, mid-frame**, releases the slot, and only then disables. `LinkTxBridge.hard_unkey()` is a
synchronous drop, called before the loop is stopped and before the gate is flipped. It has its own
acceptance test.

### The ledger sees keying, the limiter, and both refusals

- Key up/down reuse the existing `ptt` event (`tx_key_up`/`tx_key_down` with duration) — the same channel
  `TxSession` uses, so link keying lands in the operating log consistently and drives the real-time PTT
  indicator.
- A new `link_tx` event carries the link-specific records, whitelisted (no wholesale `data` copy):
  - `link_tx_forced_unkey` (with the keyed duration) — **distinct from a normal `END`**, so an operator can
    see the limiter fired and how often. That data is how `link.max_tx_seconds` stops being a guess
    (guardrail 1).
  - `link_tx_dropped` — a link `START` refused because the local operator held the slot.
  - `link_tx_refused` — a link `START` refused during cooloff.

### One reader: the bridge subsumes the listening pump

`link.receive()` is a destructive read, and the browser `LinkPump` (ADR 0043) already consumed it. Two
independent pollers would split the stream. So the bridge becomes the **single** reader: it tees each
`AudioFrame`'s PCM to the existing `link_hub` (so `/audio/link` browsers keep working, even while a link
stream is being transmitted) *and* drives the transmitter on the edges. `LinkPump`'s read role is retired;
`/audio/link` is now a pure `link_hub` subscriber, and the bridge runs on the enable gate — started by
`POST /link/enable`, stopped by `POST /link/disable`, symmetric with the outbound `LinkFeeder`.

## Consequences

- A remote peer can key the local transmitter, bounded on every axis: single-talker (`TxSlot`), half-duplex
  (`RadioArbiter`), silence (`tx.idle_timeout`), stuck-on (`TxLimiter` + cooloff), and an operator override
  (`POST /link/disable` hard unkey). The local operator always wins contention.
- `TxLimiter` (ADR 0045) is now wired — the limiter's forced unkey creates exactly the gap the ID scheduler
  needs (ADR 0045); that behavior is **inherited**, not built here, and the ID scheduler is untouched.
- **Scope limits, deliberate:** no real M17/mrefd socket or Codec2 (mock backend only); browser Talk
  behavior is unchanged beyond the contention refusal it already performs; no UI; no new config key
  (`link.max_tx_seconds`/`link.tx_cooloff` were seeded by ADR 0045), so no settings-canary bump.
- **Still ahead:** the real M17/mrefd backend behind `create_link` — native LSF/EOT edges, the
  Codec2↔canonical resample, the reflector socket — is its own empirical bring-up phase (ADR 0041's
  caveat), each its own ADR + PR.
