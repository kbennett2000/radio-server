# 0043 — Inbound link audio: `link.receive()` → browser, the listening tier

Status: Accepted

## Context

Cycle 46 built the `Link` protocol and `MockLink` (ADR 0041); Cycle 47 wired a `Link` into the running
app behind a runtime **enable gate** and the `/link` REST surface (ADR 0042) — but **no audio is routed
in either direction**. A wired Link that carries no audio is a light switch with no bulb.

There are three audio-routing directions, and they carry very different risk, so they are built as
three separate cycles in ascending order of danger:

1. **This cycle: `link.receive()` → browser.** The *listening tier* — "hear the world." It touches
   **no transmitter**, needs **no credentials**, and cannot key anything. It is deliberately first
   *because* it is the one direction that cannot put RF on the air.
2. (later) `radio.receive()` → `link.transmit()` — the world hears your radio.
3. (later) `link.receive()` → `radio.transmit()` — a stranger keys your rig.

The receive half of the radio path already exists (ADR 0014): a demand-driven `RxPump` reads
`radio.receive()` and fans raw canonical PCM through an `AudioHub` to the binary `/audio/rx` WebSocket.
This cycle builds the exact parallel for the network link, and stays mock-only — `MockLink` serves
scripted inbound frames; a real M17/AllStar socket + Codec2 is a later cycle.

## Decision

- **A second, independent `AudioHub` and pump — two streams, not one.** `AudioHub` (ADR 0014) fans
  **one** producer to **many** subscribers. Feeding two producers (`radio.receive()` and
  `link.receive()`) into one hub would **interleave** their frames on the wire, not mix them — a
  listener would hear the two sources shredded together. Mixing is sample addition with clipping, a
  real DSP operation with its own failure modes (headroom, format), and it earns its own ADR when a
  cycle actually needs it. So this cycle stands up a *second* `AudioHub` (`link_hub`) fed by a *second*
  pump (`LinkPump`), exposed on a *distinct* endpoint:

  ```
  rx_hub   <- RxPump   <- radio.receive()   -> /audio/rx     (existing, untouched)
  link_hub <- LinkPump <- link.receive()    -> /audio/link   (new)
  ```

  Two producers, two hubs, two endpoints. No mixing anywhere in this cycle.

- **`LinkPump` — a deliberately thinner mirror of `RxPump`.** A standalone async loop over the
  synchronous `link.receive()` that publishes each live frame's PCM to `link_hub`. It reuses the pump
  lifecycle discipline exactly: `start()` idempotent; `stop()` nulls its task **before** awaiting the
  cancel so a reconnect mid-teardown starts a fresh pump; **demand-driven** — started on the first
  `/audio/link` subscriber, stopped on the last; and a lifespan-shutdown stop as the real
  no-leaked-task guarantee. It is *simpler* than `RxPump` on purpose: **no arbiter, no activity gate,
  no recorder, no controller, no ledger** — just link → hub, gated by enable.

- **The arbiter is not touched — and that is precisely why this direction is safe.** The arbiter (ADR
  0017) owns "who has the radio right now." This path reads `link.receive()` and writes to a browser;
  it never reads or keys the radio, so it has no business consulting the arbiter and does not. A path
  that cannot touch the transmitter cannot be blinded by a key-up and cannot cause one — this is the
  structural reason the listening tier goes first.

- **The enable gate is enforced in the pump, in code, not in prose.** `LinkPump` checks
  `link.status().enabled` at the top of every loop iteration; while it is `False` the pump does not
  call `receive()` at all and publishes nothing. Enabling (a runtime `POST /link/enable`, ADR 0042)
  lets frames flow; disabling stops them on the next poll. Because a disabled pump never drains the
  backend, scripted/queued inbound frames survive until enable — enable/disable is a clean gate on the
  stream, not a lossy shutter. The app still always boots disabled (ADR 0041/0042), so a fresh
  `/audio/link` listener hears nothing until someone deliberately enables the link.

- **`link.backend = "none"` yields a silent stream, the WebSocket analogue of the REST 503.** When no
  Link is configured, `app.state.link` is `None` and no `LinkPump` is built. `/audio/link` still
  authenticates and accepts, sends its format header, and then simply yields nothing — the same
  "silent stream" a *disabled* link produces. A WebSocket has no clean status-code channel for
  "unavailable" the way `/link`'s REST routes return 503 (a pre-accept close surfaces to browsers as a
  generic 1006), so "connect, no frames" is the honest, non-crashing signal. The UI cycle gates on
  `GET /link` before opening the socket.

- **The backend owns format; the pump is format-transparent.** `link.receive()` returns canonical
  48k/s16le/mono (`AudioFrame`, ADR 0006). M17 is Codec2 at 8 kHz — resampling that up to canonical is
  the **M17 backend's** job, done before the frame ever reaches the pump, exactly as a real radio
  backend owns its own capture rate. `LinkPump` publishes `frame.samples` verbatim and asserts nothing
  about rate; it is a transport, not a resampler.

- **`link.receive()` returns `AudioFrame | None`; the pump handles idle first.** Unlike
  `radio.receive()` (which always returns a frame; idle is a non-empty silence frame), `Link.receive()`
  returns `None` when the network is idle. So `LinkPump` cannot reuse `RxPump`'s `if frame.samples:`
  guard unchanged — it checks `frame is not None` *before* touching `.samples`. An idle network
  publishes nothing and never raises.

## Consequences

- Network-link audio leaves the box for the first time: with `link.backend = "mock"`, a token'd LAN
  client connects to `/audio/link`, and once the link is enabled it receives the scripted inbound PCM
  frames in order, as binary — a second, independent fan-out beside `/audio/rx`.
- **The enable gate now guards real audio.** ADR 0041/0042 established the gate as a property; this
  cycle is the first place it *does* something — no frames flow while `enabled` is false, proven at the
  pump level.
- The RX/radio path is entirely untouched: no changes to `RxPump`, the arbiter, `TxSlot`, PTT, or
  `radio.transmit`. No new config key, so the settings schema and its canary are unchanged.
- **Scope limits, deliberate:** no audio mixing (two hubs, two endpoints — mixing is a future ADR); no
  real M17/AllStar socket or Codec2; no UI (a later cycle points the existing `rxWorklet` at
  `/audio/link`); and **link RX is not written to the event ledger** — a Tier-0-style "the link is
  hearing traffic" signal is a fine idea and its own cycle, not smuggled in here.
- **Still ahead:** the two transmitting directions (`radio.receive()` → `link.transmit()`, then
  `link.receive()` → `radio.transmit()`), each gating on `status().enabled` and, unlike this cycle,
  coordinating with the arbiter; then the real network backend behind `create_link`.
