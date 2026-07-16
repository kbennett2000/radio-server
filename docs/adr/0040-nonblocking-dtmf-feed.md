# 0040 — Feed the streaming DTMF decoder off the RX event loop; bound the browser re-prime

Status: Accepted

## Context

After ADR 0038 shipped, live receive audio played in the browser **cuts out** — frequent silence
gaps on speech. DTMF audio drops out too, but less. With `audio.squelch = "off"` (the deployment's
setting) the RX activity gate relays every frame, so gating is not the cause.

The RX path is a single asyncio task, `RxPump.run` (`radio_server/rx/pump.py`). On **every** ~20 ms
capture frame it drives the DTMF controller *before* it fans audio out to the browser hub
(`hub.publish`) — the ADR 0031 ordering, so decode sees the raw contiguous stream. Under the default
`decode_mode = "streaming"` (ADR 0038) that reaches `MultimonStream.write`, which did a **blocking**
`proc.stdin.write(pcm); proc.stdin.flush()` to the persistent `multimon-ng` process — *on the
event-loop thread*.

The OS pipe to multimon is finite (~64 KB on Linux). If multimon ever drains slower than real time
or hitches, the `flush()` blocks the **entire event loop**. While it is blocked, the per-client
`/audio/rx` sender tasks cannot run, so the bounded per-listener hub queues (64 frames, ADR 0014)
overflow and drop-oldest discards audio — an audible cut-out. Before ADR 0038 the DTMF subprocess
ran once per ~0.5 s via `subprocess.run`; ADR 0038 moved a *per-frame* blocking pipe write onto the
loop, ahead of the browser fan-out. This is a regression the streaming change introduced.

A second, independent factor amplified it in the browser. The RX playback worklet
(`web/src/rxWorklet.js`, ADR 0023) re-primed the **full ~150 ms** jitter buffer after *any* underrun —
even a single starved sample. So every server-side stall (and ordinary LAN/Wi-Fi jitter) became a
flat ~150 ms silence, not a proportional hiccup. Voice shows this badly; a steady loud DTMF tone
perceptually masks short gaps, and the DTMF *decode* path is independent of the browser buffer
(it reads the raw frame in the pump) — which is why DTMF "cuts out less".

## Decision

- **`MultimonStream.write` never blocks the caller (`radio_server/audio/dtmf.py`).** The blocking
  `stdin.write`/`flush` moves to a dedicated daemon **writer thread** (`_pump_stdin`), the sibling of
  the existing daemon reader thread. `write()` hands PCM to a bounded `queue.Queue`
  (`WRITE_QUEUE_MAXSIZE = 64`, ~1.3 s of DTMF-rate audio) and **drops the oldest chunk on overflow**,
  exactly like `AudioHub.publish` (ADR 0014). A slow or stuck multimon now costs at most a little
  dropped DTMF audio — never a frozen event loop that starves every listener.
  - Process spawn/respawn stays on the caller in `write()` (an instant `poll()` check under the
    existing lock), so a missing binary still **fails loud on the caller**, not silently in a
    background thread. The writer thread reads the current process fresh per chunk and marks it dead
    on a pipe error; the next `write()` respawns it — the ADR 0038 self-healing is preserved.
  - `close()` unblocks the writer with a `None` sentinel and joins it before tearing the process
    down; the writer also bails on `self._closed`, so a full backlog never delays shutdown.
  - Public surface (`write`/`read`/`close`, the `DtmfStream` protocol) and the buffered path are
    unchanged; `decode_mode = "buffered"` remains the one-line in-field revert.

- **The browser re-primes a smaller cushion after an underrun (`web/src/rxWorklet.js`, ADR 0023).**
  Cold start still primes the full ~150 ms, but an underrun re-primes only ~60 ms (`REPRIME_SAMPLES`),
  so the worst-case gap is proportional to the jitter rather than a flat 150 ms — while keeping enough
  hysteresis to avoid chattering straight back into another underrun.

## Consequences

- **The event loop can no longer be blocked by multimon.** The browser RX stream stays live
  regardless of how multimon behaves; the head-of-line coupling ADR 0038 introduced is removed.
- **Backpressure degrades DTMF, not audio.** Under a sustained multimon stall the write queue drops
  DTMF chunks (rare; multimon is normally faster than real time). Decode fails safe as always — a
  missed digit yields no auth, never a false accept (guardrail 4); the operator re-keys.
- **One more daemon thread per `MultimonStream`** (reader + writer), joined on `close()`. Small and
  centralized, matching the existing reader-thread lifecycle.
- **Not the whole jitter story.** `RxPump.receive()` is still a blocking ALSA read on the event loop
  and the loop still `sleep(poll)`s after it; delivery stays roughly real-time with ~20 ms
  burstiness, which the (now-softened) browser buffer absorbs. Moving `receive()` to a thread
  executor remains a possible future improvement (the pump docstring already flags it) but is out of
  scope here — this ADR removes the unbounded blocking, which was the actual freeze.
