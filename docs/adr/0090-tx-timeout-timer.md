# 0090 — Transmitter time-out timer: a hard cap on continuous key, on every path

Status: Accepted

## Context

Folding D-STAR onto the live radios (ADR 0089) surfaced a dangerous class of bug: with a reflector
linked, the reflector→RF crossband **got stuck keying the transmitter** and sat on the air transmitting
dead air. Neither `POST /dstar/unlink` nor `bridge.stop()` dropped PTT — only killing the process did.
(The crossband-close root cause and its own fix are ADR 0091; this ADR is the independent safety net.)

The deeper problem is that **radio-server had no hard cap on how long the transmitter can be keyed.**
Every existing safeguard is a *silence/idle* timeout that resets on each non-silent frame:

- `tx.idle_timeout` (`TxSession.idle_elapsed`) — drops PTT after N seconds of **no inbound frame**.
- The D-STAR / Mumble `tx_hang` — closes an over after N seconds of **silence**.
- `RadioHolder.stop()` — drops PTT only at teardown, and only if the arbiter says we're transmitting.

None of them bounds a **continuous** transmission. A held mic, a crossband over that never closes, or a
decode loop parked in an executor with PTT asserted transmits indefinitely. That is a Part-97 problem (an
unattended stuck carrier) and a hardware-safety problem (a transmitter keyed for minutes).

Every keying path funnels through exactly one object — the active `Radio` — via exactly two methods:
`ptt(on)` and `transmit(audio)`. Browser `/audio/tx`, the D-STAR and Mumble bridges, DTMF services,
station ID, and the REST `/ptt` & `/transmit` routes all call only those. `build_radio`
(`api/holder.py`) is the single composition root every one of them gets that `Radio` from, on the initial
build **and** every live backend swap (ADR 0076).

## Decision

Add **`TotRadio`** (`radio_server/tx/tot.py`) — a `Radio`-protocol decorator wrapping the active backend
in `build_radio`, so no keying path can hold PTT longer than `tx.tot` seconds. It is the last-resort net,
independent of and beneath the crossband-close fix.

- **One chokepoint, every path.** `TotRadio` delegates the whole `Radio` surface (and off-protocol
  `close`/CAT tuning) to the wrapped backend via `__getattr__`; only `ptt` and `transmit` are
  intercepted. Wrapping at `build_radio` covers both real backends (and the future SignaLink/V71)
  automatically, on build and on swap.
- **A hard cap, not an idle reset.** The watchdog is armed **once on key-up** (an explicit `ptt(True)`,
  or a one-shot `transmit()` while unkeyed) and is **never reset per frame** — so it bounds a continuous
  transmission, the exact gap the idle timeouts leave open. Disarmed on `ptt(False)` / one-shot return.
- **Fires on its own timer thread.** The force-unkey runs from a `threading.Timer`, not an asyncio task
  or the caller. This is the load-bearing property: when the reflector→RF loop is parked in
  `run_in_executor(vocoder.decode, …)` the caller *cannot* drop PTT, but an independent thread still
  calls `wrapped.ptt(False)`. On expiry it force-drops PTT and logs loudly (`log.error`).
- **A streaming time-out latches TOT-locked.** After a forced unkey of a *held* key, further
  `transmit()` calls are dropped until an explicit `ptt(False)`/`ptt(True)` re-key — because the
  audio-triggered SignaLink self-keys off the audio itself, so dropping the line alone won't silence a
  source that keeps feeding frames. A **one-shot** `transmit()` that overran does **not** latch (each
  clip is independent and short; a single stuck clip must not wedge station ID).
- **Config `tx.tot`** (seconds; default **180.0**, the classic repeater time-out value; `0` disables),
  next to `tx.idle_timeout` (`DEFAULT_TX_TOT` + `load_tx_tot` in `tx/session.py`; advanced key; canary
  74→75; golden `radio.toml.example` regenerated). Marked verify-against-hardware (guardrail 1).

### Why the `Radio` wrapper, not the arbiter or the backends

The arbiter (`arbiter/state.py`) is a logical latch with no radio handle and does not cover the direct
`/ptt` path. Per-backend timers would duplicate the logic in AIOC + kv4p + the unbuilt V71. The
`build_radio` wrapper is the one place all keying and all backends pass through — a per-backend timer
underneath it would be optional defense-in-depth, not the primary control.

## Consequences

- **No path can sit on the air past `tx.tot`.** Browser TX, D-STAR/Mumble crossband, services, station
  ID, REST keying — all capped, regardless of software state above the radio, including a fully wedged
  caller thread. This alone would have ended the ADR 0089 incident (the transmitter would have dropped
  at `tx.tot` instead of needing a process kill).
- **A backstop, not the cure.** 180 s of stuck dead air is still bad; the crossband over must also close
  promptly on its own (ADR 0091). The TOT is the floor beneath every keyer, not a licence to leak keys.
- **Transparent.** The wrapper delegates everything else untouched; the full suite (1218 passed) is
  unchanged by the wrap. Backend swaps re-wrap through `build_radio`.
- **Verified on fakes, never by live-keying.** `TotRadio` tests drive expiry through an injected fake
  timer (`fire_latest()`), asserting `ptt_log == [True, False]`, the streaming latch + its clear on
  re-key, the one-shot no-latch, `tx.tot=0` passthrough, delegation, and `close` disarm — deterministic,
  no real waits, no transmitter. `tx.tot` config resolves and `build_radio` wraps.
- **Follow-up.** Surfacing the forced unkey as a WS/ledger event (an `on_timeout` hook already exists on
  `TotRadio`, unwired here since `build_radio` has no hub) — a small UI polish, deferred to keep this
  PR to the safety mechanism.

Cross-refs: ADR 0091 (the D-STAR crossband teardown/close fix this backstops), ADR 0089 (the folded
crossband whose stuck-key incident prompted this), ADR 0076 (the live backend swap that re-enters
`build_radio`), ADR 0017 (the half-duplex arbiter this sits above).
