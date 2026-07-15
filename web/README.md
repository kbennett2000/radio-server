# radio-server web UI

> **For developers.** This is about building and developing the browser control panel. To just use
> radio-server you don't build anything by hand — see **[Try it first](../docs/getting-started.md)**.

A single-page control panel (React + Vite) for the radio-server API — control, live status, and
**live audio in both directions**: Listen plays what the radio hears (RX), Talk streams your mic
to the transmitter (TX). A topbar **Settings** tab edits `radio.toml` in the browser — a
schema-driven form (built from `GET /settings`, so each field shows its description inline), plus
write-only API-token rotation and TOTP re-enrollment (QR). Changes are **restart-to-apply**. See
[ADR 0022 (web-UI architecture)](../docs/adr/0022-web-ui-architecture.md),
[ADR 0023 (RX playback)](../docs/adr/0023-rx-playback.md),
[ADR 0024 (TX mic capture)](../docs/adr/0024-tx-mic-capture.md), and
[ADR 0027 (settings screen)](../docs/adr/0027-settings-ui.md).

Everything here runs identically against any backend — the mock (the dev default) or the working
AIOC/Baofeng hardware backend (ADR 0029); the TM-V71A backend is still a stub. See the
[project status](../README.md#status--read-this-first).

## Build (required before the Python server can serve it)

```sh
cd web
npm install
npm run build      # -> web/dist/  (what FastAPI serves)
```

`node_modules/` and `web/dist/` are gitignored; the build is a prerequisite, not committed.

## Run (served same-origin by FastAPI)

From the repo root, with the SPA built:

```sh
RADIO_API_TOKEN=test-lan-secret uv run python -m radio_server   # http://127.0.0.1:8000
```

FastAPI serves the built bundle from `server.web_dir` (default `<repo>/web/dist`) mounted at `/`.
The static mount is registered **last**, after the REST router and the WebSocket routes, so the
token-gated API and the `?token=` sockets always win — the catch-all `/` never shadows an API
path. If `server.web_dir` has no `index.html` (not built yet), the server returns a friendly "run
the build" placeholder instead of crashing.

Open the URL, enter the token, and the panel connects. General settings live in `radio.toml`
(point the server at a specific file with `--config PATH`); the two secrets are env vars or a
`radio-secrets.toml` (chmod 600). Useful knobs (all with marked defaults — the
[full table is in the root README](../README.md#configuration)):

| Setting                     | Default           | Effect                                                        |
| --------------------------- | ----------------- | ------------------------------------------------------------- |
| `RADIO_API_TOKEN` (secret)  | *(required)*      | LAN bearer token; the server refuses to start without it.     |
| `server.host`               | `127.0.0.1`       | Bind address; set `0.0.0.0` to reach it across the LAN.       |
| `server.port`               | `8000`            | Bind port.                                                    |
| `server.mock_cat`           | `true`            | `false` → an audio-only mock (tuning controls grey out).      |
| `server.web_dir`            | `<repo>/web/dist` | Directory of the built SPA to serve.                          |
| `RADIO_TOTP_SECRET` (secret)| *(unset)*         | When set, wires the controller so Start/Stop is live.         |

## Browser requirements

Live audio depends on browser gestures and permissions — both features do nothing until you act:

- **Listen (RX playback)** needs a **user gesture to start audio.** Browsers create an
  `AudioContext` suspended, so autoplay is impossible on load; the context is created and resumed
  only when you click **Listen** (`src/useRxAudio.js`). Audio plays at the canonical 48 kHz, so no
  resampling is needed on the way in.
- **Talk (TX mic capture)** needs **microphone permission.** Clicking **Talk** calls
  `getUserMedia` (`src/useTxAudio.js`); denying permission shows a clear message rather than
  hanging. Captured audio is resampled from the mic's context rate to the canonical 48 kHz and
  streamed as 16-bit PCM over `/audio/tx`.

Half-duplex is respected in the UI: while you are talking, the local RX monitor is muted
immediately (the jitter buffer would otherwise let you hear yourself key in and out).

## Develop (hot reload)

```sh
# terminal 1 — the API on :8000
RADIO_API_TOKEN=test-lan-secret uv run python -m radio_server
# terminal 2 — Vite dev server on :5173, proxying the API to :8000 (no CORS needed)
cd web && npm run dev
```

The dev proxy is configured in [`vite.config.js`](vite.config.js): it forwards the REST paths and
the three WebSockets (`/events`, `/audio/rx`, `/audio/tx`, all `ws: true`) to the Python server,
so the browser sees a single same-origin host in dev just as it does in production. Override the
API target with `RADIO_DEV_API` (default `http://127.0.0.1:8000`).
