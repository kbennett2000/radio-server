# 0080 — Give the kv4p a TX audio-level control (`kv4p.tx_gain`)

Status: Accepted

## Context

Announcements and voice are **overmodulated on the kv4p**. Root cause, verified in the code and the
shipped firmware (`3f0e809`):

- The firmware TX path applies **no gain** — `processTxAudio` → `txOut.write`, with none of the
  `Boost(16.0)` the RX path uses.
- The kv4p backend encodes canonical audio to Opus with **no attenuation**
  (`TxAudioEncoder`, `radio_server/backends/kv4p/audio.py`).

TTS/CW is generated near full-scale int16, so with nothing between it and the SA818 it over-deviates
the transmitter. The AIOC backend transmits the *same* canonical audio without this problem because
it rides the OS mixer: `alsamixer`'s playback slider is its TX-level stage. The kv4p has **no sound
card** — audio goes out over the UART as Opus — so there is no equivalent analog knob, and **no
backend had a software TX-level setting** at all.

This is the TX analog of ADR 0070's RX sample-rate correction: a per-hardware fact that must live in
config with a marked, verify-on-bench default, not be baked into the codec.

### One correction to the obvious mental model

Canonical audio here is **not** float in `[-1, 1]`. It is signed 16-bit LE PCM
(`CANONICAL_FORMAT` = 48000 Hz / 16-bit / mono; `AudioFrame.samples` is opaque `bytes`). At the
encoder it is a numpy `int16` array. So the gain acts on int16 samples and clamps to full-scale
int16 (`±32767`), mirroring the existing RX-correction clip idiom (`RxAudioDecoder._correct`) — not
to `[-1, 1]`.

## Decision

Add **`kv4p.tx_gain`** — a float multiplier (default `1.0`) applied to the canonical audio in the
kv4p TX path **before the Opus encoder**.

- **One choke point.** Every kv4p TX byte — a streaming `transmit()`, a one-shot `transmit()`, and
  the key-up lead-in silence — flows through a single per-keying `TxAudioEncoder` (`self._tx`, built
  in `_key_on()`). The gain is applied inside `TxAudioEncoder.push()`, on the int16 samples, before
  they enter the re-block accumulator. So it attenuates **everything transmitted** (announcements,
  browser mic, Mumble), not one source, and both transmit paths inherit it automatically because
  they share the encoder.
- **`_apply_tx_gain(samples, gain)`** (a small pure function): `gain == 1.0` returns the buffer
  untouched — an **exact int16 no-op**, so the default changes nothing for anyone. Otherwise the
  samples are promoted to float, multiplied, **clamped** to `±32767`, and rounded back to int16, so a
  `gain > 1.0` clamps to full scale rather than wrapping around into the encoder.
- **Threaded like the other kv4p knobs.** `DEFAULT_TX_GAIN = 1.0` on the backend class (the config
  source of truth, marked verify-on-bench, guardrail 1); a `tx_gain` constructor kwarg carried into
  the encoder; a `kv4p.tx_gain` spec (`coerce_positive_float`, matching `sample_rate_correction`);
  `backend_kwargs()`; and the doctor resolver. Because both the initial build and the ADR 0076 live
  rebuild go through `build_radio → backend_kwargs`, the setting is honoured on a live backend switch
  with no extra wiring.

`coerce_positive_float` (not a `≤ 1.0` cap) is deliberate: a value above `1.0` is a valid setting —
the *audio* clamps, the setting is not rejected — while `0` and negatives are.

### Why the AIOC gets no equivalent

The AIOC/Baofeng backend writes PCM straight to a sounddevice output stream with no scaling; its TX
level is entirely `alsamixer`'s job. Adding a software gain there would fight the OS mixer and
duplicate a control the operator already has. The kv4p needs one precisely *because* it has no mixer.

## Consequences

- Operators can bring an overdriven kv4p down to clean modulation without touching the firmware:
  lower `kv4p.tx_gain` until announcements/voice sound clean (documented starting point `~0.5`).
- Default `1.0` is a byte-for-byte no-op — existing kv4p deployments are unchanged until they opt in.
- New tests:
  - `_apply_tx_gain` unit behaviour — `0.5` halves the amplitude; `1.0` is an exact int16 identity;
    `2.0` on a 20000-amplitude sample **clamps to ±32767** rather than wrapping
    (`tests/test_kv4p_audio.py`).
  - the encoder scales the pre-encode accumulator — a sub-frame push (nothing encoded, no libopus)
    leaves `TxAudioEncoder(tx_gain=0.5)._acc` holding the halved samples
    (`tests/test_kv4p_audio.py`).
  - the setting reaches the live encoder built at key-up, and defaults to unity
    (`tests/test_kv4p_radio.py`); a one-shot `transmit()` is attenuated **end to end** — decode the
    emitted Opus and confirm the transmitted energy halves (`tests/test_kv4p_radio.py`).
  - the setting resolves/coerces (default `1.0`; `>1.0` accepted; `0`/negative rejected) and wires
    through to the constructor (`tests/test_config.py`, `tests/test_backend_wiring.py`).
- Schema grows by one: the settings-count canary moves 63 → 64 and `radio.toml.example` is
  regenerated with the `[kv4p] tx_gain = 1.0` entry and its "why" comment.

## Non-goals

No AIOC change (`alsamixer` owns its TX level). No limiter/AGC — a plain gain is the control asked
for, and a dynamics processor is a different, larger decision. No RX change (that is ADR 0070's
sample-rate correction). No new backends. Cross-refs: ADR 0076 (live rebuild path), ADR 0065 (kv4p
Opus codec), ADR 0070 (the RX-side per-hardware-fact precedent).
