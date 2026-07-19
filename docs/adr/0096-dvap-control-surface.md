# 0096 — DVAP control surface: config + a cached manager + `/dvap/*` routes over the remote-control client

Status: Accepted

## Context

ADR 0095 landed the ircDDBGateway remote-control client in isolation. This ADR wires it into the app so
radio-server becomes the control panel for the **DVAP** gateway modules — the two 70cm DVAPs on the
bench (module B = 441.600, module C = 441.000), which are independent `dstarrepeater` endpoints
radio-server carries no audio for. Unlike D-STAR module A (crossband: a bridge, the DV Dongle vocoder,
PTT, and only *believed* link state), the DVAP path is pure control-plane: link / unlink / read
**confirmed** state over the gateway's remote-control UDP interface. No vocoder, no bridge, no PTT.

Two constraints shaped the design:

- **`/status` must never block.** Each module's confirmed link is a bounded UDP round-trip; doing that
  on the hot `/status` path would add latency and hang when the gateway is down. So the manager **caches**
  confirmed state and only reads the gateway on an explicit refresh.
- **Off by default.** A default deployment has no DVAP and must open no socket — the same gate as
  `dstar.callsign` and `[[mumble.servers]]`.

## Decision

Wire the DVAP control surface across the config, manager, and API layers; the web card is a follow-up PR
(same ADR). Nothing here touches the D-STAR module-A path.

- **Config.** `[dvap]` scalars `host` (default 127.0.0.1) and `port` (default 10022), both advanced. The
  modules are an **array-of-tables `[[dvap.modules]]`** (`module` letter, `label`, `frequency_hz`) — the
  non-schema channel modelled exactly on `[[mumble.servers]]` (`load_dvap_modules` peels it off in
  `_flatten`; `resolve_dvap_modules` validates it into frozen `DvapModule`s, failing loud on a bad
  letter / duplicate / non-positive frequency / unknown field). The remote-control **password is a
  secret** — `dvap_remote_password` in `radio-secrets.toml` (or `RADIO_DVAP_REMOTE_PASSWORD`), never in
  `radio.toml`. The golden `radio.toml.example` gains a commented `[dvap]` + `[[dvap.modules]]` block.

- **`DvapManager`** (`dstar/dvap_manager.py`) — a thin control layer over the remote-control client for a
  list of modules. `link(module, reflector)` / `unlink(module)` issue commands; `refresh()` queries every
  module's confirmed link and updates the cache; `status()` returns the cache with **no I/O**. A module
  the gateway won't answer for is marked `reachable: false` rather than failing the whole snapshot.
  Errors: `DvapUnknownModule` (unconfigured letter), `DvapUnavailable` (gateway unreachable / rejected).

- **API** (`/dvap/*`, beside `/dstar/*`). `GET /dvap/status` refreshes and returns the block (null when
  unconfigured); `POST /dvap/link {module, reflector}` and `POST /dvap/unlink {module}` run the blocking
  client off the event loop via `asyncio.to_thread`, then **publish the confirmed (post-refresh) block**
  as a `dvap` WS event. Error mapping: 404 unknown module, 422 bad reflector name, 503
  unconfigured/gateway-unreachable. The block is embedded in `GET /status` (cached, no I/O) and self-nulls
  when unconfigured — the config-driven-visibility pattern the web card keys off, like D-STAR.

- **Wiring.** `build_app` reads the modules + host/port + secret and passes a lazy
  `UdpRemoteControlClient` factory to `create_app`, gated on `dvap_modules` being non-empty (no socket on
  a default deployment); the modules register under the station callsign (the gateway's own callsign,
  e.g. `"AE9S   B"`). The client's socket is released on lifespan shutdown (no audio/PTT to unwind).

## Consequences

- **radio-server can now steer and confirm the DVAP modules** — the missing half of "single control
  panel". Because it reads *confirmed* state, the DVAP card shows the real link, not a guess (the wart
  D-STAR module A still has; unifying module A onto this readback is a later, optional follow-on).
- **Zero impact on a default or D-STAR deployment.** No `[[dvap.modules]]` → no manager, no socket, and
  every `dvap` block is null. The D-STAR bridge/vocoder/PTT paths are untouched.
- **`/status` stays fast and never hangs on a down gateway** — it serves the cache; only `/dvap/status`
  and link/unlink do gateway I/O, off the event loop, and degrade to `reachable: false` on timeout.
- **Verified on fakes** (`MockRemoteControlClient` modelling a tiny gateway): the manager's
  link/status/unlink round-trip, confirmed-state cache, unknown-module/bad-name/unreachable handling, and
  `resolve_dvap_modules` validation; the `/dvap/*` routes' full flow, error codes, `/status` embed, and
  graceful degrade — `uv run pytest` 1278 passed. The wire protocol's agreement with a real gateway is
  the ADR 0095 hardware-phase bench check (remote-control must be enabled on the gateway first).
- **Deploy gating.** The DVAP tab only works once the operator enables the gateway's remote-control
  interface (`remoteEnabled=1` + `remotePassword`, one restart) and stands up each DVAP as a
  `dstarrepeater` node + gateway repeater band — an operator step, documented in HANDOFF, not managed by
  radio-server.

Cross-refs: ADR 0095 (the remote-control client this consumes), ADR 0088/0089 (D-STAR module A: the
believed-state, bridge-bound path this deliberately does *not* follow), ADR 0042 (the `[[mumble.servers]]`
array-of-tables pattern replicated for `[[dvap.modules]]`).
