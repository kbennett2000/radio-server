# Setting up a Quansheng UV-K5

A **Quansheng UV-K5/UV-K6** on an **AIOC cable** is the most capable radio radio-server supports. Over
the one cable that already carries the audio and the keying, the host can also tune it, set a repeater
split and a CTCSS tone, pick a transmit power level, and key it — the CAT-style control a UV-5R
cannot do.

**You may not have to flash anything.** There are three ways to run one and only two involve custom
firmware:

| | Backend | Firmware | Gets you |
|---|---|---|---|
| **A** | `baofeng` + `uvk5_tuner = "eeprom"` | **stock — nothing to flash** | tune, split, tone, mode, power. ~14 s per change |
| **B** | `baofeng` + `uvk5_tuner = "hybrid"` | the **F6 fork** (V3 radios) | the same, but instant (~1 s), and channels survive a power-cycle |
| **C** | `uvk5` | classic Dock, **or** the fork at F5+ | `scan`, a real RSSI busy line, PA read-back, register keying. **No power control** |

**Try Path A first.** No flash, works on any UV-K5, and it is the whole of what most stations want.
Its cost is speed: writing a channel into the radio's memory takes a soft reset, and the firmware
mutes the transmitter for six seconds after any memory conversation.

**Path B is what a repeater station wants** — instant retune and transmit-power control. It needs the
custom firmware below.

**Path C is the specialist option.** It is the only path with `scan` and a real busy line, and the
only one where the host drives the chip directly. But the radio's front panel is suspended while it
runs, and it cannot set transmit power: over the dock that is a raw PA-bias register write whose
per-band calibration the host cannot read.

Paths A and B are configured in
**[the `[baofeng]` section](configuration.md#tuning-a-uv-k5-over-the-aioc-cable)**. Everything on this
page after the firmware section describes **Path C** — except the wiring, which all three share.

> **The single most useful habit:** run the check-up tool first, before anything else.
> ```sh
> uv run python -m radio_server.doctor --backend uvk5
> ```
> It opens the AIOC serial link, elicits a register read, and reports which firmware the radio is
> running **and which level of it** — **without keying the transmitter**. See
> [What the check-up tool is telling you](#what-the-check-up-tool-is-telling-you). That saves you a
> long silent dead-end.

---

## Firmware — which one depends on which radio you have

**This is the step to get right, and the answer is not the same for every UV-K5.** There are two
custom firmwares. They target different microcontrollers, and neither will run on the other's radio.

| Your radio | MCU | Firmware | Flashed via |
|---|---|---|---|
| Classic UV-K5 / UV-K6 | **DP32G030** | [`nicsure/quansheng-dock-fw`](https://github.com/nicsure/quansheng-dock-fw) @ **`0.32.21q`** | the serial bootloader (hold **PTT + flashlight** while powering on) |
| **UV-K5 V3** | **PY32F071** | [`kbennett2000/uv-k1-k5v3-firmware-custom`](https://github.com/kbennett2000/uv-k1-k5v3-firmware-custom) | **USB DFU** |

The stock Quansheng firmware answers a version handshake but **ignores every dock command**, so a
stock radio looks alive and cannot be driven in full control. (It *can* still be tuned — that is
Path A above, which uses stock memory commands and needs none of this.)

### How to tell which one you have

The tell we can vouch for is **how the radio enters flash mode**: a classic UV-K5 uses the DP32G030
serial bootloader, entered by holding **PTT + flashlight** while powering on; a **V3 enumerates as a
USB DFU device** instead. The bench radio here is a V3 reporting **bootloader 7.00.07**.

> **Verify against your own radio before you flash.** That is one radio and one data point. If yours
> matches neither description, stop and identify it rather than flashing on a guess — firmware built
> for the other MCU will not run.

### If you have a V3 — the fork, and why it exists

nicsure's Dock firmware **cannot run on a V3 at all**: it is compiled for a different chip. So the V3
path is a fork we maintain — [`kbennett2000/uv-k1-k5v3-firmware-custom`](https://github.com/kbennett2000/uv-k1-k5v3-firmware-custom),
built on **[F4HWN](https://github.com/armel/uv-k1-k5v3-firmware-custom)** (Armel's community firmware
for exactly this radio) pinned at tag `v5.7.0`, Apache-2.0. It adds a **dock control mode** speaking
the same wire protocol as the classic Dock, so radio-server drives both identically.

It was built in cycles, and **the level matters** — each unlocked something the one before it lacked:

| Level | What it added | Without it |
|---|---|---|
| **F2** | dock mode at all — register read/write, enter/exit full control | the `uvk5` backend cannot connect |
| **F3** | forces the RX audio path alive | dock mode connects and receives **silence** |
| **F5** | engages the power amplifier on key-up | it keys perfectly and **radiates nothing usable** |
| **F6** | the set-VFO command (`0x0873`) | `setvfo`/`hybrid` tuning and power control fail |
| **F7** | the set-modulation command (`0x0877`) | the radio can only receive FM — no airband, and one warning at server start (below) |
| **F8** | the broadcast-FM command (`0x0879`) — the radio's *second* receiver, the BK1080 | the 88-108 MHz receiver stays unreachable from the server; nothing else changes |

> **F8 makes the radio deaf while it is on.** Broadcast FM takes over the speaker line the AIOC
> listens on, so the station hears the broadcast station and **nothing of its own channel** — and it
> still transmits normally, including its automatic station ID. Nothing on the server drives this
> yet; when something does, it is the operator's call to make.

**Flash [`radio-server-f6-v5.7.0`](https://github.com/kbennett2000/uv-k1-k5v3-firmware-custom/releases/tag/radio-server-f6-v5.7.0)**
unless you have a reason not to — it is cumulative, so it includes F2, F3 and F5. The fork's
[README](https://github.com/kbennett2000/uv-k1-k5v3-firmware-custom#readme) carries the flash runbook
and the build instructions; its `PROTOCOL.md` documents the wire protocol if you want to drive the
radio from your own software.

The check-up tool reports your level, so you never have to guess which one you are on.

> **"could not state the demodulator at startup" in the log.** On Path B, radio-server sets the radio
> to FM when it starts, because the demodulator is the one setting the radio cannot be *asked* about
> (see [Configuration](configuration.md)). A **pre-F7** build has no set-modulation command and drops
> that frame without a word, so you get one warning per start and everything else works normally — the
> radio can only receive FM anyway. The same warning appears on any firmware if the radio was switched
> off when the server came up. **Path A never sends the frame at all**, because stock firmware has no
> `0x0877` case and the `eeprom` tuner does not advertise the capability.

### If you have a classic UV-K5

Flash [`nicsure/quansheng-dock-fw`](https://github.com/nicsure/quansheng-dock-fw) at tag
**`0.32.21q`** — the pinned release radio-server's register sequences were derived against — using
the tool and steps from that project's own instructions. Its client, `nicsure/QuanshengDock` at the
same tag, is reference only; you do not need it.

> If the check-up tool later reports a **version other than `0.32.21q`**, a different build may drift
> the register/wire protocol — reflash the pinned tag.

---

## Wire it up — the AIOC and the K1 jack

The **AIOC cable** plugs into the radio's **K1 jack** (the two-pin speaker/mic connector) and presents
two things to the computer over one USB connection:

- a **serial port** (a `/dev/ttyACM*` CDC device) — dock control and keying ride this, and
- a **USB sound card** (`All-In-One-Cable: USB`) — RX/TX audio rides this.

**Plugging the AIOC into the K1 jack mutes the handheld's own speaker and microphone** — this is
expected and correct: the cable takes over the audio path so the computer hears/sends it instead of
the radio's own speaker and mic. It is not a fault.

**Use the stable by-id serial path, not `ttyACM0`.** The AIOC enumerates as `/dev/ttyACM*`, and the
bare `ttyACM0` name goes to whichever CDC device the computer sees first — with a second AIOC (or any
other ACM adapter) plugged in, your radio can land on `ttyACM1` and nothing connects. List the stable
paths and pick the AIOC:

```sh
ls /dev/serial/by-id/
```

Use the whole `…All-In-One-Cable…` path as your `uvk5.serial_port`. radio-server deliberately has **no
default here** — with an ambiguous multi-adapter bench, a guessed `ttyACM0` would be wrong, so an
unset `uvk5.serial_port` fails loud rather than pointing at the wrong device. On Linux you also need to
be in the **`dialout`** group (`sudo usermod -aG dialout $USER`, then log out and back in).

---

## Configure it

A minimal `[uvk5]` block (browser Settings tab, or `radio.toml`):

```toml
[server]
backend = "uvk5"

[uvk5]
serial_port = "/dev/serial/by-id/usb-...All-In-One-Cable..."  # REQUIRED — your AIOC's stable path
frequency = 146520000        # REQUIRED — Hz; the host owns tuning (no radio-side value to keep)
# tone = 100.0               # optional TX CTCSS tone in Hz; omit for none
mode = "FM"                  # FM (wide) or NFM (narrow)
# tx_allowed = false         # set for a genuinely receive-only node (refuses to key, fails loud)
```

Both `serial_port` and `frequency` are **required**. Unlike the kv4p (whose firmware remembers its
last frequency), a UV-K5 in full-control mode has no radio-side frequency worth preserving — so
radio-server does not invent one: an unset `uvk5.frequency` fails loud rather than putting a made-up
or stale frequency on the air. See [Changing the settings](configuration.md) for the full `[uvk5]`
section (including `uvk5.squelch_threshold`, the RSSI busy gate that `uvk5.squelch_mode = "cat"`
reads — the uvk5's own squelch mode, which defaults to `cat` and overrides the global `audio.squelch`;
ADR 0121).

---

## Bring-up checklist — the bench gates, in order

Work these **in order** on the bench. Each is a gate: don't move on until it passes. Every keying step
is you, at the bench, into a dummy load.

1. **Connect probe** — `uv run python -m radio_server.doctor --backend uvk5`. Confirms the AIOC serial
   path opens, that the radio answers on **Dock firmware**, and **which level** of it (F6 or pre-F6).
   If it reports stock firmware, go back and flash (above). *No keying.*
2. **RX gate** — `uv run python -m radio_server.doctor --backend uvk5 --rx-level --seconds 30` while a
   signal is coming in (e.g. a live repeater). Confirms the AIOC sound card is capturing real audio and
   prints the measured true sample rate. *No keying.*
3. **Keying gate** — `uv run python -m radio_server.doctor --backend uvk5 --key-test` **into a dummy
   load**. It register-keys the radio and confirms via the chip read-back that TX actually engaged, then
   drops it. This proves the register keying path.
4. **THE acceptance gate** — the one thing that cannot be settled offline: **does the register-keyed
   transmitter actually carry the AIOC-injected K1 mic audio, and does it radiate?** With a dummy load
   and a monitoring receiver, key up and play audio (e.g. an announcement) and confirm your voice/tone
   goes out — not silence, not the radio's own mic. This is also where you bench-tune
   `uvk5.tx_lead_seconds` (default 0.5 s, inherited from the AIOC/UV-5R bench — this radio earns its
   own number) and confirm the AIOC device name.

   > **If it keys cleanly and nothing comes out, suspect the firmware level before the wiring.**
   > Pre-F5 the radio reports a successful key-up, the chip read-back confirms it, and the power
   > amplifier never comes up — a near-field sniff sees a carrier and an antenna run sees nothing.
   > That cost this project a whole cycle to diagnose (ADR 0126/0128).

Once all four pass, run the server with `server.backend = uvk5`.

Other check-up flags worth knowing: `--rx-noise` (an HT-free receive test that force-opens the
receiver and restores every register it touched — this is what catches a pre-F3 silent RX),
`--rssi`, `--rx-firststart-loop` (catches a `0x0870` lost in the reset-on-open boot race), and
`--tx-tone`.

---

## ⚠️ Stuck-key warning — a mandatory server time-out, and its one residual gap

In full-control mode the host is the radio's brain, and the radio stays keyed until the host tells it to
stop — and unlike the kv4p (firmware ~200 s cutoff) or a UV-5R (its own TOT menu), the UV-K5 has **no
device-side time-out** of its own. So radio-server enforces one **for** it: a **mandatory transmitter
time-out** (`uvk5.tot`, default **180 s**, ADR 0117) force-unkeys any stuck key — a held mic, a wedged
crossband over, a decode that hung with PTT down — and logs an `alarm`. It is on by default and **cannot
be disabled**: you may shorten `uvk5.tot` (down from 180 s) but never set it to 0. This runs on its own
timer thread, so it fires even when something is wedged, and it performs the **full RX-register restore**,
not just a PTT-line drop.

**What it covers:** logic bugs, runaway sessions, a leaked transmit stream, and a **normal** stop —
`SIGTERM` / a clean crash unkey via `atexit` before exit.

**The one residual gap it cannot cover — because it is in-process:** a **hard kill of the host**
(`SIGKILL`/`kill -9`), a kernel panic, or a **power loss** while transmitting bypasses both the time-out
and the `atexit` cleanup, and **leaves the radio keyed** — transmitting dead air until you power-cycle it.
An out-of-process supervisor (a watchdog process or hardware timer that unkeys if the server stops
heart-beating) would close this, and is a named possible follow-on — not built yet. Until then: **key into
a dummy load on the bench, don't `kill -9` a transmitting server, and don't leave a UV-K5 node unattended
without knowing this failure mode.**

---

## What the check-up tool is telling you

`uv run python -m radio_server.doctor --backend uvk5` runs the connect probe and prints what it finds.
Its firmware lines map directly to the flash step above:

- **"STOCK firmware — flash the Quansheng Dock firmware"** — the radio answered the version handshake
  but not the dock register read, so it is still on **stock firmware**. This is a firmware problem, not
  wiring. Note that stock is perfectly usable via **Path A** — this line means only that *full control*
  is unavailable. Without it you'd just see a silent no-answer and have no idea firmware was the cause.
- **"no response to the register-read probe"** — nothing answered at all: the radio is off/asleep, the
  serial path is wrong, or the baud is wrong. Check the by-id path and that the AIOC is seated in the K1
  jack.
- **"set-VFO command present (0x0873/0x0874) — F6 or later"** — the firmware level check. The probe
  sends a deliberately **empty** set-VFO frame, which the firmware refuses before it decodes a field or
  touches a VFO, so it asks the question without tuning anything. This line means `setvfo`/`hybrid`
  tuning and transmit-power control all work.
- **"no 0x0874 reply — pre-F6 dock firmware"** — dock mode is alive but this is an older build (F2/F3/
  F5, or a classic Dock). `setvfo` and `hybrid` will fail and power cannot be set; **the `eeprom`
  tuner works on this firmware today**, and flashing F6 is what buys instant tuning. It is a warning,
  not a failure — the radio works, just not that way.
- **"dock version — not read"** — expected on a V3, and a PASS. The fork is always-encrypted and
  dropped the classic Dock's plaintext-HELLO toggle, so it does not answer that handshake; dock-alive
  is already proven by the register elicit above.
- **"dock version … != pinned 0.32.21q"** — only a **classic** Dock radio can reach this line. It is on
  Dock firmware but a different version than the backend was derived against; reflash the pinned tag.

---

## A few surprising-but-normal things

- **The K1 jack mutes the radio's own speaker and mic.** Expected — the AIOC takes over the audio path
  (see [Wire it up](#wire-it-up--the-aioc-and-the-k1-jack)).
- **Opening the AIOC serial port may reset the radio.** The connect probe retransmits its elicit to ride
  through a reset-on-open, so a brief settle on first connect is normal.
- **There's no tuning knob in full control.** In full-control ("XVFO") mode the host owns tuning — set
  `uvk5.frequency` (and retune live via the API). The radio's front-panel VFO is suspended.

---

## Where to go next

- **[Setting it up with your radio](install.md)** — the install steps, including the uvk5 branch.
- **[Changing the settings](configuration.md)** — the full `[uvk5]` section and the squelch settings.
- **[Using your station](using-it.md)** — the control panel and calling in over the air.
- **[The V3 firmware fork](https://github.com/kbennett2000/uv-k1-k5v3-firmware-custom)** — what the
  dock control mode adds, how to build and flash it, and `PROTOCOL.md` for driving the radio from
  your own software.
