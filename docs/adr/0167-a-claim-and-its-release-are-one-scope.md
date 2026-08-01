# ADR 0167 — A claim and its release are one scope

**Status:** Accepted · 2026-07-31 · closes the acquire-then-await shape
[ADR 0151](0151-a-failed-key-up-must-give-the-radio-back.md) audited once and listed as out of
scope; the reachable half of it is a consequence of
[ADR 0166](0166-a-dead-reader-is-not-a-healthy-station.md)

## Context

Three websocket handlers claimed a single-talker slot and then suspended before entering the region
that frees it:

```python
acquired = tx_slot.try_acquire()      # app.py:2208 / :2349 / :2449
await websocket.accept()              #      :2209 /  :2350 /  :2450   <- unguarded
if not acquired: ...
try:                                  #      :2229 /  :2355 /  :2456
    ...
finally:
    tx_slot.release()                 #      :2291 /  :2395 /  :2493
```

`TxSlot` is one bare bool. No owner, no acquire timestamp, no timeout, no watchdog, no lifespan
reset — **nothing reclaims a slot whose holder went away without releasing it.** So any failure
between the claim and the guarded region strands that talk path for the life of the process, and the
only remedy is a restart.

The blast radius is wider than the browser. `tx_slot` is shared: once wedged, `MumbleBridge`
silently counts every relay frame into `dropped_slot_busy`, and `DStarBridge._open_rx_session` sets
`_rx_slot_held = False` and keys reflector audio onto RF without owning the slot.

## The stated trigger does not reproduce — measured, not reasoned

The brief named an everyday cause: a closed tab, a flaky phone, a reload mid-handshake. **That does
not raise out of `accept()` on this stack**, and the ADR says so rather than repeating a plausible
story. Real uvicorn 0.51.0 on loopback, `ws="auto"` → `WebSocketsSansIOProtocol` (the production
path — `wsproto` is not installed), against four client drop modes:

| drop mode | `accept()` raised? | what actually happened |
|---|---|---|
| RST immediately | no | accept **succeeded** |
| FIN immediately | no | accept **succeeded** |
| FIN after 10 ms | no | accept **succeeded** |
| truncated headers + RST | no | the ASGI task never started |

In every case the handler proceeded and the *first `receive_bytes()`* raised `WebSocketDisconnect` —
which the handler already catches and whose `finally` already releases.

Why, from the source the measurement sent me back to: `websocket.connect` is queued at
`websockets_sansio_impl.py:219` **before** the ASGI task is created, so starlette's `receive()` can
never see a disconnect first; the accept write is guarded by `if not self.transport.is_closing()`,
so it is *skipped*, not raised; and the impl calls `.cancel()` only on its own ping/pong timers,
never on the app task, so a client drop cannot cancel the handler either.

The one thing that does reach the window is **`asyncio.CancelledError` on server shutdown** —
measured by parking a handler inside the window and setting `should_exit`. There, the process is
dying, so the strand is moot.

**So the accept window is not live-process reachable today.** Three things make the change
load-bearing anyway, in descending order of evidence:

1. **The teardown-before-release family is reachable, and demonstrated.** ADR 0166 made a raising
   `radio.ptt(False)` an ordinary event rather than a theoretical one — a dead serial reader is
   exactly a backend whose unkey throws. `session.close()` at `app.py:2290`,
   `end_operator_over()`'s UDP write at `app.py:2492`, and `session.close()` at `link/bridge.py:508`
   each ran *before* the release, on a live process.
2. **`/audio/rx` pins the reader.** `_acquire_rx` incremented `rx_demand` **before**
   `rx_pump.start()`, so a start that raises left the demand at 1 with no pump behind it — after
   which `_release_rx` could never reach 0 and the single reader was wedged for the life of the
   process. The subscription taken just above it leaked with it.
3. **The window is one `await` from reachable.** Any suspension later added between the claim and
   the `try:` — an async auth lookup, a rate limiter, a greeting `send_json` — makes it live and
   silent. That is exactly how the fourth copy gets written wrong, and the structural fix costs
   nothing.

The repo already half-knew. `tx/session.py:360-363` guards the *recorder* inside `close()`
specifically because "an exception escaping here would skip the slot release and permanently wedge
the single transmitter" — and left `ptt(False)` on the line above it bare.

## Decision

**A release must be a scope exit, never a statement that can be skipped.** One rule; a helper where
copy-paste risk lives, the rule applied directly where it does not.

### 1. `talker_slot()` — `radio_server/api/talkers.py`

An `@asynccontextmanager` that claims the slot, completes the handshake, refuses a second talker,
and releases on **every** exit — but only if this caller was the one that claimed it, so a refused
talker never frees the holder's slot. `try`/`finally` rather than `except Exception`, because a real
shutdown delivers `CancelledError`, which is a `BaseException` — the same reason ADR 0151's key-up
unwind catches `BaseException`.

Each handler becomes `async with talker_slot(websocket, slot, name) as acquired:` plus a two-line
`if not acquired: return`. The accept-then-1013 reasoning — a browser cannot observe a *pre-accept*
close code, so a refused talker must be accepted before it can be told why — was duplicated verbatim
three times and now lives once, where it is the reason the two steps are one unit.

**This closes the reachable family for free.** The release moved to an *outer* scope, so the
teardowns each handler still runs in its own `finally` can no longer skip it by raising.

### 2. `rx_listener()` — the same shape, different resources

Owns `subscribe → acquire → yield queue → unsubscribe + release`, releasing the demand only if it
was actually taken. Plus a two-line reorder making `_acquire_rx` atomic on its own: start the pump
**before** counting the demand, so a start that raises leaves the count at 0. No `await` between the
test and the start, so it stays atomic under asyncio for the same reason `TxSlot`'s check-and-set is.

### 3. `MumbleBridge._end_session()` — the rule without a helper

```python
try:
    session.close()
except Exception:
    log.exception("mumble: failed to close the TX session — slot released anyway")
finally:
    self._tx_slot.release()
```

`except Exception` and not `BaseException`: a cancellation must still propagate, and the `finally`
frees the slot on that path too. Logged rather than silently suppressed — it is the only record that
the transmitter may not have been un-keyed.

This was worse than a slot leak. `_end_session` is called from **six** sites, four of them exception
handlers and one a `finally`; a raise there also replaced the exception being handled and took the
relay loop down with it — the exact fault ADR 0153 closed for `feed`. `DStarBridge` already wrapped
its close in `contextlib.suppress`; **the Mumble side diverging is the finding.**

### Why not one helper for all five

One *rule* covers all five sites. One *helper* covers the three that were textually identical, which
is where a fourth wrong copy would be written. Sites 4 and 5 below need no change at all, and
`_end_session` has no claim to manage — only a release that must survive a raising teardown. Forcing
a context manager over a two-line teardown would be abstraction for its own sake.

## The audit — 5 production `try_acquire` sites, not 3

| # | site | verdict |
|---|---|---|
| 1 | `api/app.py` `audio_tx` — RF `tx_slot` | **fixed** — claim and accept were separated by a suspension point |
| 2 | `api/app.py` `audio_mumble_tx` — `mumble_talk_slot` | **fixed** — identical shape |
| 3 | `api/app.py` `audio_dstar_tx` — `dstar_talk_slot` | **fixed** — identical shape |
| 4 | `dstar/bridge.py:721` `_open_rx_session` | **AUDITED CLEAN — no change.** A plain `def` with no `await` anywhere in the method; the claim is immediately followed by the sync `TxSession(...)` it guards. It *records* the result in `_rx_slot_held` rather than gating on it (ADR 0091), so it never releases a slot it does not own. |
| 5 | `link/bridge.py:449` `_mumble_to_rf` | **AUDITED CLEAN — no change.** The claim is already *inside* the `try:` opened at `:425` and is followed by the sync `TxSession(...)`. The only one of the five whose shape was already right. |

Sites 4 and 5 are named here so a later reader does not have to re-derive that they are safe.

Other acquire-then-await shapes in the request path, checked and left alone: `/events`,
`/audio/mumble/rx` and `/audio/dstar/rx` all `accept()` **before** subscribing and enter their
`try:` on the next statement — the correct ordering, and the direct counter-example to sites 1-3.
`RxPump.begin_receive`, `RadioHolder.rebuild`, the uvk5 wire lock and the DV Dongle IO lock are all
context-managed or have no suspension point in the window. `POST /controller` sets
`controller_active` before an `await`, but nothing wedges permanently on either failure order.

## Acceptance

**Red on `master`, recorded per site.** Driven as bare coroutines off `app.routes`, because
`TestClient` cannot make `accept()` fail — the reason `test_api.py:310-312` already gives for
`_CancelWS`.

| site | red |
|---|---|
| `/audio/tx` | 3 failed |
| `/audio/mumble/tx` | 3 failed |
| `/audio/dstar/tx` | 3 failed |
| `/audio/rx` demand pin | 1 failed |
| `_end_session` | 1 failed |
| teardown-raises, `/audio/tx` + `/audio/dstar/tx` | 2 failed |

Whole suite against master's implementation: **13 failed, 2231 passed, 5 skipped** — every failure
in `tests/test_slot_unwind.py`, all behavioural, none a collection error. The 14th test in that file
(shutdown cancellation mid-talk) **passes on master**: it is a regression pin for the new shape, not
a red, and is reported as such rather than counted as one.

Green: `uv run pytest` **2244 passed, 5 skipped** (baseline 2229/5); `npx vitest run` **14 files,
138 tests** (baseline 14/138, unchanged — no UI in this cycle).

**Smoked against a real uvicorn** with `__main__`'s real `basicConfig`, over real websockets: a
talker keys, a second is refused `{"status":"busy"}` with the refusal line reaching the log, a third
keys after the holder lets go, and the RX demand goes 0 → 1 → 0. Tests do not prove a log line
survives logging configuration; ADR 0165 lost a startup line exactly that way.

**Two things only the guards and the smoke caught.** `test_relay_subscribers` — the Part 97 source
scan that pins every RF-hub subscriber by name — failed the moment `audio_hub.subscribe()` moved
into `talkers.py`, which is precisely its job; the entry moved with it, and the parameter is named
`audio_hub` **deliberately**, because that guard matches source text and a rename would have hidden
the browser Listen path from it. And the smoke surfaced a **pre-existing** stall, verified identical
on unmodified `master`: `/audio/rx` parks on `queue.get()` and only learns its client left on the
next `send_bytes`, so with no audio flowing it holds the subscription and the demand indefinitely.
Documented in the handler as a mock/edge case; on a live station a *silent receiver* — ADR 0166's
dead reader, for one — reaches the same state. Recorded as a finding, not fixed here.

## Carried finding — a stranded slot is invisible

Not fixed in this cycle, and named precisely so the successor knows what is missing:

- **`TxSlot` carries nothing.** No holder, no acquire timestamp, no counter, no `on_change` hook —
  nothing to report even if something wanted to. The sibling `RadioArbiter` *has* an `on_change` and
  uses it.
- **`/status` has no occupancy field for any of the three slots.** `transmitting` and `busy` are
  decoys — PTT state and squelch state, both `False` during a leak.
- **No event and no ledger record** on acquire, release or refusal. `"busy"` is a
  **reserved-but-never-published** event type (`api/events.py`): the surface was **designed and
  never finished**, so a successor is completing something rather than inventing it.
- **The UI asserts the opposite of the truth.** `web/src/useTxAudio.js:51` hard-codes *"Radio busy —
  another operator is transmitting."* During a leak nobody is transmitting and a dead socket owns
  the flag; `useTxAudio.js:249` then suppresses retry, discouraging the one action that might reveal
  it. That sentence has to be fixed alongside the plumbing.
- The only indirect signal is `dropped_slot_busy` in `tx_stats()` — RF slot only, needs a live
  Mumble link with inbound audio, and is a bare integer with no timestamp.

**The one exception taken now:** a single `logger.info("talk slot %s refused: already held", name)`
inside the helper, so a leak is greppable in the journal instead of invisible. Not a counter, not a
`/status` field, not a UI change. `basicConfig(level=INFO)` in `__main__.py` is what makes it reach
journald, and the smoke confirms it does.

The counter-shaped remedy of [ADR 0085](0085-mumble-rx-guard.md) is the shape that applies.

## Consequences

- Every future talk endpoint gets the guarantee by using `talker_slot`, and `TxSlot`'s docstring now
  says so — the minimality of that object is deliberate, and the guarantee lives in the context
  manager, not in the flag.
- The three handler bodies are one indent deeper. That is the cost of making the release a scope
  exit, and it is the whole point: there is no longer a line you can fail to reach.
- `_end_session` now swallows-and-logs a failing `close()` instead of propagating it. A relay loop no
  longer dies because the transmitter could not be un-keyed; the traceback is in the journal.
- The RF-hub subscriber list in `test_relay_subscribers` names `api/talkers.py` instead of
  `api/app.py`. Same subscriber, same side of ADR 0162's asymmetry, different file.

## Out of scope

The UV-K5 firmware fork; the squelch-composition successor; any change to arbiter or `TxSession`
semantics beyond the unwind; the observability remedy above (counter, `/status` field, and the UI
sentence), carried whole; and the pre-existing `/audio/rx` park-on-empty-queue stall.
