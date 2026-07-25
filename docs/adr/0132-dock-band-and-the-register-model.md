# 0132 — The dock keys the band the RADIO is on, and the backend believed whatever it found

Status: Accepted — amends [ADR 0128](0128-dock-tx-measured-pa-owned-by-firmware.md) on the timing
of the reg-0x36 write, and corrects the reg-0x33 mask specified in
[ADR 0112](0112-uvk5-radio-class-and-keying.md).

## Context

Everything in ADR 0127-0131 was proven on **445.800**. Moved to **147.555** (2 m simplex), the
station went dead in both directions: nothing heard on transmit, nothing decoded on receive. The
API reported success throughout — `POST /frequency` 200, `POST /services/01` 200, real `tx_key_up`
/ `tx_key_down` events in the operating log with plausible durations. So the fault was below the
API, and 445.800 had been hiding it.

It was three separate faults plus a fourth found on the way, and only the first two are about
bands at all.

## Fault 1 — the firmware sets the PA up from the radio's own VFO

`Dock_ForceTx` (`uvk5v3-f1-build/App/app/uart.c:724-734`) deliberately does **not** call
`BK4819_SetFrequency` — in dock mode the host owns the synthesiser via reg 0x38/0x39. But it then
does:

```c
BK4819_PickRXFilterPathBasedOnFrequency(gCurrentVfo->pTX->Frequency);   // uart.c:729
BK4819_SetupPowerAmplifier(gCurrentVfo->TXP_CalculatedSetting,
                           gCurrentVfo->pTX->Frequency);                 // uart.c:732
```

`gCurrentVfo` is the radio's own boot VFO — the frequency on its screen, which is UHF. So on a
host frequency in the other band the carrier is where the host put it and everything else is set
up for somewhere else. The firmware's own in-tree ADR flagged this as unverified
(`adr/0001-dock-force-tx.md:76-78`, "⚠ verify-on-bench") and guessed it "would only mis-scale
power". Measured, keyed on 147.555:

| reg | measured | should be | meaning |
|---|---|---|---|
| `0x38`/`0x39` | 147 555 000 Hz | ✔ | the carrier really is on 2 m |
| `0x36` | `0x0CA2` | `0x0C88` | bias 12 with the **UHF** gain byte (`bk4829.c:743`) |
| `0x33` | `0x9028` | `0x9026` | **UHF** LNA path selected |

The split the firmware uses is **280 MHz**, not the 174 MHz band edge — matched here deliberately,
because the point is to agree with the firmware, not to be independently right.

## Fault 2 — and it leaves the receiver pointing there

`PickRXFilterPathBasedOnFrequency` writes reg 0x33's LNA-path GPIOs (`bk4829.c:890-908`).
`Dock_EndTx` (`uart.c:740-746`) drops the PA rail and re-asserts RX_ENABLE but **does not put the
LNA back**, and `set_frequency` was the only thing in radio-server that ever wrote reg 0x33.

So on 147.555 the receiver worked until the first transmission and was deaf from then on, until
something retuned it. Measured: reg 0x33 `0x9046` (VHF LNA) before keying, `0x9048` (UHF) after.
Kris's own timeline fits — an automatic station ID fired five seconds after he retuned.

## Fault 3 — the backend adopted whatever state it found, for ever

This one is not about bands, is worse, and was found only because fault 1 forced a `/status.rssi`
field into existence.

`self._reg30` is the RX system-control word written back to un-key and at the end of every
`set_frequency`. It was seeded from a bare read of whatever the radio happened to be doing at
connect — and then kept for the life of the process. A radio found in a bad state therefore did
not merely start badly, it **stayed** bad: every retune wrote the bad value back, and nothing ever
re-read it.

Reproduced deliberately, twice:

```
radio left at 0x30 = 0        rssi 157 -> 0        (the receiver is off)
service started against it    rssi 0, busy false, 0 bytes of audio, indefinitely
with the repair               rssi 155-156, receiving normally
```

`0x30 = 0` is exactly where a lost un-key leaves the radio, and a register dump caught this radio
there straight after a probe run. It looked intermittent because restarting only helps when the
radio happens to be healthy in that moment. **This is the better candidate for "the server doesn't
hear me", and it is band-independent** — 147.555 had nothing to do with it.

## Fault 4 — the reg-0x33 clear mask never cleared the VHF bit

`reg33 & 0xFFE7` clears bits 3-4 while the VHF selection sets bit **2**, so VHF→UHF left *both*
LNA paths enabled. Inherited verbatim from ADR 0112's prose ("clear bits 3-4 … set bit 2 … else
bit 3"), which is self-inconsistent. The existing tests could not catch it: both start from a
`reg33` seed of 0, where the stale bit does not exist yet.

## Decision

**The band is ours; the calibration is the firmware's; and a register model must be asserted, not
remembered.**

1. **Correct the band after key-up, not before.** ADR 0128 removed the host's reg-0x36 write
   because the firmware overwrote it — the host wrote it *before* the reg-0x30 TX edge that
   triggers `Dock_ForceTx`. After the firmware's ~25 ms sequence, which `_confirm_keyed` already
   waits out (ADR 0131), the same write sticks. `_key_on` now re-writes 0x36 with the band-correct
   gain byte **keeping the firmware's bias byte**, and 0x33 with the keyed shape and the right LNA.
   ADR 0128's reasoning was right; only its conclusion about *which half* of that register the host
   can compute was too broad.
2. **The bias stays the firmware's.** It comes from per-band calibration in the radio's SPI flash
   that the dock cannot read. Computing one from a percentage is the exact mistake ADR 0128
   removed; correct the half the host can actually derive and measure whether it is enough.
3. **Non-raising.** A key-up that reached `_REG30_TX_ENABLED` is already confirmed on air. Failing
   the over because a refinement did not apply would trade a weaker signal for dead air — the wrong
   direction under the ADR 0112 invariant.
4. **`_key_off` restores the receive band path**, computed (`_rx_reg33`) rather than remembered.
5. **Repair an impossible seed.** Reg 0x30 reading `0` or carrying the dock's TX bit cannot be a
   receiving state; take the measured stock word `0xBFF1`, **write** it, and log. Connecting now
   repairs the radio instead of inheriting its damage. `set_frequency` likewise asserts the
   receiving shape of reg 0x33 rather than replaying the seed.
6. **Fix the mask** (`~(0x08|0x04)`), preserving every other bit in that byte — the firmware owns
   the rest of it.
7. **Refuse to retune while keyed.** The write list ends with the RX reg-0x30 word, which would
   drop the carrier mid-over.
8. **`status()` reports the raw RSSI.** `busy` is a threshold applied to a number nobody could see;
   sizing it meant stopping the service to open the serial port by hand, on a bench where stopping
   the service is what broke it (ADR 0127). Measured floors, per band, off a running station:
   **107 on 445.800, 154-156 on 147.555**. Both sit under the configured 220, so the single scalar
   still covers both bands and a per-band setting is **not** being added on speculation.

## Consequences — measured

Register read-back keyed on 147.555, before and after, on the deployed tree:

| | before | after |
|---|---|---|
| keyed PA gain byte | `0xA2` UHF ✘ | **`0x88` VHF ✔** |
| keyed LNA path | UHF ✘ | **VHF ✔** |
| LNA path after un-key | UHF ✘ | **VHF ✔** |
| synth while keyed | 147 555 000 ✔ | 147 555 000 ✔ |

445.800 re-measured unchanged (all three ✔, bias 12, idle RSSI 107), and `--only presets rx` on the
deployed tree still delivers **100.5 % duty, 41 ms largest gap, tone recovered 1.000**.
`uv run pytest`: **1585 passed / 5 skipped** (was 1574/5), with a regression test per fault,
including a `ForceTxFake` that models `Dock_ForceTx`/`Dock_EndTx` edge-triggered off reg 0x30 from
a configurable VFO band.

### Still open: VHF transmit is not audible at a distance

**Receive on 2 m is fixed and proven over real RF.** With a handheld keying 147.555 from across the
room, measured on the running station: `rssi 267` against a floor of 150 (**+117**), **squelch
OPEN**, held ~12 s. The configured `squelch_threshold = 220` therefore works on both bands.

**Transmit on 2 m is not.** With a real antenna on the K6, the operator hears the tones on 445.800
but only quarter-second bursts of static on 147.555 — fragments of an 8 s over, i.e. a signal that
only intermittently breaks his squelch. The bursts follow the frequency when the server retunes, so
it *is* the K6 and it *is* radiating; it is under-driven, not silent or misplaced. The registers
are right (table above), so this is a power problem, not a band-selection one.

The leading suspect is the **PA bias, which is still the firmware's and still derived for the wrong
band**. `TXP_CalculatedSetting` comes from per-band calibration, and the firmware uses a *different*
divider table for 2 m than for 70 cm (`App/radio.c:657-661`); here it is **12 of 255**, computed
for UHF, and inherited unchanged when the host tunes VHF. Correcting the gain byte (this ADR) does
not touch it, deliberately — the calibration is in SPI flash the dock protocol cannot read, and
there is no EEPROM-read opcode in the dock frame set to fetch it with.

**This bench cannot measure radiated power**, which is why it is still open rather than answered:

- The kv4p is inches away, so everything sits far above its squelch threshold.
- FM is constant-envelope — demodulated audio level does not track transmit power at all.
- Its `latest_rssi` (now surfaced) reads **0 on this firmware even while cleanly demodulating**.

`scripts/bench/uvk5_pa_sweep.py` twice produced a confident "FLAT: bias is not the knob" from that
non-functioning witness. **A flat line from a broken meter looks exactly like a flat line from a
real null, and only one of those is a finding** — it now refuses to conclude and exits non-zero
when the witness reads zero throughout. Neither of those runs is evidence of anything.

The remaining instrument is a person with a handheld, so
`scripts/bench/uvk5_audible_sweep.py` steps the bias with each step announcing its own number in
beeps before the tone. The listener reports one sentence — "I could hear step 5 onwards" — and that
names the bias. **One round trip, not yet run.**

### What is still not proven, and why

**The bench cannot hear 2 m.** The kv4p is a single-module SA818-**UHF** board (400-480 MHz;
`set_frequency` raises outside it), and it is the objective receiver every RF stage in ADR 0129's
runner depends on. So the fixes above are proven *at the register level* on VHF and by *end-to-end
RF* only on UHF. Two consequences:

- `scripts/bench/rf_listen.py` (new) measures what a running station hears on any band —
  `/status.rssi`, whether the squelch gate opened, and the received audio — so a human with a
  handheld produces every number in one key-up, and reports each signal live rather than at the end
  of the window.
- The acceptance runner's RF stages now **skip with the reason** when the two radios are not on the
  same channel, instead of reporting zero bytes / zero duty / zero tone. That output is
  indistinguishable from a broken receiver and it cost real time in this session: an early run read
  exactly that and sent me bisecting a regression that did not exist. A skipped run still exits
  non-zero — a skip is not a pass.

**A VHF kv4p board (or an RTL-SDR) is the fix**, and it is a purchase, not a code change. The
`module_type = "vhf"` path already exists and is tested (`backends/kv4p/radio.py:97-100`).

### A fifth thing, found by running the acceptance suite three times

Two of three runs failed `ALSA xruns while receiving == 1` while **every direct audio measure was
perfect** — 100.7 % duty, 41 ms largest gap, the whole 5.98 s over received, tone recovered 0.999.
The journal line behind the red X: `xrun after 0.023 s since the previous read`.

At a 1024/48000 blocksize that is **one block period** — the reader was exactly on time. So the
warning's own words, "the RX reader fell behind the card and audio was dropped", are false for this
event, and the acceptance runner counts that warning. **The false claim had become a false
verdict.** ADR 0130 had already characterised this residue as the card's own recovery with no audio
cost, and had explicitly argued for checking the property under test rather than a proxy — then
kept the proxy as the verdict.

Fixed at the source rather than by moving the threshold: an overrun reported within 1.5 block
periods of the previous read is logged as what it is (INFO — "the reader is on cadence and did not
stall"); a gap longer than the cadence still warns exactly as before; and the runner counts only
stalls. The check keeps its teeth for the ADR 0125 fault and stops firing for a non-fault. The
overflow test fake grew a read delay, because back-to-back reads against an instant fake are by
definition on cadence and could never have exercised the stall path.

### Deliberately not done

- **A transmit-band allow-list.** The uvk5 range check is 18 MHz-1.3 GHz
  (`backends/uvk5/radio.py:74-75`), so a 2 m frequency typo'd into the 1.2 GHz band passes every
  check the server has — where kv4p already models bands and rejects at config load. That is a
  Part 97 guard worth having, but it needs its own config surface and its own decisions (which
  bands, whose region, does it gate tuning or only keying), and bolting it onto a band-correctness
  fix would make both harder to review. Named here so it is not quietly forgotten.
- **The firmware fix.** Making `Dock_ForceTx` derive its band from a REG-38/39 read-back instead of
  `gCurrentVfo` is the clean permanent answer and would let the radio's screen say anything. It
  costs a flash, and a flash costs physically holding a key on the radio at power-on. The host-side
  correction above achieves the same result with neither, so the firmware change stays a
  nice-to-have (F6) rather than a requirement. The measured answer has been written back into the
  fork's own `adr/0001-dock-force-tx.md` verify-on-bench item.
