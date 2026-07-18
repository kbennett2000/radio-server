# Setting up a KV4P HT board

The **[KV4P HT](https://www.kv4p.com/)** is a small open-source board (an ESP32 with an SA818 radio
module) that plugs into a USB port and *is* the radio — there's no separate handheld, no audio cable,
and no sound card involved. That makes it, in some ways, the **easiest** radio to run with
radio-server: the software install needs no system audio libraries at all (just `uv sync --extra
kv4p` — see [Setting it up with your radio](install.md)).

The one part that's genuinely fiddly is the very first step: **flashing the board**. A brand-new board
usually needs its firmware and its band settings written to it once before it will talk to anything.
Take this slowly — it's a one-time thing, and the check-up tool will tell you plainly if something
isn't right.

> **The single most useful habit:** run the check-up tool first, before anything else.
> ```sh
> uv run python -m radio_server.doctor --backend kv4p
> ```
> It opens the board, does the handshake, and prints what the board reports — **without keying the
> transmitter**. If the board needs flashing, it says so in one line (see
> [What the check-up tool is telling you](#what-the-check-up-tool-is-telling-you) below), which can
> save you a long, silent dead-end.

---

## Flashing: two writes, in this order

A working board needs **two** things written to it, and the **order matters**:

1. **The firmware** (v17 or newer) — written at address `0x0`.
2. **The board-config** (which band your module is, and other per-board settings) — written at
   address `0x9000`.

Here's the trap that catches almost everyone: **the firmware image is one big block that spans
`0x0`–`0xeafff`, and that range covers the board-config area.** So writing the firmware **erases the
board-config.** If you flash only the firmware, the board comes up with no band setting and quietly
falls back to a compiled-in default (VHF) — which is wrong for a UHF board, and nothing tells you.

So the rule is simple: **flash the firmware first, then flash the board-config second.** Never flash
the firmware without re-flashing the board-config right afterward.

---

## Pick the right band image

The board's band is **not** auto-detected — it comes from the board-config you flash. There are six
board-config images: three PCB families (**v1x**, **v2abc**, **v2de**) each in **VHF** and **UHF**:

| Your PCB revision (read it off the silkscreen) | Use the image group |
|---|---|
| v1.x | `v1x` |
| v2.0a / v2.0b / v2.0c | `v2abc` |
| v2.0d / **v2.0e** / v2.1x | `v2de` |

Two things to get right:

- **Read the PCB revision off the board itself** — it's printed on the silkscreen. Don't guess.
- **A v2.0e board uses the v2.0d config** — the flasher groups them together as "PCB v2.0d/e/2.1x".
- **Pick the band you actually bought** — a VHF module needs the `…-vhf` config, a UHF module the
  `…-uhf` config. If the band you bought and the band the check-up tool later *reports* disagree, the
  board-config is wrong or missing — reflash it (this is exactly what the tool's "band mismatch" line
  is telling you).

So a UHF v2.0e board wants the firmware image, then `board-config-v2de-uhf.bin`.

---

## Two ways to flash: the browser, or the terminal

**The easy way — the web flasher.** The kv4p project provides a browser-based flasher (linked from
[kv4p.com](https://www.kv4p.com/)) that runs in Chrome, talks to the board over USB, and walks you
through both writes. For most people this is the way to go — you pick your PCB and band and it handles
the offsets for you.

**One sharp edge with the web flasher:** if a flash attempt fails partway, **Chrome keeps hold of the
USB port**, and *reloading the tab does not release it*. The next attempt then can't open the board and
looks like a hardware fault. The fix is to **fully quit Chrome** (not just close the tab — quit the
whole browser), then reopen it and try again.

**The fallback — the terminal (`esptool`).** If the browser flasher won't cooperate, you can write the
two images from a terminal with Espressif's `esptool`. The offsets are the same two from above:

```sh
esptool.py --port /dev/serial/by-id/usb-...  write_flash 0x0    kv4p-firmware.bin
esptool.py --port /dev/serial/by-id/usb-...  write_flash 0x9000 board-config-v2de-uhf.bin
```

(Adjust the port and the two filenames to match your board and the images you downloaded.) The
browser is the path most people want; the terminal is the escape hatch when the browser holds the
port hostage.

---

## Plug it in — and use the stable port name

The board shows up as a USB serial device (a CP210x or CH340 chip), usually `/dev/ttyUSB0` on Linux.

**Don't rely on `/dev/ttyUSB0`.** The `ttyUSB0` name goes to whichever USB serial device the computer
sees *first* — if you have anything else plugged in (another radio adapter, a dongle), it can grab
`ttyUSB0` and your kv4p lands on `ttyUSB1`, and suddenly nothing connects. Use the **stable by-id
path** instead, which always points at the same physical board no matter what else is plugged in:

```sh
ls /dev/serial/by-id/
```

Pick the entry with `CP210` or `CH340` in its name and use that whole path as your `kv4p.serial_port`
setting. On Linux you also need to be in the **`dialout`** group (`sudo usermod -aG dialout $USER`,
then log out and back in) — the same one-time step every serial radio needs.

---

## Set the frequency — there's no knob

Unlike a handheld, the board has **no tuning knob**. It remembers the last frequency it was set to (in
its own storage), so out of the box it comes up on whatever it happened to be left on — and those
factory defaults tend to land in awkward places: the **satellite segment (435.000 MHz)** or right at a
**band edge (400.000 MHz)**. Neither is a good place to key up.

So **set a frequency you choose** before you transmit. In your settings (browser Settings tab, or
`radio.toml`), set `kv4p.frequency` to a frequency in Hz — for example `146520000` for 146.520 MHz.
radio-server deliberately does **not** invent a default here: an unset value is left to the board
rather than putting a made-up frequency on the air. Pick one that's legal and appropriate for your
license and your module's band.

See [Changing the settings](configuration.md) for the full `[kv4p]` section, including the two
different squelch settings.

---

## What the check-up tool is telling you

`uv run python -m radio_server.doctor --backend kv4p` runs a handshake with the board and prints what
it finds. Two of its lines map directly to the flashing steps above:

- **"this board is running pre-KISS firmware — flash v17"** — the board answered, but in an *older*
  protocol that radio-server can't talk. This is a firmware problem, not a wiring problem: flash the
  firmware (step 1), then the board-config (step 2). Without this line you'd just see a silent
  no-answer and have no idea the firmware was the cause.
- **"band mismatch: board reports VHF, you configured UHF"** (or the reverse) — the board's flashed
  band doesn't match your `kv4p.module_type` setting. Almost always this means the board-config was
  wiped (you flashed firmware without re-flashing the board-config) or never written. **Reflash the
  board-config** for your PCB and band.

---

## A few surprising-but-normal things

None of these are faults — they're just worth knowing so they don't alarm you:

- **Starting the server reboots the board.** Opening the USB port resets the ESP32, so every time
  radio-server (or the check-up tool) connects, the board restarts. It's harmless — the connection
  re-establishes itself — but you'll see it happen.
- **After a connect, some flags get reset to safe defaults.** On a board that isn't already reporting
  its state, radio-server can't read the board's transmit-allowed / power / filter bits before it
  writes, so it sets them to safe values (transmit stays *off* until you enable it) and the check-up
  tool prints exactly what it reset. It preserves your tuned *frequency*; it can't preserve those flag
  bits. This is a firmware limitation, not a bug.
- **The board receives ~2 % fast, and radio-server corrects it.** The kv4p firmware clocks its receive
  audio about 2 % faster than the 48 kHz it reports (a deliberate anti-underrun trick in the firmware).
  Left alone that shifts every received tone slightly off-pitch — inaudible to you, but enough to stop
  the touch-tone (DTMF) login from decoding. radio-server undoes it with `kv4p.sample_rate_correction`
  (default **1.02**). You don't normally touch it; it's on by default.
- **Confirming DTMF, and trimming the correction if needed.** The exact fast-clock factor varies a
  little board to board, so it's worth checking once on the bench. Run
  `uv run python -m radio_server.doctor --backend kv4p --rx-level --seconds 30` while a signal is coming
  in: it prints the **measured true sample rate** and the correction that matches it — if that differs
  from what you have set, put the printed value in `kv4p.sample_rate_correction`. Then key `1234#` from
  a handheld and confirm the digits decode. This is the last bench item between a fresh board and a
  working node. (The received-audio decoder is tuned for normal handheld receive levels — you don't need
  a hot signal; a normal `1234#` off a nearby HT decodes.)
- **If DTMF still won't decode — capture the audio and let the tool read it.** Run
  `uv run python -m radio_server.doctor --backend kv4p --rx-capture --seconds 12 --out cap.wav` and key
  `1234#` a few times while it records. It saves the received audio to `cap.wav` and reads the touch-tone
  frequencies straight out of it, then tells you which of three things is wrong: the audio is **clipping**
  (the board's receive is too hot — the tones are there but distorted), the tones are **off-frequency**
  (nudge `kv4p.sample_rate_correction` as it suggests), or the tones are **clean** (they read fine from
  the capture — the decoder now handles normal receive levels, so a clean capture should decode; if it
  still won't, the issue is downstream, not the radio). You can re-read a saved capture any time with
  `--analyze-wav cap.wav`.

---

## Where to go next

- **[Setting it up with your radio](install.md)** — the install steps, including the kv4p branch.
- **[Changing the settings](configuration.md)** — the `[kv4p]` section and the squelch settings.
- **[Bringing up transmit](kv4p-tx-bringup.md)** — the bench session that proves the board keys and
  sends clean audio into a dummy load before you go on the air.
- **[Using your station](using-it.md)** — the control panel and calling in over the air.
