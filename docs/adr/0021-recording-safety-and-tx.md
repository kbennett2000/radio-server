# 0021 — Recording safety rails (max-duration roll, squelch-off warning, half-duplex split) + TX recording

Status: Accepted

## Context

Cycle 20 (ADR 0020) added a passive `Recorder` that writes **received** audio to timestamped WAV
files, tapped into `RxPump`. It shipped three behaviors "documented, not fixed," and deferred TX
recording. This cycle closes the footguns and folds in the deferred piece. Still **mock-only**
(guardrail 1): scripted `MockRadio` frames + `FakeClock` drive everything; no hardware.

The three ADR-0020 footguns:

1. **Unbounded file under `RADIO_SQUELCH=off`.** The default pass-through gate never returns
   `False`, so there is no gate-close edge to bound a segment — all RX accumulates into one WAV that
   only finalizes on pump stop. Nothing caps a single file's size.
2. **Half-duplex gap concatenation.** When streaming TX keys up mid-RX, the arbiter (ADR 0017)
   stands the pump down, but the open RX segment is *not* finalized — audio resumes into the *same*
   file after the keyed gap, so one "recording" spans a receive it was never part of.
3. **TX recording was deferred.** Only received audio was captured; transmitted audio was not.

## Decision

### Max-duration segment roll (the load-bearing safety rail)

`Recorder` gains an **always-on** duration cap `max_seconds` (default `3600`, from
`RADIO_RECORD_MAX_SECONDS`). `write()` checks the injected clock **before** the lazy-open: if the
open segment has run for `>= max_seconds`, it `end_segment()`s and the existing lazy-open rolls a
fresh file — so the triggering frame starts the *new* segment. Because the check reuses the clock
the Recorder already holds for filename stamps, it is `FakeClock`-deterministic. The cap is
**not disable-able**: `load_record_max_seconds` fails loud on `0`/negative rather than treating it as
"unbounded" — a bounded WAV *regardless of squelch mode* is the whole point, so an escape hatch would
reintroduce footgun #1. `_open_segment` stamps `_segment_started`; the `_wav is not None` guard makes
a stale start-time after an `_abort()` harmless (the next write lazy-opens and re-stamps). No reset
bookkeeping.

### Squelch-off + record-on startup warning

`RADIO_RECORD=on` with `RADIO_SQUELCH=off` is **not** an error — the max-duration roll makes it safe
— but it is surprising, so `build_app` logs a one-time `WARNING` (the repo's first use of `logging`)
that RX segmentation is time-based (the roll), not activity-based, in this config, and points at
`RADIO_SQUELCH=audio|cat` for one-WAV-per-transmission. Emitted in the `build_app` body (runs once
per process → naturally one-time); `caplog`-testable via propagation with no handler config.

### Half-duplex segment split

`RxPump.run`'s existing `if self._arbiter.transmitting:` branch — which stands the pump down while
TX holds the radio — now also calls `self._recorder.end_segment()` (guarded) before it sleeps. So an
RX segment is finalized at the keyed gap and the next live frame on resume lazy-opens a fresh file; a
recording reflects one continuous receive. `end_segment` is idempotent, so calling it every
transmitting iteration is a cheap no-op after the first — no rising-edge bookkeeping. This is scoped
to `arbiter.transmitting`, which only **streaming** TX (`TxSession.feed` → `acquire_tx`) sets; REST
`/ptt` keys the radio directly and never touches the arbiter, so it neither pauses nor splits the
pump (the pre-existing half-duplex behavior, unchanged).

### TX recording (the deferred piece)

The same `Recorder` records transmitted audio, distinguished only by a **`tx-` filename prefix**
(the previously-hardcoded `rx-` becomes a ctor `prefix` param). `TxSession` gains a `recorder`
injection (a local `TxRecorder` Protocol + `null_recorder` default, mirroring `rx.pump.RxRecorder`,
so `tx` never imports `recording` — the arrow stays `tx -> {audio, backends}`). `feed()` writes each
transmitted frame; `close()` finalizes the segment. The first fed frame after key-up lazy-opens the
`tx-` file; key-down/idle finalizes. Opt-in via **`RADIO_RECORD_TX`** (default off, *independent* of
`RADIO_RECORD`); it shares `RADIO_RECORD_PATH` and inherits `RADIO_RECORD_MAX_SECONDS`, but ignores
`RADIO_RECORD_MODE` (squelch gating is an RX concept; TX has no gate). RX and TX are separate
`Recorder` instances — separate sequence counters — writing to the same directory, disambiguated by
prefix. Both default to the `time.time` clock, so `tx-`/`rx-` filename stamps timestamp-align with
the ledger's `tx_key_up`/`tx_key_down` records (same wall clock; the small call-site skew is the same
one RX already has).

**Failure isolation (the sharpest point).** `feed` is the load-bearing keying state machine
(guardrail 2). Both recorder calls are guarded (catch-and-drop), *and* the `close()` finalize is
placed **after** the keying/arbiter-release work and inside `if self._keyed`: the `/audio/tx`
endpoint's `finally` runs `session.close()` **then** `tx_slot.release()`, so an exception escaping
`close()` would skip the slot release and **permanently wedge the single transmitter**. Guarding +
ordering guarantee a disk fault can never break keying or leak the slot. The shared `tx_recorder` is
only ever fed by one talker at a time — `TxSlot.try_acquire()` refuses a second concurrent client
*before* its `TxSession` is built — so concurrent isolation comes from the slot, not the recorder;
sequential talkers share the instance and get a continuous `tx-000001`, `tx-000002`… counter, each
`close()` finalizing its own segment.

## Consequences

- **No single WAV grows without bound in any squelch mode.** Even `RADIO_SQUELCH=off` produces
  bounded, rolling files; a real squelch still gives one WAV per transmission, now additionally
  capped at `RADIO_RECORD_MAX_SECONDS`.
- **A recording reflects one continuous receive.** A TX interruption finalizes the RX segment and
  resume starts a fresh file.
- **Transmitted audio is recordable** to `tx-` WAVs, distinguishable from `rx-` and timestamp-aligned
  with the event log.
- **Leaf acyclicity preserved.** `recording/` still imports only `..audio`; `tx/session.py` gained a
  local `TxRecorder` Protocol + `null_recorder` (mirroring `rx/pump.py`) and never imports
  `recording/`; every meeting point is `api/app.py`. `create_app`/`Recorder`/`TxSession` gained only
  keyword-default params, so all prior callers/tests are unchanged.
- **First use of `logging`** in the codebase (a module logger in `api/app.py`), idiomatic and
  handler-free.
- **Deferred, on purpose (unchanged):** Opus/compression; retention/cleanup; a playback/download API
  (the web UI sequence); full-capture (pre-gate) mode (seam only); decoupling recording from the
  demand-driven pump. Next: the web UI.
- **Numbering / branch note:** ADR 0021 by cycle order, cut from the cycle-19 merge point
  (`46d83c6`, ADR 0020) at 352 passed / 4 skipped. No new dependency (`wave`/`logging` are stdlib).
