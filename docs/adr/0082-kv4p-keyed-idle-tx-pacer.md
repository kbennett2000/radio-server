# 0082 — Keep the kv4p transmitter fed while keyed-but-idle (a TX pacer)

Status: Accepted

## Context

Over Mumble via the **kv4p** backend, the last ~0.5 s of speech repeats at the end of every over —
the "Max Headroom" loop. The **AIOC** backend never does this. The difference is how each backend
reaches the radio while keyed:

- **AIOC** (`radio_server/backends/aioc_baofeng.py`) opens a *continuous* sounddevice output stream
  on key-up. It "emits silence until `transmit` writes" (`_key_on`), so whenever the caller is not
  writing, the sound card is still clocking silence out to the radio. The transmitter's audio buffer
  is never starved.
- **kv4p** (`radio_server/backends/kv4p/radio.py`) is a *frame-push* backend: `transmit()` sends an
  Opus frame only when it is called, and **nothing** goes over the UART between calls.

The starve path (code-confirmed): the Mumble bridge (`link/bridge.py::_mumble_to_rf`) drives a
`tx/session.py::TxSession` that keys once via `ptt(True)` (`TxSession.feed`) and holds the key across
frames. When the far end goes quiet, the session keeps PTT **asserted** until the `mumble.tx_hang`
window elapses (`DEFAULT_MUMBLE_TX_HANG = 0.8 s`) — but delivers no audio in that gap. So the kv4p
sends nothing while still keyed, the SA818's TX audio buffer underruns, and the firmware decoder
loops its last ~0.5 s of content. Inter-word pauses shorter than `tx_hang` starve the same way. The
browser `/audio/tx` talker uses the same `TxSession`, so it has the identical latent bug.

The AIOC's continuous silence fill is exactly what the kv4p lacks. This is the TX-continuity analogue
of the AIOC's always-running output stream, done in software for a frame-push transport.

## Decision

While the kv4p is keyed (a **held** PTT), a **single coherent sender** keeps a continuous frame
stream flowing to the firmware: real audio when it is available, encoded silence otherwise — exactly
one Opus frame per ~40 ms slot (the Opus frame cadence, `FRAME_MS`). Match the AIOC contract:
`keyed ⇒ a continuous stream reaches the radio`.

### A `_TxPacer` (`radio_server/backends/kv4p/pacer.py`)

- Owns the per-keying `TxAudioEncoder` and a thread-safe, bounded PCM jitter buffer.
- `transmit()` while keyed no longer pushes to the encoder directly — it `enqueue()`s PCM into the
  buffer (non-blocking).
- A **daemon thread** calls `tick()` every frame interval. `tick()` sends **exactly one** frame:
  if the buffer holds a whole frame (`FRAME_BYTES`) it encodes+sends that (real audio, ADR 0080
  `tx_gain` applied by the encoder as always); otherwise it encodes+sends one **silence** frame
  (`push` of `FRAME_BYTES` of zeros — the same silence-through-the-encoder path the key-up lead-in
  already uses; zeros are `tx_gain`-invariant, so gain is neither applied nor double-applied).
- On key-down: `stop()` (join the thread) then `flush_tail()` on the caller thread — drain the
  buffered remainder and flush the encoder tail (never clip it), then drop PTT.

### Why a thread, and why one sender

The `Radio` surface (`ptt`/`transmit`) is synchronous, and the pacer must keep firing every ~40 ms
even while the bridge's async task is parked in `wait_for(queue.get(), timeout=tx_hang)`. So the
pacer is a daemon thread — the same shape as the transport's own reader thread and the AIOC's
background output stream (not an asyncio task like `ScanRunner`/`RxPump`). Policy lives in an
injected-clock `tick()`, tested directly (each `tick()` = one advanced slot).

Making it the **single** sender is what avoids racing the flow-control window and doubling audio:
during a held key the pacer thread is the *only* thing that pushes to the encoder and calls
`send_tx_audio`. `transmit()` (asyncio loop) only touches the buffer under its lock; `send_tx_audio`
is already thread-safe (guarded by the transport credit window). No encoder race, no doubled frame.
Feeding exactly one frame's worth per real slot means `push` returns exactly one packet and the
encoder accumulator stays `< FRAME_SAMPLES` (the 0.5 s lead-in leaves a standing ~960-sample
remainder, so real audio is phase-offset ~20 ms — all samples still ship in order).

### Scope: streaming only; the one-shot path is untouched

The one-shot `transmit()` path (station-ID service, service-dispatch TTS, `POST /transmit`,
`doctor --tx-tone`) self-keys, sends the whole clip, and drops immediately — it never holds the key
idle, so it never starves. It keeps its exact prior behavior (`_key_on` / `push` / `_key_off` with
its own synchronous flush). The pacer runs **only** between `ptt(True)` and `ptt(False)`. Streaming
station-ID audio (transmitted while keyed) routes through `enqueue()` and is paced out correctly.

## Consequences

- The Mumble/`TxSession` starve is fixed: the firmware always has a fresh frame to play, so it can
  never loop stale audio across a `tx_hang` pause or an inter-word gap. The browser talker is fixed
  the same way.
- **Encoded silence stays within the flow-control window.** A silence frame is tiny (~5–10 B payload
  + ~9 B wire) against the 2048-B window, and the firmware refunds credit via `WINDOW_UPDATE` as it
  drains, so continuous silence never blocks or times out. Asserted by a test that drives 200 silence
  slots through the real transport + credit window with modeled refunds.
- **Lost backpressure (deliberate).** Previously the streaming `transmit()` ran on the asyncio loop
  and could block in `_write_frame` up to the transport write timeout (~2 s) on a credit-starved
  window — stalling the whole event loop. Moving wire writes to the pacer thread makes `enqueue()`
  non-blocking (a latent improvement), but removes that natural backpressure. The bounded,
  drop-oldest jitter buffer (~16 frames ≈ 640 ms) is now the limiter; a `dropped_bytes` counter is
  telemetry for a producer that outpaces the real-time drain.
- **Host-clock pacing.** Slots advance on a host wall clock at exactly the frame interval, which can
  in principle drift vs the firmware's real-time consumption over a long over; the bounded buffer
  caps the resulting latency. Real Mumble audio arrives at real time, so drift over a normal over is
  negligible. (A future refinement could pace to the firmware's own consumption via `WINDOW_UPDATE`
  credits instead of the host clock — out of scope here.)
- **DTMF yield is preserved.** The bridge drops PTT immediately on a DTMF command via `ptt(False)`,
  which now synchronously stops the pacer — so the pacer can never transmit silence over an
  operator's DTMF command.
- No config surface (`frame_interval` is a fixed protocol constant, not an operator knob), so no
  schema change: `radio.toml.example` byte-identical, settings-count canary unmoved. No AIOC change
  (its continuous stream already satisfies the contract), no change to `mumble.tx_hang` (the hang is
  correct — this fills it), no new backends.

Cross-refs: ADR 0064/0065 (the Opus TX edge and `TxAudioEncoder`), ADR 0069 (TX bring-up, the
key-up lead-in and `tx_stats`), ADR 0080 (`kv4p.tx_gain`, applied to real audio only), and the AIOC
backend (ADR 0029) whose continuous-output contract this mirrors.
