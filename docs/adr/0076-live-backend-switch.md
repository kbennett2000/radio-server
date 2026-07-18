# 0076 — The live backend switch

Status: Accepted

## Context

Two ADRs laid scaffolding for switching the active radio without a restart, and both said so
explicitly:

- **ADR 0073** built the `RadioHolder` seam — one object owns the active radio (`.radio`) and the
  lifecycle of the radio-bound pipeline, with a `stop()` "deliberately designed as the first half of a
  swap." It deferred `rebuild()`, a select endpoint, and "make the routes read `holder.radio` live" to
  the swap cycle.
- **ADR 0074** built `configured_backends(settings)` — the presence-based enumeration of the backends
  this node is configured for, each with its resolved constructor kwargs. It shipped with **no caller**,
  its shape defined so this cycle is a thin HTTP wrapper.

Nothing switched yet: the holder built one backend at startup from `server.backend` and never rebuilt
it. This cycle wires the two seams into a live switch. The two-radio bench box (an AIOC UV-5R on
`/dev/ttyACM0` + a kv4p HT on its own serial path) makes AIOC↔kv4p switching a real, RX-verifiable
operation. **This cycle is the endpoint + the current/available API; the UI dropdown is the next cycle.**

### The load-bearing constraint

The REST/WS handlers are closures inside `create_app` that read the **captured locals** `radio`,
`rx_pump`, `scan_runner`, `controller` — not `app.state.holder.radio`. Swapping `holder._radio` alone
does not redirect them. Two more things `holder.stop()` tears down and a rebuild must reconstruct:

- the **pipeline** (`RxPump`/`ScanRunner`) — `start()` rebuilds it against the new radio;
- the **DTMF controller** — `stop()` calls `controller.close()` (reaps the streaming multimon-ng
  process), and the controller captures the radio for TX responses/ID, so a reused one is both closed
  and pointing at the old (now closed) radio.

And the old radio is **closed** by `stop()` before the new one is built (the AIOC sound card is
single-open), so rollback cannot reuse it — it must reconstruct the previous backend.

## Decision

### `RadioHolder.rebuild(new_settings)` — atomic, rollback-safe

`rebuild` runs the `stop(); construct; start()` contract entirely under a single `asyncio.Lock`, so two
concurrent selects serialize — no caller observes a half-torn-down pipeline. The holder gains two
injected factories (both defaulted so the DI seam is unchanged):

- `radio_factory: Callable[[Settings], Radio] = build_radio` — constructs the target backend from
  settings (and is where a **fake** is injected for tests);
- `controller_factory: Callable[[Settings, Radio], Controller | None] | None = None` — rebuilds the
  controller against the new radio (mirroring the per-scan `build_scan_engine` factory the holder
  already carries). `start()` calls it only when `_controller is None`, so the first `start()` keeps the
  injected pre-built controller and only a rebuild (which nulls it) triggers a fresh build.

Rollback is the safety case: if the target **fails to construct or open** (the kv4p resets on open and
`connect()` can race its boot; the AIOC card is single-open), the holder reconstructs and restarts the
**previous** backend and re-raises. A failed switch leaves you on the radio you had, never radio-less.

### `POST /radio/select {backend}` and `GET /radio/backends`

Both live inside `create_app` so the select handler can rebind the closure locals. Select:

1. Refuses a name not in `configured_backends(settings)` — **409**, never attempts an arbitrary backend.
2. Builds the target's settings by patching `server.backend` onto the current set and revalidating
   atomically (`resolve_settings`, the `patch_settings` idiom) so a bad value fails before any teardown.
3. `await holder.rebuild(new_settings)`; on failure (already rolled back) → **503**, config unwritten.
4. On success: persists `server.backend` via `save_settings` (through the schema, ADR 0051 — the rest
   of `radio.toml` is preserved), then **`nonlocal`-rebinds** `radio`/`rx_pump`/`scan_runner`/
   `controller` (the ADR 0073-deferred "routes follow the live radio" step — late-binding closures then
   transparently reach the new radio), re-honors RX demand (`if rx_demand > 0: rx_pump.start()`, since
   the new pump is demand-started) so received audio follows the new radio without a reconnect, and
   **re-emits** a new `capabilities` event followed by a status snapshot.

`GET /radio/backends` returns `{active, active_capabilities, backends:[{name, active, settings}]}` from
`configured_backends` + the live radio. Only the **active** backend carries live capabilities — the
others would need construction/hardware to advertise theirs (ADR 0074's deliberate exclusion; the UI
greys by the active set and re-greys on the switch's re-emit).

### Capability re-push over WebSocket

Capabilities were only ever read once over REST (`GET /capabilities`) at connect. A new
`"capabilities"` event type + `capabilities_event(radio)` helper let a switch push the new set to
connected `/events` clients so they re-grey **without reconnecting**. Wiring the frontend to consume it
(a `reduceStatus` case, lifting `caps` out of the one-shot session prop) is the **UI cycle** — this
cycle emits the event and proves it lands.

### Persistence: write it back

A live switch **persists** `server.backend` so a restart comes up on the last-selected radio — a
restart landing on a different radio than the UI last showed is a surprise. Only on success, only
through the schema round-trip (tomlkit-preserving), so the rest of `radio.toml` is untouched.

### In-flight state

`stop()` already drops PTT if the arbiter holds the transmitter, halts a running scan, and discards a
mid-accumulation DTMF entry (`controller.close()`); closing the outgoing radio physically drops any
latched key — covering the direct-`POST /ptt` gap ADR 0073 flagged (that path bypasses the arbiter, but
the device close still de-keys it). Selecting a backend never keys.

## Consequences

- Switching AIOC↔kv4p at runtime works: the active radio, the RX audio stream, the advertised
  capabilities, and the persisted selection all follow the switch; a failed target leaves the previous
  radio live.
- `create_app` gains `radio_factory`/`controller_factory` kwargs and `build_app` builds the controller
  through a factory — behaviour-identical when a switch never happens (the whole suite staying green is
  the proof). **No schema change**: `radio.toml.example` is byte-identical and the settings-count canary
  is unmoved.
- New tests: `test_radio_holder.py` gains rebuild/rollback/lock/controller-rebuild proofs against fakes;
  `test_backend_select.py` covers select (200 + capability change), unconfigured (409), rollback (503,
  previous backend live, config unwritten), the `capabilities` re-emit over `/events`, and the
  persistence round-trip preserving the rest of the file.

## Non-goals

The UI dropdown (next cycle — it consumes these endpoints + the `capabilities` event). No
keying-specific work; selecting never keys. No per-backend DTMF twist (ADR 0075 noted it for this arc —
still global). No new backends. No `Radio.close()` protocol promotion, no `ControllerRunner` removal
(ADR 0073 open items, untouched). Docs limited to this ADR + HANDOFF + the ADR index row.
