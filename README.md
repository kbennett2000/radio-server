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
The **AIOC/Baofeng** hardware backend is now implemented (ADR 0029); the TM-V71A backend is still a
stub.

| Component | State |
| --- | --- |
| REST + WebSocket API, auth, sessions, services, scan, RX/TX audio streaming, station ID, event log, recording, web UI | Built; unit-tested; browser-verified against `MockRadio` |
| `AiocBaofeng` (UV-5R hardware backend) | **Working on hardware** (ADR 0029) — serial-line PTT (DTR) + USB-audio, no CAT. Full talk-through bench-confirmed: browser Listen gates on real RX audio, TX tone heard on a second radio, Talk (computer mic → radio) works. Tune levels with `python -m radio_server.doctor --rx-level` / `--tx-tone`. Needs the `hardware` extra + `libportaudio2`. See [docs/hardware-bringup.md](docs/hardware-bringup.md). |
| `SignaLinkV71` (TM-V71A hardware backend) | **`NotImplementedError` stub** — raises on construction, pending bench bring-up |

Remaining verify-on-hardware facts belong to the **V71** backend (the exact Hamlib rig model,
`rigctl` serial speed, `multimon-ng` flags) — left as marked config defaults, not asserted as
confirmed. The AIOC's PTT line was confirmed **DTR** on the bench (`python -m radio_server.doctor
--key-test`). See [docs/hardware-bringup.md](docs/hardware-bringup.md) and
[docs/deployment.md](docs/deployment.md).

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

# ...or point it at a config file (see Configuration below)
uv run python -m radio_server --config radio.toml
```

`RADIO_API_TOKEN` is a **secret** (see [Secrets](#secrets)) and is the only thing strictly required
to bind the server against the mock. `station.callsign`, the TOTP secret, and a TTS voice become
required the moment the live controller loop is wired (they fail loud rather than transmit
unidentified). To reach the server from other machines, set `host = "0.0.0.0"` under `[server]` in
`radio.toml`.

For the full REST/WebSocket contract see [docs/api.md](docs/api.md).

## Configuration

Configuration is a **TOML file** — `radio.toml` — resolved against a schema (ADR 0025). Point the
server at it with `--config PATH` (default `./radio.toml`); a missing file falls back to the
built-in defaults, so the mock runs with no config at all. [`radio.toml.example`](radio.toml.example)
documents every setting with its default and a description — copy it to `radio.toml` and edit.

Every setting has a marked default except the two required identity settings (`station.callsign`,
`tts.voice`), which have none and fail loud when actually used. A **malformed** value fails loud at
load, naming the bad key (a set-but-unparseable number raises rather than silently falling back).
Changes take effect on **restart** — the server composes its config once at startup (live
hot-reload is a deferred enhancement).

Hardware-tuning defaults (squelch/VAD levels, TX idle timeout) are marked "verify on hardware" —
they are bench-tuned starting points, not confirmed values.

### Secrets

The two secrets are **never** in `radio.toml` (a secret must never be rendered or round-tripped
through the settings surface). They load from a separate `radio-secrets.toml` written `chmod 600`
(the server refuses a group/world-readable secrets file) **or** from the environment:

| Secret | Env var | Effect |
| --- | --- | --- |
| API token | `RADIO_API_TOKEN` | LAN API bearer token. The HTTP/WS API is closed by default; the server will not bind without it. |
| TOTP secret | `RADIO_TOTP_SECRET` | base32 shared secret for over-RF auth. Required to wire the live controller loop; without it the app runs but `/controller` reports 503. |

```toml
# radio-secrets.toml  (chmod 600 — keep out of radio.toml and version control)
api_token = "a-long-random-lan-token"
totp_secret = "JBSWY3DPEHPK3PXP"
```

Point at a non-default secrets file with `--secrets PATH`.

### Settings (`radio.toml`)

`[station]` — identity (Part 97)

| Key | Default | Effect |
| --- | --- | --- |
| `callsign` | *(required)* | FCC callsign. A station may not legally transmit without one; fails loud where the controller/services are wired. |
| `id_interval` | `600.0` | Seconds between IDs. **Rejected if > 600** (the Part-97 10-minute ceiling); also fails loud if ≤ 0 or non-numeric. |
| `id_mode` | `"cw"` | `cw` or `voice`. `voice` requires a configured `tts.voice`; no silent fallback to CW. |
| `cw_wpm` | `20.0` | CW ID speed (words per minute). |
| `cw_tone_hz` | `600.0` | CW sidetone frequency (Hz). |

`[audio]` — RX activity gate

| Key | Default | Effect |
| --- | --- | --- |
| `squelch` | `"off"` | `off` (relay everything), `audio` (software VAD), `cat` (hardware busy line). |
| `vad_on_rms` | `500.0` | VAD open threshold (int16 RMS). Verify on hardware. |
| `vad_off_rms` | `300.0` | VAD close threshold (hysteresis; below the on-threshold). Verify on hardware. |
| `vad_hang` | `0.5` | Seconds to hold the gate open after level drops. Verify on hardware. |

`[dtmf]`

| Key | Default | Effect |
| --- | --- | --- |
| `multimon_bin` | `"multimon-ng"` | Path/name of the `multimon-ng` binary for DTMF decode. |
| `timeout` | `3.0` | DTMF inter-digit timeout (s). |

`[recording]`

| Key | Default | Effect |
| --- | --- | --- |
| `enabled` | `false` | Enable RX recording (`true`/`false`, or on/off/1/0/yes/no strings). |
| `tx` | `false` | Enable TX recording (independent of `enabled`; `tx-` filename prefix). |
| `path` | `"recordings"` | Output directory for WAV segments. Opened fail-loud if unwritable. |
| `mode` | `"gated"` | `gated` (one file per received transmission). `full` is recognized but unimplemented (raises). |
| `max_seconds` | `3600.0` | Per-segment duration cap. Always on; no disable sentinel. |

With `recording.enabled` on **and** `audio.squelch = "off"`, there is no gate-close edge, so RX is
segmented purely by the time cap — the server logs a one-time warning at startup. See
[docs/operating.md](docs/operating.md#recording).

`[tts]` / `[time]` / `[tx]`

| Key | Default | Effect |
| --- | --- | --- |
| `tts.voice` | *(required)* | Path to a Piper voice `.onnx` (with its `.onnx.json` sidecar). Required for voice services / voice ID; fails loud if unset or the file is missing. |
| `time.tz` | `"UTC"` | Station timezone (IANA name) for the time service. An unknown zone fails loud. |
| `tx.idle_timeout` | `2.0` | Seconds of silence on a `/audio/tx` stream before PTT drops. Verify on hardware. |

`[scan]` / `[controller]`

| Key | Default | Effect |
| --- | --- | --- |
| `scan.settle` | `0.05` | Scan settle time after retune (s). |
| `scan.poll` | `0.5` | Scan poll cadence (s). |
| `scan.dwell` | `5.0` | Scan dwell time on an active channel (s). |
| `scan.mode` | `"carrier"` | Scan resume mode: `carrier`, `timed`, or `hold`. |
| `controller.poll` | `0.5` | Controller loop poll cadence (s). |
| `controller.session_timeout` | `300.0` | Session inactivity timeout (s). |

`[logging]` / `[server]`

| Key | Default | Effect |
| --- | --- | --- |
| `logging.path` | `"radio-server.jsonl"` | Append-only JSONL event ledger. Opened fail-loud if unwritable. |
| `server.host` | `"127.0.0.1"` | Bind address. Set `"0.0.0.0"` to serve the LAN. |
| `server.port` | `8000` | Bind port. |
| `server.backend` | `"mock"` | Backend: `mock`, `v71`, or `baofeng`. **`baofeng` is implemented (ADR 0029; see the `[baofeng]` keys + the `hardware` extra); `v71` raises `NotImplementedError` today.** |
| `server.web_dir` | `<repo>/web/dist` | Built web-UI directory served at `/`. Unbuilt → a "run the build" placeholder, not a crash. |
| `server.mock_cat` | `true` | Mock only: `false`/off/0/no/n → an audio-only mock (CAT controls grey out), to demo the Baofeng-mode capability split without hardware. |

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
