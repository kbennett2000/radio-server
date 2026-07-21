# 0112 — UV-K5 (Quansheng Dock): the `Uvk5Radio` CatRadio class and register keying

Status: Accepted

## Context

Cycles 1-2 (ADR 0110/0111) shipped the codec, the transport, and the firmware-accurate fake,
and decided control path **(b) XVFO register-write tuning, channels as server-side presets**.
This cycle builds the `Radio` class — `radio_server/backends/uvk5/radio.py` — the last piece
buildable before the hardware arrives. It mirrors the `Kv4pHt` shape (ADR 0063).

Same pins, read as a specification (cite `file:line`, nothing ported): firmware `4375c3e…`
(`app/uart.c`), client `851efa9…` (`ExtendedVFO/BK4819.cs`, `XVFO.cs`, `Defines.cs`).

**The host is the radio's brain.** In full-control ("XVFO") mode the firmware suspends its own
logic in a serial-command loop (`CMD_0870`, uart.c:672-739) until `0x0871`, and the client
drives the BK4819 directly. So — unlike kv4p's persisted desired-state reconciler — `Uvk5Radio`
holds a small tracked-register model (`reg30`, `reg33`, frequency, tone, mode, keyed) and issues
`WriteRegisters` / `ReadRegisters` directly (the dock is plain request/reply).

## Decision 1 — the derived register sequences (byte-exact, tested against the fake)

Frequency unit is **10 Hz** (`SetFrequency(double MHz)` → `round(MHz*100000)`, BK4819.cs:111).

- **`set_frequency(hz)`** (BK4819.cs:131-160): `freq10 = hz // 10`. Band bit in `reg33`: clear
  bits 3-4 (`& 0xFFE7`), set bit 2 if `freq10 < 28_000_000` else bit 3. Emit `WriteRegisters`
  `(0x38, freq10 & 0xFFFF), (0x39, (freq10>>16) & 0xFFFF), (0x33, reg33), (0x30, 0), (0x30, reg30)`.
- **`set_mode(mode)`** (`Bandwidth` BK4819.cs:221-224; `XBANDWIDTH` Defines.cs:169-170): `FM`→
  `(0x43, 18856)`, `NFM`→`(0x43, 18440)`.
- **`set_tone(tone|None)`** (GoTransmit BK4819.cs:620-647): `None`→`(0x51, 0)`; a tone →
  `code = ((round(tone*10) * 206488) + 50000) // 100000`, `(0x51, 0x904A), (0x07, code)`.
- **key-up** (GoTransmit BK4819.cs:581-648, load-bearing subset): PA power
  `(0x36, (freq10<28_000_000 ? 0x88 : 0xA2) | drive)`, FM path `(0x50, 0x3B20)`, the current
  tone's CTCSS regs, then `(0x30, 0), (0x30, 0xC1FE)` (TX enable).
- **key-down** (TransmitEnd BK4819.cs:411): `(0x30, 0), (0x30, reg30)` — restore RX.
- **`busy`** (BK4819.cs:781): `ReadRegisters(0x67)` → `rssi = value & 0x1FF`, `busy = rssi ≥
  squelch_threshold`.

`reg30` (RX system-control) and `reg33` (band) are seeded on connect from a register read-back,
mirroring the client's `Aquire` (BK4819.cs:182-189). Two new no-param command dataclasses —
`EnterHwMode` (0x0870) / `ExitHwMode` (0x0871) — were added to `frames.py`.

**Fail-loud units discipline (the kv4p precedent).** `set_frequency` raises `ValueError` when
`hz` is out of band **or not a multiple of the 10 Hz step** — never rounds or snaps.
`set_tone` raises out of the CTCSS band; `set_mode` raises on an unmapped mode. `set_channel`
raises `UnsupportedCapability(SET_CHANNEL)` — presets are host-side (ADR 0111).

## Decision 2 — keying is register-based, confirmed-or-raise; the STOP condition did not trigger

The client keys **entirely via BK4819 register writes** (`XVFO.Ptt` → `BK4819.Transmit` →
`GoTransmit`, BK4819.cs:132-143, 570-652) — **there is no RTS/DTR serial-line PTT anywhere in
the client.** Inside the `CMD_0870` loop `default: UART_HandleCommand()` (uart.c:706-708)
executes those register writes, so register-keying demonstrably works in full-control mode. The
kickoff's STOP condition ("genuinely ambiguous whether PTT works in the loop") therefore does
**not** apply. The physical PTT contact (GPIOC pin 5, Defines.cs:225) is likely inert in the
loop (the firmware does not service it there) — verify on hardware.

`ptt(True)` writes the TX-enable sequence and then **confirms**: `ReadRegisters(0x30)` must
return `0xC1FE`; if it does not, RX is restored immediately and `Uvk5KeyingError` is raised (the
kv4p rule — a silent no-key never becomes dead air). `ptt(False)` restores RX unconditionally
(RF-safe). The one-shot/streaming `_keyed` discipline matches both existing backends.

**Guardrail-2 tension (ADR 0002), recorded.** The project guardrail prefers PTT on the AIOC
serial line, not the control channel, because standard CAT-TX makes a radio transmit its own
mic. In full-control mode that footgun does not apply the same way — the radio's own firmware is
suspended and the BK4819 is driven directly while the AIOC injects audio into the K1 mic path.
**Whether register-keying in XVFO mode actually transmits the AIOC-injected audio (vs nothing /
the wrong source) is the single most important verify-on-hardware item.**

## Decision 3 — audio (transmit/receive) is deferred to a later cycle

UV-K5 audio is the AIOC **sound-card** path, a separate USB interface from the dock serial, and
is explicitly out of scope. `transmit(audio)` and `receive()` raise `NotImplementedError` naming
the audio cycle; this class delivers control + register-keying + status only. Nothing wires the
backend into the factory/config yet, so raising is safe. `Capability.SCAN` is advertised to gate
the software `ScanEngine` (which needs only a real `set_frequency` + `status().busy`, ADR 0063);
the hardware `scan(on)` raises like kv4p. Capabilities: `SHARED_CAPS | {SET_FREQUENCY, SET_TONE,
SET_MODE, SCAN}` — `SET_CHANNEL` omitted.

## Consequences

- 17 new tests (`tests/test_uvk5_radio.py`) reusing the cycle-2 `FirmwareFakeSerial` (its BK4819
  register file: 0x0850 writes land, 0x0851 reads serve). Assertions are byte-exact `(reg, value)`
  sequences decoded off the wire for tune/mode/tone/key; fail-loud rejects for off-raster /
  out-of-band / unknown-mode / out-of-range-tone; **key-up raises `Uvk5KeyingError` and leaves the
  radio un-keyed when the fake withholds `0x30 = 0xC1FE`**; `close()` unkeys + exits full-control +
  is idempotent. Full suite: **1388 passed, 5 skipped.** The class imports only `.transport` /
  `.frames` / `base` / `audio` + stdlib.

### Verify on hardware (guardrail 1 — no bench numbers exist; none fabricated)

- **Post-crash stuck-key.** If the host dies without sending `0x0871`, the firmware sits in the
  `CMD_0870` loop forever (uart.c:680), registers frozen. **If it crashed mid-key (`0x30 =
  0xC1FE`) the radio stays keyed — the full-control loop has no time-out** (unlike the normal
  firmware's TOT). `close()`/`atexit` unkey + exit cleanly, but a hard `SIGKILL` bypasses
  `atexit`. An app-level watchdog/TOT is a future concern (echoes ADR 0090-0093). Prominent risk.
- **PTT audio source in XVFO** (Decision 2) — does register-keying transmit the AIOC K1 audio.
- **Physical PTT in the loop** — likely inert; confirm.
- **Register unit at band edges** (`hz % 10` raster vs the radio's real channel raster), the RX
  band range (18-1300 MHz default), the **squelch threshold** (RSSI COS), and the fuller
  GoTransmit TX sequence (mic gain 0x7d, filter 0x47, GPIO band-steer 0x33 chain, compander) —
  a TX-quality refinement not needed to prove keying logic.

### Recorded for later cycles (this ADR builds none of it)

The `[uvk5]` config block + factory registration + settings-API canary; the **AIOC audio path**
(`transmit`/`receive` sound, and how PTT-by-register coexists with AIOC audio on one cable); the
server-side **presets** feature; `doctor`; and the web UI.
