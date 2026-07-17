# 0053 — One-command bootstrap installer

Status: Accepted

## Context

Getting a fresh machine to the control panel is five manual steps (getting-started.md): install
Python, install uv, install Node, `uv sync`, `cd web && npm install && npm run build`, then export a
token and run. Each is small, but the audience is deliberately non-technical (ham operators, not
software people), and the cliff is real — the project now wants "run one line, get a control panel
pointed at the demo server" as the front door, with the manual walk-through kept as the honest
fallback.

Constraints found in the tree:

- **uv provisions Python itself**, so the only hard external besides uv is Node.js (for the web
  build). There is no release/prebuilt-bundle infrastructure — `web/dist/` is gitignored — so
  shipping a built bundle would need CI this repo doesn't have. Node stays a build-time requirement.
- **First-run config primitives already exist**: `rotate(path, "api_token")`
  (`radio_server/config/secrets.py`) mints a URL-safe token into the `0600` secrets file the server
  reads with no env var; `python -m radio_server.enroll` mints the TOTP secret and renders a terminal
  QR when the `hardware` extra's `qrcode` is present. `doctor` is hardware diagnostics only — not a
  config wizard, so the installer only *points at* it for the real-radio path.
- **The practice radio is the default backend** (`server.backend = "mock"`), so the quickest win —
  and the getting-started path — needs none of the `hardware`/`tts`/`mumble` extras or their system
  libraries.
- **Windows can't do real-radio RX** honestly: `multimon-ng` (the DTMF decoder) has no official
  Windows build, so install.md already routes hardware use through WSL2. Practice mode and the
  browser Mumble client work natively.

## Decision

Two scripts under `scripts/` (the `gen-selfsigned-cert.sh` precedent), each curl-able and each safe
to run inside a checkout or piped from outside one:

- **`scripts/install.sh`** (macOS/Linux) and **`scripts/install.ps1`** (Windows), same shape:
  1. Locate the repo (run in place when a `radio-server` checkout is detected; otherwise `git clone`,
     with a curl-tarball fallback when `git` is absent).
  2. Install **uv** via its official installer if missing; source uv's env so PATH is fresh (never
     assume `~/.local/bin`). uv brings its own Python.
  3. Check for **Node/npm**; if absent, print the nodejs.org LTS pointer and stop with "install Node,
     then run this again — it picks up where it left off." **Never auto-install Node** (too many
     silent failure modes across package managers).
  4. `uv sync` (practice-mode core by default; a `--with-hardware` flag adds
     `--extra hardware --extra tts --extra mumble` for operators wiring a real radio now).
  5. Build the web UI (`npm install && npm run build`), skipped when `web/dist/index.html` exists
     unless `--force-web`.
  6. First-run config, each part **skipped when its file already exists** (a re-run on a configured
     station is a no-op): copy `radio.toml.example` → `radio.toml`; prompt for a callsign (Enter to
     skip — "needed before transmitting, not for looking around") read from `/dev/tty` with a
     non-interactive fallback; mint the API token via `rotate`; offer `radio_server.enroll` for TOTP
     ("only for logging in over the air — you can do this later").
  7. Print exactly how to start (`uv run python -m radio_server`) and the `http://127.0.0.1:8000`
     URL.

- **Idempotency is the retry story.** Every step guards on an "already done?" check, so a failed run
  is recovered by fixing what it printed and running the same line again — no partial-state cleanup.
  The scripts **never overwrite an existing `radio.toml` or `radio-secrets.toml`**; a configured
  station's callsign, token, and TOTP secret are untouchable by a re-run.

- **Windows posture is honest.** `install.ps1` states up front that native Windows runs practice mode
  and the browser Mumble client, but a **real radio needs WSL2** (multimon-ng has no Windows build);
  it therefore never passes `--with-hardware` and points at install.md/WSL2 for hardware.

- **Non-goals**, recorded so scope stays small: not a package manager (no PyInstaller/Homebrew/winget
  — a later ADR if ever), no Node auto-install, no system-service setup (deployment.md keeps
  systemd/HTTPS). The `rotate(...)` call couples the script to an internal API; if this grows, a
  `python -m radio_server.init` CLI is the cleaner seam.

## Consequences

- The README's "Start here" leads with one line (`curl … | sh` / `irm … | iex`); getting-started.md
  keeps the full manual walk-through, now doubling as the installer's failure-mode documentation.
- Curl-from-`main` means the script must stay backward-compatible with already-published docs —
  flags are additive, prompts read from `/dev/tty` (a piped stdin isn't a terminal), and a
  non-interactive run skips prompts and says what it skipped.
- No test harness for shell in this repo; the scripts are verified by `shellcheck`/`bash -n` and an
  end-to-end run in a scratch dir (fresh-clone and in-checkout paths, plus an idempotent re-run).
