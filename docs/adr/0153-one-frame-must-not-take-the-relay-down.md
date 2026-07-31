# 0153 — One frame must not take the relay down

Status: Accepted

Closes the dependency recorded by ADR
[0151](0151-a-failed-key-up-must-give-the-radio-back.md) and carried forward by ADR
[0152](0152-a-login-is-not-its-announcement.md): **the gate before AM can be operator-selectable.**
Hardens the Mumble relay of ADR [0041](0041-mumble-link.md)/[0045](0045-link-audio-correctness.md)
and the D-STAR reflector→RF loop of ADR [0106](0106-dstar-stream-liveness.md). Host only — no
firmware change, no UI.

## Context

ADR [0150](0150-the-host-learns-to-listen-to-am.md) taught the AIOC backend to refuse a key-up in
AM. ADR 0151 made that refusal unwind cleanly *inside* `TxSession` — the arbiter is released, the
line never asserted. But the refusal still **propagates out of** `session.feed()`, and neither
bridge relay loop survived it:

- `link/bridge.py` fed inside a `try` catching **only** `AudioFormatMismatch`. Anything else escaped
  the `while True` and **killed the Mumble→RF task** for the life of the link.
- `dstar/bridge.py` was guarded for `AudioFormatMismatch` and `ArbiterStateError` and nothing else.
  A raising key-up **killed the drain loop — the whole crossband — over one frame.**

The argument for this cycle was already written in the repo, in the `ArbiterStateError` catch:

> The shared arbiter is held by another keyer (or stuck from an earlier fault). An unhandled raise
> here would kill the drain loop — the whole crossband — over one contended frame. Drop the frame
> and count it instead.

Same sentence, different exception. That catch is the shape this ADR copies rather than a new one to
invent.

### Two things found while reading, worth stating separately

**`_end_session` was already correct** — the question the brief asked. It does not assume a keyed
session: `TxSession.close()` is documented "idempotent and a no-op if the stream never keyed" and
`TxSlot.release()` is idempotent, so on the refused path it releases exactly what the over took and
closes a never-keyed session without emitting a spurious `ptt(False)`. Unchanged.

**A counter that already lied.** `link/bridge.py` incremented `_overs_keyed` *before* attempting the
key-up, while `tx_stats` documents it as "how many transmissions the bridge keyed". A refused key-up
therefore counted an over that never happened — and shipping a refusal counter beside it would have
made the pair actively misleading (`overs_keyed: 10, dropped_key_refused: 10` for zero overs). Moved
to fire on the first *successful* feed. Called out rather than folded in silently, because it is a
behaviour change to an existing counter.

## Decision

**1. Two counters, never one.**

The AM refusal is a **standing condition**: it recurs on every frame until the operator changes the
demodulator. `OSError`/`PortAudioError` from a yanked serial cable or a dead audio device is a
**fault**: rare, and each occurrence matters. A shared counter lets the standing condition bury the
fault — 40 000 refusals and one unplugged cable read as 40 001 of the same thing, and the one that
needed a human never surfaces.

So: `dropped_key_refused` / `relay_errors` on the Mumble bridge, `rx_key_refusals` /
`rx_relay_errors` on the D-STAR bridge, each named to its file's existing convention.

**2. Narrow-only was rejected, and not for diff size.**

Catching just `RadioUnavailable` would be **a partial fix, not a smaller one**. The cycle's stated
purpose is that a raising key-up must not kill the relay loop, and a yanked serial cable kills it
identically — it simply arrives as a different exception type. `dstar/bridge.py` already carries a
broad `except Exception: log.exception("dstar: AMBE decode failed")` around the decode for exactly
this reason, so the pattern is established in the same file.

**3. Exception order is load-bearing, and is pinned by tests.**

The broad clause goes **last**. `AudioFormatMismatch` and `ArbiterStateError` keep their existing
handling and their existing counters, and tests assert that a malformed frame and a contended
arbiter each still take their named path with **both new counters at zero**. Without that pin a
later reorder would quietly absorb the ADR [0102](0102-aioc-playback-pacer.md) drop-and-retry
behaviour into "relay error" — and lose the self-healing it exists for, since the backstop ends the
over instead of retrying per frame.

**4. Logging differs by kind, deliberately.**

The refusal log is **throttled** (`== 1 or % 250 == 0`, the arbiter-conflict shape) because a
standing condition at frame rate would flood the journal. The backstop logs **`log.exception` every
time, unthrottled**: it is rare by construction, and the traceback is the only thing that identifies
an unexpected fault.

**5. Both counters surface in status**, the `rx_guarded` shape (ADR
[0085](0085-mumble-rx-guard.md)) — no new mechanism. Both bridges already expose a `tx_stats()` that
reaches `GET /link/status` and `GET /dstar/status`, and `api.md` now says which counter means *fix
it at the radio* and which means *investigate the hardware*.

**6. D-STAR ends the over; the arbiter path still retries. The asymmetry is intentional.**

Arbiter contention is **transient** — the other keyer releases in milliseconds and `feed` re-keys on
the next frame, so holding the over open is right. A key refusal is a standing **station**
condition: holding the session, the talker slot and the `mode: "rx"` latch open for an over that
physically cannot happen blocks the browser talker and reports the bridge as mid-over until the
watchdog reaps it. So the refusal and backstop paths call `_end_rx("tx-refused")` /
`_end_rx("relay-error")`. Both are deliberately **outside** the `("end", "teardown")` set, so they
behave as mid-stream cuts and keep `_last_rx_stream_id`: the reflector stream is still flowing and
must be able to re-latch the moment the radio can key again. Recovery still works — at over
granularity instead of frame granularity.

## Acceptance

**Fail-first, both bridges, run against `origin/master`: 7 failures.** The tests assert **loop
survival**, not that a counter moved — a frame injected *after* the radio recovers must reach RF:

- `tests/test_link_bridge.py` — **4 red.** Refused key-up then recovery; the unexpected-fault
  backstop; the ordering pin; and `overs_keyed`, which came back **`1` for zero overs** and so
  independently confirmed the counter defect above.
- `tests/test_dstar_bridge.py` — **3 red.** The same three shapes through the reflector→RF path.

**One failure was mine, and it is worth recording.** The first fix attempt called `log.warning` in
`link/bridge.py`, which **has no module logger** — the `NameError` raised *inside* the `except`
block, which a sibling `except` cannot catch, so it killed the loop exactly as the original bug did.
The tests caught it immediately. A module logger was added.

**`uv run pytest`: 1973 passed, 5 skipped** (1965/5 before — 8 new tests).

## Consequences

- A radio in AM no longer takes down the Mumble link or the crossband. The relay drops the frame,
  ends the over, counts it, and keeps running; the next over keys normally once the demodulator is
  changed.
- **AM is now safe to make operator-selectable** — this was the recorded gate, and it is closed.
- Two new counters per bridge in `GET /link/status` and `GET /dstar/status`. Additive; one existing
  exact-dict assertion was updated.
- `overs_keyed` reports one fewer over than before on any link where a key-up was refused — because
  those overs never happened.
- `_end_rx` gains two cause strings in its ADR 0106 cut ledger.

## Out of scope

- **The three websocket talker-slot leaks** — `app.py:1873` (`tx_slot`), `:1995`
  (`mumble_talk_slot`), `:2095` (`talk_slot`). All three do `try_acquire()` then
  `await websocket.accept()` **outside** the `try/finally` that releases, so a client vanishing
  mid-handshake holds the slot for the life of the process and every later talker gets `1013
  "busy"`. Verified this cycle and carried forward as **the named next finding**: a different
  mechanism in a different file, deserving its own fail-first.
- **The web UI.** This cycle unblocks it; it is not this cycle.
- **The `ArbiterStateError` drop-and-retry behaviour.** Appended after, never modified.
- **Any hardware claim.** The radio is not flashed with F7. Every refusal here is modelled by fakes;
  nothing has been measured on the air.
