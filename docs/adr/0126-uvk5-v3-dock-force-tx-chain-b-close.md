# 0126 — UV-K5 V3: dock-mode TX had no PA — closed by a firmware fix (Chain B / F5)

Status: Accepted

## Context

F4 split the UV-K5 V3 server-deployment regression into two chains. **Chain A** (RX choppy + no
DTMF) was a real radio-server bug, fixed and merged in [ADR 0125](0125-uvk5-v3-rx-pump-cat-gate-decouple.md).
**Chain B** (browser TX not working + services not heard) is this ADR. It was the standing
open question from [ADR 0112](0112-uvk5-radio-class-and-keying.md) / [0113](0113-uvk5-audio-shared-soundcard-seam.md):
*does register-keyed TX in dock mode actually carry usable RF?* Measurement answered it: **no — and
not because of radio-server.**

> This ADR is documentation only. **radio-server does not change** — the backend's keying (REG_30
> write + CONFIRM read-back, ADR 0112) is correct and proven. The fix lives in the firmware fork.
> The Chain B *diagnosis* was recorded in the HANDOFF by the doc-only PR #185; this ADR closes it
> with the fix. (If #185 has not yet merged to `master`, it should merge first; this ADR is
> self-contained and does not depend on its content.)

### What the bench proved (the kv4p as an objective RF sniffer)

The HT gave unreliable data all session, so the diagnosis used the bench **kv4p** (UHF SA818, on
8091) whose `status().busy` is a hardware carrier-detect ([`backends/kv4p/radio.py:562`](../../radio_server/backends/kv4p/radio.py#L562)).
Both radios on **445.800** (the kv4p is UHF-only), UV-K5 inches away:

| Measurement | Meaning |
|---|---|
| Browser PTT → `tx_key_up` 5.64 s; `_key_on` CONFIRM read-back (`REG_30`→keyed) passes | Software keys **reliably** — not a radio-server bug |
| Dummy-load run: kv4p saw a modulated carrier (RMS to 7427, voice bins) | The **BK4819 chip TX engages** — near-field only |
| Antenna run, same confirmed **5.7 s** key (`dual_tx_watch.py`): UV-K5 `transmitting=True`, kv4p carrier **False** throughout (9 polls keyed-WITHOUT-RF, 0 with) | The **external PA / antenna switch never engages** — no usable radiated power |
| RX still works with the antenna on (Kris hears himself) | Radio **is** in dock mode; the dark path is specifically TX PA |

### Root cause — and a correction to the F4 framing

radio-server keys by writing **`REG_30`** (`0xC1FE`, TX_DSP). Stock TX (`FUNCTION_Transmit` →
`RADIO_SetTxParameters`, firmware `App/radio.c:972`) does much more; a bare `REG_30` write **skips**
the external **PA-enable** (`BK4819_ToggleGpioOut(GPIO1_PIN29_PA_ENABLE)`), the **PA bias**
(`BK4819_SetupPowerAmplifier`), and the `REG_50/37/52` un-mute. So the modulator lights up and
nothing reaches the antenna — exactly near-field-carrier-but-no-radiated-RF.

The F4 HANDOFF called the missing pieces "MCU-side PA-enable + antenna-switch GPIOs," by analogy to
ADR 0120's genuinely-un-dockable `GPIOA8`. **The firmware source corrects this:** on this PY32F071
port there are **no MCU GPIOs in the TX path** — PA-enable, the T/R switch, and the LNAs are all
**internal BK4819 GPIOs written through `REG_33`**, and PA bias is `REG_36`. Both are BK4819
registers, reachable over the dock *in principle*; radio-server simply never writes them. The
mechanism (no PA) stands; the "un-dockable" label was imprecise. This closes the oldest open
question in the arc: register-keyed TX does **not** carry usable RF in dock mode until the PA is
explicitly engaged.

## Decision — the fix is firmware; radio-server is unchanged

The TX mirror of ADR 0120's `Dock_ForceRxAudioAlive`. In the fork
(`kbennett2000/uv-k1-k5v3-firmware-custom`, branch `f5-dock-force-tx` @ `79d522a`, base
`f3-rx-audio-fix` @ `79f9b21`, pin v5.7.0/`3bd3ebba`; fork ADR `adr/0001-dock-force-tx.md`):

- **`App/app/dock.c`** (pure protocol core) edge-detects the `REG_30` `ENABLE_TX_DSP` bit and calls
  a new optional `dock_hal_t.tx_set(on)` callback **before** completing the register write. Zero
  wire-byte change (no new opcodes/replies), so radio-server's `FirmwareFakeSerial`/`Uvk5Decoder`
  byte-compat and the whole F2/ADR 0119 invariant hold.
- **`App/app/uart.c`** binds it to `Dock_ForceTx`/`Dock_EndTx`, which add exactly the stock PA steps
  (`PrepareTransmit` → `PickRXFilterPath` → `ToggleGpioOut(PA_ENABLE, true)` →
  `SetupPowerAmplifier(TXP_CalculatedSetting, freq)`), drop them in stock order on un-key (bias→0
  then PA-enable off), and re-open the F3a RX audio path so a service announcement doesn't leave RX
  deaf. PA is strictly slaved to `REG_30` and forced off at `0x0870` enter / `0x0871` exit; the
  existing TOT/RF-guards (untouched) remain the crash backstop.

**radio-server impact: none.** No `frames.py`/`transport.py`/backend change; `uv run pytest`
unchanged (1566/5). The fork's host dock tests went 19 → 31 checks; the Fusion build is 104,340 B /
118 KB (86.35%), +196 B over F3. Pre-release
[`radio-server-f5-v5.7.0`](https://github.com/kbennett2000/uv-k1-k5v3-firmware-custom/releases/tag/radio-server-f5-v5.7.0)
(`f4hwn.fusion.v5.7.0.f5-dock-force-tx.bin`, sha256 `881ca3fd…`).

## Consequences / acceptance

- **Bench acceptance (Kris; the number that closes Chain B):** flash F5 → dummy load → `doctor
  --key-test` (CONFIRM) still passes → `doctor --tx-tone` with `/tmp/dual_tx_watch.py` running →
  **kv4p carrier `True` while keyed** where F4 showed 0-with / 9-without → browser TX + a service
  heard → antenna range proof. Full sequence in the fork `BENCH.md` (F5 section).
- **Verify-on-bench (firmware):** which `OUTPUT_POWER` level dock TX radiates and the `gCurrentVfo`
  frequency source for the PA bias — marked in the `Dock_ForceTx` comment, not asserted.
- **Carry-forward:** the F4 first-key settle flake (`REG_30=0xBFF1`, retry passes) — the F5
  `SYSTEM_DelayMs` settle points target it; confirm at bench.

## Out of scope

The bench acceptance itself (Kris flashes and runs the F5 sequence — the kv4p carrier reading is the
confirmation); any radio-server code change (the byte-identical invariant held); a VHF objective-RX
check (the kv4p is UHF; the PA gap is expected band-independent).
