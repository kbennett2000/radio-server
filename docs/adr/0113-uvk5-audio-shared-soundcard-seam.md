# 0113 — UV-K5 (Quansheng Dock): AIOC audio via a shared sound-card seam

Status: Accepted

## Context

Cycle 3 (ADR 0112) left `Uvk5Radio` control/keying/status-complete but with `transmit()` and
`receive()` raising — audio deferred. The UV-K6's audio is the **AIOC's USB sound card**
(`sounddevice`/ALSA), the exact class of path `AiocBaofeng` (ADR 0029) already runs: the one AIOC
cable presents a serial control line **and** a USB sound card. This cycle makes `Uvk5Radio`
audio-complete by **reusing** that machinery rather than duplicating it. Keying stays the BK4819
register path from ADR 0112 (there is no serial-line PTT on this backend).

Two facts shaped the design:

- **kv4p is not a model here.** kv4p audio is Opus-over-UART with its own pacer/encoder; the
  sound-card contract to mirror is `AiocBaofeng`. The canonical RX contract the pump
  (`rx/pump.py`) expects is a **full-length canonical `AudioFrame`** (48 kHz / s16le / mono),
  blocking ~one block — which a continuous capture stream satisfies naturally (no kv4p-style
  synthesized silence is needed; the card always returns a full block). `TxSession` owns keying
  (`ptt(True)` … `transmit(frame)*` … `ptt(False)`) and the one-shot-vs-streaming discipline; the
  backend owns the TX lead-in silence.
- **All of the AIOC sound-card machinery was inline in `aioc_baofeng.py`** — the only shared seam
  was `AudioFrame`/`CANONICAL_FORMAT`. So genuine reuse required extracting a shared seam.

## Decision 1 — extract a shared, PTT-independent `soundcard` seam (behaviour-preserving)

New module `radio_server/backends/soundcard.py` holds everything both backends share, all of it
PTT-independent:

- `SoundCardTxPacer` — the daemon-thread playout writer (ADR 0102), moved verbatim from
  `aioc_baofeng._AiocTxPacer` (public API `enqueue` / `wait_drained` / `dropped_bytes` / `error` /
  `stop`; bounded drop-oldest; `on_error` unkey hook).
- `open_capture_stream` / `open_playout_stream` — open + start the canonical 48 kHz s16le mono raw
  streams on a named device.
- `load_sounddevice(injected, *, extra_hint)` — the lazy import / test-injection seam (catches
  `ImportError` **and** `OSError`, since PortAudio loads at import); each backend passes its own
  extra-hint string.
- `lead_in_bytes` / `playout_buffer_bytes` and the shared `DEFAULT_*` device / block / lead /
  buffer constants.

**The extraction is behaviour-preserving, verified by an untouched baofeng suite.** `AiocBaofeng`
keeps its exact attribute surface (`_pacer`, `_playback`, `_capture`, `_audio_mod`, `_lead_bytes`,
`_keyed`) and every RF-safety invariant (open-stream-first key-up with atomic line-assert undo;
drop-line-first teardown), now delegating the stream/pacer/byte-math to the shared helpers. The
moved names are **re-exported** from `aioc_baofeng` (`_AiocTxPacer = SoundCardTxPacer`, plus the
`DEFAULT_*` constants) so its tests, `doctor.py`, and `config/spec.py` keep importing them from
`aioc_baofeng` **with no edit at all** — not even import paths. `tests/test_aioc_baofeng.py` is
byte-for-byte unchanged and green (35 passed, 1 hardware-skip). The kickoff's STOP condition
("reuse requires changing baofeng behaviour") therefore did not trigger.

## Decision 2 — `Uvk5Radio` composes the seam; the audio wraps the register keying

`receive()` lazily opens a capture stream and returns `AudioFrame(bytes(read(blocksize)),
CANONICAL_FORMAT)` — mirroring baofeng. `transmit(frame)` fails loud on a non-canonical format,
then **streaming** (`_keyed`) enqueues to the pacer, or **one-shot** self-keys / enqueues /
`wait_drained` / re-raises any pacer error / unkeys in `finally`.

The novel part is interleaving the sound card with ADR 0112's register keying, preserving the
baofeng RF-safety ordering:

- **`_key_on()`**: frequency-set guard first (no stream leak) → open the playout stream + start
  `SoundCardTxPacer(on_error=self._key_off)` **before** keying (a failed audio open never writes a
  TX-enable register) → write the BK4819 TX-enable sequence and **confirm by read-back**
  (`reg 0x30 == 0xC1FE`) → on **any** failure (no-confirm `Uvk5KeyingError`, or a transport error),
  undo the whole key-up via `_key_off()` and re-raise → on success, enqueue the TX lead-in.
- **`_key_off()`**: restore RX registers **first** (unconditional, RF-safe), **then** stop the
  pacer (discard) and tear down the stream — best-effort and non-raising, so a transport error
  while unkeying can neither mask the teardown nor break `close()` / the one-shot `finally`. It is
  also the pacer's `on_error`, so a playout write that dies mid-over unkeys the register TX.

Constructor gains `input_device` / `output_device` / `blocksize` / `tx_lead_seconds` and an
`_audio` test seam; `close()` also tears down the capture stream. The `Uvk5KeyingError`
read-back-confirm semantics from ADR 0112 are intact — a silent no-key never becomes dead air.

## Decision 3 — the `uvk5` extra (ADR 0067 discipline)

`uvk5 = ["radio-server[serial]", "radio-server[soundcard]"]` — serial + soundcard, **no opus**
(UV-K5 audio is raw PCM, not the kv4p Opus transport), composed from existing leaves. `uv lock`
records only the new extra grouping: **no package version moved**, and the `hardware` / `kv4p` /
`mumble` closures are byte-identical — the deployed box (which installs `hardware`) is untouched.
The `uvk5` transport / audio lazy-import messages now name `radio-server[uvk5]`.

## Consequences

- 6 new audio tests in `tests/test_uvk5_radio.py` (reusing the baofeng `FakeAudio` fakes per the
  kickoff): `receive()` canonical round-trip; non-canonical `transmit` rejected; one-shot
  `transmit` plays `[clip]` and drops; lead-in `[lead, clip]`; streaming holds one stream across
  frames; and the load-bearing **full sequence** tune → register-confirmed key → transmit → unkey
  driven against `FirmwareFakeSerial` **and** the fake sound card together (asserting both the
  `0xC1FE` confirm and the playout `[lead, frame]`). Full suite **1393 passed, 5 skipped**, incl.
  the unchanged baofeng suite (the extraction proof).

### Verify on hardware (guardrail 1 — no bench numbers exist; none fabricated)

- **The acceptance gate for bench day (carried from ADR 0112).** Whether register-keying in
  full-control (XVFO) mode actually transmits the **AIOC-injected K1 mic audio** — versus nothing,
  or the wrong source — **cannot be settled offline**. Nothing in this cycle claims it does; this
  is the single gate the whole UV-K5 TX path must pass on the bench before it is trusted.
- **TX lead-in.** The `0.5 s` default is inherited from the AIOC/UV-5R bench (kv4p proved the lead
  is RF physics). This radio earns its **own** bench number — bench-tune `tx_lead_seconds`.
- **Device selection / xrun robustness** — the AIOC card name and `blocksize` against this radio's
  real codec, as for baofeng.

### Recorded for later cycles (this ADR builds none of it)

The `[uvk5]` config block + factory registration + settings-API canary; `doctor` wiring; the
server-side **presets** feature; the web UI; and the stuck-key **watchdog/TOT** (tracked from ADR
0112 — the full-control loop has no time-out).
