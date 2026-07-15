# 0039 — The activity-summary route: `GET /activity/summary`

Status: Accepted

## Context

Three cycles built the Tier-0 "is this repeater actually dead?" answer end to
end, but kept it internal:

- ADR 0035 persists `rx_open`/`rx_close` edges to the append-only ledger.
- ADR 0036 gave `summarize_activity(records, *, now, tz, window, min_duration)`,
  a leaf-pure transform from those edges to a `ChannelActivity`.
- ADR 0038 gave `read_records(path)`, a streaming reader over the on-disk ledger.

`summarize_activity(read_records(load_log_path(settings)), now=…, tz=…)` is now a
complete composition from the JSONL file to a `ChannelActivity` — but nothing
exposes it over HTTP. This cycle adds the one route that does.

**Scope is the route only — no UI.** The web UI that renders this summary is a
later cycle.

## Decision

Add `GET /activity/summary`, gated by the existing bearer token like every other
REST route, returning the five `ChannelActivity` fields as JSON.

The route lives in a new `radio_server/api/activity.py` with a
`register_activity_routes(api, app)` function, mirroring `settings.py`'s
`register_settings_routes(api, app)`. It attaches to the same token-gated
`APIRouter` (`api`), so bearer auth is inherited — there is no per-route
`Depends`; membership on `api` *is* the opt-in. It reads state off `app.state`,
exactly as the settings routes do.

Response shape follows the house style: `ChannelActivity` is a frozen dataclass,
returned via `dataclasses.asdict(summary)` — a plain `dict`, no Pydantic response
model (matching `GET /status`). The two tuple fields serialize as JSON arrays;
`last_heard` may be `null`.

### The load-bearing decision: run it off the event loop

`read_records` does synchronous file I/O and `summarize_activity` walks the
**whole** ledger — `O(all history)` per call, the limit named in ADR 0038.

This process's main job is real-time audio. Doing an unbounded ledger walk
inline in an `async` handler would **block the event loop**, stalling the
`RxPump` and every `/events` / `/audio/rx` subscriber. A year of RX edges would
turn one summary request into an audible glitch for everyone listening.

So the entire blocking chain is handed to `asyncio.to_thread`:

```python
summary = await asyncio.to_thread(_run_summary, path, now, tz, window, min_duration)
```

`_run_summary` runs `summarize_activity(read_records(path), …)` whole inside the
worker thread. Because `read_records` is a generator consumed *by*
`summarize_activity`, offloading the summarize call moves **both** the file I/O
and the full walk off the loop — not just the `open`.

This is the **first `asyncio.to_thread` in the codebase**, so the reasoning is
recorded here deliberately. It is the same instinct as ADR 0028's async scan
runner — keep synchronous work off the event loop so real-time work is never
starved — but a different mechanism. ADR 0028 chunks *short* synchronous work
(`ScanEngine.tick()`) across many steps with `await asyncio.sleep(poll)` between
them; that works because each step is bounded. A single ledger walk is one
unbounded blocking call that cannot be chunked that way, so it is offloaded whole
to a thread instead. (Note ADR 0028 / `RxPump` already flagged "run blocking work
in a thread executor" as an open bring-up question; this is the first concrete
place the answer is "yes, offload it.")

### Empty / missing ledger → zeroed summary, `200`

The route adds **no** error handling for a missing or empty ledger, by design.
`read_records` yields nothing for a missing file (ADR 0038), and
`summarize_activity` over no records returns a zeroed `ChannelActivity`
(`busy_count=0`, `total_airtime=0.0`, `last_heard=None`, `by_hour` all zero,
`by_weekday` all zero). That is returned as an ordinary `200` body —
**deliberately not `404` and not `500`**. "No history yet" is a valid Tier-0
answer ("nothing heard"), not an error, consistent with ADR 0038's tolerant
read: a fresh install that has never received is Tuesday, not a fault.

### Settings drive the window and the crackle cutoff

Two new base-tier settings, group `activity`, both `coerce_positive_float`:

| key | env | default | note |
|---|---|---|---|
| `activity.window` | `RADIO_ACTIVITY_WINDOW` | `604800.0` (7 days, seconds) | marked default |
| `activity.min_duration` | `RADIO_ACTIVITY_MIN_DURATION` | `1.0` | **verify on hardware** (guardrail 1) — a squelch-crackle vs QSO cutoff, a placeholder, never a confirmed value |

Per the config convention (a marked default is a `DEFAULT_*` constant in the
owning subsystem, not an inlined literal), the specs point at constants in
`eventlog/summary.py`: `activity.min_duration` reuses the existing
`MIN_DURATION_DEFAULT`; `activity.window` points at a new `DEFAULT_WINDOW_SECONDS
= 604800.0`, with the existing `DEFAULT_WINDOW` re-expressed as
`timedelta(seconds=DEFAULT_WINDOW_SECONDS)` so the seconds float and the timedelta
stay a single source of truth (a non-behavioral change — `timedelta(days=7)` is
exactly 604800 s). The route reads the configured float and wraps it back with
`timedelta(seconds=…)`.

`tz` comes from the existing `time.tz` (`load_timezone(settings)`); `now` is
`time.time()` resolved at the route, the established composition-edge convention
(the API layer has no injected clock today). No query params this cycle — window
and min_duration come only from settings.

Adding two settings bumps the settings **canary count** (the total-fields
assertion in `tests/test_settings_api.py`) from 49 to 51. This is an **expected,
deliberate** change, not a surprise, and `radio.toml.example` is regenerated so
the new `[activity]` table ships in banner order.

## Known limit (unchanged, not solved here)

Every call re-reads the whole ledger — the `O(all history)` cost named in ADR
0038. Offloading to a thread keeps that cost off the event loop; it does not
reduce the cost. Reverse-seek, an index, rotation, and caching all remain
deferred.

## Consequences

- **Tier-0 is reachable over the LAN.** A client can ask "is this repeater dead?"
  with one authenticated `GET` and get a `ChannelActivity` back.
- **First thread offload in the tree.** `asyncio.to_thread` is introduced with
  its rationale recorded; future unbounded-but-synchronous work in async handlers
  has a precedent to follow.
- **The route is thin and schema-driven.** It adds no per-setting logic; the two
  new settings flow through the existing config/API machinery for free.
- **Deferred, on purpose (out of scope here):** any UI over the summary; query
  params for window/min_duration; caching/rotation/indexing; any Link/network
  work.
- **Cross-references:** ADR 0026 (the settings REST surface and bearer gate this
  route joins), ADR 0028 (the async scan runner — the keep-work-off-the-loop
  precedent), ADR 0036 (the summarizer), ADR 0038 (the reader and the O(all
  history) limit).
