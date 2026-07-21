# 0111 — UV-K5 (Quansheng Dock): the serial transport, and the control-path decision

Status: Accepted

## Context

Cycle 1 (ADR 0110) shipped the pure wire codec (`backends/uvk5/frames.py`). This cycle adds
the **serial transport** (`backends/uvk5/transport.py`) and the load-bearing
**firmware-accurate test fake**, still entirely pre-hardware, and records the **control-path
decision**. It mirrors the kv4p transport arc (ADR 0062/0064/0066) in *shape*.

Same pins as cycle 1, read as a specification (cite `file:line`, nothing ported): firmware
`quansheng-dock-fw` **0.32.21q** `4375c3e9604ee4c14ec4bdae67af077879a96f34` (`app/uart.c`);
client `QuanshengDock` **0.32.21q** `851efa955740db9251811cc90195e927b52ba68c`
(`Serial/Comms.cs`, `ExtendedVFO/BK4819.cs`).

### The dock is a plain request/reply protocol.

Unlike kv4p — which has a flow-control window (credits in encoded bytes) and a persisted
desired-state reconciler with sequence numbers — the dock has **none of that**. The firmware
`UART_HandleCommand` (uart.c:1042-1140) simply replies to each command. So this transport is
materially simpler: no credits, no sequence gate, no reconcile.

### Connect must *elicit* — the dock does not stream at top level.

At top level the firmware emits nothing unsolicited; it only replies to requests. It streams
unsolicited `0xB5` UI/DTMF element packets **only** inside full-control mode (uart.c:728-733)
or remote-UI mode (uart.c:341, gated on `gSetting_Remote_UI`). So — unlike kv4p's
passive-first connect — `connect()` cannot listen for an unsolicited report; it must send a
probe and wait. **Silence therefore means "no answer" (a timeout), not a steady state.** The
probe is a `ReadRegisters([0x30])` — a register read changes no radio state and `0x0851` is
dispatched at top level (uart.c:1115), so it works without entering any special mode; a
`RegisterInfo` (0x0951) reply proves the link.

### Replies carry a dummy CRC (the cycle-1 asymmetry, confirmed in `SendReply`).

`SendReply` obfuscates only the payload and writes `obf(0xFF 0xFF)` where a command's CRC
would sit (uart.c:256-279); the client's own decoder ignores those two bytes
(Comms.cs:181-186). So the transport's decoder runs `validate_crc=False`.

## Decision 1 — Control path = **(b) BK4819 register-write tuning**; channels are server-side presets

Confirmed viable against the pinned source, with **no hard blocker** (the kickoff's
stop-condition does not trigger):

- **`0x0870` "enter hardware control mode"** (uart.c:672-739) clears the display, sets
  `gSetting_XVFO`, backs up the registers, and **sits in a loop that services only serial
  commands** until it receives `0x0871`, which calls `RestoreRadio()`. While in that loop the
  radio's own logic is suspended, so our BK4819 writes are not fought — exactly what (b)
  needs. Inside the loop, `default: UART_HandleCommand()` (uart.c:706-708) means the normal
  dock commands (register read/write, jet scan) all work, plus an extended set (0x0871 exit,
  0x0872 modulation, 0x0873/4 backlight, 0x0875/6 AM emulation).
- **Tuning is register writes**: `WriteRegisters` (0x0850) with BK4819 regs `0x38`/`0x39` =
  low/high 16 bits of `freq_hz / 10` (uint32), `0x33` band, `0x30` tuning (client
  `BK4819.cs SetFrequency`). The client's enter sequence is `0x0870` → `SendHello(0x0514)` →
  modulation/bandwidth setup → `ReadRegisters` readback (`BK4819.cs Aquire`, 168-190).
- **TX/PTT is not the dock protocol's job.** It is the AIOC serial control line (DTR/RTS),
  exactly as the Baofeng backend (Guardrail 2, ADR 0002: PTT is never a CAT/serial command).
  So (b) needing no dock TX command is a feature, not a blocker.

Rationale for (b) over (a) keypress-simulation: (b) maps directly onto the existing
`CatRadio` surface (`set_frequency`/`set_tone`/`set_mode`) and the reactive capability UI
(ADR 0076/0077) unchanged; it gives **deterministic host-known state** (the host set the
tuning) rather than (a)'s fragile, mode-dependent screen-memory scraping; and server-side
presets benefit every CAT backend (kv4p already treats `memory_id` as a host-side concept).
Keypress-simulation (`0x0801`) stays in the codec as a diagnostic only.

**Refinement of the cycle-1 `0x0872` discrepancy.** `0x0872 SetModulation` is absent from the
*top-level* dispatch (uart.c:1098-1137) **but is handled inside the full-control loop**
(uart.c:700-703, `RADIO_SetModulation`). It sends no reply in either mode, so "0x0872 → no
reply" holds; the fake models it as accepted-but-silent, meaningful only in full-control mode.

## Decision 2 — Transport shape: mirror kv4p's I/O skeleton, drop its reconciler

`Uvk5Transport` (`backends/uvk5/transport.py`):

- **Serial seam** (from `aioc_baofeng.py` / `kv4p/transport.py`): lazy `_load_serial()`
  raising an actionable `RuntimeError` for the missing `hardware` extra; a
  `_default_serial_factory(port, baud)` that sets `dtr`/`rts` **False before `open()`**; a
  constructor `_serial_factory=None` injection seam. `DEFAULT_BAUD = 38400`,
  `DEFAULT_SERIAL_PORT = "/dev/ttyACM0"`.
- **Daemon reader thread**: `read` → `Uvk5Decoder(obfuscated=True, validate_crc=False)` →
  `parse_frame` → dispatch to blocked waiters. A `b""` read continues; a read exception is
  surfaced via a stored `_reader_error` that wakes all waiters; a single malformed frame is
  logged and skipped, never killing the reader. Unsolicited `0xB5` bytes are dropped by the
  decoder's resync (the dock-frame decoder only syncs on `0xAB`).
- **Request/reply**: `request(msg, match, timeout)` registers a waiter **before** writing (so
  a fast reply is never missed), then blocks on the `deadline`/`remaining`/`wait(remaining)`
  idiom, raising `Uvk5Timeout` on the deadline or `Uvk5Closed` if closed mid-wait (both
  `RuntimeError` subclasses, per kv4p). `send(msg)` is fire-and-forget for no-reply commands.
- **`connect(timeout)`**: retransmits the `ReadRegisters` probe every ~0.25 s until a
  `RegisterInfo` arrives or the budget runs out. The retransmit tolerates a possible
  reset-on-open boot race.
- **`close()`**: idempotent (`_closed` guard), `atexit`-registered, stops the reader
  (bounded `join`, guarded against join-from-reader), swallows teardown errors. No PTT
  reconcile here — PTT is not this transport's concern.

## Decision 3 — `FirmwareFakeSerial` models the firmware's real acceptance

The important deliverable, per the kv4p "950 green tests hid a backend that never talked to
the device" lesson (ADR 0066). A two-layer fake (in `tests/test_uvk5_transport.py`): a dumb
`FakeSerial` pipe, and a `FirmwareFakeSerial` that on every `write` runs the exact firmware
receive parser (`UART_IsCommandAvailable`, uart.c:949-1040) — sync `AB CD`, read length,
require the `DC BA` footer, honour the `bIsEncrypted` toggle (starts True; a
*pre-de-obfuscation* opcode of `0x0514` clears it, `0x6902` sets it, uart.c:1024-1028),
de-obfuscate `Size+2`, and **validate the command CRC**, dropping silently what the firmware
drops. Dispatch matches the pin (0x0851→RegisterInfo, 0x0850→store, 0x0888→JetScanReply,
0x0803→raw `0xEF`+1024 screen dump, **0x0872→no reply**, 0x0870/0x0871→enter/exit
full-control), and replies use the `SendReply` dummy-CRC shape. This is what makes a
non-communicating transport fail **loud**: the regression test wires the transport to send
plaintext against an encrypted fake, every frame fails CRC and is dropped, and `connect`
times out instead of false-passing.

## Consequences

- 16 new tests (`tests/test_uvk5_transport.py`): connect succeeds against the firmware fake,
  retransmits through initial silence, and **times out when frames are dropped**; write-then-
  read register round-trips through the modelled store; `0x0872` and a bad-CRC command both
  get no reply; a plaintext `0x0514` toggles encryption off while an obfuscated one does not;
  reader reassembles split frames, survives empty reads, and surfaces a read exception to a
  blocked waiter; the default factory holds `dtr`/`rts` low strictly before `open()`; missing
  pyserial gives an actionable error; and the `close()`/closed-request lifecycle. Full suite:
  **1371 passed, 5 skipped.** The transport imports pyserial only lazily.

### Verify on hardware (guardrail 1 — no bench numbers exist; none are fabricated)

1. Whether opening the AIOC serial port resets/reboots the UV-K5 (the kv4p DTR/RTS
   reset-race). `connect`'s retransmit tolerates it either way; the outcome is unasserted.
2. Whether the AIOC exposes the K1 UART data path **and** a usable PTT control line on one
   serial device simultaneously — (b) tuning + AIOC PTT sharing one handle. If not, the
   backend-class cycle must reconcile how PTT and dock data share the AIOC.
3. `DEFAULT_BAUD = 38400` (client `Comms.cs:101`, the stock Quansheng speed) and the real
   `/dev/serial/by-id/*` path.
4. Whether the radio idle-sleeps in the full-control loop and needs a keepalive.

### Recorded for the backend-class cycle (this ADR builds none of it)

The `Radio`/`CatRadio` class composing this transport with tuning; the enter/exit-XVFO
handshake (`0x0870` → optional `0x0514`/setup → `ReadRegisters` readback → … → `0x0871` on
teardown); the `freq_hz → (0x38/0x39/0x33/0x30)` register arithmetic; whether to send `0x0514`
(and go plaintext + remote-UI, streaming `0xB5`) or stay obfuscated like the shipped client;
handling the unsolicited `0xB5` UI/DTMF stream if ever needed; the `[uvk5]` config block +
factory registration + settings-API canary; the server-side **presets** feature; `doctor`;
audio (the AIOC's existing sounddevice path); and the web UI.
