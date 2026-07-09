# 0022 — Web UI architecture (React SPA, same-origin static serving, in-memory token, capability-driven rendering)

Status: Accepted

## Context

Every prior cycle built server-side: the whole software tower (REST + WebSocket API, capability
split, arbiter, scan, controller, event taxonomy, recording) runs live against `MockRadio`, but
there has been **no human-facing client** — and, flagged repeatedly in HANDOFF, **no entrypoint
binds `build_app()` to a port**. `create_app`/`build_app` existed; nothing served them.

This cycle ships the first browser client: a **control + visibility** panel (live audio is deferred
to cycles 22–23). It is a pure client of the existing API against the mock server — **no backend
behavior changes** — plus the small, load-bearing glue needed to *serve* it. It finally surfaces
guardrail 3 in a UI: tuning controls render on a CAT backend and grey out on an audio-only one.
Still **mock-only** (guardrail 1): `RADIO_BACKEND` defaults to the mock and a new `RADIO_MOCK_CAT`
toggle lets a browser exercise both sides of the capability split without hardware.

## Decision

### Framework: React + Vite, in `web/`

- **A React SPA built by Vite**, sources under a new top-level `web/` (`src/` components + hooks),
  building to `web/dist/`. `node_modules/` and `web/dist/` are gitignored — a
  `npm install && npm run build` step is a documented prerequisite (the deliberate cost of a build
  toolchain in a uv-only repo; the tradeoff was chosen explicitly over a zero-build vanilla page).
  The bundle uses `base: "./"` so it is served correctly regardless of mount path, and a dev-only
  Vite proxy forwards the API paths + `/events` to the Python server so `npm run dev` stays
  same-origin (no CORS in dev either).

### Same-origin static serving + the missing entrypoint

- **`create_app` gains an opt-in `web_dir`.** When set, the built SPA is mounted at `/` via
  `StaticFiles(html=True)` — **mounted last**, after the REST router and the three WebSocket routes,
  so the token-gated API always wins over the catch-all. When `web_dir` is set but *unbuilt* (no
  `index.html`), `GET /` returns a friendly "run `npm run build`" placeholder rather than crashing,
  so the API stays runnable before the SPA exists. `web_dir=None` (the DI-seam default every prior
  test uses) adds no `/` route — the pre-cycle surface is unchanged.
- **`build_app` resolves `web_dir` from `RADIO_WEB_DIR`** (marked default → the repo's `web/dist`).
- **`python -m radio_server` is the entrypoint** (`radio_server/__main__.py`) — the missing seam
  that binds uvicorn to `RADIO_HOST` (marked default `127.0.0.1`) / `RADIO_PORT` (default `8000`)
  around the env-composed app. It stays thin; all wiring remains in `build_app`, which still fails
  loud when `RADIO_API_TOKEN` is unset (the server never binds open).
- **`websockets` is now a runtime dependency.** Plain `uvicorn` ships no WebSocket implementation,
  so a bound server 404s every `/events` upgrade — a gap the TestClient masked (it carries its own
  in-process WS, which is why the API tests never needed it). Since this cycle is the first to bind
  a real server, it owns the fix; `websockets` is added explicitly (not `uvicorn[standard]`) to pull
  in only the WS piece and keep the install lean.

### In-memory token auth

- The SPA **prompts for the LAN API token and holds it in React state only** — never `localStorage`,
  so a refresh re-prompts and nothing persists the secret. It sends `Authorization: Bearer <token>`
  on every REST call and `?token=` on the `/events` WebSocket (browsers can't header a WS handshake).
  `GET /capabilities` doubles as the token check at the gate: a bad token throws `Unauthorized` and
  the gate shows a clear error — never a silent hang. An `Unauthorized` anywhere after entry (or a
  WS `1008` policy close) drops back to the gate.

### Capability-driven rendering (guardrail 3, finally in a UI)

- On auth the panel reads `/capabilities` and greys any CAT control whose capability is absent, with
  a "not supported on this radio" note. As a **defensive backup**, a `501` from any CAT call reads
  its machine-named `detail.capability` and greys exactly that control at runtime. On an audio-only
  backend the whole Tune block is disabled and annotated — never a dead button that silently no-ops.

### `/events`-driven live state

- **One WebSocket feeds the whole UI.** Frames (`status`/`ptt`/`scan`/`session`/`auth`/`command`/
  `arbiter`) fold into a live status object (the status panel) and append to a **bounded (~500)**
  event list (the scrolling operating log — the live stream, not the persisted JSONL ledger, which
  has no GET API yet). The socket **reconnects with exponential backoff** on any drop; a `1008`
  (rejected token) stops retrying and bubbles an auth error back to the gate.
- **Scan is honest about the API.** `POST /scan` is one synchronous sweep returning the held
  frequency; there is no server-side stop. The UI offers "Scan" and reflects live scan phase — no
  dead stop button (a scan-stop endpoint is a future cycle). **Controller** start/stop surfaces the
  `503` "not configured" case as a disabled control with a message, not a dead button.

## Consequences

- **The app is finally servable and browsable.** `python -m radio_server` brings up the mock server
  + UI in one command; the control panel gates on a token, drives every existing endpoint, and
  renders live state off `/events`.
- **Guardrail 3 is visible.** `RADIO_MOCK_CAT=off` yields an audio-only mock whose tuning controls
  grey out; the default full-CAT mock lights them up — both demonstrable in a browser, no hardware.
- **Backend surface is unchanged for existing callers.** `create_app` gained only a keyword-default
  `web_dir`, and `build_app` only reads new env vars — all prior callers/tests are untouched.
- **Verified end-to-end in a real browser** (headless Chromium against the live server), not just
  pytest: token gate (wrong→error / right→panel), CAT-vs-audio-only greying, each control hitting
  its endpoint and reflecting the result, `/events` driving the status panel + event log, controller
  `503` handling, and WS reconnect after a server drop/restart.
- **Untouched, deliberately:** `backends/`, `arbiter/`, `scan/`, `controller/`, `rx/`, `tx/`,
  `activity/`, `audio/`, `auth/`, `eventlog/`, `recording/`, `services/` — the UI is a pure client;
  the only Python changes are `api/app.py` (the mount + env toggles), the new `__main__.py`, and the
  `websockets` runtime dep.
- Full suite: **385 passed, 4 skipped** (was 377; +8 in `tests/test_web.py` covering the static
  mount, the unbuilt placeholder, static-never-shadows-the-gated-API, `web_dir=None` unchanged
  surface, and the `RADIO_WEB_DIR` / `RADIO_MOCK_CAT` env wiring). The SPA itself is browser-verified.
- **Deferred, on purpose:** live RX playback (cycle 22), TX mic capture (cycle 23), recordings
  download/playback and a GET API for the JSONL ledger, a server-side scan-stop, and an `/events`
  "suspended" marker for arbiter RX-pause. Next: live RX audio.
- **Numbering / branch note:** ADR 0022 by cycle order, cut from the cycle-20/21 merge point
  (`9fd5388`, ADR 0021) at 377 passed / 4 skipped. Touches `api/app.py`, adds `radio_server/__main__.py`,
  the `web/` SPA, `tests/test_web.py`, and the `websockets` runtime dependency.
