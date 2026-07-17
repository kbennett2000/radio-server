# Setting it up with your radio

This guide picks up where [Try it first](getting-started.md) leaves off: you've seen the control
panel with the practice radio, and now you want to connect a **real** radio.

Take your time. You can always go back to the practice radio if something isn't working — connecting
real equipment doesn't change any of that. And the payoff is worth it: once your radio is connected,
keying **10#** on your handheld links it to a voice channel on the internet, and you hear your own
voice come back through the free Mumble app on your phone — the demo server comes already set up, so it
works the first time you try it.

> **Which radios work today?** radio-server works with **any radio the
> [NA6D AIOC cable](https://na6d.com/products/aioc-ham-radio-all-in-one-cable) supports** — a small
> USB cable that carries the audio and the push-to-talk signal (described below). The **Baofeng
> UV-5R** is the tested reference, and it's what the rest of this guide uses in its examples.
> Support for the **Kenwood TM-V71A/E and TM-D710 family** (they share the same control system) is
> planned but not ready yet, and so is the **[KV4P HT](https://www.kv4p.com/)** — an open-source
> gadget that turns a phone into a radio.

---

## The cable

The piece that connects your handheld to your computer is a small USB cable called an **AIOC**
("All-In-One-Cable"). It plugs into a USB port on your computer and into your radio, and it carries
two things: the **audio** (so the computer can hear and speak through the radio) and the
**press-to-talk** signal (so the computer can key the transmitter for you).

There is no tuning control over this cable — you still set the frequency **by hand on the radio**, the
way you always have. The computer handles the audio and the keying; you handle the dial.

**You'll want two radios.** The one wired to the AIOC becomes your *gateway* — it sits by the
computer doing the linking, and it's busy doing that. To actually talk over the air you key a
**second** handheld, the one in your hand. (No spare radio yet? You can still do everything from the
browser — it joins the Mumble channel directly, no radio needed at all. The gateway radio is only for
bringing people who are on the air into the conversation.)

**A note on the USB cable.** When your radio transmits, a little of that RF energy can sneak back up
the USB cable and upset the computer — crackle in the audio, or the computer briefly "losing" the
AIOC. It often works fine on a plain cable, so try what you have first. If you *do* see trouble when
you key up, the fix is almost always a better cable: search a site like Amazon for a **"shielded USB
cable with a ferrite core"** (a ferrite is the little cylindrical bead molded onto the cable) in the
connector type your computer and AIOC use. It's a couple of dollars and heads the problem off.

---

## A few extra pieces to install

Beyond the two tools from [Try it first](getting-started.md) (uv and Node.js), the real radio
needs a few small helpers. How you install them depends on your computer:

| Piece | What it's for |
|---|---|
| **PortAudio** | Lets the program use your computer's sound in and out. |
| **A voice** | A "Piper" voice file, so the spoken services (like the time) have a voice to speak with. |

Install the program's radio parts, the voice support, and (if you'll use it) the Mumble link, all
in one command:

```sh
uv sync --extra hardware --extra tts --extra mumble
```

> Name every extra you use in a **single** `uv sync` — sync installs exactly what's listed, so
> running it again with a different `--extra` removes the previous one. Leave off `--extra mumble`
> if you won't link to Mumble servers.

> **Used the one-line installer?** Re-run it with `--with-hardware` and it does this `uv sync` for
> you. Pass the flag through the pipe with `curl -LsSf …/scripts/install.sh | sh -s -- --with-hardware`,
> or from a checkout run `./scripts/install.sh --with-hardware`. You'll still need the PortAudio system
> library below, which lives outside the installer.

### Linux (Debian / Ubuntu) — tried and tested

```sh
sudo apt install libportaudio2
sudo usermod -aG dialout $USER      # lets the program use the cable; log out and back in afterward
```

Your radio's cable shows up as `/dev/ttyACM0`. (A more stable name lives under
`/dev/serial/by-id/` if you'd like to use that instead — it won't change if you unplug and replug.)

### macOS — should work, not yet tested

This uses **Homebrew**, a free package manager for the Mac. If you don't have it, install it first
from [brew.sh](https://brew.sh) (it will also set up Apple's Xcode command-line tools, which it needs).

```sh
brew install portaudio
```

Your cable usually shows up as something like `/dev/cu.usbmodem…` — you can list the options with
`ls /dev/cu.*`. There's no "dialout" step on a Mac.

> These macOS steps are expected to work but haven't been confirmed on real hardware yet. If you try
> it, we'd love to hear how it goes.

### Windows — should work, not yet tested

The sound support (PortAudio) comes bundled, so there's nothing extra to install for audio — and the
touch-tone login decodes in-process now (no external helper), so DTMF works on Windows without WSL2.
Your cable shows up as a **COM port** (like `COM3`) — check Device Manager to see which one.

> These Windows notes are our best guidance but haven't been confirmed on real hardware yet.

---

## Getting a voice

The spoken services (the time, the station ID) need a **Piper** voice file to speak with — it's the
one piece the program can't fetch for you, and there's no default, so grab one before you go on the
air.

Piper voices live at <https://huggingface.co/rhasspy/piper-voices> (MIT-licensed). You can hear what
they sound like at <https://rhasspy.github.io/piper-samples/>, and the full list is at
<https://github.com/rhasspy/piper/blob/master/VOICES.md>.

A good default is **`en_US-amy-medium`**. It comes as **two** files — download **both** into the same
folder:

- <https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/amy/medium/en_US-amy-medium.onnx>
- <https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/amy/medium/en_US-amy-medium.onnx.json>

Then point the **voice** setting (`[tts] voice` in `radio.toml`, or the **Voice file** field on the
Settings tab) at the `.onnx` file. The `.onnx.json` sidecar must sit **right beside** it — the server
reads the voice's sample rate from that sidecar and fails loudly at startup if it's missing.

Pick **medium**, not high: the audio is going out over ~3 kHz FM, so a `high` voice is wasted CPU for
no audible gain on the air.

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
uv run python -m radio_server.doctor
```

Fix anything it flags, then it will walk you through confirming the audio levels and the
press-to-talk line. This part is genuinely fiddly — every radio and cable is a little different — so
there's a dedicated, step-by-step guide for it: **[Troubleshooting — "I hear nothing"](troubleshooting.md)**.
It covers the classic "I've set everything up but I hear nothing" situation, which is almost always
just an audio-level knob.

---

## Set your callsign and login code

Before your station goes on the air, two things need setting up: your **callsign** (so every
transmission is legally identified — radio-server won't transmit without it) and a **login code** (so
only you, and people you trust, can use the over-the-air services). Here's the whole thing, start to
finish. It takes a few minutes and you only do it once.

### 1. Install an authenticator app on your phone

The login code is a **6-digit number that changes every 30 seconds** — the same kind banks and
websites use. Your phone generates it with a free *authenticator* app. If you don't already have one,
install **Google Authenticator** (or Authy, or 1Password — any "TOTP" app works) from your phone's
app store. That's the only new app you need.

### 2. Set your callsign

Open the control panel in your browser, go to the **Settings** tab, and enter your callsign (or set
`station.callsign` in `radio.toml` — see [Changing the settings](configuration.md)). radio-server
then IDs your station automatically, so you stay legal without thinking about it.

### 3. Create your login code

**The easy way — in the browser:** on the **Settings** tab, find the **Secrets** section and the
**Over-the-air login code** row. Click **Set up login code**. A **QR code appears on the screen** —
open your authenticator app, tap to add an account, and scan it. (Can't scan? The app can take the
short code shown beside it instead.) That's it — your phone now shows a rolling 6-digit code for your
station.

> It's shown **once**, on purpose — a login code is a secret. If you miss it, just click the button
> again for a fresh one.

**The other way — from the command line** (handy for a headless server with no browser handy):

```sh
uv run python -m radio_server.enroll
```

It prints a QR code right in the terminal to scan — same result.

### 4. Restart the server

The login only switches on after a restart. Stop the server (**Ctrl+C** in the window it's running
in) and start it again with `uv run python -m radio_server` — or use the **Restart** button on the
Settings screen if you've set that up. Now the over-the-air login is live.

### 5. Log in from the radio

On your handheld, key your current **6-digit code** followed by **`#`**. The station answers with its
ID and you're logged in — then key `02#` to hear the time, or `10#` to link to the world.
[Using your station](using-it.md#logging-in) has the full list and the calling-in details.

---

## Where to go next

- **[Using your station](using-it.md)** — the control panel, and calling in over the air.
- **[Troubleshooting — "I hear nothing"](troubleshooting.md)** — set your audio levels and fix the
  most common problem.
- **[Changing the settings](configuration.md)** — every setting, in plain language.
- **[Running your own Mumble server](mumble-server/README.md)** — when you're ready to host your own
  voice channel instead of (or alongside) the demo server.
