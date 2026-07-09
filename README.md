# radio-server

Control a ham radio over one HTTP/WebSocket API on your LAN, and expose DTMF-authenticated
voice services (e.g. "announce the time") over the air. One API, two radios, two operating
modes:

- **TM-V71A mode — full control.** Audio + PTT over a SignaLink USB on the radio's DATA jack;
  frequency/channel/tone/mode over CAT (Hamlib `rigctld`) on the PC/COM jack.
- **Baofeng mode — TX/RX only.** Audio + PTT over an NA6D AIOC cable on a UV-5R. No CAT —
  frequency is set by hand on the radio.

Everything above the radio layer — DTMF decode, TOTP auth, sessions, voice services, TTS,
station ID — is backend-agnostic: it calls only `receive()` and `transmit()`, so every service
works identically in both modes. See [docs/architecture.md](docs/architecture.md) for the full
tower.

## Status — read this first

**The full software stack is built and browser-verified against the mock radio backend.**
There is no working hardware backend yet.

| Component | State |
| --- | --- |
| REST + WebSocket API, auth, sessions, services, scan, RX/TX audio streaming, station ID, event log, recording, web UI | Built; unit-tested; browser-verified against `MockRadio` |
| `SignaLinkV71` (TM-V71A hardware backend) | **`NotImplementedError` stub** — raises on construction, pending bench bring-up |
| `AiocBaofeng` (UV-5R hardware backend) | **`NotImplementedError` stub** — raises on construction, pending bench bring-up |

Nothing here has been proven against a real radio. Hardware-specific facts (the exact Hamlib
rig model, `rigctl` serial speed, `multimon-ng` flags, the AIOC's PTT line) are deliberately
left as marked, verify-on-hardware config defaults — not asserted as confirmed. See
[docs/hardware-bringup.md](docs/hardware-bringup.md) and
[docs/deployment.md](docs/deployment.md) (both pending).

## ⚠️ Two separate auth planes

Do not conflate these — different threats, different mechanisms, different secrets:

- **Over-RF DTMF/TOTP** (`RADIO_TOTP_SECRET`) gates *keying the transmitter* from over the air.
  Single-use TOTP with a short window (see [docs/operating.md](docs/operating.md)).
- **LAN API token** (`RADIO_API_TOKEN`) gates *the HTTP/WebSocket surface* on your network. A
  plain static bearer secret, constant-time compared.

Neither is "secure" in the encryption sense — everything on RF is in the clear. Auth is gated
access, not confidentiality. See [docs/operating.md](docs/operating.md#security-reality).

## Quickstart (against the mock)

Requires Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/).

```sh
# run the test suite (all against MockRadio — no hardware needed)
uv run pytest

# run the server against the mock backend (the web UI is served too, once built — see web/README.md)
RADIO_API_TOKEN=dev-lan-secret uv run python -m radio_server
# -> http://127.0.0.1:8000
```

`RADIO_API_TOKEN` is the only variable strictly required to bind the server against the mock.
`RADIO_CALLSIGN`, `RADIO_TOTP_SECRET`, and a TTS voice become required the moment the live
controller loop is wired (the loaders fail loud rather than transmit unidentified). To reach the
server from other machines, set `RADIO_HOST=0.0.0.0`.

For the full REST/WebSocket contract see [docs/api.md](docs/api.md).

## Configuration

All configuration is via environment variables (there is no config file). Every variable is
`RADIO_`-prefixed and owned by the module that reads it. **Four are fail-loud with no default —
the server refuses to start (or to transmit) without them.** The rest have marked defaults but
still fail loud on a *malformed* value (a set-but-unparseable number raises rather than silently
falling back).

Hardware-tuning defaults (squelch/VAD levels, TX idle timeout) are marked "verify on hardware" —
they are bench-tuned starting points, not confirmed values.

### Required (fail loud if unset)

| Variable | Effect |
| --- | --- |
| `RADIO_API_TOKEN` | LAN API bearer token. The HTTP/WS API is closed by default; the server will not bind without it. |
| `RADIO_CALLSIGN` | FCC callsign. A station may not legally transmit without one; loaded fail-loud where the controller/services are wired. |
| `RADIO_TOTP_SECRET` | base32 TOTP shared secret for over-RF auth. Required to wire the live controller loop; without it the app runs but `/controller` reports 503. |
| `RADIO_TTS_VOICE` | Path to a Piper voice `.onnx`. Required for voice services / voice ID; fails loud if unset or the file is missing. |

### Server / backend

| Variable | Default | Effect |
| --- | --- | --- |
| `RADIO_HOST` | `127.0.0.1` | Bind address. Set `0.0.0.0` to serve the LAN. |
| `RADIO_PORT` | `8000` | Bind port. |
| `RADIO_BACKEND` | `mock` | Backend: `mock`, `v71`, or `baofeng`. **`v71`/`baofeng` raise `NotImplementedError` today.** |
| `RADIO_MOCK_CAT` | `on` | Mock only: `off`/`0`/`false`/`no`/`n` → an audio-only mock (CAT controls grey out), to demo the Baofeng-mode capability split without hardware. |
| `RADIO_WEB_DIR` | `<repo>/web/dist` | Built web-UI directory served at `/`. Unbuilt → a "run the build" placeholder, not a crash. |

### Station ID (Part 97)

| Variable | Default | Effect |
| --- | --- | --- |
| `RADIO_ID_INTERVAL` | `600.0` | Seconds between IDs. **Rejected if > 600** (the Part-97 10-minute ceiling); also fails loud if ≤ 0 or non-numeric. |
| `RADIO_ID_MODE` | `cw` | `cw` or `voice`. `voice` requires a configured Piper voice; no silent fallback to CW. |
| `RADIO_CW_WPM` | `20.0` | CW ID speed (words per minute). |
| `RADIO_CW_TONE_HZ` | `600.0` | CW sidetone frequency (Hz). |

### Audio / squelch (RX gate)

| Variable | Default | Effect |
| --- | --- | --- |
| `RADIO_SQUELCH` | `off` | RX activity gate: `off` (relay everything), `audio` (software VAD), `cat` (hardware busy line). |
| `RADIO_VAD_ON_RMS` | `500.0` | VAD open threshold (int16 RMS). Verify on hardware. |
| `RADIO_VAD_OFF_RMS` | `300.0` | VAD close threshold (hysteresis; below the on-threshold). Verify on hardware. |
| `RADIO_VAD_HANG` | `0.5` | Seconds to hold the gate open after level drops. Verify on hardware. |
| `RADIO_TX_IDLE_TIMEOUT` | `2.0` | Seconds of silence on a `/audio/tx` stream before PTT drops. Verify on hardware. |

### Recording

| Variable | Default | Effect |
| --- | --- | --- |
| `RADIO_RECORD` | off | Enable RX recording (`on/off/true/false/1/0/yes/no`). |
| `RADIO_RECORD_TX` | off | Enable TX recording (independent of `RADIO_RECORD`; `tx-` filename prefix). |
| `RADIO_RECORD_PATH` | `recordings` | Output directory for WAV segments. |
| `RADIO_RECORD_MODE` | `gated` | `gated` (one file per received transmission). `full` is recognized but unimplemented (raises). |
| `RADIO_RECORD_MAX_SECONDS` | `3600.0` | Per-segment duration cap. Always on; no disable sentinel. |

With `RADIO_RECORD` on **and** `RADIO_SQUELCH=off`, there is no gate-close edge, so RX is
segmented purely by the time cap — the server logs a one-time warning at startup. See
[docs/operating.md](docs/operating.md#recording).

### Controller loop & scan

| Variable | Default | Effect |
| --- | --- | --- |
| `RADIO_CONTROLLER_POLL` | `0.5` | Controller loop poll cadence (s). |
| `RADIO_SESSION_TIMEOUT` | `300.0` | Session inactivity timeout (s). |
| `RADIO_SCAN_SETTLE` | `0.05` | Scan settle time after retune (s). |
| `RADIO_SCAN_POLL` | `0.5` | Scan poll cadence (s). |
| `RADIO_SCAN_DWELL` | `5.0` | Scan dwell time on an active channel (s). |
| `RADIO_SCAN_MODE` | `carrier` | Scan resume mode: `carrier`, `timed`, or `hold`. |

### DTMF, TTS, logging, time

| Variable | Default | Effect |
| --- | --- | --- |
| `RADIO_MULTIMON_BIN` | `multimon-ng` | Path/name of the `multimon-ng` binary for DTMF decode. |
| `RADIO_DTMF_TIMEOUT` | `3.0` | DTMF inter-digit timeout (s). |
| `RADIO_TZ` | `UTC` | Station timezone (IANA name) for the time service. |
| `RADIO_LOG_PATH` | `radio-server.jsonl` | Append-only JSONL event ledger. Opened fail-loud if unwritable. |

## Documentation

- [docs/api.md](docs/api.md) — REST + WebSocket reference (endpoints, event taxonomy, close codes).
- [docs/architecture.md](docs/architecture.md) — the `Radio` protocol, layer map, duplex arbiter, mock-first design.
- [docs/operating.md](docs/operating.md) — the ham / Part-97 doc: auth planes, station ID, the operating log, security reality.
- [web/README.md](web/README.md) — the browser control panel (build, serve, browser requirements).
- Architecture decision records live in [docs/adr/](docs/adr/) — the "why" behind each cycle. The docs above link to them for rationale rather than restating it.

Pending (hardware phase, not yet written):
[docs/hardware-bringup.md](docs/hardware-bringup.md),
[docs/deployment.md](docs/deployment.md).

## Development

```sh
uv run pytest           # the whole suite runs against MockRadio — no hardware, no external tools
```

Work proceeds one small, ADR-first cycle at a time (see [CLAUDE.md](CLAUDE.md)). External tools
used by the production path — Hamlib `rigctld`, `multimon-ng`, Piper TTS — are out-of-band
dependencies brought in during hardware bring-up; the mock-backed test suite needs none of them.
