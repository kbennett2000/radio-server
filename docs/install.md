# Setting it up with your radio

This guide picks up where [Try it first](getting-started.md) leaves off: you've seen the control
panel with the practice radio, and now you want to connect a **real** radio.

Take your time. You can always go back to the practice radio if something isn't working — connecting
real equipment doesn't change any of that.

> **Which radios work today?** Right now, radio-server works with a **Baofeng UV-5R** handheld
> connected through an **AIOC cable** (described below). Support for the **Kenwood TM-V71A** is
> planned but not ready yet. The rest of this guide covers the Baofeng setup.

---

## The cable

The piece that connects your handheld to your computer is a small USB cable called an **AIOC**
("All-In-One-Cable"). It plugs into a USB port on your computer and into your UV-5R, and it carries
two things: the **audio** (so the computer can hear and speak through the radio) and the
**press-to-talk** signal (so the computer can key the transmitter for you).

There is no tuning control over this cable — you still set the frequency **by hand on the radio**, the
way you always have. The computer handles the audio and the keying; you handle the dial.

---

## A few extra pieces to install

Beyond the three tools from [Try it first](getting-started.md) (Python, uv, Node.js), the real radio
needs a few small helpers. How you install them depends on your computer:

| Piece | What it's for |
|---|---|
| **PortAudio** | Lets the program use your computer's sound in and out. |
| **multimon-ng** | Understands the touch-tones people key on their radio to log in and pick a service. |
| **A voice** | A "Piper" voice file, so the spoken services (like the time) have a voice to speak with. |

Install the program's radio parts and the voice support with:

```sh
uv sync --extra hardware      # the parts that talk to the cable and sound card
uv sync --extra tts           # the spoken-voice support
```

### Linux (Debian / Ubuntu) — tried and tested

```sh
sudo apt install libportaudio2 multimon-ng
sudo usermod -aG dialout $USER      # lets the program use the cable; log out and back in afterward
```

Your radio's cable shows up as `/dev/ttyACM0`. (A more stable name lives under
`/dev/serial/by-id/` if you'd like to use that instead — it won't change if you unplug and replug.)

### macOS — should work, not yet tested

```sh
brew install portaudio multimon-ng
```

Your cable usually shows up as something like `/dev/cu.usbmodem…` — you can list the options with
`ls /dev/cu.*`. There's no "dialout" step on a Mac.

> These macOS steps are expected to work but haven't been confirmed on real hardware yet. If you try
> it, we'd love to hear how it goes.

### Windows — should work, not yet tested

The sound support (PortAudio) comes bundled, so there's nothing extra to install for audio. The
touch-tone helper (multimon-ng) is the tricky part on Windows: there's no official Windows version.
The most reliable route is to run everything inside **WSL2** (a free, built-in way to run Linux on
Windows) and follow the Linux steps above. Your cable shows up as a **COM port** (like `COM3`) —
check Device Manager to see which one.

> These Windows notes are our best guidance but haven't been confirmed on real hardware yet.

---

## Turn on the radio in the settings

You can change settings from the browser (the **Settings** tab in the control panel) or by editing
the settings file — see [Changing the settings](configuration.md) for both. The three things to set
for a Baofeng are:

- **Radio type:** set it to `baofeng`.
- **Squelch:** set it to `audio` (this stops the computer from streaming static when no one is
  talking — more on that below).
- **The cable:** the serial port name for your computer (from the list above), and the audio device
  name (usually `All-In-One-Cable: USB`).

If you're editing the settings file, [radio.toml.example](../radio.toml.example) shows every option
with a plain description; copy it to `radio.toml` and change what you need.

---

## Check it before you transmit

radio-server has a built-in check-up tool that looks at the cable and sound card **without keying the
radio**, and tells you if anything's wrong:

```sh
python -m radio_server.doctor
```

Fix anything it flags, then it will walk you through confirming the audio levels and the
press-to-talk line. This part is genuinely fiddly — every radio and cable is a little different — so
there's a dedicated, step-by-step guide for it: **[Bench setup & troubleshooting](hardware-bringup.md)**.
It covers the classic "I've set everything up but I hear nothing" situation, which is almost always
just an audio-level knob.

---

## Set your callsign and login code

Before your station goes on the air, two more things:

- **Your callsign** — every transmission is legally your station, so radio-server won't transmit
  until you've set it. It then identifies your station automatically, so you stay legal without
  thinking about it.
- **A login code** — so only you (and people you trust) can use the over-the-air services. This uses
  the same free **Google Authenticator** app you may already use for websites. Setting it up prints a
  QR code you scan with your phone.

Both are covered in **[Using your station](using-it.md)**, which also explains how to actually call in
over the air once you're set up.

---

## Where to go next

- **[Running on a LAN server](lan-server-setup.md)** — the start-to-finish runbook for putting
  radio-server on the box with the radio, reachable from your phone over HTTPS.
- **[Using your station](using-it.md)** — the control panel, and calling in over the air.
- **[Bench setup & troubleshooting](hardware-bringup.md)** — the detailed hardware check-up and
  level-setting.
- **[Changing the settings](configuration.md)** — every setting, in plain language.
