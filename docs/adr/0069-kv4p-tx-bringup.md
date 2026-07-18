# 0069 — kv4p TX bring-up: the measurement rig, two bugs it surfaced, and the first bench numbers

Status: Accepted

## Context

Every kv4p bench session through PR #121 was **RX-only**. The whole transmit path — `ptt()`'s
reconciled `PTT_REQUESTED`→`TX_ACTIVE` handshake and its `Kv4pKeyingError` fail-safe, the firmware
`TX_ALLOWED` gate, `transmit()` (one-shot self-key and streaming), the Opus **encoder** (only the
decoder had run on hardware), the `send_tx_audio` credit window, and `tx_lead_seconds` — had never
touched a radio. Three numbers the backend carried were marked verify-on-bench guesses:
`tx_lead_seconds` (0.2), the encoded frame size, and the flow-control window (2048). Doctor's
`--key-test` / `--tx-tone` existed and were guarded, but printed **no numbers** — a keyed run produced
nothing to record.

The keying itself is inherently a human-at-the-bench action: the RF guards refuse a non-interactive
terminal by design, and the acceptance evidence (what a second receiver hears, a clip-free onset) is a
physical observation. So this cycle **instruments the keyed path** (no keying, tested against fakes),
then the operator keys live (dummy load, **445.800 MHz**, UHF, second receiver) and the measured
numbers are folded in.

## Decision

### The measurement rig (no keying; unit-tested against the fake transport)
- **`transport.TxStats`** — per-keying counters updated under the credit lock: encoded Opus bytes/frame
  (`send_tx_audio`), on-wire escaped bytes, `blocked_frames`, and `min_credits` (`_write_frame`).
  Exposed via `Kv4pTransport.tx_stats` / `window_size`; `reset_tx_stats()` runs at each `_key_on`.
- **`Kv4pHt.tx_stats` / `window_size`** — surface the telemetry to doctor.
- **doctor** — key-up latency in `_kv4p_keying_core`; a pure `_format_tx_stats()` helper that
  `--tx-tone` prints after a keyed run; a kv4p-specific "no tone heard" hint (the stale AIOC
  "alsamixer" text is dropped); and a **`--tx-lead SECONDS`** override so the operator can sweep the
  lead-in without editing `radio.toml` between keyings.
- A bench runbook, **`docs/kv4p-tx-bringup.md`**.

### Two bugs the bring-up surfaced (fixed here)
1. **Doctor ignored `radio.toml`.** Every `load_settings()` call in `doctor.py` passed *no path*, which
   resolves to pure schema **defaults** — the file was never read. So `--key-test` / `--tx-tone` /
   `--rx-level` and the backend auto-resolution silently used the **default** serial port and band. On
   this bench that is actively dangerous: `/dev/ttyUSB0` is a *different* USB-serial device (a DV
   Dongle); the kv4p is on `ttyUSB1`, and there is **no CLI flag for `kv4p.frequency`**, so 445.800 was
   unreachable without the file. Fix: `_doctor_settings()` reads `DEFAULT_CONFIG_PATH`, like
   `radio_server.__main__`, routed through every doctor config helper.
2. **Keying modes gave no next step on a connect failure.** The very first `--key-test` lost the elicit
   handshake (a first-connect / reset-on-open race, ADR 0066) and printed only the raw transport error.
   Fix: on an open failure the keying modes point at the non-keying **connect probe**, which
   distinguishes the race (a retry succeeds) from pre-KISS firmware.

### The bench numbers (445.800 MHz, UHF, dummy load, 2026-07-18)
- **It keys.** `TX_ACTIVE` confirmed, clean unkey; the firmware `TX_ALLOWED` gate works
  (`kv4p.tx_allowed = true`). **Key-up latency ≈ 103 ms.**
- **Audio reaches the air** — a 1000 Hz tone was heard clean on a monitoring receiver.
- **Encoded ≈ 230 bytes/frame** (min 5 for the silence/ramp frames, max 245), **~25 frames/s ≈ 46 kbps** —
  lighter than the retired ADPCM path, but far heavier than the code's "tens of bytes" assumption.
- **Window = 2048 bytes, HELLO-confirmed** — the device's real buffer, holding **~8.6 frames**.
- A one-shot 3 s clip **blocked 28/80 frames** (min credits 15) and recovered every time. This is
  **correct backpressure**: `transmit()` dumps a pre-synthesized clip faster than the device drains it
  at real-time ~25 fps, so the fixed device window paces the host to it. The audio was clean and no
  write ever neared the 2 s timeout.
- **`tx_lead_seconds`: 0.2 clipped the tone's onset; 0.5 started clean** → `DEFAULT_TX_LEAD_SECONDS`
  moves from the 0.2 guess to a bench-measured **0.5** (the same lead the AIOC needed).

## Consequences

- `tx_lead_seconds` is now a bench fact (0.5), not a guess (spec + `radio.toml.example` updated). The
  window size is confirmed at the device's real 2048 — it must **not** be raised past the device buffer,
  so the telemetry's "blocked" line is reframed as expected backpressure, not "raise the window."
- The window is exercised on **every pre-synthesized clip** (TTS, station ID, the DTMF services), which
  all push faster than real time. It is safe as long as `WINDOW_UPDATE` refunds stay under the 2 s write
  timeout — which held with wide margin at real-time drain.
- The doctor `radio.toml` fix changes behavior for **all** backends: a doctor check now reflects the
  operator's real config (serial port, band, frequency, device names) instead of defaults.

## Follow-ups (not this cycle)

- **First-connect reliability** (reset-on-open / lost-elicit race): a retry works; whether to lengthen
  the connect timeout or add a boot-settle retry inside `connect()` is ADR 0066 territory, not TX.
- **Encoder bitrate**: ~230 bytes/frame is high for a pure tone; an explicit Opus bitrate cap would ease
  window pressure but trades audio fidelity — it needs its own analysis and was not changed here.
- **DTMF over kv4p RF** — still the open RX bench item from ADR 0068 (needs a real carrier, not a dummy
  load); and the installer kv4p path / conditional Mumble-banner gate (ADR 0067).
