# radio-server web UI

A single-page control panel (React + Vite) for the radio-server REST + `/events` API — control
and visibility only; live audio arrives in later cycles. See `docs/adr/0022-web-ui-architecture.md`.

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
RADIO_API_TOKEN=test-lan-secret python -m radio_server        # http://127.0.0.1:8000
```

Open the URL, enter the token, and the panel connects. Useful env knobs (all with marked defaults):

| Env var            | Default        | Effect                                                        |
| ------------------ | -------------- | ------------------------------------------------------------- |
| `RADIO_API_TOKEN`  | *(required)*   | LAN bearer token; the server refuses to start without it.     |
| `RADIO_HOST`       | `127.0.0.1`    | Bind address; set `0.0.0.0` to reach it across the LAN.       |
| `RADIO_PORT`       | `8000`         | Bind port.                                                    |
| `RADIO_MOCK_CAT`   | `on`           | `off` → an audio-only mock (tuning controls grey out).        |
| `RADIO_WEB_DIR`    | `<repo>/web/dist` | Directory of the built SPA to serve.                       |
| `RADIO_TOTP_SECRET`| *(unset)*      | When set, wires the controller so Start/Stop is live.         |

## Develop (hot reload)

```sh
# terminal 1 — the API on :8000
RADIO_API_TOKEN=test-lan-secret python -m radio_server
# terminal 2 — Vite dev server on :5173, proxying the API paths to :8000 (no CORS needed)
cd web && npm run dev
```
