# 0097 — Reflector→RF liveness follows decoded content, with a hard per-over ceiling

Status: Accepted

## Context

The first bring-up of the module-A D-STAR crossband on the live AIOC radio (a supervised
dummy-load bench test, 2026-07-20) **stuck-keyed the transmitter**: an operator keyed a short
D-STAR HT over into the reflector (XLX999 A), and the FM side keyed up and then **held PTT on dead
air well beyond the over**. It was safed by stopping radio-server. (A *second*, separate defect —
the decoded audio was garbage — is tracked on its own; this ADR is only the safety gap that let a
non-terminating stream hold the key.)

Root cause, confirmed by tracing `radio_server/dstar/bridge.py`: a reflector→RF over is closed by
exactly three signals, and **all three are defeated by a continuous inbound stream**:

1. the DSRP **end-bit** (`dsrp.py`) — never arrives if the stream never terminates cleanly;
2. the **queue-drain hang** (`_reflector_to_rf`, `asyncio.wait_for(..., tx_hang)`) — never fires
   while DATA frames keep arriving faster than `tx_hang`;
3. the **idle watchdog** (`_rx_watchdog` + `TxSession.idle_elapsed`, ADR 0092) — never fires
   because `idle_elapsed` is measured from `TxSession._last_active`, which `feed` re-stamps on
   **every frame that arrives**.

The load-bearing flaw is #3: **the over's liveness was measured by frame *arrival*, not by decoded
audio *content*.** A stream of frames that decode to dead air (or garbage) still re-stamped the
deadline on every frame, so the watchdog saw a permanently "healthy" over and left PTT up until the
only remaining cap — the global 180 s TOT (ADR 0090). The prior stuck-key ADRs (0090/0092) were
built against a decode that *stops* feeding (a parked/wedged decode); none of them cap a decode that
*keeps* feeding non-speech.

The RF→reflector direction already had the right idea: ADR 0091's `AudioLevelGate` (`_rf_gate`) keys
the reflector only on real RF audio, never on receiver hiss. The reflector→RF direction had no such
content gate — it fed every decoded frame unconditionally.

## Decision

Two layered changes to `DStarBridge`, both DI-injected so a bare bridge in tests keeps the old shape.

1. **Content-gated liveness (`rx_gate`).** Apply an `AudioLevelGate` (the same class, a fresh
   independent instance) to the **decoded** reflector→RF audio in `_play_ambe`. Only frames the gate
   passes `feed` the keying session; a below-threshold (dead-air / garbage-silence) frame is dropped
   *before* `feed`, so it neither keys a fresh over nor refreshes the idle deadline. The existing
   `_rx_watchdog` then idles a silent over out in ~`tx_hang`. The gate's hang bridges word gaps, so
   real speech is never clipped; the decode is still published to the browser monitor **before** the
   gate, so the listen path is unchanged and an operator can still hear a garbled decode to diagnose
   it. This is the direct fix: liveness now tracks content, symmetric with `_rf_gate`.

2. **Hard per-over ceiling (`max_over`, config `dstar.max_over_seconds`, default 60 s).** Armed at
   rx-session key-up and — unlike the idle deadline — **never reset per frame**, so it bounds a
   *continuous* over regardless of content. `_rx_watchdog` (and the `_reflector_to_rf` loop-top
   check) force-end the over once `_over_expired()` is true. This is the content-independent backstop
   for the case the level gate can't catch — a continuous *loud* garbage stream — and it sits below
   the global 180 s TOT (ADR 0090) so a runaway over closes here first. `0` disables it.

Both values are marked tunable (guardrail 1): the `audio.vad_*` thresholds the `rx_gate` reuses must
be verified against *decoded* D-STAR audio on the bench (real speech must open the gate), and
`dstar.max_over_seconds` must be long enough not to clip a legitimate long over yet short enough that
junk can't sit on the air.

## Consequences

- The 2026-07-20 dead-air stuck-key closes in ~`tx_hang` instead of riding the 180 s TOT; a loud
  non-terminating stream closes at `dstar.max_over_seconds`. The transmitter can no longer be held on
  the air by an inbound stream that never ends cleanly, independent of what the decode produces.
- New regression tests in `tests/test_dstar_bridge.py` drive a `LevelVocoder` at chosen decoded
  amplitudes: a continuous dead-air stream never keys; real-speech-level audio keys and closes on the
  end-bit; a continuous loud stream (idle continuously refreshed, gate held open) is still force-ended
  by the ceiling.
- **This does not re-enable the crossband.** Module-A D-STAR stays disabled on the live radios; the
  garbage-decode defect is still open, and re-enable remains gated on a joint dummy-load re-proof with
  the operator watching (the standing rule from ADRs 0091–0094). This ADR only ensures that when the
  crossband *is* re-proved, a non-terminating over cannot strand the key.
- Scope: reflector→RF only. The browser listen path, the RF→reflector direction, and the DVAP modules
  are untouched.
