# Install & configuration

How to install, configure, and run radio-server on **Windows, macOS, and Linux**. The software
stack (API, auth, services, mock radio) is OS-agnostic and runs the same everywhere; the
differences are all in the **hardware backend** — the serial port name, the audio device, and how
you install the out-of-band system tools.

> **Platform status.** The AIOC/Baofeng hardware backend is bench-verified on **Linux** only. The
> underlying libraries (`sounddevice`/PortAudio, `pyserial`) are cross-platform, so the hardware
> backend *should* work on macOS and Windows, but the device paths and system-tool install steps
> below for those platforms are **untested — verify on your own hardware** (guardrail 1). The
> **mock backend and the whole web/API stack are fully cross-platform.**

There is no `radio-server` console command — every entry point is a Python module:
`python -m radio_server` (serve), `python -m radio_server.doctor` (hardware diagnostic),
`python -m radio_server.enroll` (TOTP enrollment).

---

## 1. Common prerequisites (all platforms)

| Tool | Why | Notes |
|---|---|---|
| **Python ≥ 3.11** | Runtime | 3.11 or newer. |
| **[uv](https://docs.astral.sh/uv/)** | Packaging / venv / running | Installs deps and runs the app (`uv sync`, `uv run`). |
| **Node.js + npm** | Build the web UI | Only needed to build the browser control panel (`web/dist`). Skip if you only use the REST/WebSocket API. |

Clone and install the Python dependencies:

```sh
git clone <repo-url> radio-server
cd radio-server
uv sync                     # core runtime (mock backend, API, auth, services)
uv sync --extra tts         # add Piper neural TTS (piper-tts + onnxruntime) for voice services / voice ID
uv sync --extra hardware    # add the AIOC/Baofeng backend (pyserial, sounddevice) + the enroll QR (qrcode)
```

Build the web UI (optional; the server serves a "run the build" placeholder until you do):

```sh
cd web
npm install
npm run build               # -> web/dist/  (what FastAPI serves at /)
cd ..
```

---

## 2. Configure

Configuration is a single TOML file. Copy the fully-documented example and edit it:

```sh
cp radio.toml.example radio.toml
```

[`radio.toml.example`](../radio.toml.example) documents **every** setting with its default and a
description (it is generated from the schema, so it never drifts). The [README configuration
reference](../README.md#configuration) is a curated tour of the load-bearing keys.

**Settings come only from `radio.toml`** — there is no environment-variable override for regular
settings. The environment is consulted for exactly two values, both **secrets**:

| Secret | Env var | Effect |
|---|---|---|
| API token | `RADIO_API_TOKEN` | LAN API bearer token. The HTTP/WS API is closed by default; the server will not bind without it. |
| TOTP secret | `RADIO_TOTP_SECRET` | base32 shared secret for over-RF DTMF auth. Without it the app runs but `/controller` reports 503. |

Secrets are **never** in `radio.toml`. Put them in `radio-secrets.toml` (which the loader requires
to be mode `0600` — see the Windows note below) **or** in the environment. To mint a TOTP secret and
enroll Google Authenticator (QR in the terminal with the `hardware` extra):

```sh
python -m radio_server.enroll        # writes radio-secrets.toml (0600), prints a QR / otpauth URI
```

---

## 3. Run the mock (identical on all platforms)

The mock backend needs no hardware and no system tools — it exercises the entire stack:

```sh
RADIO_API_TOKEN=dev-lan-secret uv run python -m radio_server            # -> http://127.0.0.1:8000
# ...or point at a config file:
uv run python -m radio_server --config radio.toml
```

On Windows PowerShell, set the token separately (there is no inline `VAR=value cmd` syntax):

```powershell
$env:RADIO_API_TOKEN = "dev-lan-secret"
uv run python -m radio_server
```

Open the URL, enter the token, and the panel connects. To reach the server from other machines, set
`host = "0.0.0.0"` under `[server]` (see [deployment.md](deployment.md) for the server story).

---

## 4. Hardware backend (AIOC/Baofeng) — per-OS setup

Set `server.backend = "baofeng"` and `audio.squelch = "audio"`, then fill in the `[baofeng]`
section. Two things differ by OS: the **serial port name** (`baofeng.serial_port`) and how you
install the out-of-band tools. The audio device is matched by a **`sounddevice`/PortAudio name
substring** (default `"All-In-One-Cable: USB"`) or an integer index on every platform — never a raw
ALSA `hw:` string. `python -m radio_server.doctor` enumerates the devices and prints exactly what to
set.

Out-of-band tools (not pip-installable):

- **`multimon-ng`** — decodes DTMF from received audio (the server shells out to it). Only needed
  for over-the-air auth/services. Configurable path via `dtmf.multimon_bin`.
- **PortAudio** — the system audio library behind `sounddevice`.
- **A Piper voice** — a `.onnx` model plus its `.onnx.json` sidecar; path in `tts.voice`.

### Linux (Debian/Ubuntu) — *verified*

```sh
sudo apt install libportaudio2 multimon-ng
sudo usermod -aG dialout $USER      # serial access; log out/in, then: id -nG | grep dialout
```

- Serial: `/dev/ttyACM0`, or better the stable `/dev/serial/by-id/usb-...All-In-One-Cable...` path.
- Audio levels: `alsamixer` (F6 → the All-In-One-Cable card) to set capture (RX) / playback (TX).
- Follow [hardware-bringup.md](hardware-bringup.md) — the authoritative, bench-verified bring-up
  flow (`doctor --key-test` / `--rx-level` / `--tx-tone` / `--dtmf`).

### macOS — *untested; verify on your hardware*

```sh
brew install portaudio multimon-ng
```

- Serial: the AIOC typically enumerates as `/dev/cu.usbmodem*`. List with `ls /dev/cu.*`.
- No `dialout` group — macOS doesn't gate serial access that way.
- Audio: pick the AIOC by its Core Audio name substring (run `python -m radio_server.doctor` to see
  the exact PortAudio name/index).
- `tts`/`hardware` extras install via pip wheels as usual.
- The `doctor` bring-up flow (`--rx-level`, `--tx-tone`, etc.) is written against Linux device
  enumeration; treat its serial/audio specifics as unverified here.

### Windows — *untested; verify on your hardware*

- **PortAudio** ships inside the `sounddevice` wheel, so usually no separate install.
- **`multimon-ng` is the sticking point** — there is no official Windows build. The practical
  options are **WSL2** (run the whole server under Linux) or a third-party prebuilt binary pointed
  to via `dtmf.multimon_bin`. Without it, mock/API and audio work but over-the-air DTMF decode does
  not.
- Serial: the AIOC appears as `COMx` (check Device Manager) — set `baofeng.serial_port = "COM3"`
  (or whichever).
- Audio: match the AIOC by its MME/WASAPI device name substring or index from
  `python -m radio_server.doctor`.
- **Secrets file permissions:** the loader enforces `chmod 600` on `radio-secrets.toml` using POSIX
  permission bits, which don't map cleanly to Windows ACLs. If the 0600 check gives you trouble,
  supply the two secrets via **environment variables** (`RADIO_API_TOKEN`, `RADIO_TOTP_SECRET`)
  instead of the file.

---

## 5. What differs per OS — quick reference

| Setting / step | Linux | macOS | Windows |
|---|---|---|---|
| `baofeng.serial_port` | `/dev/ttyACM0` or `/dev/serial/by-id/...` | `/dev/cu.usbmodem*` | `COMx` |
| Serial permission | add user to `dialout` | none | none |
| PortAudio | `apt install libportaudio2` | `brew install portaudio` | bundled in `sounddevice` wheel |
| `multimon-ng` | `apt install multimon-ng` | `brew install multimon-ng` | WSL2 or prebuilt binary (`dtmf.multimon_bin`) |
| Audio levels tool | `alsamixer` | system Sound settings | system Sound settings |
| Secrets file 0600 | enforced (POSIX) | enforced (POSIX) | prefer env vars |

The `baofeng.input_device` / `output_device` name-substring matching and `dtmf.multimon_bin` are the
same mechanism on every platform — only the values differ.

---

## See also

- [hardware-bringup.md](hardware-bringup.md) — the empirical AIOC bench bring-up (Linux-verified).
- [deployment.md](deployment.md) — running headless on a Linux server (systemd, LAN, TLS).
- [../README.md#configuration](../README.md#configuration) — the settings reference.
- [operating.md](operating.md) — Part-97 operating behavior, the two auth planes, security reality.
