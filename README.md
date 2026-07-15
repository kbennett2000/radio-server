![radio-server — talk to your radio from anywhere at home](docs/banner.png)

# radio-server

**Put your ham radio on your home network.** radio-server connects a small computer to your radio so
you can listen and talk from a web page in your browser — and let people call in over the air to hear
spoken information back, like the current time or the weather.

It's built to be friendly to set up, and it takes care of the legal basics (like identifying your
station with your callsign) for you.

---

## What you can do with it

- **Listen and talk from your browser.** Open a page on your home network to hear what the radio hears
  and transmit by speaking into your computer's microphone.
- **Let callers get spoken information over the air.** Someone with a handheld keys a short code to log
  in, then a single button to hear the **time**, **weather**, **sun & moon times**, a **quote**, a
  **Bible verse**, and more — read aloud back to them.
- **Stay legal without thinking about it.** Your station is identified automatically, on schedule, in
  Morse or a spoken voice.
- **Keep an operating log** of what your station has done, and optionally record audio.

Everything works the same whether you're using the built-in practice radio or a real one, so you can
try it all before connecting any equipment.

## What you'll need

- A computer (Windows, macOS, or Linux) on your home network.
- A **Baofeng UV-5R** handheld and an **AIOC cable** to connect it. (Support for the Kenwood TM-V71A
  is planned.) You can also explore the whole thing with **no radio at all**, using the practice mode.
- An amateur radio license to transmit — this is a tool for licensed operators.

---

## Start here

👉 **[Try it first — no radio needed](docs/getting-started.md).** In about 15 minutes you'll have the
control panel open on your computer and can click around safely. It's the best way to see what
radio-server does before connecting anything.

When you're ready for the real thing, [Setting it up with your radio](docs/install.md) takes it from
there.

---

## Guides

**Getting started**
- [Try it first — no radio needed](docs/getting-started.md) — see it working in 15 minutes.
- [Setting it up with your radio](docs/install.md) — connect a real Baofeng, step by step.

**Everyday use**
- [Using your station](docs/using-it.md) — the control panel, and calling in over the air.
- [Changing the settings](docs/configuration.md) — adjust anything, mostly from the browser.
- [Bench setup & troubleshooting](docs/hardware-bringup.md) — set audio levels and fix "I hear
  nothing."

**Under the hood** (for the technically inclined — you don't need these to use radio-server)
- [Operating guide](docs/operating.md) — how login, station ID, logging, and security work in detail.
- [Running it as an always-on server](docs/deployment.md) — leave it running unattended on a Linux box.
- [The browser control panel](web/README.md) — building and developing the web page.
- [How it's built](docs/architecture.md) and [the API reference](docs/api.md) — for developers.

---

## A note on privacy

Everything sent over amateur radio is in the open — that's normal, and radio-server doesn't change it.
The login code isn't there to keep things secret; it's there so only you can use your station's
services. See [Using your station](docs/using-it.md#a-note-on-privacy-nothing-over-the-air-is-secret)
for the plain-English version.

## Building on it

radio-server is a Python project. If you'd like to develop it or add a service, see
[AGENTS.md](AGENTS.md) and [How it's built](docs/architecture.md). The whole test suite runs against
the practice radio, so you need no hardware to work on it:

```sh
uv run pytest
```
