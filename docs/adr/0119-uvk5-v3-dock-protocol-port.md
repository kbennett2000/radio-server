# 0119 — UV-K5 V3 firmware: the dock protocol port (F2)

Status: Accepted

## Context

ADR 0118 (F1) forked the UV-K5 **V3** community firmware
(`kbennett2000/uv-k1-k5v3-firmware-custom`, tag `v5.7.0` / `3bd3ebb`) and proved the
build→flash→boot pipeline; Kris confirmed the F1 bin boots clean at the bench. **F2 ports the
dock control mode into that fork** so radio-server can drive the V3 over the same wire
protocol it already speaks to the classic Quansheng Dock (ADRs 0110–0112).

The load-bearing invariant of the whole arc: **byte-compatibility with radio-server's
existing `FirmwareFakeSerial` + `Uvk5Decoder`**, so `radio_server/` code and **all 1487 tests
change ZERO**. A genuine V3 wire difference would be a STOP-and-report, not a quiet patch on
either side. None was found — the V3 tree's stock UART framing is already byte-identical to
the pin.

This is a **docs-only** ADR in radio-server; all code lives in the fork.

### Source of truth (the pins).

- **Destination** — fork `kbennett2000/uv-k1-k5v3-firmware-custom`, branch `f2-dock-port`,
  commit **`32c600b`** (base `v5.7.0` / `3bd3ebb`, Apache-2.0). The V3 tree's stock command
  dispatch already carries the framing: `App/app/uart.c` — `Header_t`/`Footer_t` (uart.c:63-71),
  the identical `Obfuscation[16]` table (uart.c:163-166), `AB CD`/`DC BA` sentinels,
  `UART_IsCommandAvailable` parser + CRC validate (uart.c:641-783), the dispatch `switch`
  (uart.c:807), `SendReply` dummy-CRC path (uart.c:242-282), the `CMD_0601/0602` BK4819
  register-R/W precedent (uart.c:603-639), `BK4819_ReadRegister`/`WriteRegister`
  (bk4819.c:189/211), and the 38400-baud UART (driver/uart.c:102).
- **Protocol source ported from** — `nicsure/quansheng-dock-fw` @ `0.32.21q` /
  **`4375c3e9604ee4c14ec4bdae67af077879a96f34`** (Apache-2.0): `app/uart.c` — the framing, the
  `Obfuscation[16]` table (uart.c:232-235), `SendReply` dummy-CRC (uart.c:251-283), the
  `CMD_0870` full-control loop (uart.c:672-739), and `CMD_0850`/`CMD_0851` register handlers
  (uart.c:569-591). **Both trees are Apache-2.0, so this is a real port with attribution** (the
  fork's `NOTICE` and `App/app/dock.c` carry the modified-file notice) — unlike the GPL-2.0
  `QuanshengDock` C# client (ADR 0110), which stays spec-only.
- **Definition of done** — radio-server `tests/test_uvk5_transport.py::FirmwareFakeSerial` +
  `radio_server/backends/uvk5/frames.py::Uvk5Decoder` — unchanged, the contract the port matches.

## Decision 1 — the port surface is tiny (registers do everything).

radio-server drives the radio entirely through BK4819 registers, so the port implements only:
`0x0850` write-registers, `0x0851` read-registers → `0x0951` `RegisterInfo`, and `0x0870`/`0x0871`
enter/exit full-control. **No** keypress (0x0801), screen (0x0803), scans (0x0808/0x0888), GPIO
(0x0860/0x0861), AM/FSK/UI/backlight, or modulation (0x0872) — all of nicsure's dock is dropped.

Opcode precision (as ADR 0118's note): `0x0851` is the read **request**; `0x0951` `RegisterInfo`
is the read **reply** (one per register); `0x0850` writes and gives no reply. The connect probe is
a top-level `ReadRegisters(0x30)` (`0x0851`) → any `RegisterInfo` (`0x0951`) — register R/W works
both at top level and inside the `0x0870` loop.

## Decision 2 — a pure, host-compilable protocol core behind a thin HAL.

New `App/app/dock.c` + `App/app/dock.h` are **pure C with no hardware or firmware-tree include**:
the framing (`AB CD`/`DC BA`, LE size, 16-byte XOR, CRC-16/XMODEM), command-CRC validation,
dummy `obf(0xFF 0xFF)` replies, register dispatch, and a streaming deframer mirroring
`UART_IsCommandAvailable`'s acceptance rules. All hardware sits behind a `dock_hal_t`
(`read_reg`/`write_reg`/`send` + `user`). This makes the protocol testable **on the host, before
any flash** — the module's "definition of correct" is the fake.

`App/app/uart.c` (all `#ifdef ENABLE_DOCK`) binds the HAL to `BK4819_ReadRegister`/`WriteRegister`
+ `UART_Send`, adds dispatch cases `0x0850`/`0x0851`/`0x0871` → `dock_dispatch` and `0x0870` → a
blocking full-control loop that re-enters the existing UART dispatch for in-loop register R/W
(nicsure's `default: UART_HandleCommand()` shape). This **extends** the tree's `switch`, not a
parallel path — surgical enough that an upstream PR stays plausible. Gated by
`enable_feature(ENABLE_DOCK app/dock.c)` (`App/CMakeLists.txt`), **on in the Fusion preset**.

## Decision 3 — the spec is the fake: a host test harness.

`tests/host/` (a `Makefile` + `test_dock.c`, plain `gcc`, no hardware) exercises `dock.c` with a
fake HAL (register array + capture buffer). Cases mirror `FirmwareFakeSerial`: malformed frame
dropped (bad 2nd preamble / bad footer / zero-size / oversize), drop-and-resync, streaming
byte-by-byte, **command CRC enforced** (bad CRC → no dispatch, no reply), `0x0851` → one `0x0951`
per register, `0x0850` → no reply + store updated, `0x0870`/`0x0871` full-control, and a
**byte-exact `0x0951` reply vector** as an independent oracle. **19/19 checks pass** — the
pre-flash proof of byte-compatibility.

## Decision 4 — V3 derivation (from the tree, not assumed from the old MCU).

The kickoff flagged watchdog feeding, interrupt masking, and BK4819 contention as risks. The V3
tree settles them more simply:
- **No hardware watchdog is active** (IWDG/WWDG never initialized) — the blocking loop needs no
  WDT service.
- **No async ISR reprograms the BK4819.** SysTick (10 ms) only sets scheduler flags; BK4819
  interrupts are *polled* in `CheckRadioInterrupts()` within the same 10 ms slice, which cannot
  run while the loop blocks (the loop is invoked from that very slice, `app.c:1600`). So blocking
  is itself the quiesce; no IRQ masking. On exit the loop calls `RADIO_SetupRegisters(true)` to
  resume RX — **marked verify-on-bench** in `BENCH.md`.
- **UART = 38400** already (driver/uart.c:102); no change.
- The V3 hard-defines `bIsEncrypted == true` and comments out the classic dock's plaintext-`0x0514`
  encryption toggle. `dock.c` matches (obfuscation always on); radio-server's dock transport always
  obfuscates, so **no frame it sends to a working dock exercises the toggle** — not a wire
  difference on the operational path.
- **Flash fits:** the `ENABLE_DOCK` Fusion build is **104,116 B of 118 KB (86.2%)**, **+572 bytes**
  over the F1 baseline (103,544 B); ~16.3 KiB headroom.

## Consequences

- **Zero change to radio-server.** No file under `radio_server/` and no `tests/test_uvk5_*.py` /
  `tests/test_doctor.py` is touched — the arc's whole point. **`uv run pytest`: 1487 passed, 5
  skipped** (unchanged). This ADR + `docs/adr/README.md` row + a `docs/HANDOFF.md` entry are the
  only radio-server changes.
- **External deliverables (fork):** branch `f2-dock-port` @ `32c600b` (`App/app/dock.{c,h}`,
  `App/app/uart.c`, CMake `ENABLE_DOCK`, `tests/host/`, `NOTICE`, `BENCH.md`); pre-release
  [`radio-server-f2-v5.7.0`](https://github.com/kbennett2000/uv-k1-k5v3-firmware-custom/releases/tag/radio-server-f2-v5.7.0)
  with `f4hwn.fusion.v5.7.0.f2-dock.bin` (sha256 `68208de1…`) + `SHA256SUMS` + provenance.
- **BENCH.md** gained the F2 acceptance sequence: flash → `doctor --backend uvk5` connect probe
  (the `ReadRegisters(0x30)` elicit answering a `RegisterInfo` **is** the port working) → the four
  F1 gates with the dock idle.

## Out of scope (named; built here: none in radio-server)

Any radio-server code change (a genuine V3 wire difference is a STOP-and-report); calib/EEPROM
features; upstream PR submission; bench claims (Kris flashes and runs doctor — F2's bench
acceptance and any V3-specific surprises are **F3**, the full end-to-end bench loop). No nicsure
code beyond the framing/reply/register-dispatch actually needed was ported.
