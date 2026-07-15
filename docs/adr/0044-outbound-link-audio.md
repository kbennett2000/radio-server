# 0044 — Outbound link audio: `radio.receive()` → `link.transmit()`, the talking tier

Status: Accepted

## Context

Cycle 48 opened the first of three audio-routing directions — `link.receive()` → browser, the
*listening* tier (ADR 0043) — chosen first because it touches no transmitter and needs no credentials.
This cycle opens the **second**: `radio.receive()` → `link.transmit()`, the *talking* tier. **The world
now hears your radio.** It is still not the dangerous direction — nothing here calls `radio.transmit()`,
`ptt()`, or touches `TxSlot`; that is direction three, a stranger keying your rig, next cycle. But it
does put your receiver's audio onto a public reflector, which forces two questions the listening tier
never had to answer: *when* may the feed run, and *how* is one transmission framed.

The receive half already exists (ADR 0014/0031): a demand-driven `RxPump` reads `radio.receive()` and
fans canonical PCM through the shared `audio_hub` to the `/audio/rx` browsers, publishing **only while
the RX activity gate is open** (ADR 0015). Everything this cycle needs to route radio RX to the network
is already on that hub.

## Decision

- **No new pump, no new hub — the link is just another subscriber.** `AudioHub` (ADR 0014) already fans
  one producer to many subscribers, and `RxPump` already publishes to it only while the gate is open. So
  the outbound path is a `LinkFeeder` that **subscribes to the existing `audio_hub`** alongside the
  browsers and calls `link.transmit()` per frame — and **registers as an RX demand source** (the
  `_acquire_rx`/`_release_rx` reference count) so enabling the link runs the shared reader even when
  nobody is browsing. This is the deliberate contrast with ADR 0043's inbound direction, which needed a
  *second* hub+pump because two producers into one hub interleave. Here there is one producer (the
  radio) and one hub; the link is one more consumer.

  ```
  radio.receive() → RxPump → audio_hub ─┬─→ /audio/rx        (browsers, existing)
                                        └─→ LinkFeeder → link.transmit()   (new subscriber)
  ```

- **"Feed only while the gate is open" is inherited, not re-derived.** `RxPump` calls `hub.publish`
  only inside its gate-open branch, so a plain subscriber already receives gate-open frames only. One
  gate-open..gate-close span is one transmission. The feeder does not run its own gate, sample its own
  levels, or infer anything — it consumes what the pump already decided was live.

- **A gate edge is a stream boundary, and `transmit()` alone cannot express it.** A real network
  protocol frames each transmission: M17 sends an **LSF** at the start and an **EOT** at the end; a
  stream is not an undifferentiated frame spray. `Link.transmit(AudioFrame)` — one frame — cannot say
  "this is where the stream begins/ends," and having the backend *infer* boundaries from gaps between
  frames is guesswork. The radio already solves the symmetric problem: `ptt(on)` is separate from
  `transmit(audio)`. So `Link` gets the same split — a new `stream(on: bool)` (ADR 0041, amended):
  `stream(True)` opens (LSF), `stream(False)` closes (EOT), `transmit(frame)` carries the payload in
  between. It is part of the `TRANSMIT` surface, not a new capability.

- **The boundary rides the same queue as the frames, so it can never race them.** The stream edges come
  from the pump's existing `on_activity(active)` edge callback (fired `True` *before* the first frame is
  published, `False` on gate-close with no frame after). But `on_activity` is **synchronous** in the
  pump loop while frame delivery to the feeder is **async** via the hub queue. If the feeder called
  `stream(False)` straight from the callback, the EOT could overtake frames still sitting in the queue —
  an end-of-transmission before the last audio. So `LinkFeeder.note_activity` instead pushes a **boundary
  sentinel into its own subscriber queue** — the very queue `hub.publish` feeds. Because `on_activity`
  and `hub.publish` run synchronously within a single pump iteration, the sentinel is enqueued in strict
  order against the frames (START before the first frame, END after the last), and the single consumer
  drains them in that order. An idempotent lazy-open guard covers a mid-span subscribe where the START
  edge was missed. No frame-gap inference anywhere.

- **The load-bearing safety rule: refuse to enable with `audio.squelch = "off"`.** With squelch off the
  gate is `pass_through` — it never closes, so there is **no gate-close edge**. No edge means the feeder
  never ends its stream, which means it transmits the receiver's noise floor to every peer on the
  reflector continuously. That is not a degraded feature; it is antisocial output. So `POST /link/enable`
  **fails loud, by name** (HTTP 400 naming `audio.squelch`) when squelch is off, requiring `"audio"`
  (software VAD) or `"cat"` (hardware busy). This is the same fail-loud instinct as rejecting
  `id_interval > 600` over the Part-97 ceiling — a configuration that would produce indefensible RF is
  refused, not clamped or silently degraded. The refusal guards the *shared* enable gate, so it also
  gates the inbound listening tier; that is the intended cost of a single enable act — you do not turn
  the link on at all in a squelch-off deployment.

- **The arbiter is inherited, not changed.** `RxPump` already stands down while TX holds the radio (ADR
  0017): while `arbiter.transmitting`, it does not pull `receive()` and it fires `on_activity(False)`. So
  a local key-up looks to the feeder exactly like a gate-close — the outbound feed pauses (EOT) and
  resumes on its own (a fresh LSF) when TX drops. This needs **zero** arbiter changes. Its **consequence,
  documented and left unchanged this cycle:** the link does **not** hear locally-generated audio (station
  ID, voice services), because those go out `radio.transmit()` while RX is stood down. Bridging local
  announcements onto the link is a separate concern for a later cycle, not a bug here.

## Consequences

- Radio RX leaves the box onto the network for the first time: with `link.backend = "mock"` and
  `audio.squelch = "audio"`, `POST /link/enable` starts the feeder, and a received transmission arrives
  at `MockLink.tx_log` as its PCM frames **bracketed by one `StreamEdge.START`/`StreamEdge.END`** pair.
  With `audio.squelch = "off"`, `POST /link/enable` is refused 400 by name.
- **The feeder is RX demand.** Enabling the link starts the shared `RxPump` even with no `/audio/rx`
  listener, and disabling it releases that demand — so the radio is only read when something wants the
  audio, browser or link.
- **`Link` grew a boundary method.** ADR 0041 is amended (not superseded) to add `stream(on)` — the
  network mirror of `Radio.ptt(on)` — with `MockLink` recording the edges inline in `tx_log`. No new
  `LinkCapability`: streaming is part of transmitting.
- Backpressure caveat: a boundary sentinel shares the hub's bounded, drop-oldest queue, so under
  sustained backpressure (a queue that stays full for a whole span) a START/END could be evicted with the
  oldest frames. Boundaries are rare (once per span) so this is a real-backend edge case, not a
  mock-cycle concern; a real M17 backend can add an EOT-on-timeout backstop when it lands.
- **Scope limits, deliberate:** no call to `radio.transmit()`, `ptt()`, or `TxSlot` (direction three,
  next cycle); no audio mixing and no third hub; no real M17/AllStar socket or Codec2; no UI. The RX/radio
  path and its tests are untouched, and there is no new config key (no settings-canary bump).
- **Still ahead:** direction three (`link.receive()` → `radio.transmit()` — a stranger keys your rig),
  which unlike this cycle *does* coordinate with the arbiter and TX slot; then the real network backend
  behind `create_link`.
