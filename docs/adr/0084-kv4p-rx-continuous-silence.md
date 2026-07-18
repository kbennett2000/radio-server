# 0084 — Make the kv4p RECEIVE path a continuous stream (silence in the gaps)

Status: Accepted

## Context

Over Mumble via the **kv4p**, the last ~0.5 s of a *received* transmission repeats on the Mumble/phone
side ("Max Headroom") when a signal ends — not on the AIOC. This is the RX mirror of the TX starve
ADR 0082 fixed; the root cause is symmetric.

The two backends present different RX contracts:

- **AIOC** (`aioc_baofeng.py`) reads a **continuous** sounddevice input stream, so `receive()` always
  returns a full-length audio frame — the receiver's silence (noise floor ~ zero) *between*
  transmissions is still audio. The RX→Mumble feed is therefore continuous, so when a signal ends the
  stream tapers into silence and the Mumble client closes it cleanly.
- **kv4p** (`backends/kv4p/radio.py`) is **frame-push**: the firmware sends RX Opus only while a
  signal is present and nothing when the channel is idle. So `receive()` returned decoded frames
  during a signal and an **empty** frame (`AudioFrame(b"")`) on the idle-poll timeout.

The empty frame is the bug. The shared `RxPump` (`rx/pump.py`) skips empty frames
(`if frame.samples:`) *before* the activity gate, so an empty frame never reaches the gate. That
matters because the VAD gate's **hang** (`activity/gate.py::AudioLevelGate`) is what publishes the
trailing silence: for `hang` seconds after the energy drops, the gate stays open and keeps publishing
frames. With the AIOC, those post-signal frames are real (silent) audio, so the hang publishes a
taper and the RF→Mumble subscriber (`link/bridge.py`, subscribed to the same `AudioHub`) tapers into
silence. With the kv4p, the signal ends → the firmware stops sending → `receive()` returns empty →
the pump skips it → the gate is never called → **no taper is published**, the RX→Mumble stream stops
abruptly, and the far-end Mumble/phone client conceals the gap by looping the last ~0.5 s.

## Decision

Give the kv4p RX the AIOC's **continuous-output contract**: on an idle-poll timeout, `receive()`
returns a full-length canonical **silence** frame (`_RX_SILENCE` — 1920 zero samples, the same shape
a decoded packet yields) instead of an empty frame. A real packet still returns immediately when one
is available; only the idle timeout changed from empty → silence.

That single change makes the kv4p RX stream continuous, so every downstream consumer treats it
**identically to the AIOC** — there is no backend-specific branching in the pump or gate:

- **Activity gate / squelch:** a silence frame has zero RMS, so the VAD reads it as "not active." It
  now reaches the gate (non-empty), so the gate's hang publishes the trailing silence for `hang`
  seconds (the taper) and then **closes** — it does not latch the channel open. `squelch=cat` (the
  kv4p's hardware SQ line) is frame-agnostic and unaffected; `squelch=off` (pass-through) publishes
  the silence directly. All match the AIOC.
- **Recording:** gate-open frames are recorded, so the trailing hang-silence is written into the
  segment and the segment ends when the gate closes — exactly as with the AIOC. Idle silence with the
  gate closed is not recorded (the gate rejects it; `end_segment` is idempotent).
- **DTMF:** zeros decode to nothing, so the continuous silence floor changes no decode (the same as
  the AIOC's continuous silence, which DTMF already tolerates).

### One diagnostic guard

`doctor measure_rx_levels` (the `--rx-level` ADR-0070 ADC-clock calibration) counts received frames
and averages their RMS. It now **skips fully-silent (all-zero) frames**, so the continuity fill (and
true inter-transmission silence) can't dilute the average RMS or inflate the frame-rate estimate. The
calibration measures real received audio only — run it with a signal present, as ADR 0070 already
instructs.

## Consequences

- The received-transmission tail no longer loops on the Mumble/phone side; the kv4p RX now behaves
  like the AIOC. Bench-confirmed by the operator over Mumble (this cycle is software/fakes only).
- **Cadence note (for the bench):** the firmware sends nothing when idle, so the continuity silence is
  produced at the `receive()` idle-timeout cadence (`DEFAULT_RECEIVE_TIMEOUT`, 0.1 s) rather than
  real-time. That is enough to break the *sustained* "stream stopped" loop (the far end keeps getting
  fresh silence instead of concealing a permanent gap). If a smoother taper is wanted, lowering
  `DEFAULT_RECEIVE_TIMEOUT` toward the 40 ms frame interval is a follow-up tuning knob — kept at 0.1 s
  here so a healthy signal's inter-packet jitter never trips the timeout mid-signal (injecting a
  silence blip into live audio).
- No change to the TX pacer (ADR 0082 owns kv4p→RF), no AIOC change, no `mumble.tx_hang` change, no
  schema change (`radio.toml.example` byte-identical, canary unmoved), no new backends.

Cross-refs: ADR 0082 (the TX mirror), ADR 0064/0065 (the kv4p Opus RX edge), ADR 0070 (the
`--rx-level` calibration the diagnostic guard protects), ADR 0045 (the activity gate / RF→Mumble
keying), the AIOC backend (ADR 0029) whose contract this mirrors.
