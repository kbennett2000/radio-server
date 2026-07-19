# 0092 — The parked decode can no longer hold PTT: an independent watchdog, a re-key guard, an unconditional unkey

Status: Accepted

## Context

ADR 0091 was meant to guarantee "the over always closes, PTT always drops." It was verified on
fakes and merged (PR #145). The first **dummy-load** reflector→RF test on the real DV Dongle then
stuck-keyed the transmitter anyway — `mode=rx`, `rx_frames` frozen, PTT asserted for ~40 s until it
was force-dropped. Two hardware realities the fake-based tests could not reproduce were behind it:

1. **The idle watchdog was inline in a loop that parks.** ADR 0091 put the RX PTT watchdog at the
   **top of the `_reflector_to_rf` loop**. But that loop `await`s each AMBE decode in the single-
   worker vocoder executor (`run_in_executor`), and a *wedged* DV Dongle decode parks the loop
   there for far longer than `tx_hang` — the FakeVocoder decodes instantly, so no test ever parked
   it. **While the loop is parked, its own loop-top check never runs**, so the over never closes and
   the transmitter sits keyed until the ADR 0090 TOT (180 s of dead air — unacceptable). Note that
   `asyncio.wait_for` around `run_in_executor` does **not** fix this: on timeout `wait_for` cancels
   the future and then *awaits it to finish cancelling*, and an executor future that is already
   running is uncancellable — so `wait_for` blocks until the wedged thread returns anyway.

2. **`close()` could skip the unkey, and a resumed decode could re-key.** `unlink` returned in ~1 s
   (ADR 0091's teardown-deadlock fix held) but PTT stayed asserted. `TxSession.close()` transmits a
   Part-97 sign-off **station ID before** `ptt(False)`; on a wedged backend that `transmit` raises,
   and — because `_force_unkey` wraps `close()` in `contextlib.suppress` — the exception was
   swallowed and the `ptt(False)` after it was skipped. Separately, when the watchdog *did* close an
   over, a decode still parked in the executor could resume and feed its late frame into the just-
   closed session — **re-keying** the transmitter (a stuck-key by another name).

MockRadio's `ptt`/`transmit` never raise and its decode never blocks, so both were invisible to the
suite. This ADR is the empirical follow-up ADR 0091's re-enable gate demanded ("prove it on a dummy
load first") — the dummy load did its job.

## Decision

Four changes; no new config, no schema/canary change.

- **The PTT-safety watchdog runs as its own task, off the parking loop.** New `_rx_watchdog` (started
  beside `_reflector_to_rf` when `tx_to_rf`) only ever `await`s `asyncio.sleep`, never the executor —
  so the event loop keeps scheduling it while `_reflector_to_rf` is parked in a wedged decode. It
  closes an idle keyed over (`TxSession.idle_elapsed()`) directly, dropping PTT within
  ~`tx_hang` + one poll interval instead of at the TOT. The loop-top check in `_reflector_to_rf`
  stays as a cheap belt-and-suspenders for the non-parked case.
- **`_play_ambe` never re-keys a closed over.** After the (possibly slow) decode returns, it drops
  the frame if `self._mode != "rx"` — so a decode that resumes *after* the watchdog or a teardown
  closed the over cannot feed it and re-assert PTT.
- **`TxSession.close()` always unkeys.** The key-down sign-off ID `transmit` is now wrapped so a raise
  can never skip the `ptt(False)` / arbiter release beneath it. The ID is best-effort (same posture
  the recorder already had); dropping PTT is the load-bearing safety work. This hardens **every**
  streaming keyer (browser TX and the Mumble bridge too), not just D-STAR.
- **`_force_unkey` drops PTT directly.** After closing the session it calls `radio.ptt(False)`
  unconditionally — the last, path-independent guarantee that a teardown leaves the transmitter
  unkeyed even if the session-close path is somehow defeated.

## Consequences

- **A wedged/parked decode can no longer hold the transmitter keyed.** The independent watchdog drops
  PTT off the event loop regardless of what the decode loop is doing; the re-key guard stops a
  resumed decode from re-asserting it; the hardened `close()` + direct `_force_unkey` guarantee the
  unkey lands. The ADR 0090 TOT remains the absolute backstop beneath all of it.
- **All streaming keyers get the always-unkey fix**, since it lives in `TxSession.close()`.
- **Verified on fakes that now model the hardware failure**, never by live-keying: a `_BlockingVocoder`
  whose decode parks in the executor proves the over closes **on its own** (no teardown) while the
  decode is still parked; a late decode released after close proves no re-key; a spy radio that
  raises on the sign-off `transmit` proves `close()` still unkeys. `uv run pytest` green.
- **Re-enable stays gated on a real dummy-load proof.** D-STAR remains disabled on the live radios
  (`[dstar] callsign=""`) until this merges and a dummy-load reflector→RF test shows the over close
  and PTT drop on the actual dongle — the very test that surfaced these bugs.

Cross-refs: ADR 0091 (whose inline watchdog / suppressed close this corrects), ADR 0090 (the TOT
backstop), ADR 0089 (the folded crossband), ADR 0087 (the bridge + half-duplex latch), ADR 0041
(the streaming station-ID seam hardened here).
