# 0045 — The TX time limiter: bounding a stuck-on transmission

Status: Accepted

## Context

Direction three of the link (`link.receive()` → `radio.transmit()`, a later cycle) puts a **remote peer
in control of the licensee's transmitter**. Before that lands, the policy that *bounds* it is built and
reviewed on its own — a pure leaf with no wiring, so the bound can be scrutinized in isolation.

`tx.idle_timeout` (ADR 0016) already handles **one** runaway mode: the inbound stream stops, so after a
few seconds of silence PTT drops. It does **not** handle the other, and worse, one — the reason this
cycle exists: **continuous audio.** A peer with a stuck VOX, a reflector spraying noise, a bridge looped
back on itself — none of that is silence, so `idle_timeout` never fires and the radio keys
**indefinitely.** That is:

- a **cooked finals stage** on a UV-5R (these radios are not rated for a 100 % duty cycle);
- **your own local channel jammed by you** — a carrier nobody can talk over;
- a **Part 97 problem**: the station-ID scheduler cannot acquire the radio while TX holds it (ADR 0017),
  so a transmission long enough to *need* an ID is exactly the one that can't get one.

**Silence and stuck-on are different failures. `idle_timeout` covers the first; this covers the second.**
They compose — a real deployment wants both — but neither subsumes the other.

This cycle is a **pure leaf, no wiring** — the same discipline as `arbiter/` (ADR 0017) and `activity/`
(ADR 0015): a policy object that *answers questions* and keys nothing.

## Decision

- **A new pure-leaf `radio_server/txlimit/` package** holding one small object, `TxLimiter`, and its
  `TxLimitState` (`idle` / `keyed` / `cooloff`). It imports **nothing** from the rest of `radio_server`
  — and, because every time-dependent method takes an explicit `now: float`, it imports no `time`
  either — so every future consumer's dependency arrow stays clean and acyclic (`tx -> txlimit`,
  `api -> txlimit`), and the whole policy is exercisable from a fake clock (passed floats) with no radio
  and no I/O. An AST test enforces the import isolation, mirroring the one on `link/`.

- **A policy object, not a mechanism.** The limiter answers questions; it never touches a radio. The
  caller (a later cycle) reports keying edges and consults the oracle:
  - `key_down(now)` — a keyed period began.
  - `key_up(now)` — it ended **normally** (the peer stopped). No cooloff.
  - `force_unkey(now)` — it ended because the **limit forced it**. Enters cooloff.
  - `expired(now)` → has this key-down been held for at least `max_seconds`.
  - `may_key(now)` → `False` during the cooloff (below), `True` otherwise.

- **The cooloff is the point.** After a `force_unkey`, `may_key` is `False` for `cooloff_seconds`.
  Without it a stuck peer whose transmission you just cut simply re-keys on the next frame, and you have
  built a **square-wave generator** — key/unkey/key/unkey at the limit boundary — instead of a limiter.
  The cooloff turns "cut the runaway" into "cut it *and keep it cut* long enough to matter."

- **State is derived, never stored** — the arbiter's modeling choice (ADR 0017). Two timestamps,
  `_keyed_since` and `_cooloff_until`, and a derived `mode`: keyed if `_keyed_since` is set, else cooloff
  while `now < _cooloff_until`, else idle. There is no `_state` field to fall out of sync with the
  clock: cooloff simply *stops being true* once time passes it, and `may_key` reflects that for free —
  exactly as the arbiter's `mode` falls back to `receiving` on its own when TX releases, with no
  preempt/restore bookkeeping.

- **`on_change` mirrors `RadioArbiter.on_change`** (optional, injected, keyword-only), fired **only when
  the derived state actually changes** on a reported edge: `key_down` → `KEYED`, `key_up` → `IDLE`,
  `force_unkey` → `COOLOFF`. A no-op call (`key_down` while already keyed, `key_up`/`force_unkey` while
  not keyed) fires nothing. The callback is trusted non-raising, so it is not guarded here — the same
  contract the arbiter states. The composition root (a later cycle) will wire it to publish a mode
  transition to the ledger, exactly as the arbiter's is wired.

- **The one deliberate asymmetry with the arbiter: cooloff expiry is not an `on_change` event.** The
  `COOLOFF` → `IDLE` transition is caused by the *passage of time*, and a pure leaf has no clock tick of
  its own — so it is derived and surfaced by `may_key` returning `True` again, not announced. This
  mirrors the arbiter, which likewise only fires on latch flips and never on the passage of time. If a
  future consumer wants a "cooloff ended" event it can compare `state(now)` across its own poll; the
  leaf does not invent a timer to manufacture one.

- **Clock-agnostic by explicit `now` (not a stored clock).** Every time-dependent method takes
  `now: float`, supplied by the caller from *its* injected monotonic clock — the `controller.step(now,
  …)` shape, where the driver owns the clock and the policy is pure. The alternative (a stored
  `clock=time.monotonic`, the `arbiter`-sibling convention with arg-less `expired()`) was not taken:
  passing `now` keeps `txlimit` a *true* leaf that imports no `time` at all, and makes fake-clock tests
  a matter of passing floats — no monkeypatching, no `FakeClock` plumbing.

- **Bounds are config, validated at load, not in the leaf.** `link.max_tx_seconds` (default **180.0**)
  and `link.tx_cooloff` (default **10.0**) join the `[link]` group, using the existing
  `coerce_positive_float` coercer so a `<= 0` or non-numeric value **fails loud at load, naming the key**
  — the same fail-loud instinct as the `id_interval` ceiling. The defaults are **marked, VERIFY ON
  HARDWARE (guardrail 1)**: a max key-down and a cooloff are thermal + courtesy facts about a *specific*
  radio, not known numbers. The leaf itself does no numeric validation — it trusts already-resolved
  positive inputs, the same split as `create_link` trusting resolved config.

## Consequences

- **The stuck-on runaway now has a reviewed policy object**, independent of any transmitter: expiry at
  `max_seconds`, a `cooloff_seconds` re-key refusal, and honest `on_change` transitions — all proven
  from a fake clock with no radio.
- **The composition, named here so no one optimizes the limiter away.** The forced unkey is what
  **creates the gap the station-ID scheduler needs.** Because TX holding the radio blocks RX/ID
  acquisition (ADR 0017), forcing an unkey during a long link transmission is *precisely* what lets the
  ID scheduler acquire the radio and identify. So the limiter is **not only hardware safety — it is what
  makes Part 97 ID reachable on a long link TX.** That is a happy consequence of the arbiter's design,
  and it is written down now, before the wiring cycle, so a later "simplification" that drops the forced
  unkey is understood to also break lawful identification, not just thermal protection.
- **Nothing is wired.** No `TxSession`, arbiter, link, or PTT change; browser Talk behavior is
  untouched; no `load_*` helper (loaders live in the config/api layer and belong to the wiring cycle).
  The two new settings are inert until a consumer reads them.
- **Config ripple only:** the settings **canary** count rises (52 → 54), `radio.toml.example` is
  regenerated from the schema, and the per-group default + fail-loud-rejection tests gain rows for the
  two keys.
- **Logical-vs-timing boundary (guardrail 1).** The limiter models the *policy* (when a transmission is
  too long, how long to stay off) — never the *milliseconds*. The real max-key duration and cooloff are
  bench facts tuned during hardware bring-up; the mock proves the policy, the radio proves the numbers.
- **Still ahead:** the wiring cycle that drives `TxLimiter` from the link's `radio.transmit()` path —
  `key_down`/`expired`/`force_unkey` around the arbiter and PTT, `may_key` gating re-key — and, before
  that direction, the separate cycle relaxing the TOTP gate. Then the real network backend.
