# radio-server web UI

A single-page control panel (React + Vite) for the radio-server API — control, live status, and
**live audio in both directions**: Listen plays what the radio hears (RX), Talk streams your mic
to the transmitter (TX). See
[ADR 0022 (web-UI architecture)](../docs/adr/0022-web-ui-architecture.md),
[ADR 0023 (RX playback)](../docs/adr/0023-rx-playback.md), and
[ADR 0024 (TX mic capture)](../docs/adr/0024-tx-mic-capture.md).

Everything here runs against the mock backend (there is no working hardware backend yet — see the
[project status](../README.md#status--read-this-first)).

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

FastAPI serves the built bundle from `RADIO_WEB_DIR` (default `<repo>/web/dist`) mounted at `/`.
The static mount is registered **last**, after the REST router and the WebSocket routes, so the
token-gated API and the `?token=` sockets always win — the catch-all `/` never shadows an API
path. If `RADIO_WEB_DIR` has no `index.html` (not built yet), the server returns a friendly "run
the build" placeholder instead of crashing.

Open the URL, enter the token, and the panel connects. Useful env knobs (all with marked
defaults — the [full table is in the root README](../README.md#configuration)):

| Env var            | Default        | Effect                                                        |
| ------------------ | -------------- | ------------------------------------------------------------- |
| `RADIO_API_TOKEN`  | *(required)*   | LAN bearer token; the server refuses to start without it.     |
| `RADIO_HOST`       | `127.0.0.1`    | Bind address; set `0.0.0.0` to reach it across the LAN.       |
| `RADIO_PORT`       | `8000`         | Bind port.                                                    |
| `RADIO_MOCK_CAT`   | `on`           | `off` → an audio-only mock (tuning controls grey out).        |
| `RADIO_WEB_DIR`    | `<repo>/web/dist` | Directory of the built SPA to serve.                       |
| `RADIO_TOTP_SECRET`| *(unset)*      | When set, wires the controller so Start/Stop is live.         |

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
