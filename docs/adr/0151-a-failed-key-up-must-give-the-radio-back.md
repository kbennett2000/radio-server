# 0151 — A failed key-up must give the radio back

Status: Accepted

Repairs the upstream consequence of ADR
[0150](0150-the-host-learns-to-listen-to-am.md)'s key-up refusal, in the TX session of ADR
[0016](0016-tx-audio-ingest.md) and the half-duplex arbiter of ADR
[0017](0017-duplex-arbiter.md), applying the atomic-undo shape ADR
[0093](0093-aioc-panic-unkey.md) put on the backend one layer up. Host only — no firmware
change.

## Context

ADR 0150 taught `AiocBaofeng._key_on` to refuse: built without `ENABLE_TX_WHEN_AM`, the radio sets
`VFO_STATE_TX_DISABLE` for any non-FM modulation and will not key its own PTT path — which is the
path this backend keys, through the AIOC's DTR line. So `ptt(True)` now **raises** rather than
asserting the line into silence. That was the first time a key-up on the deployed station could
routinely fail, and it exposed a bug sitting directly upstream of it.

**Reproduced before anything was planned or written:**

```
feed raised: demodulating AM, refuses its own PTT path
after close -> arbiter.transmitting = True | mode = transmitting
session.keyed = False
```

`TxSession.feed` claimed the arbiter and *then* keyed:

```python
self._arbiter.acquire_tx()
self._radio.ptt(True)      # raises
self._keyed = True         # never reached
```

`close()` guards its release behind `if self._keyed:`, so the release never ran. The arbiter sat in
`TRANSMITTING` **for the life of the process**, and it is a shared latch: the RX pump stops pulling
`receive()` (no RX audio, no DTMF decode, no controller), the scan engine stops advancing, the
holder starts firing a spurious `ptt(False)` on every teardown, and the D-STAR bridge counts every
reflector frame as a conflict against a keyer that no longer exists. One refused key-up took the
whole station off the air until restart.

Reachable from all three keying call sites: the browser `/audio/tx` talker, the Mumble→RF relay, and
the reflector→RF drain loop.

### The audit found more than the shape it was sent to find

Two other things surfaced while checking the acquire-then-act pattern and the ordering it depends on.

**A ninth station-keying call site.** All five `StationId` methods end in `radio.transmit()`, which
self-keys on `AiocBaofeng`, so all of them raise in AM. Eight are in `controller/engine.py`
(`identify`, `check`, `sign_off`, and five announcement `transmit`s). The ninth is
`services/dispatch.py` — **every DTMF voice service** — which is the most-travelled keying path in
normal operation and the one a controller-shaped search never looks at. Every one of them was
swallowed silently: `Controller.step` is driven by the RX pump, which wraps it in a bare
`except Exception: pass`.

**A Part 97 defect with no guarding involved.** `StationId.transmit` advanced `_last_id` *before*
the radio call. A failed announcement-with-ID therefore **marked the ID as sent** and suppressed the
next one for the full ten-minute interval. `identify`, `check` and `sign_off` were already ordered
correctly; only `transmit` was wrong.

## Decision

**1. Key-up in `TxSession` is all-or-nothing.**

Two windows, because there are two distinct states to undo. Before the line is up, only the arbiter
was claimed — release it and re-raise. After the line is up we are genuinely keyed, so `close()` is
the unwind: it drops PTT, releases the arbiter, and fires the paired `on_key(False)` the ledger
needs to close its `tx_key_up` record. Reusing `close()` rather than open-coding the undo keeps one
teardown path instead of a second, subtly different one.

`BaseException`, not `Exception`, so a cancellation arriving at key-up still gives the radio back.
The release is itself suppressed, so it can never mask the fault the caller needs to see.

A failed **key-up ID** unwinds rather than being swallowed. `key_up_id` returns audio only when an
ID is *due*, so carrying on would put an un-ID'd transmission on the air — refusing the over is the
Part 97-safe answer, and it is the only unwind that reaches the D-STAR bridge, whose per-frame feed
has no surrounding `finally`.

**2. The state invariant lives in `StationId`; the guard lives in the controller.**

Split deliberately, because they are different concerns and each should be owned exactly once.

`StationId` fixes its own ordering: `_last_id` and `_transmitted_this_session` advance only after
the radio call returns. **No caller can repair state it cannot see**, so a call-site-only guard
would have left the ten-minute suppression in place. Under Part 97 under-identifying is the
violation and over-identifying is merely untidy, so every failure here now leaves the ID still due.

The guard is `Controller._keying`, because that is where the event channel is. `StationId` has no
`on_event` seam and adding one would duplicate `Controller.on_event`. The helper returns the call's
**own** value on success, so `check`/`sign_off` keep reporting whether an ID actually went out and
the `id`/`session_close` records stay honest; `False` on failure reads the same way. The dispatcher
reports through `DispatchResult`, whose `transmitted` flag already gates the `command` record — it
gains an `error` field so the two ways of being `False` (nothing was meant to go out; the radio
refused) stop being the same event to every caller above.

**3. `strict` re-raises on the two API entry points, and this asymmetry is the point.**

The guard exists because the over-the-air path **cannot report a fault to anyone** — the pump
swallows everything. The two API entry points can: `trigger` and `open_session` raise onto the
app-wide `RadioUnavailable` handler ADR [0143](0143-a-tune-must-know-it-has-a-session.md) built for
exactly this, and the operator gets a 503 carrying the backend's own sentence. Swallowing there
would hand someone who clicked "play ID" a 200 for a transmission that never happened —
reintroducing the fault class this repo has spent four cycles closing. The `tx_failed` event is
emitted **before** the re-raise on both paths, so the record is never the thing traded away.

**4. `tx_failed` is a ledger record, not just a live event.**

A new controller phase carrying `what` and `reason`. It needed no API wiring: the hub adapter's
catch-all branch already carries an unrecognised phase as `Event(type="session", ...)`, and the web
UI preserves its session indicator for any phase it does not recognise — both verified, and the
first is now pinned by a test so a later whitelist cannot silently drop it.

It reaches the **JSONL ledger** because that is the durable Part 97 artifact. An operator auditing
whether the station identified can see the gap and its reason, instead of inferring it from records
that are simply absent — and absence is not evidence of anything, which is the whole lesson of ADR
[0140](0140-the-first-key-is-always-lost.md)/[0143](0143-a-tune-must-know-it-has-a-session.md).

Named for the **condition, not the caller**: five of the nine sites are announcements rather than
IDs, but `StationId.transmit` prepends the ID when due, so a failed announcement is often also a
failed ID. `what` discriminates.

## Acceptance

**Three fail-first runs, each against a real bug rather than cited.**

- **The reported bug** — the arbiter-leak tests against unmodified `master`: **8 failures across 4
  files**. The unit case, the reuse case, the key-up-ID unwind, the `/audio/tx` socket, the Mumble
  relay, the D-STAR drain loop, and the RX-resume proof. The RX-resume test *hung* rather than
  failing at first, which is the blast radius stated as plainly as it can be: the pump never wakes
  up again. It is now bounded so it reports instead of hanging.
- **The `_last_id` ordering** — red on `master` by construction, independent of any code written
  this cycle: `assert [b'hello again'] == [b'<id:AE9S>hello again']`. The other two new ordering
  tests stayed green, which is what says `identify`/`sign_off` were already correct and only
  `transmit` was not.
- **A guard that swallows instead of recording** (`except Exception: return False`): **7 failures** —
  and the discriminating result is what survived. `test_step_survives_a_radio_that_cannot_key`
  failed on its *record* assertion **after** both survival assertions passed, and three tests stayed
  **green**: the ones asserting `id_sent is False`, `signed_off is False`, and
  `transmitted is False`. Every return-value honesty test passes under a completely silent guard.
  Recorded here so nobody later cites them as proof the failure is visible. Only the `tx_failed`
  tests and the ledger test are.

The original in-memory reproduction now reports `arbiter.transmitting = False | mode = idle`.

**`uv run pytest`: 1961 passed, 5 skipped** (1939/5 before — 22 new tests).

## Consequences

- A refused key-up costs one over instead of the station. RX, DTMF decode, the controller and scan
  all survive it.
- A station that cannot identify now says so — a log line, a live event, and a ledger record with
  the reason. Previously it produced nothing at all, in the one place guardrail 5 makes mandatory.
- `POST /services/{digit}` and `POST /auth/session` return **503 with the reason** where a refusing
  radio previously produced an unhandled 500. `POST /ptt` and `POST /transmit` are unchanged — the
  app-wide handler already covered them (confirmed, not modified).
- `DispatchResult` gains a field and `CONTROLLER_PHASES` gains a member. Both are additive; the
  ledger's unknown-phase branch already returned `None`, so no existing record changed shape.
- An ID that fails is retried on the next over rather than suppressed for ten minutes. A session
  whose only over failed now correctly owes no closing ID, because it never transmitted.

## Out of scope

- **Hardening the two bridge relay loops — a dependency, not a note.** A raising key-up still
  propagates out of `session.feed()`, and `link/bridge.py` and `dstar/bridge.py` catch only
  `AudioFormatMismatch` (plus `ArbiterStateError` in the D-STAR case), so the Mumble→RF task and the
  reflector→RF drain loop die on the first refusal. That is pre-existing and orthogonal — this fix
  neither causes nor worsens it — and it cannot fire yet, because F7 is unflashed and AM is not
  selectable. **The bridges must be hardened before AM is selectable by an operator, i.e. before the
  UI cycle.** The shape is already in the repo: a counted, logged drop surfaced in `/link/status`,
  as ADR [0085](0085-mumble-rx-guard.md)'s `rx_guarded` and the D-STAR bridge's own
  `rx_arbiter_conflicts` counter both do.
- **Three more acquire-then-act leaks, reported and not fixed.** `tx_slot.try_acquire()` before
  `await websocket.accept()` (a client vanishing mid-handshake holds the single-talker slot forever,
  so every later talker gets 1013 "busy" — the most likely of the three to bite); `audio_hub
  .subscribe()` before `await _acquire_rx()`; and `RxPump`'s `begin_receive()` outside its own `try`,
  correct today only because `_start_gate` swallows everything internally. Same shape, different
  resources, each needing its own reproduction.
- **The web UI.** Still the next cycle. No control is added and none is greyed differently; the
  `tx_failed` event has no renderer yet and reaches the operator through the ledger and the log.
- **Any hardware claim.** The radio is not flashed with F7. Nothing here has keyed a transmitter;
  everything is proven against fakes that model the refusal.
