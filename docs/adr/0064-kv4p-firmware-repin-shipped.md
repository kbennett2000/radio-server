# 0064 — kv4p HT: re-pin the wire spec to shipped firmware (`0x07` + Opus, not `0x0C` + ADPCM)

Status: Accepted

## Context

ADRs 0061 (`frames.py` wire codec), 0062 (`transport.py` handshake), and 0063 (`Kv4pHt` backend) each
cite kv4p-ht firmware pinned at commit `e9935bd37e7505f70ae7023c78fe6a714be90be9` as their *source of
truth*, "read as a specification" (guardrail 1). The bench cycle (ADR-less, PR #116) drove the real board
and found `doctor --backend kv4p --rx-level` captures **zero** RX audio, and traced it to a firmware
mismatch: our code listens for RX audio on vendor command `0x0C`, but the board never sends it.

This ADR pins **what actually ships** and records the real protocol from source — not inferred from the
wire, which is the discipline that isolated the bug. The finding is stark: **`e9935bd` is unreleased.**

- The latest shipped firmware is **v2.0.0.1** (tag `3f0e809baa02a946c3f0602681303f600c321d31`, released
  2026-06-01); the prior release **v2.0.0.0** (`6a3b3e30…`, 2026-05-22) matches it on every fact below.
- `e9935bd` is **`FIRMWARE_VER = 17` and exactly 44 commits *ahead* of** v2.0.0.1 (`git compare` →
  `ahead_by=44, behind_by=0`) — a later, BLE-development-line build **no user can flash**. v2.0.0.1 is
  **also `FIRMWARE_VER = 17`.** The version number is identical across two different protocols; it cannot
  discriminate them. We pinned repo-tip and assumed the shipped v17 matched it. It does not.

All facts below were read from the shipped source at `3f0e809…`:
`microcontroller-src/kv4p_ht_esp32_wroom_32/{protocol.h, rxAudio.h, txAudio.h, globals.h,
kv4p_ht_esp32_wroom_32.ino}` (GPL-3.0, read as a specification — not ported).

### What actually ships (v2.0.0.1) vs. our pin (`e9935bd`)

| | Shipped **v2.0.0.1** (`3f0e809…`) | Pinned `e9935bd` (unreleased, +44) |
|---|---|---|
| `FIRMWARE_VER` | 17 | 17 |
| RX audio cmd (`COMMAND_RX_AUDIO`) | **`0x07`** | `0x0C` |
| TX audio cmd (`COMMAND_HOST_TX_AUDIO`) | **`0x07`** | `0x0C` |
| Codec | **Opus** | IMA ADPCM (16 kHz, 128-byte block → 249 samples) |
| Desired-state apply | whole-struct `memcpy` on `param_len == 22`; **no flag mask, no sequence gate** | whole-struct + `flags &= HOST_STATE_GLOBAL_FLAG_MASK`; BT/BLE `ProtocolSession` + session-flag mask |

**The Opus parameters (from `rxAudio.h` / `txAudio.h`, read as spec):** the firmware's RX encoder is
`OpusAudioEncoder` on `AudioInfo(AUDIO_SAMPLE_RATE, 1, 16)` — **48 kHz, mono, 16-bit** — configured
`application = OPUS_APPLICATION_AUDIO`, `frame_sizes_ms_x2 = OPUS_FRAMESIZE_40_MS` (**40 ms** frames),
`vbr = 1`, `max_bandwidth = OPUS_BANDWIDTH_NARROWBAND`, no explicit bitrate (VBR chooses). The TX decoder
is `OpusAudioDecoder`, same 48 kHz/mono/16, `max_buffer_write_size = PROTO_MTU`. **There is no length
prefix:** each `COMMAND_RX_AUDIO` KISS vendor frame carries exactly one encoded Opus packet, delimited by
the KISS `FEND` boundary and bounded by `PROTO_MTU = 2048`. This **replaces the 128-byte / 249-sample block
contract** ADR 0061 recorded: frames are now variable-length, and there is no fixed block size to re-block
against — Opus is natively 48 kHz, so the 16 k↔48 k resamplers and the 249→747 re-blocker are moot too.

**What else moved — checked, not assumed.** The `protocol.h` and `globals.h` diffs (`e9935bd` → shipped)
were read in full. Beyond the two audio facts above, `e9935bd` *adds* the BT/BLE `ProtocolSession`
plumbing, the `HOST_STATE_SESSION_FLAG_MASK` / `HOST_STATE_GLOBAL_FLAG_MASK` constants, per-session flag
gating, and the ADPCM-specific `globals.h` constants (`AUDIO_WIRE_SAMPLE_RATE 16000`,
`AUDIO_FRAME_SAMPLES_WIRE 249`, `AUDIO_FRAME_SAMPLES_48K 747`, `AUDIO_FRAME_BYTES 128`,
`AUDIO_RESAMPLE_RATIO 3`) — none of which exist in shipped. Everything else is **byte-identical**: KISS
framing and the vendor envelope, the `HostDesiredState` (22 B), `DeviceState` (26 B) and `Version` (17 B)
structs, every `HOST_STATE_*` / `DEVICE_STATE_*` flag bit, and the non-audio command IDs
(`HOST_DESIRED_STATE = 0x0D`, `HELLO = 0x06`, `WINDOW_UPDATE = 0x09`, `DEVICE_STATE = 0x0B`).

**The handshake mechanism ADR 0062 describes is `e9935bd`, not shipped.** Shipped `handleCommands` is:

```c
case COMMAND_HOST_DESIRED_STATE:
  if (param_len == sizeof(HostDesiredState)) {  // == 22
    memcpy(&desiredState, params, sizeof(HostDesiredState));  // whole struct, verbatim
    reconcileDesiredState();
  }
```

There is **no `incoming.sequence > desiredState.sequence` gate** and **no `flags &= …_GLOBAL_FLAG_MASK`
masking** — the entire struct, including the full 16-bit `flags` word and the `sequence`, is taken
verbatim on a length match. `deviceStateFlags()` then echoes the whole host `flags` word (OR'd with the
live `PHYS_PTT_DOWN` / `TX_ACTIVE` / `SQUELCHED` bits), and `currentDeviceState().appliedSequence =
desiredState.sequence` echoes the sequence we sent. This means (a) the "a host counting from 1 against a
device at sequence 40 is silently ignored" hazard ADR 0062 built Decision 1 around **does not occur on
shipped firmware**, and (b) `sendCurrentDeviceState()` emits a `DeviceState` only when `deviceStateDirty`
**and** `ENABLE_STATUS_REPORTS` — status reports are **edge-triggered on change**. So `connect()`'s
timeout on a *running* board (bench, PR #116) is because a no-op probe (RADIO_CONFIG_VALID = 0, nothing
changed) marks nothing dirty and draws no echo — **not** a sequence gate. Our host-side
`appliedSequence` sync remains correct and safe on shipped (it resyncs to the sequence the device just
echoed); only ADR 0062's *rationale* for it was drawn from the unreleased line.

## Decision

**1. Re-pin the spec, and the audio command IDs, to shipped v2.0.0.1 (`3f0e809…`).** ADRs 0061/0062/0063
and the four backend source files (`frames.py`, `transport.py`, `radio.py`, `audio.py`) are corrected to
cite `3f0e809…`; `frames.py`'s `RcvCommand.HOST_TX_AUDIO` and `SndCommand.RX_AUDIO` become `0x07`. This
cycle is **the pin correction and the command IDs only — no codec.** Pinning the source before writing the
Opus decoder is deliberate: the same "same version number, different protocol" trap is exactly what a
wire-first guess would have walked back into.

**2. Support shipped firmware only.** We target v2.0.0.1's `0x07` + Opus. We **reject** auto-supporting
both lines by sniffing which command ID (`0x07` vs `0x0C`) RX audio arrives on: `FIRMWARE_VER` is 17 on
both and the HELLO `Version.features` bits (`HAS_HL` / `HAS_PHY_PTT` / `HAS_ESP32_AFSK`) encode neither the
codec nor the line, so the arriving audio command ID is the *only* runtime discriminator — and building a
branch for `e9935bd` means shipping code for firmware **no user can install**, which is what this ADR
exists to stop. Revisit only if the BLE line actually releases.

**3. The IMA-ADPCM codec (PR #111) is dead.** `radio_server/backends/kv4p/audio.py` in full
(`decode_adpcm_block` / `encode_adpcm_block`, `AdpcmEncoder`, `RxAudioDecoder`, `TxAudioEncoder`,
`StreamResampler`, the IMA step/index tables) and `tests/test_kv4p_audio.py` implement the unreleased
`e9935bd` protocol and are dead under the shipped Opus target. They are **marked dead here, not deleted**:
deletion belongs with the replacement, so the Opus cycle removes them in the same change that adds the
Opus decoder/encoder — the tree is never left without an RX decoder. This cycle only stops the dead path
from *crashing*: with RX audio now routed on `0x07`, `Kv4pHt.receive()` will be handed variable-length
Opus payloads, and `decode_adpcm_block` raises on any length ≠ 128; `receive()` now drops a wrong-length
block (returns an empty `AudioFrame`) instead of propagating a `ValueError` up the unguarded RX pump. RX
audio is therefore *recognized but not decodable* until the Opus cycle — a known, documented interim state.

## Consequences

- **RX audio still does not flow after this cycle** — and that is correct. The command ID now matches the
  board, so frames are recognized, but the decoder is the dead ADPCM path; the Opus cycle makes audio
  actually play. This cycle's job is to make the tree *tell the truth* and not crash on a live board.
- **The Opus cycle is well-scoped and cheaper than it looks (reuse ADR 0056/0057).** libopus is already
  solved: `radio_server/link/_opus.py` `ensure_opus_loadable()` (find_spec → patch `ctypes` `find_library`)
  and the `opuslib-next-bundled` carrier gated to five wheel tags (Pi aarch64 + macOS arm64 confirmed). The
  kv4p RX decoder is `opuslib.Decoder(48000, 1)` fed one packet per `RX_AUDIO` frame; the TX encoder is the
  mirror. No resampling, no re-blocking (Opus is native 48 kHz) — the entire `audio.py` block-math layer
  disappears.
- **A packaging question, now with a second consumer (record, do not fix here).** `opuslib` rides the
  **`mumble` extra** today (transitive via pymumble), alongside the `opuslib-next-bundled` binary carrier.
  A kv4p Opus codec needs libopus **without** the mumble extra — so the kv4p-only-extra question the docs
  cycle already flagged (a kv4p node needs no sound card, no PortAudio) now also has a libopus dependency.
  This is for the packaging cycle; no `pyproject.toml` change here.
- **Two live follow-ups remain** (see HANDOFF): (a) the **Opus codec** swap (delete ADPCM, add Opus RX/TX),
  and (b) **handshake bootstrap on a running board** — now correctly understood as edge-triggered status
  reports (`deviceStateDirty` + `ENABLE_STATUS_REPORTS`), not a sequence gate; the exact dirty-trigger is
  read from shipped `reconcileDesiredState` in that cycle.
