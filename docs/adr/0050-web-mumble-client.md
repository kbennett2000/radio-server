# 0050 — The web UI as a Mumble client: browser monitor/talk on the linked channel

## Status

Accepted. Builds on the Mumble link (ADR 0041/0042) and reuses the ADR 0049 timed-gate pattern; keys
no RF, so ADR 0045's and 0049's RF-side behavior is untouched.

## Context

Until now the browser is a client of the **radio only**: `/audio/rx` plays RF receive audio and
`/audio/tx` keys the RF transmitter (ADR 0014/0016). When a Mumble link is active, the RF↔Mumble
bridge (ADR 0041) relays between RF and the Mumble channel — but the operator sitting at the web UI
**cannot hear or talk to the Mumble net directly.** The bridge's inbound audio is single-consumer (the
one `MumbleClient.on_audio` callback, delivered only to the RF drain) and its outbound is the RF relay.
An operator at the computer who wants to join the Mumble net has to run a separate Mumble client.

The operator asked for the app to double as a Mumble client: **Talk → the Mumble server, and Mumble
channel audio → the app.** Chosen behavior: Talk is **Mumble-only** (does not key RF); Listen is the
**Mumble channel only**; the existing Monitor/Transmit cards **repurpose** to Mumble while a link is
active and revert on disconnect.

**The load-bearing constraint:** there is exactly **one** Mumble connection — one channel user,
`<callsign> (radio-server)`, owned by the bridge. The browser shares it. So inbound audio must be
**fanned out** (today it feeds one consumer), and outbound must be **serialized**: the RF→Mumble relay
and the operator's mic must never voice the single user at the same time, or peers hear both garbled
together (pymumble's `sound_output` interleaves concurrent senders, it does not mix).

## Decision

Add no new audio mechanism — mirror the two that exist (the `AudioHub` fan-out and the ADR 0049
timed-gate yield).

**Inbound (Mumble → browser): a second `AudioHub`, published by the bridge.** A `mumble_rx_hub` is
created at the composition root and passed to the bridge. The bridge's thread-safe inbound handoff now
publishes every received Mumble frame to that hub **regardless of `tx_to_rf`** (so even receive-only
entries feed the browser), in addition to the existing RF drain enqueue. A new WebSocket
`/audio/mumble/rx` subscribes to the hub exactly as `/audio/rx` subscribes to the RF hub. No RF pump
demand is taken — this stream is fed by the Mumble receive path, not `receive()`.

**Outbound (browser mic → Mumble) through the bridge, with an operator-talk yield.** A new WebSocket
`/audio/mumble/tx` forwards the operator's canonical-PCM frames to `bridge.send_operator_audio(pcm)` —
so the bridge stays the **sole sender** on the one connection. `send_operator_audio` arms an internal
timed gate (`DtmfMuteGate`, reused as a generic latch) and sends; the RF→Mumble relay checks that gate
and **steps aside while the operator is talking** (the same shape as the DTMF yield). One voice on the
shared user at a time, operator wins. This path **never keys the radio** — no `TxSession`, no arbiter,
no station ID — and uses a **separate single-talker slot** from the RF `/audio/tx`, so Mumble talk and
RF talk don't block each other.

**UI:** the browser derives "a link is active" from the reliably-pushed `link` WS event
(`state.link.active`) and, when set, points the existing Monitor/Transmit controls at the two new
endpoints and relabels them. The hooks are endpoint-parameterized; the 48 kHz format, mute, PTT,
spacebar, and close-code handling carry over unchanged.

## Consequences

- The operator can monitor and talk on the Mumble net from the app, with no separate client.
- **Deliberate trade:** while the operator talks, the RF→Mumble relay pauses (frames counted as
  `op_yielded`), so RF traffic is briefly not forwarded to Mumble — exactly what a gateway operator
  taking the mic would do. When the operator stops (a short hold later), the relay resumes.
- Receive-only entries (`tx_to_rf = false`) still feed the browser monitor — the fan-out is independent
  of the RF drain.
- **Part 97 is untouched:** nothing on this path keys RF, so automatic station ID and the RF auth plane
  are unaffected. The Mumble user is still identified by its callsign nick (ADR 0042).
- Both directions are unit-testable against `MockMumbleClient` + `MockRadio` (no Murmur, no pymumble):
  `inject()` drives the inbound hub, `sent_audio` records operator outbound. On-air feel (levels, the
  yield's snappiness) is a hardware bench check; `bridge.dtmf_mute_hold`-style tuning of the operator
  hold, if needed, is a marked default.
