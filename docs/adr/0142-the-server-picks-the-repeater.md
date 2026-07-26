# 0142 — The server picks the repeater

Status: Accepted

Completes ADR [0141](0141-one-byte-over-the-wire.md). Corrects ADR
[0137](0137-let-the-radio-be-a-radio.md)'s account of the serial TX lockout. Retires the ADR
[0139](0139-opening-a-real-repeater.md) acceptance in favour of a differential one.

## Context

The operator's question has been the same since the beginning: *"What good is any of this if I have
to set the radio to a different channel each time?"*

ADR 0141 fixed the station's **reliability** — dual watch was alternating the transmit VFO on a
timer, one byte over the wire took key-ups from ~50 % to 30/30. It did not touch the actual
complaint. `radio.toml` holds **41 presets, 38 of them repeaters**, `POST /presets/apply` has
existed for cycles, and `apply_preset` calls `set_frequency` → `set_split` → `set_mode` →
`set_tone`. All of it worked. And none of it did anything, because `AiocBaofeng.capabilities()`
returned `SHARED_CAPS`: every tuning field was reported skipped and the radio never moved.

So the whole product was built except the one seam that makes it true.

## Decision

Give the Baofeng backend a tuner, injected, and let the existing preset path do the rest.

The UV-K5's UART rides the **same AIOC cable** the backend already holds for PTT, so nothing new is
plugged in and nothing has to be stopped. Two strategies behind one `Uvk5Tuner` protocol:

| | `setvfo` | `eeprom` |
|---|---|---|
| mechanism | one `0x0873` frame | write the channel, soft-reset onto it |
| firmware | F6 fork | **stock — what is already on the radio** |
| cost | instant | ~15 s, one flash write |
| survives a power cycle | no (RAM) | yes (storage) |
| confirmation | `0x0874` read-back of live state | read-back of storage |

`baofeng.uvk5_tuner` selects; default `off`, so a UV-5R is untouched and the setters raise
`UnsupportedCapability` rather than silently no-op'ing (guardrail 3).

**Not auto-detected.** Stock firmware drops an unknown opcode without a word, so probing means
waiting out a timeout at every startup to learn something the operator already knows.

### Batching, because a preset is one channel

`apply_preset` makes up to four setter calls. On the EEPROM path each commit is a reboot and a flash
cycle, so committing per setter would reboot the radio three times on the way to the channel asked
for, and leave it briefly on combinations — right frequency, no tone; right tone, wrong split —
worse than either end state. Setters stage a `VfoImage`; `tuning_batch` commits once. Verified by
test: a whole preset costs exactly one tune, and re-applying the same preset costs none.

## Four traps, each read out of the firmware rather than assumed

1. **`att == 0xFFFF` skips the VFO record entirely.** `radio.c:302-313` returns early and
   initialises to the band's *lower edge*, so the tune "succeeds" and changes nothing. The
   attribute word at `0x8000 + channel*2` must be programmed first.
2. **`data[12] == 0xFF` sets `TX_LOCK`** (`radio.c:377-383`) — an unprogrammed byte yields a radio
   that silently refuses to transmit. Byte 12 is written explicitly on every path.
3. **Band must match frequency.** The firmware takes `band = channel - 1024` for a frequency-mode
   channel and the PA bias is per band — writing 145 MHz into the UHF slot is the wrong-band-PA
   fault of ADR 0132/0134.
4. **`0xFF` is not "off".** `DUAL_WATCH` loaded as ON from an unprogrammed byte and cost four
   cycles. `CROSS_BAND_RX_TX` was checked and *does* fall back to OFF (`settings.c:171`).

## Three bugs found by building the instrument, not by flashing

The `0x0873` path was written and host-tested in a previous cycle but had never run on hardware.
Writing its reply found all three before the radio was ever flashed:

- **Units.** The wire carries Hz; the radio's VFO stores **10 Hz units** (`frequencies.c` has
  400 MHz as `40000000`). Every tune would have been ten times too high — and silently, because
  `FREQUENCY_GetBand()` *clamps* rather than reporting a miss, so 4.485 GHz resolves to band 7 and
  programmes the PA with band-7 calibration. No error, no RF anywhere anyone is listening.
- **Power scale.** `dock.h` documented `0 low, 1 mid, 2 high`, but the enum is
  `{USER, LOW1..LOW5, MID, HIGH}`, so "high" wrote `OUTPUT_POWER_LOW2`. This is the failure that
  looks exactly like the problem being solved: correct tune, radio keys, repeater stays shut.
- **Five silent rejections.** Short frame, full-control held, bad direction, bad bandwidth/power,
  and a tone missing from the radio's table — which fell through to *no tone*. The old comment
  conceded a caller "cannot distinguish 'refused' from 'applied'".

`0x0874` now answers every one, carrying the status plus the frequencies and power level read back
out of the radio's own VFO struct after `RADIO_ApplyOffset` — what it got, not what it was asked
for. Host tests 48 → 66 checks, including a byte-exact vector that the Python decoder is verified
against, so both sides of the wire are pinned to the same bytes.

## What ADR 0137 got wrong about the lockout

ADR 0137 refused the EEPROM path partly over "a 6-second TX lockout", read as a consequence of
*writing*. It is not. `gSerialConfigCountDown_500ms = 12` is armed by `CMD_0514` (**Hello**) and
`CMD_051B` (**EEPROM read**) as well as `CMD_051D` (`app/uart.c:355, 393, 447`). **Any** dock
conversation mutes the transmitter for six seconds.

That matters because this tuner ends with a Hello and a verification read, so it was handing back
"tuned" to a radio that would ignore the next six seconds of PTT. The bench said so precisely: every
carrier row failed on attempt #1 and passed on every attempt after. A tune now waits the lockout out
before returning — a tune is not finished until the radio can actually transmit.

The `setvfo` path never arms it (`0x0873` is not one of those four commands), which is a real
advantage of the flashed path beyond mere speed.

## Two more things the bench corrected

- **`busy` is hardwired `False` on this backend** (`AiocBaofeng.status`, ADR 0015 — no
  carrier-detect line). The first RX probe polled it, so it scored a working receiver as deaf and
  "passed" the detuned row for entirely the wrong reason. The measurement is now `tone_power` at
  1000 Hz from `/audio/rx`: **0.96 tuned versus 0.000 detuned**.
- **The shared serial handle needs the transport's port settings, not just its baud.** With
  pyserial's default `timeout=None`, the reader's `read(4096)` blocks for a full buffer and a
  24-byte reply is never dispatched — every request times out against a radio answering perfectly.
  `apply_port_settings` is now shared by both openers so they cannot drift.

## Acceptance — differential, and it measures us

ADR 0139's acceptance was to key a real repeater. The operator's objection was correct: *"you want
me to set the uv-k6 to a repeater? if so, WTF does that even test? I can do that without
radio-server entirely."* Keying a machine measures the antenna, the band and the repeater's mood at
least as much as this code, and on failure cannot separate "tuning is broken" from "nobody heard
us".

`scripts/bench/tune_follows_preset.py` applies a preset and checks **where the carrier is and where
it is not**. The silence rows are the measurement: a radio stuck on one frequency passes every
"is there a carrier?" row by accident and fails the moment you ask where the carrier should not be.

| preset | witness on | expect | result |
|---|---|---|---|
| Bench Simplex 445.800 | 445.800 | carrier | pass |
| Bench Simplex 445.800 | 446.000 | **silence** | pass |
| Bench Alt 446.000 | 446.000 | carrier | pass |
| Bench Alt 446.000 | 445.800 | **silence** | pass |
| Bench Split (rx 445.800 / **tx 446.400**) | 446.400 | carrier | pass |
| Bench Split | 445.800 | **silence** | pass |
| RX: Bench Simplex 445.800 | witness TX 445.800 | tone recovered | 0.96 |
| RX: Bench Alt 446.000 | witness TX 445.800 | **deaf** | 0.000 |

Row 5 is the repeater case in full: the carrier appears on the **transmit leg** of a split, which is
the entire mechanism a repeater needs. Every frequency is in `BENCH_TX_HZ`; nothing here keys a
repeater input, and nobody touches the radio.

## What this does not claim

- **No real repeater has been opened by the server yet.** The mechanism is proven on the bench;
  K0PRA 448.525 is a separate, clearly-labelled confirmation, not the gate.
- **2 m is unverified.** 36 of the 38 repeater presets are 145/144 MHz and the witness is an
  SA818-**UHF**. The code is band-generic and derives the 2 m band index the same way, but this
  bench cannot see it. Nothing here should be read as evidence about 2 m.
- **`setvfo` is unproven on hardware.** It is written, host-tested and the image is built and
  verified, but until the radio is flashed the only path with bench evidence is `eeprom`.
- **Absolute RF power** remains unmeasured (ADR 0137's open gap).
- `POST /radio/select` baofeng→uvk5 still **segfaults** (139). Untouched; this cycle never leaves
  the `baofeng` backend.
