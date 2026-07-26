# 0141 — Dual watch was the fault, and one byte over the wire fixed it

Status: Accepted

Completes ADR [0140](0140-the-first-key-is-always-lost.md)'s diagnosis. Overturns ADR
[0137](0137-let-the-radio-be-a-radio.md)'s refusal of the EEPROM path.

## Context

The operator, after a merged PR changed nothing: *"repeaters STILL don't work. So what the fuck are
we doing?"* Fair. PR #196 shipped a harness, a diagnostic and docs — the actual fix was firmware in
an unmerged PR that had never been flashed, and that was buried at the bottom of a long message
instead of stated first.

ADR 0140 had established the shape of the fault but not closed it: the key-up success rate is a
**function of the gap between keys** (9/10 at 6 s, 5/12 at 19 s, strict alternation at ~21 s), the
PTT line is verifiably HIGH on 100 % of overs (`TIOCMGET`, 16/16 samples × 10), and dual watch was
the suspected two-state toggle.

## What was wrong with "we need a firmware flash"

Nothing, except that it was not necessary. **The firmware already on the radio dispatches EEPROM
read, EEPROM write, and a soft reset** (`App/app/uart.c`): `0x051B` → `0x051C`, `0x051D` → `0x051E`,
`0x05DD` → `NVIC_SystemReset()`. ADR 0137 refused this path on two grounds and both were wrong for a
*configuration* write:

- *"a 6-second TX lockout"* — `gSerialConfigCountDown_500ms = 12` only bites if you key immediately
  after writing. For one config change followed by a settle it is irrelevant.
- *"no channel-select opcode exists"* — true, and beside the point. **You do not select a channel;
  you write the VFO.**

## Two wrong addresses, caught by reading before writing

The classic UV-K5 settings block is `0x0E70`. On this V3 tree it is not:

- `settings.c` bypasses the compat layer entirely and uses **raw flash** —
  `PY25Q16_WriteBuffer(0x00A130, …)`, `0x00A150`, `0x00A158`.
- `eeprom_compat.c` maps host EEPROM `0xA000..0xA170` onto flash `0xA000` identity, while `0x0E70`
  (3696) falls inside the identity-mapped **channel** region `0x0000..0x1000`.

So a read at `0x0E70` returns unprogrammed channel space — **all `0xFF`**, which is indistinguishable
from a settings block full of defaults. The first probe returned exactly that, and writing on the
strength of it would have put a byte into someone's memory channels. The tell was that *every* byte
was `0xFF`; real settings have small values. The block is at **`0xA000`**, and `DUAL_WATCH` at
**`0xA00C`**.

## The finding

```
0xa000: FF FF FF FF C0 FF FF FF FF FF FF FF FF FF FF C8
                                             ^^ 0xA00C = DUAL_WATCH
```

`settings.c:173` loads it as `(Data[4] < 3) ? Data[4] : DUAL_WATCH_CHAN_A`. **`0xFF` is not < 3, so
an unprogrammed byte loads as dual watch ON.** It was never configured; it was the shipped default.

With dual watch on, `RADIO_SelectCurrentVfo` (`radio.c:715-721`) points `gCurrentVfo` at `gRxVfo`,
which dual watch alternates on its own clock — so **which VFO the station transmits from is decided
by a timer**. Roughly half the overs left on the other VFO, and the kv4p is SA818-UHF, so anything
on 2 m was inaudible. That is why `carrier_hunt` swept 48 candidates and found the carrier "nowhere":
it searched the only band it can hear.

## Decision — write the byte, verified, and prove it by the signature that found it

`scripts/bench/uvk5_eeprom.py`, read-only by default. `--set-dual-watch off` dumps the block first,
read-modify-writes only `0xA00C`, reads back, and **refuses to reboot on any mismatch**. The frames
live in `backends/uvk5/frames.py` (`EepromRead`/`EepromWrite`/`Reset`) because Phase 2 needs them in
the product, with the two guards that matter pinned by test: writes must be a whole multiple of 8
bytes and 8-byte aligned, because `CMD_051D` writes whole chunks — a 1-byte payload does not update
one byte, it updates eight, with seven bytes of whatever followed in the buffer.

**Result — the gap-dependence is gone, not merely improved:**

| gap between keys | before | after |
|---|---|---|
| 6 s | 9/10 | **10/10** |
| 19 s | 5/12, strictly alternating | **10/10** |
| 21 s | 5/10, strictly alternating | **10/10** |

30/30, carrier 1.17–1.27 s throughout. The verification is deliberately the same measurement that
exposed the fault: a fix that only showed up at one spacing would be indistinguishable from luck.

**On reversibility, stated honestly:** the flash is NOR, so a write only clears bits and putting
`0xFF` back needs a sector erase that would wipe every setting. It does not need to be undone — dual
watch is an ordinary menu setting the operator can restore from the front panel, and the radio's own
save path rewrites the block properly whenever any setting changes.

## What this does not claim

- **No repeater has been opened.** The station now keys reliably; it still transmits on whatever its
  front panel says, so the preset problem is untouched. Phase 2 — writing the VFO block from a preset
  and rebooting — is the same mechanism and is not yet built.
- **The other VFO's frequency was never read.** The alternation, its spacing-dependence and the fix
  all line up, but nothing here observed the second VFO directly; the bench has no VHF receiver.
- **Absolute RF power** remains unmeasured (ADR 0137's open gap), and **2 m** remains outside every
  instrument here.
- `POST /radio/select` baofeng→uvk5 still **segfaults the server** (status 139). It also persisted
  `server.backend = "uvk5"` mid-crash, which silently changed the resting mode and cost a confused
  measurement in this cycle.
