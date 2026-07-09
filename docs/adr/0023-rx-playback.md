# 0023 — Live RX audio playback in the browser (Web Audio worklet, PCM decode, jitter buffer, autoplay-gesture gate, TX-suspend gap handling)

Status: Accepted

## Context

The server has relayed receive audio since cycle 13: `GET /audio/rx` streams raw canonical PCM over a
binary WebSocket (ADR 0014), demand-driven by an `RxPump`/`AudioHub` that start on the first listener
and stop on the last. Cycle 21 shipped the first browser client — a control + visibility panel — but
**live audio was deferred to cycles 22–23**. This cycle makes the browser **play what the radio
hears**: it consumes the existing `/audio/rx` socket, decodes the PCM, and feeds Web Audio for
continuous playback. It is a pure client feature over an existing endpoint, plus one minimal,
symmetric server change.

**Verified, not assumed.** The brief flagged a "cycle-15 symmetry decision" — an RX format-declaring
header — and told us to verify before adding it. It was **never actually implemented**: `/audio/rx`
sent nothing but raw `send_bytes` frames after `accept()`, while `/audio/tx` (ADR 0016) has a
client-declares/server-validates handshake and a `{"status":"ready","format":…}` ack. So RX and TX
were asymmetric. Per the brief we realize the deferred decision now, minimally.

## Decision

### Server: a symmetric `/audio/rx` format header

- `/audio/rx` now sends **one JSON message first** — `{"status": "ready", "format":
  asdict(CANONICAL_FORMAT)}` — immediately after `accept()`, before any binary frame. This mirrors
  `/audio/tx`'s ready ack exactly (same shape), so the two directions are symmetric: TX declares its
  format and RX declares its. `asdict`/`CANONICAL_FORMAT` were already imported; the demand-driven
  pump lifecycle (start on first subscriber, stop on last) is unchanged. Everything after the header
  is raw canonical PCM (48000 Hz / s16le / mono) via `send_bytes`, as before.
- This is the **only** backend change. Three existing tests that read `receive_bytes()` as the very
  first message now consume the header first (`receive_json()`); a new `test_audio_rx_sends_format_header`
  asserts the header equals `{"status":"ready","format":{"rate":48000,"width":2,"channels":1}}`. The
  reject-token tests are unaffected (they close `1008` **before** `accept()`, so no header is sent).

### Client: AudioWorklet ring-buffer player, fed by a decoded binary WebSocket

- **AudioWorklet over a buffer queue** (the brief's preferred path). `web/src/rxWorklet.js` is an
  `AudioWorkletProcessor` ("rx-player") holding a Float32 ring buffer. The main thread decodes each
  frame and hands Float32 chunks over via **`port.postMessage`** — deliberately **not**
  `SharedArrayBuffer`, so the page needs no cross-origin-isolation (COOP/COEP) response headers and
  the cycle-21 same-origin `StaticFiles` mount is untouched.
- **Jitter buffer, continuity over latency.** The worklet primes ~150 ms before it starts draining
  and caps buffered latency at ~500 ms (dropping the oldest beyond — mirroring the server hub's
  drop-oldest). On an **underrun** it outputs silence (zeros) and re-primes. That single mechanism
  handles every gap: a scripted RX silence, a WebSocket reconnect, and — the important one — the
  **arbiter suspending RX during TX** (half-duplex; ADR 0017), where `/audio/rx` simply stops
  delivering frames. A gap is a clean pause, never a buzz or a crash, and playback resumes cleanly
  when frames return.
- **Autoplay-gesture gate.** A browser starts a fresh `AudioContext` suspended, so nothing can play
  on load. `web/src/useRxAudio.js` creates nothing until `listen()` runs from the button click:
  it builds the context at 48 kHz (so canonical PCM maps 1:1, no resample), `resume()`s it, loads the
  worklet module, and wires `worklet → GainNode(mute) → destination`.
- **The socket mirrors `useEvents`** (ADR 0022): `?token=` auth, exponential-backoff reconnect, and a
  `1008` policy close (rejected token) bubbles to `onAuthError` back to the token gate instead of
  retrying. `binaryType="arraybuffer"`; the first message is the JSON header (noted, but playback
  assumes canonical regardless, so a header-less older server still plays); each subsequent frame is
  `Int16Array → Float32` (`/32768`; browsers are little-endian, matching s16le) posted to the worklet.
- **Controls.** Listen/Stop, a mute (a `GainNode` at 0/1), and a level meter (per-frame peak, smoothed
  on `requestAnimationFrame` so it moves with audio without thrashing React — the meter reflects
  incoming audio even when muted). Stop closes the socket (last listener → server pump idles) and
  tears the graph down. A "receiving paused (transmitting)" note is driven off the `/events`
  `transmitting`/`arbiter` state the panel already folds — no distinct server "suspended" marker is
  added (the existing frames suffice).
- **Dev proxy.** `web/vite.config.js` gains `/audio/rx` (and `/audio/tx`, reserved for cycle 23) as
  `ws: true` proxy entries so `npm run dev` reaches the Python RX socket; production is same-origin.

## Consequences

- **The browser plays live receive audio.** Clicking Listen resumes the context and plays scripted
  mock RX continuously; a scripted silence/TX-suspend gap is a clean pause (no buzz, no crash) that
  resumes cleanly; Stop disconnects and the demand-driven pump goes idle; the meter tracks the audio.
- **RX and TX are now symmetric at the wire.** Both declare their format in a leading JSON message.
- **No cross-origin-isolation headers needed** — postMessage transport keeps the static-serving story
  from ADR 0022 intact.
- **Backend surface is otherwise unchanged.** The only Python edit is the one-line header send in
  `api/app.py`; `backends/`, `rx/`, `arbiter/`, `audio/`, etc. are untouched. All other cycle-21
  behavior stands.
- **Verified end-to-end in a real browser** (headless Chromium against the live mock server, seeded
  with an audible looping tone), not just pytest: Listen → continuous audio + moving meter; a
  TX-suspend gap (via a streaming `/audio/tx` client) → paused indicator, no buzz, clean resume; Stop
  → pump idle; autoplay confirmed impossible before the gesture.
- Full suite: **386 passed, 4 skipped** (was 385; +1 `test_audio_rx_sends_format_header`, three RX WS
  tests updated to read the header first). The SPA is browser-verified.
- **Deferred, on purpose:** TX mic capture (cycle 23); recordings playback/download + a GET API for
  the JSONL ledger; a distinct `/events` "suspended" marker; Opus/compression.
