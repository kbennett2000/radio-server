# 0061 — kv4p HT: a UART/KISS backend shape, starting with the wire codec

Status: Accepted

## Context

Every backend the project has so far — `MockRadio`, `AiocBaofeng` (ADR 0029), and the
`SignaLinkV71` stub — is the same shape: audio rides a USB **sound card** (sounddevice/ALSA,
ADR 0006) and PTT is a **separate control line** (SignaLink self-key off the DATA jack; the
AIOC's RTS/DTR serial line). CAT tuning, where it exists, is a *second* channel (Hamlib over
the PC/COM jack). Only `MockRadio` implements the CAT surface (`CatRadio`,
`radio_server/backends/base.py:130-156`); `SignaLinkV71` (`backends/signalink_v71.py`) raises
`NotImplementedError`. So the tuning surface — the API's `capabilities()` gate, `ScanEngine`
(`radio_server/scan/engine.py`), the UI's capability greying — has never had real hardware
behind it.

The **kv4p HT** (https://github.com/VanceVagell/kv4p-ht) is an ESP32 + SA818 board that
presents as a CP210x/CH340 **USB UART at 115200 8N1**. Over that one serial wire rides
*everything* — RX/TX audio, tuning, PTT, squelch, status — framed in **KISS**. There is no
sound card and no Hamlib. Adopting it is worthwhile precisely because it exercises parts of the
stack nothing else does, and its wire shape differs from our existing backends in three ways
worth recording before any code commits to them.

**Source of truth.** The wire protocol is a *source fact*, not a hardware fact — it is read
from the firmware headers, not guessed. This ADR and the codec are derived from kv4p-ht pinned
at commit `e9935bd37e7505f70ae7023c78fe6a714be90be9`:

- `microcontroller-src/kv4p_ht_esp32_wroom_32/protocol.h` — framing, commands, structs, flags
- `microcontroller-src/kv4p_ht_esp32_wroom_32/globals.h` — `PROTO_MTU`, `RfModuleType`, audio constants

**License.** kv4p-ht is **GPL-3.0**; radio-server is not. Talking to a device over a serial
wire is not a derivative work, so an independent clean-room implementation is clean — but the
headers were read as a *specification*, not copied or line-by-line ported. No firmware source
(C++ or Android Java) is pasted into this repo.

### Three ways the kv4p shape is new

1. **It is a state reconciler, not a command protocol.** The host never sends "set frequency".
   It sends a whole `HostDesiredState` struct (`protocol.h:172-182`) with a monotonically
   increasing `sequence`; the firmware applies the delta and echoes `DeviceState.appliedSequence`
   (`protocol.h:185-199`). **PTT is a flag *inside* that struct** — `HOST_STATE_PTT_REQUESTED`
   (`protocol.h:78`) — not a serial control line and not a CAT command. This makes guardrail 2
   (ADR 0002 — never key via a command; CAT is for tuning only) *easier* here than on any prior
   backend: there is no command path to misuse, because there are no commands, only desired
   state. The backend will assert PTT by setting a bit and bumping the sequence.

2. **It would be our first real `CatRadio`.** Frequency, bandwidth, and CTCSS ride the same
   `HostDesiredState`/`DeviceState` structs over the same UART — no Hamlib, no second channel.
   Bringing this backend up is therefore also the first real exercise of the tuning surface
   (`set_frequency`, the API capability gate, `ScanEngine`) against hardware.

3. **It reports a real busy line.** `DeviceState.flags` carries `DEVICE_STATE_SQUELCHED`
   (`protocol.h:100`) plus `latestRssi`. That is exactly the hardware busy signal
   `audio.squelch = "cat"` needs (`SquelchMode.CAT`, `radio_server/activity/gate.py:177-188`) —
   which `radio_server/api/app.py:1276-1286` **rejects with a `RuntimeError` for
   `server.backend='baofeng'`** because the UV-5R has no busy line (ADR 0015). For the kv4p
   backend, `audio.squelch = "cat"` becomes valid — a reconciliation the backend cycle must
   wire (relax that rejection for this backend, drive the gate off `status().busy`).

## Decision

**Land the ADR plus the pure, I/O-free wire codec now; defer the backend itself.** This cycle
is the frame/struct layer only — the load-bearing, hardware-independent core that the reader
thread, audio codec, and `Kv4pHt` class will sit on. No serial I/O, no ADPCM, no backend class,
no factory/config/app wiring.

- **New package `radio_server/backends/kv4p/`, module `frames.py`** — stdlib only (`struct`,
  `enum`, `dataclasses`); imports nothing from the rest of the tree, performs no I/O.
  - **KISS framing.** `FEND/FESC/TFEND/TFESC` encode/escape; a **streaming `KissDecoder`** fed
    arbitrary serial chunks that emits complete decoded frames and holds the partial remainder.
    It mirrors the firmware parser (`protocol.h:392-515`) byte-for-byte: bytes before the first
    `FEND` are ignored (the firmware prints a plaintext boot banner first); an unknown escape
    drops the current frame and resyncs at the next `FEND`; a frame past `KISS_MAX_FRAME_SIZE`
    (`PROTO_MTU + 1 + 6` = 2055) is **dropped, not truncated**.
  - **Vendor envelope.** `FEND | 0x06 (SETHARDWARE) | "KV4P" | 0x01 | <cmd> | payload | FEND`.
    The command byte's low nibble is the KISS command and the high nibble is the port — non-zero
    ports are dropped. A bad `"KV4P"` prefix or wrong protocol version is ignored, not raised.
    **KISS DATA frames (`0x00`) are a SEPARATE dispatch path** (`Ax25Frame`): the codec parses
    and exposes the AX.25 payload but does nothing with it — that is the future text-over-RF arc.
  - **Struct codecs** for `HostDesiredState`, `DeviceState`, `Hello`/`Version`, `WindowUpdate` —
    frozen dataclasses, `struct` with an explicit `<` (little-endian + no native padding, to
    match `[[gnu::packed]]`). Sizes are 17/22/26/43/4 bytes, asserted with `struct.calcsize`
    against the documented field list in `tests/test_kv4p_frames.py` (the load-bearing check
    that our format strings track the firmware layout). `RfModuleType` is `uint8_t`
    (`globals.h:24-27`), which fixes `Version` at 17 bytes; ESP32 Xtensa `char` is signed, hence
    the signed-byte codes for the `radioModuleStatus` fields.
  - **Flags/enums.** `HostStateFlag` / `DeviceStateFlag` as `IntFlag`; `RcvCommand` / `SndCommand`
    / `DeviceMode` / `DeviceStateError` / `RfModuleType` / `FeatureFlag` as enums. The host-state
    mask split (`HOST_STATE_SESSION_FLAG_MASK` / `HOST_STATE_GLOBAL_FLAG_MASK`, `protocol.h:102-114`)
    is carried as module constants because the reconciler cycle needs it (session flags reset per
    connection; global flags persist).

### Alternative considered — model kv4p as a `CatRadio` like the V71

Tempting, since it tunes. But the V71 mental model is "issue a CAT command per change," and the
kv4p is the opposite: send whole desired state, read whole reported state, reconcile on a
sequence number. Forcing per-setter CAT semantics onto it would hide the sequence/ack handshake
the flow-control window depends on. The `CatRadio` *protocol surface* (`set_frequency`, …) is
still the right API — the backend will implement it by mutating a pending `HostDesiredState` and
bumping the sequence — but the ADR records the reconciler shape so the backend cycle builds the
state machine, not a command dispatcher. Deferred, not rejected.

## Consequences

- **The codec is fully testable with zero hardware and zero I/O**, and it is done: 28 pure tests
  (round-trips, escaping, streaming reassembly across chunks, boot-banner discard, unknown-escape
  resync, oversize drop, bad-prefix/version ignore, DATA-vs-vendor dispatch). Full suite: 873
  passed, 5 skipped.
- **Recorded for the backend cycle (this ADR builds none of it):**
  - **Flow-control counts *encoded* bytes.** The firmware acks each frame it decodes with a
    `WindowUpdate` carrying the **encoded** length — escaped, both `FEND`s included
    (`protocol.h:421-431`, `_encodedFrameLen`) — not the decoded payload length. The window
    accounting in the reader must count encoded bytes, so the encoder/decoder will need to
    surface that count.
  - **Audio is 16 kHz 4-bit IMA ADPCM** (`globals.h:29-36`): WAV block layout, 128-byte block →
    **249 samples** (`AUDIO_FRAME_SAMPLES_WIRE`), device hardware at 48 kHz. Our canonical audio
    is 48 kHz/s16le in 960-sample (20 ms) blocks (ADR 0006), and **249 does not divide 960** — the
    audio cycle owns ADPCM decode + resampling and this reframing, none of it here.
  - **Wire budget.** At 115200 baud, one direction of ADPCM audio is ≈ 89 kbit/s ≈ 77% of the
    line — tight but workable; the reader/writer must not stall the loop.
  - **Open question — which capabilities to advertise.** `Capability.SCAN` in `CAT_CAPS`
    (`base.py:53-61`) is the *radio's own* hardware scan; the kv4p has none. Our `ScanEngine` is a
    **software** loop that drives `set_frequency` + `status().busy` itself and is explicitly
    distinct from `CatRadio.scan(on)` — yet `ScanEngine.__init__` (`scan/engine.py:199-200`)
    *requires* `Capability.SCAN` in `radio.capabilities()`. So the backend cycle must decide:
    advertise `SCAN` (to unlock the software `ScanEngine`, leaving `scan(on)` as a no-op/unsupported)
    or omit it (and let the engine's requirement be revisited). Left open here.
  - The `audio.squelch = "cat"` rejection at `app.py:1276-1286` must be relaxed for this backend.
