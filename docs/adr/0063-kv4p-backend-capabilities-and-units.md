# 0063 — kv4p HT backend: capabilities, the SCAN reversal, and unit mapping

Status: Accepted

## Context

ADRs 0061 (`frames.py` wire codec), the audio edge (`audio.py`), and 0062 (`transport.py` — reader
thread, encoded-byte window, reconciler) built the kv4p HT bottom-up as hardware-free layers. This
cycle composes them into `radio_server/backends/kv4p/radio.py` — the **`Kv4pHt` class implementing
the `Radio`/`CatRadio` surface** (ADR 0002). It is the first real `CatRadio` backend, the first
backend with a genuine hardware busy line, and the first where the software `ScanEngine` can run on
real hardware. It is still built and tested against a **fake transport** (guardrail 6 — hardware
bring-up is its own phase); factory/config/`app.py` wiring and `doctor` are a later cycle.

Two decisions are neither obvious nor derivable from the layers below, so they are recorded here.
Both rest on firmware facts **read as a specification** (kv4p-ht GPL-3.0 @
`e9935bd37e7505f70ae7023c78fe6a714be90be9`, `kv4p_ht_esp32_wroom_32.ino` / `protocol.h`), not
asserted from memory; the values we cannot read from a header are marked verify-on-bench
(guardrail 1).

The class also encodes one load-bearing invariant worth stating up front. **`HostDesiredState` is a
complete state, not a partial update:** the firmware's `handleCommands` does
`desiredState = incomingState; desiredState.flags &= HOST_STATE_GLOBAL_FLAG_MASK` — the whole struct
and the whole global-flag word are replaced every frame, so a flag you set last time but omit now is
silently cleared. `Kv4pHt` therefore owns a complete desired-state model and every mutation is
read-modify-write-the-whole-thing, then reconcile. Two global flags must ride **every** frame:
`RADIO_CONFIG_VALID` (gates the entire `sa818.group(...)` apply — drop it and frequency/tone/squelch
stop reaching the module) and `TX_ALLOWED` (hard-gates PTT, persists to NVS, defaults false — drop
it and `ptt(True)` is accepted, reconciles cleanly, and never keys, with no error anywhere).
`RX_AUDIO_OPEN` (a session flag) likewise rides every frame so RX audio flows. On key-up we set
`PTT_REQUESTED` **and assert `TX_ACTIVE` came back**, so a silent no-key surfaces as a raise
(`Kv4pKeyingError`) instead of dead air.

## Decision 1 — capabilities, and the SCAN reversal

`Kv4pHt.capabilities()` returns `SHARED_CAPS | {SET_FREQUENCY, SET_TONE, SET_MODE, SCAN}` — it
**omits `SET_CHANNEL`**, and it **includes `SCAN`**, which reverses what ADR 0061 tentatively
recorded.

- **`SCAN` is in.** `Capability.SCAN` does not mean "the radio has a native scan button"; in this
  tree it gates the **software** `ScanEngine` (ADR 0012), whose own docstring disclaims the hardware
  toggle. That engine steps a plan of frequencies, tunes each via `set_frequency`, lets the reading
  settle, and polls `status().busy`. kv4p has a real `set_frequency` **and** a real busy line, so
  the software sweep genuinely works here — the first backend where it runs on hardware at all.
  `api/app.py`'s `/scan` gates on `Capability.SCAN`, so advertising it is what lets the sweep run.
- **`radio.scan(on)` (the hardware toggle) therefore has no device meaning and raises**
  (`NotImplementedError` with a message pointing at the software engine). This is not a
  contradiction with advertising `SCAN`: the capability is about the software sweep, the method is
  the (absent) hardware toggle. `radio.scan()` is in fact **dead code across the whole tree** — only
  `tests/test_capabilities.py` calls it — so `Capability.SCAN` is overloaded. Flagged here as a
  possible future tidy (split the capability, or delete `Radio.scan`); **not** done this cycle.
- **`SET_CHANNEL` is out.** The wire's `memory_id` is an opaque host-side tag: the firmware only
  echoes it and diffs it to trigger a reconcile — there is no memory table on the device.
  `set_channel` raises `UnsupportedCapability(Capability.SET_CHANNEL)`.

## Decision 2 — unit mapping (the wire does not speak our types)

The `Radio`/`CatRadio` API is int Hz, a free-text mode, and CTCSS Hz; the wire is float MHz, a
DRA818 bandwidth code, and CTCSS **indices**. We convert, and where a value does not map we **fail
loud** rather than clamp-or-snap-and-lie — a wrong reading in `status()` is worse than a raise.

- **Frequency** — `HostDesiredState.freq_tx`/`freq_rx` are float MHz; `set_frequency` takes int Hz.
  There are separate TX and RX frequencies but the `Radio` protocol has one value, so we set **both**
  (simplex). Split/offset is out of scope — a **future ADR**, not an invented API. We validate
  against the HELLO's min/max (falling back to a per-module default band when no HELLO arrived) and
  **raise out of band** — the firmware clamps silently (`clampModuleRadioFreq`), which would make
  `status()` report a frequency the caller never asked for. The set frequency is quantized to the
  SA818 raster (a marked default, verify-on-bench).
- **Tone** — `ctcss_tx`/`ctcss_rx` are uint8 **indices** into the standard 38-tone CTCSS table
  (0 = off, 1..38), not Hz. `set_tone(hz)` maps through that table and **rejects an unmapped value**
  rather than snapping to the nearest. We set **`ctcss_tx` only** and leave `ctcss_rx` at 0: repeater
  access (a TX tone) is the case that matters, and RX tone squelch would silence the receiver in a
  way nothing in our stack can observe. (The 38-tone table is a public EIA table, not firmware code;
  the exact index↔Hz mapping the module uses is verify-on-bench.)
- **Mode** — there is no mode field on the wire, only `bw` (DRA818 25 kHz / 12.5 kHz). We map our
  free-string `mode` onto the only mode-shaped knob the radio has: `FM → 25 kHz`, `NFM → 12.5 kHz`,
  and reject anything else. The bandwidth code integers, the raster, the per-module default bands,
  and the TX lead-in are all marked defaults, **verify-on-bench** (guardrail 1).

## What the backend does (built this cycle)

- **`transmit` / `ptt`** mirror `AiocBaofeng`'s `_keyed` one-shot-vs-streaming discipline: a lone
  `transmit(clip)` self-keys for exactly that clip; an explicit `ptt(True)` holds the key across many
  `transmit(frame)` calls until `ptt(False)`. Keying here is a **reconciled `PTT_REQUESTED` flag**,
  not a control line; TX audio is `HOST_TX_AUDIO` blocks (48k → the audio edge's ADPCM re-blocker),
  written through the transport's flow-control window via a new `send_tx_audio`. A `tx_lead_seconds`
  knob prepends silence on key-up — its value is **unknown** (the reconcile round-trip has its own
  latency), a marked default to bench-tune, not the AIOC's 0.5 s by analogy.
- **`receive`** polls the transport's bounded RX queue (blocking ~one block) and decodes each ADPCM
  block to one canonical `AudioFrame`.
- **`status`** reports `busy = not SQUELCHED` (a genuine carrier detect off the module's SQ pin —
  this is what makes `audio.squelch="cat"` valid for this backend), `transmitting = TX_ACTIVE`,
  `frequency` from `freq_rx`, and `tone`/`mode` inverted through the same tables.

## Consequences

- **Fully testable with zero hardware:** `tests/test_kv4p_radio.py` — a `FakeTransport` that echoes
  the last desired state as a synthesized `DeviceState`. Cases: the whole-word flag regression
  (`set_frequency` then `ptt(True)` still carries `RADIO_CONFIG_VALID` + `TX_ALLOWED` +
  `RX_AUDIO_OPEN`); a withheld `TX_ACTIVE` raises rather than silently not keying; unit conversions
  (Hz→MHz on both legs, Hz→CTCSS index, unmapped tone and out-of-band frequency both raise before
  anything is sent); capabilities exactly as specified with `set_channel`/`scan` raising; `status()`
  busy/transmitting/frequency; the one-shot-vs-streaming keying discipline; and `receive()` decode +
  clean timeout. Full suite green (**918 passed, 5 skipped** — 901 baseline + 17).
- **One change outside `radio.py`:** the transport gained a public `send_tx_audio(block)` — TX audio
  is the bulk of the link and must ride the same credit window, but the transport (its own cycle)
  exposed only `send_desired_state`. It reuses the existing private flow-controlled writer.
- **Verify-on-bench (guardrail 1), recorded not asserted:** the DRA818 bandwidth code integers; the
  CTCSS index↔Hz mapping; the SA818 tuning raster; the per-module default frequency bands; and the
  `tx_lead_seconds` value.
- **Deferred to the wiring cycle:** factory registration, `config/spec.py`, the `app.py` backend
  branch, `doctor` bring-up, and relaxing the `audio.squelch="cat"` rejection (`api/app.py`) now
  that this backend reports a real `busy`. Also noted for the config cycle: the `squelch` **level**
  (0..8) feeds the same module, and at level 0 the SQ pin never asserts, so `busy` would read True
  forever and a CAT-squelch scan would dwell on every channel — a sane non-zero default is that
  cycle's call, the real number verify-on-bench. And `RUNAWAY_TX_SEC = 200`: the device self-drops
  TX after ~200 s regardless of what we requested; `status().transmitting` reports the truth via
  `TX_ACTIVE`.
