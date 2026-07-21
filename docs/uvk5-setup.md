# Setting up a UV-K5 (Quansheng Dock)

A **Quansheng UV-K5/UV-K6** running the open-source **[Quansheng Dock](https://github.com/nicsure/quansheng-dock-fw)**
firmware becomes a full-control radio for radio-server: over one **AIOC cable** the host drives
tuning, tone, mode and keying as direct chip-register writes, and the AIOC's built-in USB sound card
carries the audio. It's the same AIOC pattern the [Baofeng backend](configuration.md) uses — but with
CAT-style tuning the UV-5R can't do.

Two parts are genuinely fiddly and both are one-time: **flashing the Dock firmware** onto the radio,
and finding the AIOC's stable serial path. Take them slowly — the check-up tool tells you plainly if
either isn't right.

> **The single most useful habit:** run the check-up tool first, before anything else.
> ```sh
> uv run python -m radio_server.doctor --backend uvk5
> ```
> It opens the AIOC serial link, elicits a register read, and reports whether the radio is running
> Dock firmware — **without keying the transmitter**. If the radio still has stock firmware it says so
> in one line (see [What the check-up tool is telling you](#what-the-check-up-tool-is-telling-you)),
> which saves you a long silent dead-end.

---

## Flash the Quansheng Dock firmware

The stock Quansheng firmware answers a version handshake but **ignores every dock command** — so a
stock radio looks alive but can't be driven. You flash the Dock firmware once.

radio-server's UV-K5 backend was derived against a **pinned release** — do not flash a random build,
or the wire/register protocol may not match what the backend expects:

- **Firmware:** `nicsure/quansheng-dock-fw`, tag **`0.32.21q`**.
- **Client (reference only):** `nicsure/QuanshengDock`, tag **`0.32.21q`**.

Flash it with the **tool and steps from the pinned repo's own instructions** (the nicsure Quansheng
Dock project documents the flasher and the exact procedure for your radio). Follow that recipe at the
`0.32.21q` tag rather than a guessed one. When the flash is done, the check-up tool's connect probe
confirms the radio is on Dock firmware and reports the version.

> If the check-up tool later reports a **version other than `0.32.21q`**, the backend was derived
> against the pinned firmware and a different build may drift the register/wire protocol — reflash the
> pinned tag.

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
section (including `uvk5.squelch_threshold`, the RSSI busy gate that `audio.squelch = "cat"` reads).

---

## Bring-up checklist — the bench gates, in order

Work these **in order** on the bench. Each is a gate: don't move on until it passes. Every keying step
is you, at the bench, into a dummy load.

1. **Connect probe** — `uv run python -m radio_server.doctor --backend uvk5`. Confirms the AIOC serial
   path opens and the radio answers on **Dock firmware** (and reports the version). If it reports stock
   firmware, go back and flash (above). *No keying.*
2. **RX gate** — `uv run python -m radio_server.doctor --backend uvk5 --rx-level --seconds 30` while a
   signal is coming in (e.g. a live repeater). Confirms the AIOC sound card is capturing real audio and
   prints the measured true sample rate. *No keying.*
3. **Keying gate** — `uv run python -m radio_server.doctor --backend uvk5 --key-test` **into a dummy
   load**. It register-keys the radio and confirms via the chip read-back that TX actually engaged, then
   drops it. This proves the register keying path.
4. **THE acceptance gate** — the one thing that cannot be settled offline: **does the register-keyed
   transmitter actually carry the AIOC-injected K1 mic audio?** With a dummy load and a monitoring
   receiver, key up and play audio (e.g. an announcement) and confirm your voice/tone goes out — not
   silence, not the radio's own mic. Until this passes on *your* bench, the UV-K5 TX path is not trusted
   (ADR 0112/0113). This is also where you bench-tune `uvk5.tx_lead_seconds` (default 0.5 s, inherited
   from the AIOC/UV-5R bench — this radio earns its own number) and confirm the AIOC device name.

Once all four pass, run the server with `server.backend = uvk5`.

---

## ⚠️ Stuck-key warning — the full-control loop has no time-out

In full-control mode the host is the radio's brain, and **the radio stays keyed until the host tells it
to stop.** radio-server drops the key cleanly on shutdown and via `atexit`, but a **hard kill of the
host process while it is transmitting** (`SIGKILL`, a power loss, a yanked cable mid-over) bypasses that
cleanup and **leaves the radio keyed** — transmitting dead air until you power-cycle it. There is no
firmware time-out to save you (a controller-side watchdog/TOT is a future addition, tracked from ADR
0112). Until then: **key into a dummy load on the bench, and don't `kill -9` a transmitting server.**

---

## What the check-up tool is telling you

`uv run python -m radio_server.doctor --backend uvk5` runs the connect probe and prints what it finds.
Its firmware lines map directly to the flash step above:

- **"STOCK firmware — flash the Quansheng Dock firmware"** — the radio answered the version handshake
  but not the dock register read, so it is still on **stock firmware**. This is a firmware problem, not
  wiring: flash the Dock firmware (above). Without this line you'd just see a silent no-answer and have
  no idea firmware was the cause.
- **"no response to the register-read probe"** — nothing answered at all: the radio is off/asleep, the
  serial path is wrong, or the baud is wrong. Check the by-id path and that the AIOC is seated in the K1
  jack.
- **"dock version … != pinned 0.32.21q"** — the radio is on Dock firmware but a **different version**
  than the backend was derived against; reflash the pinned tag.

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
