# 0065 — kv4p HT: the Opus audio codec (replace the dead ADPCM edge)

Status: Accepted

## Context

ADR 0064 re-pinned the kv4p wire spec to shipped firmware **v2.0.0.1** (`3f0e809…`): RX/TX audio ride
vendor command `0x07` as **Opus**, not the unreleased `e9935bd` line's `0x0C` + IMA-ADPCM. That cycle
corrected the command IDs and *marked the ADPCM layer dead*, but deliberately wrote no codec — pinning
the source before inferring from the wire. It left the tree in a known interim state: `Kv4pHt.receive()`
recognised `0x07` frames but could not decode them (the only decoder was the ADPCM path, which raises on
any length ≠ 128), guarded by a scaffolding length-drop so a live board would not crash.

This ADR records **implementing** the Opus codec. Audio now actually crosses the backend.

The parameters are from shipped source (`rxAudio.h` / `txAudio.h` @ `3f0e809…`, read as a spec — ADR
0064), not the wire:

| | Value |
|---|---|
| Sample rate / channels / width | 48 kHz / mono / s16le — **already `CANONICAL_FORMAT`** (ADR 0006) |
| Frame | 40 ms = **1920 samples = 3840 bytes** (`OPUS_FRAMESIZE_40_MS`) |
| Application / rate control / bandwidth | `OPUS_APPLICATION_AUDIO` / VBR (`vbr = 1`, no explicit bitrate) / `max_bandwidth = OPUS_BANDWIDTH_NARROWBAND` |
| Framing | one Opus packet per `0x07` KISS frame, **no length prefix**, bounded by `PROTO_MTU = 2048` |

Because Opus is natively 48 kHz — identical to the canonical rate — the entire block-math layer the
ADPCM edge needed is gone: no 16 k↔48 k `soxr` resamplers, no 128-byte block, no 249↔747 re-blocking.

## Decision

**1. Rewrite `backends/kv4p/audio.py` from IMA-ADPCM to Opus (`opuslib`).** Deleted: `decode/
encode_adpcm_block`, `AdpcmEncoder`, `StreamResampler`, the ADPCM RX/TX bodies, the IMA step/index
tables, and the `soxr` dependency of this module. New surface, same class names:

- **`RxAudioDecoder.push(packet) -> AudioFrame`** — `opuslib.Decoder(48000, 1)`, one packet → one
  canonical frame (1920 samples for a 40 ms firmware frame). **No re-blocking**: `AudioFrame` is
  format-identity-only with no length contract (audio/format.py, the still-true reasoning carried over
  from PR #111), so one packet is one frame.
- **`TxAudioEncoder.push(frame) / .flush()`** — `opuslib.Encoder(48000, 1, OPUS_APPLICATION_AUDIO)`,
  re-blocking arbitrary 48 kHz input to exact 1920-sample frames (the **only** re-blocker left). `flush`
  zero-pads the final partial frame to 1920 and encodes it — padding, never dropping, so every input
  sample ships.

**2. Match the TX encoder to the firmware's decoder, not to taste.** Opus is self-describing, so a
wrong encoder setting decodes fine and merely *sounds wrong on the air* — the worst failure mode
available. The encoder therefore mirrors the firmware's own RX encoder: `OPUS_APPLICATION_AUDIO`,
`vbr = 1`, `max_bandwidth = OPUS_BANDWIDTH_NARROWBAND` (ADR 0064). Frame size is 40 ms to match.

**3. Load libopus lazily, and fail loud and actionable when it is absent.** libopus is loaded through
the shared shim `radio_server/link/_opus.py` `ensure_opus_loadable()` (ADR 0056/0057 — the same
carrier-wheel path the Mumble link uses), then `import opuslib`. This happens on the **first
encode/decode**, not at import and not at `Kv4pHt` construction, so the ~30 codec-free backend unit
tests need no libopus. A missing `opuslib` or missing libopus becomes **`Kv4pOpusUnavailable`** carrying
`opus_install_hint()` — a clear install message, not an `ImportError` three frames down the codec. A
missing libopus is a *configuration* error and surfaces; a *corrupt wire packet* (`opuslib.OpusError`)
is dropped inside `push` (empty frame, no raise) so a bad byte can never kill the RX reader/consumer.

**4. `receive()` drops the ADR-0064 scaffolding.** The `len(block) != AUDIO_FRAME_BYTES` guard existed
only to keep the dead ADPCM decoder from raising on Opus frames for one cycle. It is removed; `receive()`
now hands each queued packet straight to `RxAudioDecoder.push`.

## Consequences

- **RX audio flows for the first time on this backend.** `doctor --backend kv4p --rx-level` against a
  freshly-reset board should now report real frames and a level (bench acceptance).
- **Packaging gap — recorded, not fixed (this is the packaging cycle's job).** `opuslib` rides the
  **`mumble` extra** today (transitive via pymumble, alongside the `opuslib-next-bundled` libopus
  carrier). So a kv4p node currently needs `uv sync --extra mumble` to get libopus, even though it has
  nothing to do with Mumble. `Kv4pOpusUnavailable` names the fix in its message. The clean split — a
  kv4p-only extra that pulls just the libopus carrier (no sound card, no PortAudio, no pymumble) — is
  the extras-taxonomy cycle the docs cycle already flagged; no `pyproject.toml` change here.
- **Flow-control headroom.** Narrowband VBR Opus (~25 frames/s) is far under the retired ADPCM path's
  ~89 kbit/s that shaped ADR 0062's window sizing. The encoded-byte flow-control window stays (it is the
  device's contract), but the RX hand-off deque depth (`DEFAULT_RX_AUDIO_DEPTH = 256`) now has ample
  headroom — a knob to revisit against real bench numbers if RX latency ever matters, not now.
- **Tests.** `tests/test_kv4p_audio.py` (the ADPCM suite) is deleted with its codec. The Opus round-trip,
  corrupt-drop, and TX-re-block tests run only when libopus is importable (`pytest.importorskip`, the
  Mumble integration-test precedent) so a bare `uv run pytest` stays green; the missing-libopus →
  `Kv4pOpusUnavailable` test always runs (it forces the absent path). `FirmwareFakeSerial` grows an Opus
  RX path so the firmware-accurate fake exercises a real encoded packet end-to-end.
- **Two follow-ups remain** (see HANDOFF): (a) the **running-board handshake** — `connect()` completes
  only right after a boot because shipped status reports are edge-triggered (ADR 0064); and (b) the
  **extras taxonomy** — give kv4p its own extra so libopus arrives without `--extra mumble`.
