# 0137 — Let the radio be a radio: the deviation hypothesis is dead, measured

Status: Accepted

Supersedes the open question in ADR [0135](0135-ctcss-deviation-and-the-instrument-gap.md) and
[0136](0136-the-probe-measured-the-window-not-the-transmission.md). Corrects
[0132](0132-dock-band-and-the-register-model.md).

## Context

The operator asked the question three cycles should have asked first:

> "I can buy this radio stock out of the box and program repeaters into it and it would work fine.
> So why don't we just load repeaters into the radio and select them from the UI? Are we
> overengineering this?"

He is right about the disease. radio-server suspends the radio's firmware and hand-writes ~10
BK4819 registers to re-create what one existing firmware function, `RADIO_SetTxParameters`
(`App/radio.c:972`), already does correctly — and three cycles went into debugging that
re-creation against repeaters a stock radio opens with a thumb.

## The blocker that never existed

Every prior cycle ended "no RF measured — no shell on the bench box." **False.** `ssh
kb@192.168.1.62 true` returns 0 and always did. `ssh kb@home` was failing a *host-key* check that
was read as an auth failure, and the box's large pre-auth ASCII MOTD got read once as success and
once as failure. Neither time was the **exit code** checked, which is the only thing that answers
the question. Two cycles of hand-keyed procedure were designed around a blocker that was not there.

Rule: an access claim is a tested claim — `ssh ... true; echo $?`, never a conclusion drawn from
output text.

## What the air actually says

Measured through the kv4p witness against the UV-K5 driven exactly as a repeater preset drives it.
`scripts/bench/repeater_evidence.py`.

**CTCSS tone — present and strong.** Keyed with the tone off, on, then off again:

| | tone @141.3 Hz |
|---|---|
| OFF (control) | 19.11 |
| **ON** | **2840.71** |
| OFF again | 18.79 |

**149x** over a floor whose two bracketing controls agree. An earlier run read 527x. Either way the
tone is not weak, not missing, and not marginal. **The under-deviation hypothesis that ADRs 0135 and
0136 built an entire instrument for is dead.**

That first attempt at this measurement had its two controls disagree 17x (10.33 vs 170.69) because
the tone had not finished leaving the chip after 0.5 s. Taking the larger control and declaring the
tone weak would have reported an unstable bench as a weak transmitter — the same class of error as
ADR 0136's. The script now **voids** a run whose controls disagree by more than 4x rather than
producing a verdict from it, and waits 1.5 s.

**The split works.** Repeater presets differ from simplex in exactly one way — they transmit
somewhere else — so a synthetic split between two bench frequencies:

| witness parked on | RMS |
|---|---|
| TX leg 446.000 | 2872.7 |
| RX leg 445.800 | **0.0** |

A clean null on the receive leg. The carrier goes where a repeater's input would be.

**All 41 presets arm correctly.** Applied each and read back `/status` — software only, never keyed,
which is what makes it safe against presets whose transmit legs are live machines. Every RX, every
TX leg, every tone. The `-600 kHz` 2 m sub-band, the `+600 kHz` above 147.0, the `-5 MHz` 70 cm — all
right.

**Tone frequency is a witness artefact, not a radio fault.** All six tones read high by a *constant
ratio*:

| asked | measured | ratio |
|---|---|---|
| 88.5 | 88.798 | 1.00337 |
| 100.0 | 100.384 | 1.00384 |
| 123.0 | 123.457 | 1.00372 |
| 141.3 | 141.842 | 1.00384 |

Mean +0.372%, spread 0.00054. A constant *percentage* across every tone is the signature of a clock,
not of a bad tone code — and the witness has an uncalibrated one (`kv4p.sample_rate_correction`,
default 1.02, unset on this bench). Even taken at face value, 0.54 Hz at 141.3 is inside any repeater
decoder's capture range.

So: correct frequency, correct split, correct tone, at strength. **On the air, the dock path does
what it is asked.** Whatever stops these repeaters, it is not any of the four things measured here.

## Decision — Gate 0 before any more building

`scripts/bench/aioc_ptt_gate0.py`. One question, and it decides the architecture:

**Does the AIOC's serial PTT line key this radio?**

If it does, the dock register path is *optional* for repeater work. The operator picks the channel on
the radio, `AiocBaofeng` — which already exists and is bench-proven on a UV-5R (ADR 0029) — asserts
DTR, and the radio's own firmware sets up TX the way it does when a human presses PTT. That is
"Baofeng mode" from the project brief, already designed, needing no new backend.

The synthesised plan proposed watching the radio's TX LED. An LED says "something happened", not "RF
appeared on the right frequency". The kv4p runs as a **separate** user service on `:8091`, so it
stays up while the uvk5 service is stopped, and it gives an objective answer.

**The one thing a human must do**: DTR keying transmits on whatever the front panel is set to. The
host cannot see that and cannot override it — there is no front-panel read-back path anywhere in this
repo. So the radio must be parked on the bench frequency first. The script refuses any non-bench
frequency, aborts if the witness already hears a carrier, and restarts the service in a `finally`
(ADR 0127: stopping around a single-open test is the sanctioned pattern; *leaving* it stopped is not).

## What is NOT decided here

- **The literal "write channels over serial" version is refused.** Stock `0x051B`/`0x051D` each arm
  `gSerialConfigCountDown_500ms = 12` — a **6-second TX lockout** (`uart.c:355/393/447`) that masks
  PTT (`app.c:1441`), makes `RADIO_PrepareTX` return `TX_DISABLE` (`radio.c:1219`) and de-keys a
  transmission in progress (`app.c:1191`). Reading the channel to find out where you are costs 6
  seconds of no transmit. Also: no channel-select opcode exists in this firmware at all — the
  `0x0801`/`0x0803` frames in `frames.py` are modelled with no dispatch case behind them.
- **Root cause is still unknown.** Four things were ruled out by measurement, not one thing ruled in.
  Notably unmeasured: **absolute RF power**, because the witness is inches away and would hear a
  microwatt. That is the obvious next instrument and it is not built.
- **Nothing on 2 m was measured.** The witness is SA818-UHF. 15 VHF repeaters remain inferred.

## Correction to ADR 0132

ADR 0132:242-247 closed the PA-calibration door with "`0x100D0 = 65744 > 0xFFFF`, the address cannot
be expressed". **That arithmetic is wrong.** `eeprom_compat.c:69` maps flash `0x010000-0x0101FF` to
EEPROM `0xB000-0xB1FF`, so the calibration at flash `0x100D0` is EEPROM **`0xB0D0`** — comfortably
inside the u16 offset field. The TX calibration is readable with a stock `0x051B` and no firmware
change. Worth doing regardless of Gate 0: it retires the "power not characterised" warning
(`radio.py:858-867`), and register keying is not subject to the 6 s lockout.
