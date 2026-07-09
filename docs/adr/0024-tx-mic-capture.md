# 0024 — Browser TX mic capture (getUserMedia, client-side resample to canonical, handshake + rejection handling, push-to-talk lifecycle, half-duplex RX self-mute)

Status: Accepted

## Context

The gateway relays voice both directions on the server: `/audio/rx` (cycle 13) out, `/audio/tx`
(cycle 15) in. Cycle 22 gave the browser the receive half — it can now **hear** the radio. This cycle
adds the transmit half: a browser operator **talks through the gateway** by capturing the microphone
and streaming it to `/audio/tx`. It is the mirror of cycle 22 — a pure client feature over an
**unchanged** backend.

**Verified, not assumed.** The `/audio/tx` contract is fully present: `?token=` auth (→1008), the
single-talker `TxSlot` (→1013), the JSON format-declaration handshake
(`{"rate":48000,"width":2,"channels":1}` → `parse_tx_format`, non-canonical →1003), the
`{"status":"ready","format":…}` ack, whole-sample framing (odd bytes →1003), PTT keyed on the first
real frame and dropped on close/idle (2 s default), and `MockRadio.tx_log` as the transmitted-audio
record — all exercised by `tests/test_tx_audio.py`. The client speaks this contract; **one minimal
server change** was needed and is called out below (the busy signal wasn't browser-visible — the exact
"server verification gap gets a pytest" the brief anticipated). The full suite stays green (386 passed,
4 skipped).

## Decision

### getUserMedia behind a user gesture
`startTalk()` runs only from the Talk button click (`getUserMedia` requires a gesture and an
`AudioContext` starts suspended). It requests a mono mic (`echoCancellation`/`noiseSuppression` on),
builds the capture graph, opens the socket, and streams. A permission denial (`NotAllowedError`) sets
a clear "microphone permission denied" state — never a hang.

### Client-side resample to canonical (the load-bearing piece)
The mic drives the `AudioContext` at its **native rate** (often 44.1 k), but `/audio/tx` demands
canonical **48000/s16le/mono** and rejects anything else. So the client **resamples**
`ctx.sampleRate → 48000` (streaming linear interpolation, carrying one sample of history and the
fractional read position across render quanta so the stream is click-free) and encodes Float32 →
Int16 LE — the exact inverse of cycle 22's Int16 → Float32 decode. The context is created at its
**default rate** (not forced to 48 k) so the resampler is always the real path — an identity fast-path
only when the device is already 48 k. `web/src/txWorklet.js` is a `"tx-capture"` sink worklet
(`numberOfOutputs: 0`) that just forwards each captured quantum (a copy) to the main thread, where the
resample/encode/send happens.

### Handshake + rejection handling → explicit UI states
`useTxAudio` opens `/audio/tx?token=`, sends the canonical header, awaits the ready ack, then streams
~20 ms binary frames. Rejections map to explicit states with **no retry-hammer** ever (TX never
auto-reconnects): **busy** → "radio busy — another operator is transmitting"; **1003** → a
format-error state (shouldn't happen — we send canonical); a **1008** or any pre-ready drop → a clear
"could not start transmit" error (and `onAuthError` when the code is visible).

### The one server change: make the busy signal browser-visible
Empirically (verified in a real headless browser), a browser **cannot observe a pre-accept WebSocket
close code** — a rejected handshake surfaces as a generic **1006**, so the server's app-level 1013
(and 1008) are lost. The single-talker refusal was closed *before* `accept()`, so a browser second
talker saw only 1006 and couldn't show a clear "busy". The minimal fix: on the busy path, **accept
first, send an explicit `{"status":"busy"}` message** the client reads, then close 1013. Ordering is
load-bearing — the busy path returns before the `session`/`finally`, so it never releases the slot the
*other* talker holds. `token`/1008 stays a pre-accept close (unchanged); a browser 1008 is a rare
rotated-token edge — the token is already validated at the gate before any audio socket opens — and
falls into the generic "could not start transmit" error. `tests/test_tx_audio.py`'s two second-talker
tests now assert the busy message then the 1013 close.

### Push-to-talk lifecycle (toggle) — keyed for the stream duration
The Talk button is a **toggle** (chosen over hold-to-talk for browser robustness; the brief allowed
either). Talking opens the socket → the server keys PTT on the first frame and holds it for the stream;
Stop closes the socket → the server's `finally` drops PTT and frees the slot. **TX deliberately does
not auto-reconnect** (unlike RX's passive backoff): a keyed transmitter must never silently resurrect
after a drop — the operator presses Talk again. `stopTalk()` also stops the mic tracks (clears the OS
mic indicator). `PttControl` (REST `/ptt`) is left untouched — an orthogonal manual-key toggle; both
just reflect `state.transmitting`, so they don't fight.

### Half-duplex UX — immediate local RX self-mute
When the local operator keys, the server arbiter suspends RX — but the RX **jitter buffer holds up to
~500 ms**, so its buffered tail would still play, and you'd hear yourself gate in/out. So the monitor
is muted **locally and immediately** on local keying: `ControlPanel` lifts the Talk state and passes
`suspendedLocally` to `ListenControl`, which feeds a new `forceMute` input to `useRxAudio` (effective
gain `= (muted || forceMute) ? 0 : 1`, ramped on the running graph). It is gated on **this** operator's
own talk, not the global `transmitting`, so a *remote* operator's TX doesn't mute your monitor. The
existing "receiving paused (transmitting)" notice is kept.

## Consequences

- **The browser can transmit.** Talk requests the mic, keys the radio, and streams canonical PCM;
  releasing drops PTT and frees the single-talker slot.
- **Non-48 k mics work.** The client resamples to canonical, so a 44.1 k context still lands 48 k PCM
  the server accepts.
- **Backend nearly unchanged.** The only Python edit is the busy-path accept-then-inform in
  `api/app.py` (above), with its two `test_tx_audio.py` second-talker tests updated. Everything else is
  new `web/src/` files (`txWorklet.js`, `useTxAudio.js`, `components/TalkControl.jsx`), the `forceMute`
  addition to `useRxAudio.js` + `ListenControl.jsx`, the `ControlPanel` wiring, and one CSS class. The
  suite stays 386 passed / 4 skipped.
- **Verified end-to-end in a real browser** (headless Chrome with a fake mic device), not just pytest:
  Talk keys + streams canonical frames into `tx_log`; a forced-44.1 k context still lands ~48 k/s
  (resample proven); releasing drops PTT and frees the slot; a second talker gets the "radio busy"
  state with no retry; a denied mic shows a clear message and no hang; and talking mutes/pauses the
  local RX monitor.
- **Deferred, on purpose:** recordings playback/download UI; async scan + `/scan/stop` (the noted
  backend gap); Opus/compression.
