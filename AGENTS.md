# AGENTS.md

Guidance for AI agents and developers working in this repository. For the human-facing
documentation, start at [README.md](README.md).

## Overview

radio-server controls a ham radio over one HTTP/WebSocket API on a LAN and exposes
DTMF-authenticated voice services (e.g. "announce the time"). Four backends ship behind one API:
**`baofeng`** (audio + serial-line PTT over an AIOC cable — plus server-driven tuning when the radio
on the far end is a UV-K5, `baofeng.uvk5_tuner`), **`kv4p`** (an ESP32+SA818 board), **`uvk5`** (a
Quansheng UV-K5/K6 on a dock firmware — nicsure's for a classic DP32G030 radio, our
[V3 fork](https://github.com/kbennett2000/uv-k1-k5v3-firmware-custom) for a PY32F071 V3) and
**`mock`**; a **TM-V71A** backend is still a stub.
Everything above the radio layer is backend-agnostic and is developed and tested against the **mock
radio**, so no hardware is needed to build or test.

Python + FastAPI. Packaged with [uv](https://docs.astral.sh/uv/).

## Setup

```sh
uv sync                     # core runtime + dev dependencies
uv sync --extra hardware    # AIOC/Baofeng backend: pyserial + sounddevice
uv sync --extra kv4p        # kv4p HT backend: pyserial + the Opus stack (NO sound card, NO system lib)
uv sync --extra tts         # Piper neural TTS: piper-tts, onnxruntime
uv sync --extra mumble      # Mumble/Murmur link: pymumble (git-pinned) + the Opus stack
uv sync --extra uvk5        # UV-K5 dock backend: pyserial + sounddevice
```

Extras taxonomy (ADR 0067): the backends compose small leaf extras so each node installs only what it
uses. Leaves — `serial` (pyserial), `soundcard` (sounddevice + system libportaudio2), `opus` (opuslib
+ the bundled-wheel libopus carrier). Composites — `hardware = serial + soundcard`, `kv4p = serial +
opus`, `mumble = opus + pymumble`, `uvk5 = serial + soundcard`. So a kv4p node needs no sound card and no system library at all;
`hardware` and `mumble` keep the exact package closures they had before the split.

These extras are only needed for real hardware, real speech, or the Mumble link; the test suite needs
none of them. Note `uv sync` is **exact** — it uninstalls any extra you don't name, so to keep several
installed, list them all in one command:
`uv sync --extra hardware --extra tts --extra mumble` (this is what `update-radio-server.sh` runs).

## Build & test

```sh
uv run pytest               # the whole suite runs against MockRadio — no hardware, no external tools
```

The web control panel (only needed to serve the UI, not to test):

```sh
cd web && npm install && npm run build   # -> web/dist/
```

There are no lint/format tooling gates configured; match the style of the surrounding code.

## Run

There are no console scripts — everything is a Python module:

```sh
python -m radio_server            # serve (uvicorn); --config PATH, --secrets PATH
python -m radio_server.doctor     # hardware diagnostic; --backend picks the radio, one mode flag picks
                                  # the check. RX: --rx-level --rx-noise --rssi --rx-capture
                                  #   --rx-firststart-loop --analyze-wav
                                  # TX: --tx-tone --key-test --dtmf   (both key — dummy load)
                                  # other: --link --ptt-line --vocoder-loopback --vocoder-latency
                                  #   --dstar-echo --dstar-browser-echo
                                  # `--help` is authoritative; this list drifts.
python -m radio_server.enroll     # mint a TOTP secret + enroll Google Authenticator
```

The API is closed by default: `RADIO_API_TOKEN` must be set (env var or `radio-secrets.toml`) or the
server refuses to bind.

## Project layout

Each package under `radio_server/` owns one concern; dependencies point downward and `api/` composes
everything. See [docs/architecture.md](docs/architecture.md) for the full map and rationale.

- `backends/` — the `Radio`/`CatRadio` protocol, `MockRadio`, and the hardware backends.
- `audio/` — canonical PCM format, resample, tone synth, DTMF decode.
- `auth/` — over-RF TOTP verify + session state machine.
- `services/` — DTMF command dispatch, the pluggable voice services, and station ID.
- `scan/`, `controller/`, `rx/`, `tx/`, `arbiter/`, `activity/`, `eventlog/`, `recording/` — the
  scan engine, live loop, audio streaming, duplex arbiter, and the passive sinks.
- `link/` — the Mumble/Murmur bridge (RF ↔ Mumble channel), multi-server manager, DTMF tone mute.
- `dstar/`, `vocoder/` — the D-STAR gateway link, crossband bridge and DVAP control; PCM ⇄ AMBE over
  a DV Dongle. **The reflector→RF crossband is disabled** — see [docs/dstar-setup.md](docs/dstar-setup.md).
- `presets.py`, `chirp.py` — server-side channel presets and the CHIRP CSV importer.
- `api/` — REST + 7 WebSockets over an injected `Radio`.
- `config/` — schema-driven TOML settings + the separate secrets channel.

## The sibling repository

The UV-K5 V3 dock firmware is **not in this repo**. It lives in
[`kbennett2000/uv-k1-k5v3-firmware-custom`](https://github.com/kbennett2000/uv-k1-k5v3-firmware-custom)
(a fork of F4HWN, Apache-2.0), and the two are coupled by **one contract: the dock wire protocol must
stay byte-compatible**. `radio_server/backends/uvk5/frames.py` and that repo's `App/app/dock.c` are
independent implementations of the same spec, and the firmware's `PROTOCOL.md` documents it.

Practical consequences when working here:

- A change to `frames.py` that alters bytes on the wire is a **cross-repo change**. Nothing
  automatically checks the two stay in step (ADR 0148, left open).
- Most of that arc's reasoning is in **this** repo's ADRs (0118–0120, 0126, 0140), not the firmware's.
- Capabilities are gated on firmware **level** (F2 dock, F3 RX audio, F5 PA, F6 set-VFO). The three
  pre-F6 failure modes all look like hardware faults from here; `doctor --backend uvk5` reports the
  level.

## Conventions

- **ADR-first.** Significant decisions get an ADR in [docs/adr/](docs/adr/) before implementation.
- **Small, reviewable, load-bearing units** — one cohesive change per cycle.
- **Mock-first.** No feature should require real hardware to be testable; bring hardware up last.

## Guardrails (do not violate)

1. **Verify hardware facts empirically** — never assert the Hamlib rig model, serial speed,
   `multimon-ng` flags, or the AIOC PTT line from memory. Keep them as config with a marked default
   and a "verify on hardware" note.
2. **PTT is never keyed over CAT.** It is keyed via the DATA-port audio (SignaLink) or the AIOC
   serial line (DTR by default). Keying over CAT transmits the radio's mic audio and ignores app
   audio. CAT is for tuning only.
3. **Capability split at the API.** A backend advertises what it implements and the API returns a
   clear "unsupported in this mode" (501, naming the capability) rather than silently no-op'ing.
   Read `capabilities()`; never infer the surface from the backend name — since ADR 0142 the
   `baofeng` backend gains the full tuning surface when `baofeng.uvk5_tuner` is set, and the
   `uvk5` backend does **not** advertise `set_power`.
4. **Auth is gated access, not confidentiality.** Everything on RF is in the clear. Enforce
   single-use TOTP (burn consumed codes) and short sessions; guard anything that keys TX harder than
   "announce the time."
5. **Part 97:** every transmission is the licensee's station. Automatic station ID (CW or voice) on a
   ≤10-minute interval and at session end is required controller behavior, not optional.
6. **Software/mock cycles first;** hardware bring-up is a separate empirical phase.

## How work runs here

This repo is driven by headless, one-cycle-at-a-time agent runs with a strict branch/commit/PR
contract (branch from a fresh `origin/master`, never stack cycles, always open a PR against
`master`). **[CLAUDE.md](CLAUDE.md) is the authority on that process** — read it before making
changes.

## Documentation map

- User-facing: [README.md](README.md) → [getting-started](docs/getting-started.md),
  [install](docs/install.md), [using-it](docs/using-it.md), [configuration](docs/configuration.md).
- Reference: [operating.md](docs/operating.md), [hardware-bringup.md](docs/hardware-bringup.md),
  [deployment.md](docs/deployment.md), [api.md](docs/api.md), [architecture.md](docs/architecture.md),
  [troubleshooting.md](docs/troubleshooting.md).
- Per-radio setup: [uvk5-setup.md](docs/uvk5-setup.md), [kv4p-setup.md](docs/kv4p-setup.md),
  [dstar-setup.md](docs/dstar-setup.md) — **read that last one's warning before enabling D-STAR**.
- Bench/ops: [server-notes.md](docs/server-notes.md) (dated log; only the top block is current),
  `scripts/bench/` (acceptance runners — several key the transmitter).
- Decisions: [docs/adr/](docs/adr/) — start with the [ADR index](docs/adr/README.md).
