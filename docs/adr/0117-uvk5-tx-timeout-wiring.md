# 0117 — Wiring the UV-K5 into the transmitter time-out (the stuck-key gate)

Status: Accepted

## Context

ADR 0112 flagged that the docked UV-K6 (the `uvk5` backend) full-control loop has no device-side
time-out: if a key sticks, it transmits until something stops it. The kv4p has a firmware backstop
(`RUNAWAY_TX_SEC ≈ 200 s`, ADR 0063) and a UV-5R has its own radio-side TOT menu — the UV-K5 in
full-control (XVFO) mode has **nothing** device-side, so the server is the only protection.

**Reconnaissance first (the cycle's Deliverable 0): a reusable server-side TOT already exists.** ADR 0090
shipped `TotRadio` (`radio_server/tx/tot.py`) — a `Radio`-surface decorator that hard-caps *continuous*
key time (armed once on key-up, never reset per frame, distinct from the per-frame idle timeouts) and
force-drops PTT from its **own daemon `threading.Timer`**, so it fires even while the caller thread is
parked in a wedged decode. It is wired at the one composition root every keying path funnels through:
`build_radio` wraps **every** backend, on the initial build and every live swap. So the docked UV-K6 is
already covered today.

This cycle is therefore **extension / wiring, not invention.** Three things ADR 0090 left undone are the
real work, plus honest residual-risk docs.

## Decision 1 — placement: follow ADR 0090's decorator; the arbiter is the wrong home

The kickoff argued from ADR 0017 ("the `RadioArbiter` owns TX state") toward an arbiter-level TOT. ADR
0090 already rejected that, and the code confirms why: `RadioArbiter` (`radio_server/arbiter/state.py`)
is a pure synchronous latch — no timer, no async task — and it sees only the `acquire_tx()`/`release_tx()`
edges. It **misses the direct `POST /ptt` path** (which bypasses the arbiter) and **one-shot `transmit()`**
(station ID, DTMF services, which key inside the `Radio` without claiming the arbiter). The `Radio`-boundary
decorator is the only chokepoint all keying funnels through. We **follow** ADR 0090's placement; an
arbiter-level TOT would be a second, competing mechanism with worse coverage. No new mechanism is invented.

## Decision 2 — the forced unkey is already the FULL restore; prove it, don't fix it

The kickoff required that the uvk5 force-unkey be the full `_key_off` (RX registers restored, then audio
teardown), not a bare PTT drop. This is already true in code: `TotRadio._fire()` calls `self._radio.ptt(False)`,
and on the UV-K5 `ptt(False)` routes to `_key_off` — which restores the RX registers on the wire **first**
(`_write_registers([(0x30, 0), (0x30, self._reg30)])`), then stops the pacer and tears down the playout
stream, best-effort and non-raising. So a TOT expiry already leaves the transmitter truly un-keyed. This
cycle **proves** it with a test against the firmware-accurate `FirmwareFakeSerial`: after `fire_latest()`
the fake's register file has `registers[0x30] == radio._reg30` (RX), and the final wire pair is the
`_key_off` restore — no code change to the keying path.

## Decision 3 — surface the forced unkey non-silently (the real observability gap)

`TotRadio` has always had an `on_timeout` hook, but `build_radio` never passed it, so a forced unkey was
**log-only, silent to `/events`** (ADR 0090 explicitly deferred this). We wire it:

- **A first-class `"alarm"` event type** (`radio_server/api/events.py`), payload
  `{"kind": "tx_timeout", "tot": <seconds>}`. It flows to the passive event-log subscriber → the operating
  log and the Log card, over the existing reactive `/events` fan-out — **no new UI machinery**.
- **Thread-safe by construction.** `on_timeout` fires on `TotRadio`'s `threading.Timer` thread, but the
  `EventHub` is asyncio. The publisher hops the boundary with `loop.call_soon_threadsafe(hub.publish, …)`,
  the loop captured at lifespan startup — the only loop-safe way in from another thread.
- **Two wiring points, forced by the composition order.** A *swapped-in* radio gets its hook at
  construction: `build_radio(settings, on_tot_timeout=…)` closes the resolved cap into `TotRadio`'s no-arg
  hook (keeping ADR 0090's contract). The *initial* radio is built by `build_app` **before** the hub
  exists, so `create_app` wires it post-construction via the new `TotRadio.set_on_timeout` — the same
  hub-doesn't-exist-yet pattern the controller's `on_event` already uses. A test-injected fake factory or
  a non-`TotRadio` DI radio is left untouched (guarded by `radio_factory is build_radio` / an `isinstance`
  check).

## Decision 4 — the uvk5 TOT is mandatory: a per-backend key with a backend-declared default

The global `tx.tot` (default 180 s) allows `0` to disable it — acceptable for backends whose firmware or
radio provides its own cutoff, but not for the backstop-less UV-K6. So the UV-K5 gets its **own mandatory**
cap (per the kickoff's "per-backend config and a backend-declared default"):

- **Backend-declared default** `DEFAULT_TOT = 180.0` on `Uvk5Radio` (a declared constant consumed at the
  composition root, since the TOT is a decorator concern, not a constructor arg).
- **A dedicated `uvk5.tot` setting** with a reject-not-clamp coercer (`coerce_uvk5_tot`, modeled on
  `coerce_id_interval`): `0`/negative is **rejected** (it may be shortened, never disabled) and any value
  **above the 180 s default is rejected** (never weakened past the mandatory ceiling). Config can only
  *shorten* it.
- **Resolved per-backend** in `build_radio` (`resolve_tot`): the uvk5 backend uses `uvk5.tot`; every other
  backend uses the global `tx.tot`. So even when an operator sets `tx.tot=0`, the UV-K6 keeps its cap,
  while the other backends fall back to their **device-side story**: kv4p firmware `RUNAWAY_TX_SEC ≈ 200 s`
  (ADR 0063), the UV-5R's TOT menu, and mock (none — it never keys hardware).

## Coverage and residual risk (named honestly)

In-process, `TotRadio` covers: logic bugs, runaway sessions, leaked `TxSession`s, and **`SIGTERM` / normal
crashes** (the backend's `atexit.register(self.close)` unkeys via `_key_off` before exit). It **cannot**
cover host death: `SIGKILL`, a kernel panic, or power loss bypasses both the timer and `atexit`, leaving
the radio keyed until power-cycled. An **out-of-process supervisor** (a watchdog process or hardware timer
that unkeys if the server stops heart-beating) would close this and is a **named possible follow-on — not
built here.** The residual is documented in `docs/uvk5-setup.md` so an operator knows the failure mode
before leaving a node unattended.

## Consequences

- New tests: `tests/test_uvk5_tot.py` (the full RX-register-restore on expiry; normal key/unkey under the
  cap never fires; `uvk5.tot` bounds — 0/negative/over-ceiling rejected, shorten allowed; per-backend
  `resolve_tot`; and the cross-thread `alarm` event end-to-end over `/events`), plus `TotRadio` hook/
  property tests in `tests/test_tx_tot.py` and app-level per-backend resolution in
  `tests/test_backend_wiring.py`. The settings canary moves `89 → 90` for `uvk5.tot`; `radio.toml.example`
  is regenerated.
- No new dependencies. No web change (the alarm rides the existing reactive path and Log card).

## Out of scope (named; built here: none)

The out-of-process supervisor (the named follow-on for host-death coverage), split/offset, EEPROM channel
import, any bench numbers, and any arbiter-level TOT (superseded — we follow ADR 0090's decorator).
