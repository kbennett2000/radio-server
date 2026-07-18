# 0070 — kv4p RX sample-rate correction: the firmware `*1.02` offset that broke DTMF (and drifts every RX consumer)

Status: Accepted

## Context

DTMF never decoded on the kv4p backend. Three separate analyses had cleared the obvious suspects —
the Opus **codec** (decodes cleanly), the **level** (`--rx-level` shows healthy RMS), and **clipping**
(no saturation) — and each was right. The cause is one line of **shipped firmware**, not host code.

Verified against the pinned source (kv4p-ht @ `3f0e809`, the commit ADR 0064 already tracks):

- `rxAudio.h` — `config.sample_rate = AUDIO_SAMPLE_RATE * 1.02; // 2% over sample rate to avoid buffer underruns`.
  The RX **ADC is clocked ~2 % fast** (48000 → ~48960 Hz).
- The Opus RX encoder is handed `AudioInfo rxInfo(AUDIO_SAMPLE_RATE, 1, 16)` — the **unmultiplied**
  48000. So the board captures at ~48960 but labels the stream 48 kHz: the host is never told, and
  every received sample arrives ~2 % off.
- `globals.h` — `AUDIO_SAMPLE_RATE 48000`, `SAMPLING_RATE_OFFSET 0`; `txAudio.h` uses the unmultiplied
  rate. **The offset is RX-only. TX is clean.**

Why it kills DTMF specifically: the native `GoertzelStream` (ADR 0054) is a matched single-bin DFT at
8000 Hz, `N = 205`, bin spacing `8000/205 ≈ 39.0 Hz`, with no offset accommodation. A 2 % error moves
1633 Hz by ~32.7 Hz — most of a bin — so tones fall off their bins and fail the validity gauntlet
(energy floor, twist, group dominance, harmonic rejection). Every DTMF test in the suite used **exact**
frequencies, which is precisely why they all passed while real RF never decoded. Corroborated on the
bench: `--rx-level` counted ~25.8 frames/s against a nominal 25.

**This is wider than DTMF.** 2 % is a real clock drift for every *continuous* RX consumer: the RX hub,
the recorder, and especially the Mumble link accumulate ~1.2 s/min against a 48 kHz sink. Inaudible as
pitch, but an unbounded buffer-growth bug. One correction at the decode edge fixes all of them.

The irony worth recording: PR #118 deleted the kv4p soxr resamplers reasoning *"Opus is native
48 kHz, no resample needed."* True of the **codec**, false of the **ADC**.

## Decision

**Correct the RX stream back to a real 48 kHz at the decode edge**, and make the rate a measured,
per-hardware config fact — not a hardcoded constant.

- **`RxAudioDecoder` (backends/kv4p/audio.py)** gains `sample_rate_correction: float = 1.0`. At `1.0`
  it is a byte-for-byte pass-through (the generic decoder is unchanged). When it differs, `push()`
  streams the decoded PCM through a **stateful `soxr.ResampleStream`** from the true device rate
  (`round(48000 × correction)`) to 48000, quality **HQ** — the `GoertzelStream` precedent (ADR 0054),
  *not* the VHQ one-shot whose ~150 ms buffering is a latency trap on a live path. Output frames run
  ~2 % shorter (~1882 samples); fine — `AudioFrame` is format-identity-only, no length contract.
- **Config knob `kv4p.sample_rate_correction`** (`DEFAULT_SAMPLE_RATE_CORRECTION = 1.02` in
  backends/kv4p/radio.py; spec + `radio.toml.example`), threaded through both construction paths
  (`api/app.py` and `doctor._build_backend`). Marked default, **verify-on-bench** (guardrail 1): the
  firmware *requests* 48960 but the ESP32 I2S divider quantizes it, so the real rate must be measured.
- **doctor `--rx-level` prints the measured true rate.** The device emits exactly one 1920-sample Opus
  packet per 1920 ADC samples, so the packet arrival rate reveals the true ADC clock **independent of
  any host-side correction**: `fps × 1920` is the real capture rate and `/ 48000` is the correction to
  set. A long window (`--seconds 30`) averages out USB jitter; short windows are flagged, not trusted.
- **The regression the DTMF suite was missing:** feed a tone shaped by the firmware offset through the
  native decoder and assert it **fails**, then through the correction and assert it **decodes** — for
  each of `1234#`. Exact-frequency tests never could have caught this.

## Consequences

- **DTMF can decode on kv4p RF** (pending the bench confirmation below), and the hub/recorder/Mumble
  clock drift is removed in the same change. "DTMF fix" undersells it; the ADR says so on purpose.
- The correction runs on **every** RX frame when engaged — cheap (a ~2 % soxr HQ stream) and exactly
  the point: one edge, all consumers.
- The correction is a kv4p hardware fact **threaded from config**, not baked into the codec — the
  generic decoder default stays `1.0`, so nothing else in the stack changes.
- **Also fixed here:** `DEFAULT_CONNECT_TIMEOUT` 2.0 → 10.0 (transport.py). Opening the port resets the
  ESP32, so a fresh connect races the ~1 s boot and 2.0 s intermittently lost the elicit, failing
  `--rx-level` — ADR 0069's deferred first-connect item, one constant.

## Bench acceptance (RX only — no keying; the operator's, per ADR 0069)

1. `doctor --backend kv4p --rx-level --seconds 30` on 445.800 → read the measured true rate; trim
   `kv4p.sample_rate_correction` to `rate / 48000` if it isn't ~1.02.
2. `doctor --backend kv4p --dtmf` → key `1234#` from a handheld; the digits decode. **This is the last
   open item between here and a working node.** Record the measured rate and the decoded digits.

## Follow-ups (not this cycle)

- Confirm the measured rate on the bench and lock `kv4p.sample_rate_correction` to it.
- Encoder bitrate cap (ADR 0069) and the installer kv4p path / conditional Mumble-banner gate (ADR
  0067) remain open.
