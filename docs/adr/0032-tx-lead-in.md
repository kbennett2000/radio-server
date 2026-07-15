# 0032 — TX lead-in: silence after key-up so speech isn't clipped

Status: Accepted

## Context

On the bench, every transmitted clip (service TTS, station ID, `/transmit`) lost its **first ~0.5 s**
over the air — the leading word of an announcement, and part of the station ID, were simply not heard.

This is a hardware key-up race, not an audio bug. `AiocBaofeng._key_on()` opens the playback stream,
asserts the PTT serial line, and returns; the caller then *immediately* writes the real PCM
(`transmit()` → `_playback.write(audio.samples)`). But a UV-5R does not put RF on the air the instant
the line is asserted — the transmitter takes a few hundred ms to come up — and the *receiving* radio's
squelch needs time to open on that new carrier. So the first fraction of a second of audio is emitted
before the RF path exists and is lost.

The **tail** was already protected: `_key_off()` drains the stream (`stream.stop()` blocks until pending
buffers finish) before dropping the line. There was **no head/lead protection** anywhere in the
codebase — the `_key_on()` docstring even noted "the stream only emits silence until `transmit`
writes," but that silence was of uncontrolled (effectively zero) duration.

## Decision

Add a **TX lead-in**: a fixed, configurable slug of silence written to the playback stream
**immediately after the PTT line is asserted**, before the caller's real audio. The radio keys up and
the far-end squelch opens *during* the silence; speech then starts on an established link.

- **Where:** in `AiocBaofeng._key_on()`, right after the line is asserted and `_transmitting = True`.
  This method backs **both** transmit shapes — one-shot `transmit()` (self-key) and streaming
  `ptt(True)` (Talk / `/audio/tx`) — so the lead-in fires **exactly once per physical key-up**,
  covering every path that keys the radio. The RF-safety ordering is preserved: device opens → line
  asserts → lead silence → real audio (a failed device-open still never asserts the line).
- **What:** raw zero PCM (`b"\x00" * lead_bytes`), `lead_bytes` precomputed once in `__init__` from
  `round(rate * tx_lead_seconds) * frame_bytes`. Silence is harmless, drains through the existing tail
  logic, and needs no `AudioFrame` (the stream takes raw bytes).
- **Config:** `baofeng.tx_lead_seconds` (env `RADIO_BAOFENG_TX_LEAD`, default **0.5 s** — the observed
  clip; `coerce_nonneg_float`, so **0 disables**). Per-hardware (guardrail 1): bench-tune per radio.

### Why not the `StationId` seam

Dispatched service audio flows through `StationId.transmit()` (which prepends the ID), so a lead-in
there would seem to cover announcements. It was rejected: `StationId` is format-agnostic and prepends
via `AudioFrame.__add__`, whose fail-loud format check rejects the symbolic **non-PCM stub payloads**
(`b"<id:AE9S>"`) the deterministic tests rely on — prepending silence PCM would break the
exactly-assertable `tx_log` tests and fail on stub frames. It would also miss the raw paths (`/transmit`,
`TxSession` streaming, `check()`/`sign_off()` IDs). The lead-in is a **hardware-keying** concern and
belongs in the backend, applied once per key-up — where SignaLink's future audio-triggered PTT will
also want its own (VOX-appropriate) head handling.

## Consequences

- Announcements are heard in full — the leading word and the station ID are no longer clipped.
- Every keyed over is ~0.5 s longer by default; negligible for TTS announcements, and tunable (raise
  if a given radio still clips, lower or 0 if the pause drags). Streaming Talk gets one lead-in at
  key-up, not per frame.
- Mock/CI stay hardware-free: the lead-in lives in the AIOC backend and is unit-tested through the
  injected `_audio` fake by asserting the leading zero buffer's exact byte length.
