# 0120 — UV-K5 V3: RX audio was dead in dock mode — the audio-path amp (F3a)

Status: Accepted

## Context

With the F2 dock firmware (ADR 0119) flashed, TX worked end-to-end and the connect probe passed,
but **received audio to the AIOC read the noise floor.** `doctor --rx-level` reported ~107–121 RMS
flat where an early run — with an HT keying 445.800 *before the first-ever TX* — had read **12431
RMS**. F3a is the live-bench diagnostic that found the cause and fixed it at the proven layer.

### The two RX-audio gates (one of them un-dockable)

The compiled fork driver (`App/driver/bk4829.c`) gates RX audio to the AIOC's speaker-line tap
behind **two independent things**:

1. **BK4819 `REG_47` — the AF selector (reachable over the dock).** `BK4819_SetAF` writes
   `0x6042 | (AF<<8)`: **MUTE = `0x6042`, FM/unmute = `0x6142`**. Squelch does not hard-gate AF on
   this chip — firmware software-mutes by holding `REG_47` at MUTE and only flips it to FM when its
   squelch opens.
2. **`GPIOA8` — the external audio power-amp enable (an MCU GPIO, NOT reachable over the dock).**
   `AUDIO_AudioPathOn()` → `GPIO_EnableAudioPath()` drives `GPIO_PIN_AUDIO_PATH = GPIOA pin 8` high.
   The dock HAL only calls `BK4819_ReadRegister`/`WriteRegister` — it cannot toggle any STM32 GPIO.

radio-server holds the radio in the `0x0870` full-control blocking loop for its whole lifetime,
which **starves the firmware's 10 ms timeslice** — so `APP_StartListening()` (the only path that
raises GPIOA8 and unmutes `REG_47`, on squelch-open) never runs. GPIOA8 and `REG_47` stay frozen at
whatever state they held when `EnterHwMode` arrived. radio-server's backend never writes `REG_47` or
GPIOA8 (`radio.py` only re-latches `REG_30`), so it cannot open RX audio itself.

### The bench proof (register dumps, actual runs — never inferred)

Driving the live radio over the dock through the real backend, from a fresh power-cycle:

| Step | REG_30 | REG_47 | REG_48 | REG_37 | AIOC RMS |
|---|---|---|---|---|---|
| Baseline (fresh idle) | `0xBFF1` | **`0x6042` MUTE** | `0xB37E` | `0x9F1F` | — |
| Force RX alive + **read-back** | `0xBFF1` ✓ | **`0x6142` FM** ✓ | `0xB3A8` ✓ | `0x9F1F` ✓ | **109 (FLOOR)** |

**The BK4819 was confirmed fully RX-alive and unmuted by read-back, yet the AIOC still read the noise
floor** (237 frames captured — the capture leg is live, just silent). BK4819 correct + silent ⇒ the
dead gate is **outside the BK4819 = GPIOA8**, exactly the un-dockable one. (The healthy 12431 reading
had caught the firmware mid-listen — HT keying → squelch open → GPIOA8 high — when full-control froze
it live; every reading since entered full-control from idle, GPIOA8 low.)

The `0x0871` resume was also settled with data: the baseline→post-exit register diff shows
`RADIO_SetupRegisters(true)` re-applies frequency/bandwidth (`0x38`/`0x39`/`0x43`) and leaves `REG_47`
at MUTE / audio-path off — it returns the radio to the normal *muted idle* RX state (this supersedes
F2's `⚠ CONFIRM AT BENCH` resume-RX flag).

### Source of truth (the pins)

- **Fix destination** — fork `kbennett2000/uv-k1-k5v3-firmware-custom`, branch `f3-rx-audio-fix`,
  commit **`79f9b21`** (base `f2-dock-port` @ `32c600b`, Apache-2.0). Symbols used, all already
  reachable in `App/app/uart.c`: `GPIO_EnableAudioPath()` (`gpio.h:57`), `gEnableSpeaker`
  (`misc.h:365`), `BK4819_SetAF`/`BK4819_AF_FM`/`BK4819_SetRxAudioGain` (`bk4819.h`).
- **Instrument** — radio-server `radio_server/doctor.py::_rx_noise` + the `--rx-noise` flag.

## Decision 1 — the fix is firmware, at dock entry (radio-server unchanged)

Because GPIOA8 is un-dockable, only the firmware can raise it. `Dock_EnterFullControl` now calls a
small `Dock_ForceRxAudioAlive()` right after taking full-control, before the blocking loop:

```c
GPIO_EnableAudioPath();          // GPIOA8 high — the un-dockable audio-amp gate
gEnableSpeaker = true;
BK4819_SetAF(BK4819_AF_FM);      // REG_47 = 0x6142 (unmute)
BK4819_SetRxAudioGain();         // REG_48 — normal RX AF/DAC gain from EEPROM
```

RX audio now flows the entire time radio-server holds full-control, independent of the frozen
firmware loop. `0x0871` exit already re-baselines via `RADIO_SetupRegisters(true)` (audio-path off,
`REG_47`→MUTE), so no new restore path is needed. **This keeps the whole fix in the fork — the dock
wire protocol and `radio_server/backends/uvk5/{frames,transport,radio}.py` change ZERO, so the F2
byte-compat invariant and all its tests hold.** `+28 bytes` (**104,144 B / 118 KB = 86.2%**); the
pure `App/app/dock.c` protocol core is untouched, so its host harness stays **19/19**.

Consequence: with RX audio forced open for the whole session, the radio's own speaker hisses
continuously while radio-server is connected (squelch is open at the chip). That is inherent to how
radio-server operates — it reads raw audio off the AIOC and gates in software — and is fully reverted
on `0x0871` exit.

## Decision 2 — leave an instrument: `doctor --rx-noise`

A new **HT-free** RX self-test so "is RX alive?" never again needs a second radio and a guess about
who was keying. `_rx_noise` (UV-K5 only) enters full-control, force-opens the receiver from registers
(`REG_37`/`REG_30`/`REG_48`/`REG_47`=FM), measures the AIOC, and **restores every register it
touched** (guardrail: no leaked force-open state). Verdict: loud noise (≥1000 RMS) ⇒ RX chain +
capture leg alive; floor ⇒ dead even force-open — suspect GPIOA8 (needs the F3 build) or the analog
leg. It reproduced the bug on the F2 build (110 RMS → "DEAD") and becomes the F3 acceptance ("LOUD").

## Consequences

- **radio-server change is the instrument only.** `radio_server/doctor.py` (+`_rx_noise`, `--rx-noise`)
  and `tests/test_doctor.py` (+4 tests); no dock-protocol / backend file touched. **`uv run pytest`:
  1492 passed, 4 skipped** (was 1487/5 — exactly the 4 new tests, plus one environment-dependent skip
  that now runs). A genuine V3 wire difference or any need to change the backend's operational path
  would have been STOP-and-report; neither arose — the cause was firmware-side and un-dockable.
- **External deliverable (fork):** branch `f3-rx-audio-fix` @ `79f9b21` (`App/app/uart.c` dock-entry
  RX bring-up, `BENCH.md` F3 acceptance); pre-release `radio-server-f3-v5.7.0` with
  `f4hwn.fusion.v5.7.0.f3-rx-audio.bin` (sha256 `f9a3cc2d…`) + `SHA256SUMS` + provenance.
- **BENCH.md** gained the F3 sequence: flash → `doctor --backend uvk5 --rx-noise` reads thousands
  (the fix) → live RX with an HT → the post-fix TX check (a key/unkey cycle must not re-kill RX;
  `_key_off` restores only `REG_30`, suspect `REG_50` if it does) → the settled resume-RX note.

## Out of scope (built here: none in the backend)

Any change to the dock wire protocol or the uvk5 backend's operational path (adding a GPIO opcode to
radio-server would violate the F2 invariant); calib/EEPROM; upstream PR submission; the browser
playback leg (not reached — the radio/AIOC leg is the proven fault); bench acceptance of the reflashed
fix (Kris flashes and runs `--rx-noise` — the LOUD reading is the confirmation).
