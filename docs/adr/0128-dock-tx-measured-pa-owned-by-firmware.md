# 0128 — Dock TX measured end-to-end: Chain B is closed, and the PA belongs to the firmware

Status: Accepted — supersedes the mechanism described in
[ADR 0126](0126-uvk5-v3-dock-force-tx-chain-b-close.md) on two points of fact.

## Context

ADR 0126 closed Chain B *on paper*: it identified that dock keying lit the modulator but not the
power amplifier, shipped an F5 firmware fix (`Dock_ForceTx`), and left the bench acceptance to be
run later. This ADR is that acceptance, run against the deployed system, plus two corrections that
only measurement could produce.

The measurement instrument is `scripts/bench/uvk5_tx_regs.py` (new): with the service stopped, it
opens the dock, snapshots the BK4819 TX-path registers idle, keys, snapshots again, un-keys, and
snapshots a third time. Register state is the ground truth here because the two radios sit inches
apart — a bare modulator with no PA still couples enough near-field energy for the far end to call
it "a carrier" (F4 measured RMS 7427 exactly that way).

## What the bench actually said

Keyed on 445.800, dummy load, UV-K5 V3 over the AIOC dock:

| reg | idle | **keyed** | un-keyed | reading |
|---|---|---|---|---|
| `0x30` | `0xBFF1` | **`0xC1FE`** | `0xBFF1` | modulator keyed ✔ |
| `0x33` | `0x9048` | **`0x9028`** | `0x9048` | `0x20` **PA_ENABLE set**, `0x40` RX_ENABLE cleared ✔ |
| `0x36` | `0x0088` | **`0x0CA2`** | `0x0088` | PA bias **12**, UHF gain byte |
| `0x37` | `0x9F1F` | `0x9D1F` | `0x9D1F` | `PrepareTransmit` ran ✔ |
| `0x47` | `0x6142` | `0x6042` | `0x6142` | RX AF muted for TX, restored after ✔ |

### Correction 1 — the F5 firmware is flashed, is firing, and Chain B is fixed

radio-server never writes reg `0x33`'s `0x20` bit. It is set while keyed and clear again after, and
`0x37` moves to the stock `PrepareTransmit` value. **Only firmware can do that.** So the F5
`Dock_ForceTx` build is on the radio and behaving exactly as designed, and the open question from
ADR 0112/0113 — *does register-keyed TX in dock mode carry usable RF?* — is answered **yes**.

End-to-end, through the deployed HTTP API with the kv4p as an objective receiver
(`acceptance.py --only tx`):

```
kv4p carrier polls   4 with RF / 6 total
kv4p received RMS    7615
kv4p recovered 1000 Hz  0.990   (fraction of spectral energy in a 60 Hz band; noise floor ~0.005)
```

Where F4 measured **0 polls with RF / 9 without**, this is **4 with RF** and a cleanly recovered
tone. Chain B is closed by measurement, not by inference. **No firmware flash was needed** — the
build shipped by the F5 cycle was already on the radio.

A voice service is heard the same way (`--only services`): 476 728 B over a 4.8 s over, RMS 2761,
68 % of the energy in the 300–3000 Hz speech band.

### Correction 2 — radio-server's PA-power write was dead code

ADR 0126 said a bare `REG_30` write "skips the external PA-enable, **the PA bias**, and
REG_50/37/52". The bias half is wrong: `_key_on` *did* write reg `0x36`, computing
`(tx_power_pct × 2.55) << 8 | 0x88|0xA2` — byte-identical in form to the firmware's
`BK4819_SetupPowerAmplifier` (`bk4829.c:729`).

It never survived. `Dock_ForceTx` fires on the `REG_30` TX edge, which radio-server writes **last**
in the same batch, and the firmware then calls
`BK4819_SetupPowerAmplifier(gCurrentVfo->TXP_CalculatedSetting, freq)`. The read-back proves the
outcome: **bias 12**, never the 255 the backend computed from its `tx_power_pct = 100.0` default.

## Decision

**The firmware owns the PA chain. The backend stops pretending otherwise.**

Remove the `0x36` write and the `tx_power_pct` / `DEFAULT_TX_POWER_PCT` plumbing from
`radio_server/backends/uvk5/radio.py`. Rationale, in order of weight:

1. It has no effect — the firmware overwrites it microseconds later. A knob that silently does
   nothing is a trap for whoever reads this next.
2. It *could not* be right even if it did land. `TXP_CalculatedSetting` is derived from per-band
   calibration bytes in the radio's external SPI flash (`radio.c:601`), which the dock protocol
   cannot read. The host has no basis for computing a correct bias.
3. Overriding a calibrated PA bias with a raw percentage is exactly the "used improperly" failure
   mode. The radio's own **OUTPUT_POWER (Low/Mid/High)** setting is the calibrated lever, and
   `TXP_CalculatedSetting` follows it.

It was internal surface only — no config key, no documentation, no test referenced it — so removal
costs nothing. `uv run pytest` stays at **1566 passed / 5 skipped**.

## Consequences

- **The ADR 0126 verify-on-bench item is answered: dock TX radiates at PA bias 12** (reg
  `0x36 = 0x0CA2`, UHF gain byte `0xA2`). That is a low setting. It is enough for the bench
  acceptance above; if more radiated power is wanted, the lever is the **radio's own power level**,
  not a host register write. Left as a deliberate, named decision rather than a silent change.
- **Dock TX requires F5-or-later firmware.** Pre-F5 the PA rail never comes up, so removing the
  (already ineffective) host bias write costs nothing there either — that firmware cannot radiate
  usefully regardless.
- The F4 "first-key settle flake" (`REG_30 = 0xBFF1`, retry passes) did not recur across the
  keying in this work.
- `scripts/bench/uvk5_tx_regs.py` is kept as the permanent answer to "is the PA actually up?" — it
  needs the service stopped, and prints a verdict rather than raw registers.
