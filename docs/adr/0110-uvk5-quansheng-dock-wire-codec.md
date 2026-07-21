# 0110 — UV-K5 (Quansheng Dock): a serial backend shape, starting with the wire codec

Status: Accepted

## Context

We are adding a **third backend**: a Quansheng UV-K6 running nicsure's "Quansheng Dock"
custom firmware, wired via an AIOC cable — serial + audio through the radio's K1 jack,
the same AIOC pattern the `baofeng` backend uses (ADR 0029). The multi-cycle goal is
browser-selectable channel switching for repeater monitoring. **The radio is ordered but
not yet on the bench**, so this cycle is pure offline protocol work.

This mirrors the kv4p arc's shape (ADR 0061): land the ADR plus the pure, I/O-free wire
codec now, and defer the transport, the `Radio` class, and all app wiring. The Dock rides
the **stock Quansheng UART framing** (shared `Header_t`, a 16-byte XOR obfuscation table,
CRC-16, `0xAB 0xCD` / `0xDC 0xBA` sentinels, 38400 baud) and adds a set of `0x08xx`
commands behind the firmware's `ENABLE_DOCK` build flag. We implement the **dock serial
protocol** — *not* the Kenwood-style CAT server, which exists only in the C# Windows app.

### Source of truth.

The wire protocol is a *source fact, not a hardware fact*, so it is pinned to the exact
releases that will be flashed and read as a **specification only** — no C or C# is pasted
or line-by-line ported (Guardrail 2, ADR 0002; same discipline as kv4p `frames.py`).

- Firmware **`nicsure/quansheng-dock-fw`**, tag **`0.32.21q`**, commit
  **`4375c3e9604ee4c14ec4bdae67af077879a96f34`** (Apache-2.0):
  - `app/uart.c` — `Header_t`/`Footer_t` (uart.c:55-63), the `Obfuscation[16]` table
    (uart.c:232-235), the receive parser `UART_IsCommandAvailable` (uart.c:949-1040), the
    transmit `SendReply` (uart.c:251-283), the dock command structs (uart.c:150-230) and
    dispatch (uart.c:1042-1140).
  - `driver/crc.c` — `CRC_Calculate` config: `CRC_16_CCITT`, `IV = 0`, input/output
    normal (not reflected), no final XOR (crc.c:21-47) — i.e. CRC-16/XMODEM.
- Client **`nicsure/QuanshengDock`**, tag **`0.32.21q`**, commit
  **`851efa955740db9251811cc90195e927b52ba68c`** (GPL-2.0), read as the host-side spec:
  - `Serial/Comms.cs` — the authoritative encoder `SendCommand2` (Comms.cs:389-482), the
    streaming decoder `ByteIn` (Comms.cs:152-220), `Crc16` (Comms.cs:63-76), and
    `xor_array` (Comms.cs:39, byte-identical to the firmware `Obfuscation` table).
  - `Serial/Packet.cs` — the command/reply opcode constants (Packet.cs:12-37).
  - `ExtendedVFO/BK4819.cs` — `SetFrequency` (recorded below for the control-path cycle).

### License.

The firmware is Apache-2.0 and the client GPL-2.0. Talking to a device over a serial wire
is not a derivative work; the sources were read as a *specification*. `frames.py` is a
clean-room reimplementation citing `file:line` throughout — the CRC is the textbook
CRC-16/XMODEM, the framing is our own code, and no upstream source is copied. The golden
test vectors are computed from the documented framing, not lifted from the client tree.

### The framing (uart.c:264-282, 949-1040; Comms.cs:389-456).

```
[0xAB 0xCD]  [Size:u16 LE]  [ obf( payload[Size] + CRC16[2] ) ]  [0xDC 0xBA]
 preamble     payload len          XOR-scrambled body               footer
```

- `Size` counts the payload only; total wire length is `Size + 8` (uart.c:986).
- The payload is itself `[opcode:u16 LE][param_len:u16 LE][params…]` — an inner `Header_t`
  whose `ID` is the command/reply opcode and whose `Size` is the param length
  (uart.c:55-58; the client writes opcode at offset 4 and param length at offset 6,
  Comms.cs:394-443).
- CRC-16 is computed over the **plaintext** payload; the obfuscation XOR (`table[i % 16]`)
  covers `Size + 2` bytes — the payload *and* the two CRC bytes (uart.c:1030-1039,
  Comms.cs:445-451). De-obfuscation happens before CRC validation.
- **Receive acceptance** (mirrored by our streaming decoder): sync on `0xAB`, require
  `0xCD`; read `Size`; bounds-check `Size + 8`; wait for the whole frame; require the tail
  `0xDC 0xBA`; on any preamble/footer mismatch discard and resync — malformed/wrong-length
  frames are dropped, never truncated (uart.c:963-1002).

### The reply-CRC asymmetry (the one non-obvious protocol fact).

The two bytes before the footer mean different things by direction:

- **Host → radio commands** put a real CRC-16 there; the firmware parser validates it and
  drops the frame on mismatch (uart.c:1037-1039). `build_frame` produces this.
- **Radio → host replies** put `obf(0xFF 0xFF)` there — a *dummy*, not a CRC — because
  `SendReply` obfuscates only the `Size` payload bytes and writes the footer padding
  separately (uart.c:256-279). The client's own decoder consumes and **ignores** those two
  bytes (Comms.cs:181-186).

So a decoder that strictly enforced the firmware parser's CRC rule would reject every real
reply. `Uvk5Decoder` therefore defaults to **not** validating the CRC (`validate_crc=False`,
client parity); `validate_crc=True` opts into the firmware parser's stricter rule for the
command direction. A test documents both behaviours against a `SendReply`-modelled frame.

Also noted from the pin: obfuscation is a session toggle — the firmware disables it on
receiving `0x0514` HELLO and re-enables on `0x6902` (uart.c:1024-1028), and the shipped
client never sends HELLO (it is commented out, Comms.cs:116-120), so normal operation stays
obfuscated. `build_frame`/`Uvk5Decoder` take an `obfuscate`/`obfuscated` flag; the default
is obfuscated, with a plaintext path for the HELLO exchange. Which handshake radio-server
actually performs is a transport-cycle decision.

## Decision

**Land this ADR plus `radio_server/backends/uvk5/frames.py` and its tests — the pure,
I/O-free wire codec — and nothing else.** The module imports only the stdlib (`struct`,
`dataclasses`, `enum`, `typing`), imports nothing from `radio_server.*`, and performs no
serial I/O. It provides:

- `crc16` (CRC-16/XMODEM) and `obfuscate`/`deobfuscate` (self-inverse XOR) as their own
  cited helpers — the divergence from kv4p, which has no CRC or obfuscation.
- `build_frame(command, params, obfuscate_body=True)` mirroring `SendCommand2` byte-for-byte.
- `Uvk5Decoder` — a stateful streaming deframer modelled on the client `ByteIn` state
  machine: drop-and-resync on malformed input, oversize dropped not truncated, never raises;
  `validate_crc` opt-in.
- Frozen-dataclass struct codecs for every dock command and reply — `Hello` (0x0514),
  `KeyPress` (0x0801), `GetScreen` (0x0803), `Scan` (0x0808), `WriteRegisters` (0x0850),
  `ReadRegisters` (0x0851), `WriteGpio` (0x0860), `ReadGpio` (0x0861), `SetModulation`
  (0x0872), `JetScan` (0x0888), and the replies `ImHere` (0x0515), `ScanReply` (0x0908),
  `RegisterInfo` (0x0951), `GpioInfo` (0x0961), `JetScanReply` (0x0988). Fixed structs carry
  a `struct` format + `SIZE` asserted against the C layout; variable ones (register/GPIO
  lists) carry length-checked pack/unpack.
- `parse_frame(payload)` dispatching a decoded payload to its typed message, returning a
  `RawMessage` for unknown/ill-fitting opcodes and `None` only when too short to hold the
  inner header — never raising.

The codec covers **both** future control-path command families (keypress-simulation and
register-write tuning), so this cycle forecloses neither.

### Pin discrepancy recorded.

The kickoff listed `0x0872` "set modulation (+reply)". At `0.32.21q` the `CMD_0872_t`
struct exists (uart.c:208-212) but there is **no `0x0872` case** in the dispatch switch
(uart.c:1098-1137; the switch has `0x0870` full-control instead). We keep the `SetModulation`
codec for completeness with a "not dispatched at this pin — verify before use" note in both
the enum and the dataclass. This is exactly the value of pinning over trusting a summary.

### BK4819 tuning, recorded for the control-path cycle (not built here).

`SetFrequency` (BK4819.cs) writes registers via a `WriteRegisters` (0x0850) payload: reg
`0x38` = low 16 bits and `0x39` = high 16 bits of `freq_hz / 10` (uint32), `0x33` band-select
bits, `0x30` tuning. The `WriteRegisters` codec takes raw `(register, value)` pairs; the
frequency→register arithmetic is control logic and belongs in the radio class, not the codec.

### Alternative considered — a single flat `aioc_uvk5.py` like `aioc_baofeng.py`.

The `baofeng` backend is one flat module; kv4p is a package. We chose a **package**
(`backends/uvk5/`) to match the kv4p arc we are mirroring: the Dock adds a genuine framed
serial protocol (codec + transport + a control layer) that wants its own files, whereas the
Baofeng backend is audio-and-PTT only with no wire protocol. Starting with `frames.py` alone
keeps this cycle small while giving the later transport/radio modules a home.

## Consequences

- 26 new pure tests (`tests/test_uvk5_frames.py`): `calcsize`/SIZE layout checks, struct
  round-trips (fixed and variable), CRC anchored to the XMODEM check value `0x31C3` and
  obfuscation self-inverse, hand-derived framing golden vectors (an obfuscated `KeyPress`
  and a plaintext `HELLO`, computed from the documented steps and cross-checked against an
  independent reference), streaming decode/resync (`[malformed] + [good]` single-chunk,
  split chunks, leading garbage, bad footer, oversize, zero-length, dummy-CRC reply), and
  dispatch. Full suite: **1355 passed, 5 skipped.**
- The codec is invisible to the running server: no `factory` registration, no `[uvk5]`
  config schema, no route or UI change. A `[uvk5]` block in `radio.toml` is not yet
  recognised.

### Recorded for the transport cycle (this ADR builds none of it)

- **Serial transport + AIOC wiring**: 38400 baud (Comms.cs:101; the stock Quansheng speed —
  verify on hardware), the AIOC `/dev/serial/by-id` path and PTT line, and audio via
  sounddevice reusing the `aioc_baofeng.py` pattern (or an extracted shared AIOC transport).
- **The HELLO / session handshake**: whether radio-server sends `0x0514` (and thus runs
  plaintext) or relies on the shipped client's obfuscated-throughout flow (Comms.cs:116-120);
  the `0x12345678` timestamp enables the firmware's remote-UI mode (uart.c:341).
- **Reply CRC**: real replies carry a dummy CRC (above) — the transport's decoder should keep
  `validate_crc=False`; verify on the bench that live replies decode.
- **The `Radio`/`CatRadio` class and capabilities**: an AIOC-driven UV-K5 keys PTT over the
  serial line, never CAT (Guardrail 2). The open control-path decision — (a) keypress-sim
  driving the radio's own memory channels + screen readback vs (b) XVFO register-write tuning
  with channels as radio-server presets — is the subject of the next ADR; the codec supports
  both.
- **Config + app wiring**: `[uvk5]` schema (`config/spec.py`), `backend_kwargs`
  (`api/backend_config.py`), factory registration, the settings-API key canary
  (`tests/test_settings_api.py`), `radio.toml.example`, `doctor`, and the backend-select UI.
