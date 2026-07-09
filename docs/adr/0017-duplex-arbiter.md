# 0017 — Duplex arbiter: half-duplex TX-priority exclusion of RX and scan

Status: Accepted

## Context

The voice relay now streams both directions: `/audio/rx` fans received audio *out* (ADR 0014, with
the ADR 0015 squelch) and `/audio/tx` accepts audio *in* and keys the transmitter (ADR 0016).
Nothing coordinates the two. A half-duplex radio — both target rigs (TM-V71A, UV-5R) — **physically
cannot receive and transmit at once**: keying the transmitter blinds the receiver. But today the
`RxPump` keeps calling `radio.receive()` while a `TxSession` holds PTT asserted, and a live
`ScanEngine.tick()` keeps tuning and polling the same radio a transmission is using. On the mock
these are independent readers that happen not to collide; on real hardware they are a direct
conflict — reading a blinded receiver and fighting the transmitter for the tuner.

This cycle defines and enforces the policy, mock-only: **TX takes the radio. When a `TxSession`
keys up, the RX pump suspends and any live scan pauses; when TX drops, both resume.** This is the
real-world constraint, not a preference. The remaining work after this is entirely hardware (the
two real backends' actual audio I/O and on-bench timing), so this is the last pure-software cycle.

## Decision

- **A new pure-leaf `radio_server/arbiter/` package** holding one small object, `RadioArbiter`, that
  owns "who has the radio right now" as `RadioMode` — `idle` / `receiving` / `transmitting`. It
  imports **nothing** from the rest of `radio_server` (only stdlib), so every consumer's dependency
  arrow stays clean and acyclic: `tx -> arbiter`, `rx -> arbiter`, `scan -> arbiter`,
  `api -> arbiter`. One instance is created at the composition root (`create_app`) and **injected**
  into the TX session (the writer) and the RX pump + scan engine (the readers) — the same
  DI-with-safe-default discipline the RX `gate` seam uses.

- **Two independent latches, a derived mode with TX priority.** The arbiter holds `_transmitting`
  (set by TX) and `_receiving` (set by the RX pump), and derives `mode` as
  `transmitting > receiving > idle`. This is the load-bearing modeling choice: RX *wanting* the
  radio and TX *holding* it are independent facts, and the physical exclusion (they can't both
  *happen*) is the derived mode — TX wins — enforced by the readers checking `transmitting` and
  standing down. The alternative, a single mode field with explicit **preempt/restore**
  bookkeeping (TX saves "was receiving", restores it on release), is strictly more state to get
  wrong. With two latches, releasing TX needs no memory: `_receiving` is still set, so `mode`
  falls back to `receiving` on its own. RX listeners are never dropped — they stay subscribed; only
  frame *delivery* pauses, which is exactly "the receiver is deaf right now," not "the listener
  hung up."

- **Coherence guard (reject incoherent states).** `acquire_tx()` raises `ArbiterStateError` if
  already transmitting — one transmitter, one talker, you cannot key it twice. `release_tx()` is
  deliberately **lenient** (idempotent, a no-op when not transmitting) so it mirrors
  `TxSession.close()`, which may run on a stream that never keyed. The asymmetry is intentional:
  the acquire side is where an incoherent state is *created*, so that is where the guard belongs;
  the release side must be safe to call unconditionally in a `finally`.

- **TX is the writer, hooked at its two existing keying points** (`radio_server/tx/session.py`).
  `TxSession.__init__` gains an injected `arbiter` (defaulting to a private `RadioArbiter()`, the
  same default-shape as its `clock`, so a standalone session is behaviorally unchanged). `feed()`
  calls `arbiter.acquire_tx()` **before** `ptt(True)` inside the `if not self._keyed` guard — after
  framing validation, so a bad frame never keys or claims; `close()` calls `arbiter.release_tx()`
  after `ptt(False)` inside the `if self._keyed` guard, so a never-keyed close is a no-op. Under the
  ADR-0016 `TxSlot` single-talker guard, with the shared arbiter starting idle/receiving,
  `acquire_tx` never actually raises in the app — the guard is belt-and-suspenders, exercised only
  by the arbiter unit test.

- **The RX pump is a reader that stands down while keyed** (`radio_server/rx/pump.py`). `RxPump`
  gains an injected `arbiter` (private idle default → `transmitting` always False → cycle-13/14
  behavior preserved). `run()` calls `begin_receive()` on entry and `end_receive()` in its
  `finally` (an honest `receiving` state for the pump's lifetime), and its loop now checks
  `arbiter.transmitting` **before pulling**: while keyed it does not call `receive()` at all — it
  sleeps a poll and continues. Not pulling (rather than pulling-and-dropping) is the load-bearing
  choice: on shared hardware you cannot read a blinded receiver. This ownership check sits *beside*
  the empty-frame transport skip — it is a transport/ownership filter, orthogonal to the
  frame-content `gate` (ADR 0015), so it is **not** a gate implementation.

- **A live scan pauses in place** (`radio_server/scan/engine.py`). `ScanEngine` (and
  `build_scan_engine`) gain an injected `arbiter` (private idle default → never pauses → existing
  scan tests unchanged). `tick()` early-returns the current state while `arbiter.transmitting` —
  before any tune/poll/advance — so it neither fights the tuner nor advances the scan. **Resume
  needs only the flag:** every positional field (`_state`, `_i`, `_current_freq`, `_tuned_at`,
  `_dwell_deadline`) already lives on the instance and survives across ticks, so the first tick
  after TX releases continues from exactly where it paused; no saved-position state is added. The
  synchronous `POST /scan` `sweep()` path is untouched — it runs to completion in one sync call and
  cannot interleave with a TX key-up.

- **Wiring is confined to `create_app`.** One `arbiter = RadioArbiter()` next to `tx_slot`, stored
  as `app.state.arbiter`, injected into the pump and every per-connection `TxSession`. `build_app`
  needs no change (nothing env-driven). **Scan note:** no live path currently attaches a tick-scan
  to the controller (only `POST /scan` runs `sweep()`), so no app.py scan change is load-bearing
  this cycle; the arbiter flows through `ScanEngine`/`build_scan_engine` so a future
  controller-attached tick-scan already respects it, and the interaction is proven at the
  `ScanEngine` unit level.

## Consequences

- **The half-duplex conflict now has an enforced policy.** A TX key-up takes the radio; the RX pump
  stops pulling and delivering, a live scan freezes on its held channel, and both resume when PTT
  drops — proven with the real `RxPump` (the counting radio is never polled while keyed, then the
  same subscriber queue receives all frames in order) and the real `ScanEngine` (no tune, no
  advance, frozen state while transmitting; continues from the held channel after release).
- **RX listeners are not dropped.** The suspend pauses *delivery*, not the subscription — the test
  asserts `subscriber_count == 1` across the whole suspend. A UI's `/audio/rx` socket stays open
  and simply goes quiet while the operator transmits.
- **The arbiter is honest and coherent.** `mode` reports all three states through the real pump
  (`idle → receiving → transmitting → receiving → idle`), and the double-key guard is a checked
  property (`ArbiterStateError`), not a hope; `release_tx` idempotency keeps it paired safely with
  the idempotent `TxSession.close()`.
- **Every prior test passes unchanged.** The injected-arbiter defaults (a private idle arbiter when
  none is supplied) mean `RxPump`, `TxSession`, and `ScanEngine` all behave exactly as before when
  constructed standalone — `test_rx_audio.py`, `test_tx_audio.py`, and `test_scan.py` are untouched.
- **Untouched, deliberately:** `backends/mock.py`, `audio/format.py`, `activity/`, `controller/`,
  `auth/`, `events.py`, and the `POST /scan` sweep path. `MockRadio` already sufficed — the tune
  and receive spies live in the test (the ADR-0016 `_PttSpyRadio` pattern).
- Full suite: **293 passed, 4 skipped** (was 283; +10 — 6 arbiter unit, 2 RX-pump, 1 scan, 1
  end-to-end; the 4 skips remain the multimon + piper hardware/model gates).
- **Deferred — the `/events` "suspended" marker.** The instruction offered it as an alternative to
  "listeners just stop getting frames," and the required behavior (stay connected, delivery
  pauses/resumes, socket never dropped) is fully delivered without it. Wiring a new event would
  mean inventing event semantics and an arbiter→hub callback with a status/ordering question
  (`status().transmitting` tracks `ptt`, not the arbiter). Left as a cheap future observability add.
- **Logical-vs-timing boundary (guardrail 1).** The arbiter models the *logical* exclusion only.
  The real PTT tail and TX-to-RX turnaround — how long after PTT drops the receiver is actually
  usable again — are bench facts, tuned during hardware bring-up; the mock proves the *policy*
  (who owns the radio, who stands down), never the *milliseconds*.
- **Numbering / branch note:** ADR 0017 by cycle order, cut from the cycle-15 merge point
  (`a4c8c7e`) at **283 passed, 4 skipped**. It adds the `arbiter` package and touches `tx`, `rx`,
  `scan`, and the `api` composition root.
- **Still ahead before RF:** only the hardware bring-up phase — the two real backends
  (`SignaLinkV71` audio-triggered keying, `AiocBaofeng` explicit RTS) with real transmit/receive
  and on-bench PTT-tail / turnaround tuning — plus Opus/compression. This was the last pure-software
  cycle.
