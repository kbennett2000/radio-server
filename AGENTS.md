# AGENTS.md

Guidance for AI agents and developers working in this repository. For the human-facing
documentation, start at [README.md](README.md).

## Overview

radio-server controls a ham radio over one HTTP/WebSocket API on a LAN and exposes
DTMF-authenticated voice services (e.g. "announce the time"). It supports two radios behind one API:
a **TM-V71A** (full CAT control — not yet implemented) and a **Baofeng UV-5R** via an AIOC cable
(audio + serial-line PTT, no CAT). Everything above the radio layer is backend-agnostic and is
developed and tested against a **mock radio**, so no hardware is needed to build or test.

Python + FastAPI. Packaged with [uv](https://docs.astral.sh/uv/).

## Setup

```sh
uv sync                     # core runtime + dev dependencies
uv sync --extra hardware    # AIOC/Baofeng backend: pyserial, sounddevice, qrcode
uv sync --extra tts         # Piper neural TTS: piper-tts, onnxruntime
```

The hardware/TTS extras are only needed for real hardware or real speech; the test suite needs
neither.

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
python -m radio_server.doctor     # AIOC hardware diagnostic (--rx-level, --tx-tone, --dtmf, --key-test)
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
- `api/` — REST + 3 WebSockets over an injected `Radio`.
- `config/` — schema-driven TOML settings + the separate secrets channel.

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
3. **Capability split at the API.** In Baofeng mode the CAT methods do not exist; the API returns a
   clear "unsupported in this mode" (501) rather than silently no-op'ing.
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
  [deployment.md](docs/deployment.md), [api.md](docs/api.md), [architecture.md](docs/architecture.md).
- Decisions: [docs/adr/](docs/adr/).
