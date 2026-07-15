# 0035 — RX activity in the ledger: persisting squelch open/close to the durable record

Status: Accepted

## Context

The first product feature — "is this repeater actually dead?" — needs a durable history of
**when the receiver was busy**. That signal already exists in flight but is thrown away at the
last step.

ADR 0031's single capture reader drives an `RxPump` whose gate fires an activity edge on every
squelch open/close. The composition root already adapts that edge to a hub event:

```python
# radio_server/api/app.py
on_activity=lambda active: hub.publish(Event(type="rx", data={"active": active})),
```

So live `/events` subscribers already see `rx` open/close, and on the audio-only Baofeng this
gate is the *only* real RX-activity signal (`status().busy` is always `False` there — there is no
CAT busy line). But the durable side is missing: `EventLog._record_for`
(`radio_server/eventlog/log.py`) — the passive station ledger of ADR 0018 — has no `rx` branch, so
the event falls through to `return None` and **nothing is persisted**. The ledger, the durable
source every future feature will query, has no RX in it.

This is exactly ADR 0019's deferred-instrumentation pattern **inverted**. There, the mapper
branches were written ahead of their producers and were dead until a later cycle added the
`hub.publish` sites. Here the producer has shipped and it is the *mapper* that is missing. The fix
is symmetric: add the one branch, and the already-flowing event lights up in the ledger.

## Decision

- **`EventLog._record_for` gains an `rx` branch, mirroring the existing `ptt` case exactly.** The
  `rx` event carries `data={"active": bool}` (where `ptt` carries `{"on": bool}`):

  - `active=True` → stash the open timestamp in a new `self._rx_open_at` field and emit
    `{"ts", "type": "rx_open"}`.
  - `active=False` → emit `{"ts", "type": "rx_close", "duration": <since the paired open>}`,
    with `duration=None` when the close is unpaired (no open seen), then clear the stash.

  This is the same paired-timestamp instance state as `_keyup_at`/`tx_key_down`, the same injected
  clock (`now = self._clock()`), and the same **whitelist discipline** — only the `active` key is
  read; `event.data` is never spread, so no upstream field can leak into the ledger.

- **The invariants that make the ledger safe to hang off the live path are untouched.** `eventlog/`
  stays a leaf: stdlib only, no new imports, no dependency on any other `radio_server` layer
  (ADR 0018). Failure isolation holds — `EventLog.handle` still catches everything, so a ledger
  fault can never reach the audio path (ADR 0018). The hub keeps publishing exactly what it
  published before; this cycle changes only what the ledger *does* with it.

## The edge-only constraint (documented, not worked around)

`rx_open`/`rx_close` fire **only on a gate edge**. With `audio.squelch = "off"` (the default) the
gate is `pass_through_gate`, which returns `True` for every frame and never closes — so **no RX
records are produced at all**. This is the same hazard the recording path already documents for
segmentation (`docs/operating.md`, "Squelch-off warning"): without a real squelch there is no
close edge. Per-transmission RX history therefore requires `audio.squelch = "audio"` (software VAD,
the Baofeng path) or `"cat"` (the V71 hardware busy line, ADR 0015). This is noted in
`docs/operating.md` alongside the operating-log record list. A startup warning specific to RX
logging is intentionally out of scope for this cycle.

## Consequences

- **The ledger now carries RX.** A run against `MockRadio` with `audio.squelch = "audio"` produces
  paired `rx_open`/`rx_close` records with plausible busy durations in `radio-server.jsonl`. An
  end-to-end wiring test drives a loud→silent frame sequence through a real `AudioLevelGate` and
  `create_app` with a live `EventLog(JsonlSink(...))`, and asserts the paired records reach the
  file with no secret material — the same composition-root proof ADR 0019 used for the rest of the
  taxonomy.
- **The taxonomy grows by two record types** (`rx_open`, `rx_close`); the mapper gains one stateful
  branch (its second, after `tx_key_up`/`tx_key_down`). No producer changed; the API composition
  root is untouched.
- **Deferred, on purpose (out of scope here):** any aggregation, querying, or API/UI surface over
  the RX history; a startup warning for the squelch-off case; anything on the Link/network side.
  This cycle closes only the persistence gap.
- **Cross-references:** ADR 0015 (activity detection / squelch modes), ADR 0018 (the event log),
  ADR 0019 (deferred instrumentation — the pattern this inverts), ADR 0031 (the single capture
  reader that produces the edge).
