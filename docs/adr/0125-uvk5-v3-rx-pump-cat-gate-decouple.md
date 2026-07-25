# 0125 — UV-K5 V3 RX pump: decouple the CAT squelch read from the audio reader

Status: Accepted

## Context

The uvk5 stack passed end-to-end on the dev PC across ADR 0120–0124. Moved to the LAN server
(`ubuntuserver`) it degraded into four reported symptoms: (1) browser RX audio choppy, (2) browser
TX not working, (3) DTMF not decoded, (4) services not announcing. Per the kv4p lesson — **no fixes
before measurements** — this cycle is a measurement-first triage. The four symptoms split into two
independent chains: **Chain A — RX (1)+(3)**, one capture stream feeds both the browser hub and the
DTMF controller, so sample loss on that leg chops the browser *and* shreds DTMF; **Chain B — TX
(2)+(4)**, the log shows the radio *keyed* (`tx_key_up`→`tx_key_down` 4.019 s) and a service
*dispatched* (`command_dispatched service=time`) with audio reaching `TxSession.feed`, yet nothing
was heard — which is the never-settled ADR 0112/0113 acceptance gate ("does register-keyed TX carry
the AIOC-injected K1 mic audio"), not a deployment regression.

**This ADR is Chain A only.** Chain B stays open pending the bench `--tx-tone` run (a TTY-gated
`CONFIRM`), tracked in HANDOFF and the ADR 0112/0113 line.

### What the bench measured (numbers, not adjectives)

A byte-rate probe on `/audio/rx` and a faithful reproduction of `RxPump`'s read loop (driven through
the production `backend_kwargs` + `create_radio` path), run live on the server:

| Measurement | Result |
|---|---|
| `/audio/rx` WS, 30 s, localhost | **0 bytes** — not choppy, *nothing* |
| Pump repro, **CAT gate on** (today) | **14.3 %** duty (7.1 reads/s vs 50); gate p50 **59.6 ms**, p99 2000 ms; gate ate **71 %** of wall-clock; `busy=True` on **0 of 143** frames |
| Pump repro, **gate off** | **98.5 %** duty (49.3 reads/s) |
| `radio.status()` standalone, no audio | **p50 100.2 ms**, pinned to `transport._READ_TIMEOUT = 0.1` |

**Root cause.** `RxPump` calls `self._gate(frame)` once per 20 ms audio frame on the single capture
reader. ADR 0121 made `cat` the uvk5 default squelch mode **one day earlier** (PR #179), so that gate
is now `CatBusyGate`, whose `__call__` is `radio.status().busy` → a live `ReadRegisters(0x67)` serial
round-trip that lands on the ~100 ms `_READ_TIMEOUT` quantum. A 100 ms blocking read demanded every
20 ms is 5× over budget: the pump reads ~1 frame in 7, the other ~86 % overruns the 60 ms ALSA ring
(2880 samples), and because `busy` never returned True, `hub.publish` never ran — the browser got
**zero** audio. DTMF is fed the *raw* pre-gate frame (deliberate, `pump.py`) but only 7 frames/s ever
get read, so tones arrive shredded. **One root cause, both RX symptoms.**

**Why the dev PC "passed":** every ADR 0120–0124 validation ran through `doctor`, which uses its own
tight capture loop and never touches `RxPump` or the gate. Nothing had run a live server on uvk5
since `cat` became the default.

**A second, independent Chain-A fault (ops, not code).** `doctor --rx-level` on the server read
peak sample **32752/32767 (−0.0 dBFS)** on idle noise — the receiver audio was **clipping**, the
volume knob turned too far up (host mixer already maxed, ADR 0124). Clipping harmonics trip the
native DTMF decoder's 2nd-harmonic/dominance gates. Backed the knob down to peak **28032 (−1.4
dBFS)** and `doctor --dtmf` then decoded `1234#1234#` cleanly — proving the decoder and level are
fine once not clipping, and that doctor (bypassing the pump) decodes while the live server cannot.
This is an ops fix (recorded in `server-notes.md`), not code; it is noted here because it is the
*other* half of why DTMF failed, and both faults had to clear.

### The fix shape was validated on hardware before writing it

The first hypothesis — cache the CAT verdict on a poll interval, still inline in the loop — was
**refuted on the bench**: inline caching tops out at **84.4 %** duty (cache 0.5 s), because every
cache-miss iteration still does a 100 ms blocking read that stalls the single reader past the 60 ms
ring. 84 % is a slower failure, not a fix — the ring still overflows in ~0.4 s. The decisive
measurement:

| Configuration | Pump duty | Serial in the audio loop |
|---|---|---|
| Today — CAT gate per frame | **10.5 %** | 100 ms every frame |
| Inline cache 0.5 s | 84.4 % | 100 ms every cache-miss (still overflows) |
| **Decoupled: bg-thread poll 0.2 s + sleep-if-idle** | **100.2 %** | **zero** |
| Decoupled + sleep-always | 98.7 % | zero |

**You cannot do a 100 ms blocking serial read on the thread that must drain a 60 ms ring, ever.** The
read must move off that thread entirely.

## Decision 1 — `PolledGate`: run the expensive gate on a background thread, cache its verdict

A new `PolledGate` wrapper in `activity/gate.py` decorates an inner signal gate (only `CatBusyGate`
needs it — `AudioLevelGate` is pure-CPU, `pass_through` is trivial). A background daemon thread calls
`inner(frame)` every `interval` seconds (default **0.2 s** — `DEFAULT_CAT_POLL_INTERVAL`) and stores
the boolean verdict; `__call__` returns the cached bool with **zero serial** in the audio path.
`detects_signal` mirrors the inner gate, so the pump's signal-aware activity edge is unchanged. The
wrapper exposes `.inner` so the composition root and tests can reach the wrapped `CatBusyGate`.

`build_rx_gate`'s `cat` branch now returns `PolledGate(CatBusyGate(radio))`. The `off` and `audio`
branches are untouched. This is the one observable type change: `build_rx_gate(...)` for a CAT
backend now returns a `PolledGate` whose `.inner` is the `CatBusyGate` — the holder's "re-point the
CAT gate at the freshly built radio on a swap" invariant (ADR 0121) still holds, since a rebuild
constructs a fresh `PolledGate` over a fresh `CatBusyGate` bound to the new radio.

**Interval trade-off.** 0.2 s means the gate's open/close decision lags the channel by up to 0.2 s
(bench: 2.7 serial polls/s, `busy` stays fresh). Voice tolerates a 0.2 s squelch tail; DTMF is
unaffected because the controller is fed the raw frame *before* the gate. 0.2 s over the 0.5 s that
scored highest duty is the deliberate freshness-vs-throughput pick — 0.2 s already reaches full duty.

**Thread-safety.** The `status().busy` read now runs on a background thread concurrently with the
event-loop thread's `receive()`. `receive()` touches only the ALSA card; `status()` only the serial
transport — different devices. The transport is already built for concurrent request/reply: a
dedicated reader thread deframes replies and dispatches them to per-request waiters via a
`Condition`, and `_read_register`'s match predicate is register-specific (`m.register == reg`), so
two in-flight reads cannot cross-deliver. During TX, `status()` short-circuits on `_keyed` without
touching serial, so the poller never contends with keying writes.

## Decision 2 — `RxPump`: own the gate's lifecycle, and sleep only when idle

**Lifecycle.** `RxPump.run()` calls `gate.start()` before the loop and `gate.stop()` in its `finally`,
guarded and *only if the gate exposes them* (a generic `getattr` check — `pass_through`/`AudioLevelGate`
have neither, so their behaviour is byte-identical). This maps the poller thread onto the pump's
demand-driven lifetime: the thread runs exactly while the pump runs, and `PolledGate.start()/stop()`
are idempotent and **restartable**, because the same pump (hence the same gate) is start/stopped many
times as listeners come and go, and a rebuild stops the old pump before building a fresh gate.

**Sleep-if-idle.** `pump.py`'s loop slept `self._poll` (20 ms) after *every* iteration, including a
successful read — 50 frames/s × 20 ms = 1000 ms of sleep per second, i.e. the loop ran at 100 % of
budget with **zero headroom** (why even gate-off measured only 98.5 %). The docstring always claimed
`DEFAULT_RX_POLL` "only paces the idle loop"; the code never matched it. The loop now `await`s a
**zero-delay** `asyncio.sleep(0)` on a frame with samples (yields to the event loop for the WS
writers / `/events` — fairness the synchronous bench repro can't see, but production needs) and the
full `self._poll` only when the read returned empty. Bench: this restores the last ~1.5 % of headroom
(98.7 → 100.2 %) and matches the documented intent.

## Decision 3 — make ALSA overruns visible (the instrument that would have caught this)

The whole failure was invisible: the uvk5 backend **discarded** the `_overflowed` xrun flag from
`capture.read()` (`radio.py`), so the journal showed "zero xruns" precisely because nothing logged
them. `receive()` now logs a **rate-limited** (≤1/5 s) `WARNING` naming the overrun and pointing at
this ADR when the capture reports an overflow. The samples read on an xrun are still valid audio, so
the frame is returned unchanged — this adds visibility only, no behavioural change to the audio path.

## Consequences

- **Touched:** `activity/gate.py` (`PolledGate`, `DEFAULT_CAT_POLL_INTERVAL`, `build_rx_gate` cat
  branch, export), `rx/pump.py` (gate start/stop lifecycle; sleep-if-idle), `backends/uvk5/radio.py`
  (rate-limited xrun warning; discarded flag now read).
- **Tests:** new `tests/test_polled_gate.py` (poll-once caches, background thread refreshes and
  `__call__` does no inner call, start/stop lifecycle + restart, exceptions swallowed,
  `detects_signal` mirrors inner, `.inner` exposed); pump tests for the start/stop-if-present
  lifecycle and sleep-if-idle pacing; a uvk5 receive() xrun-log test. The two existing
  `isinstance(gate, CatBusyGate)` assertions (`test_activity.py`, `test_squelch_per_backend.py`)
  updated to unwrap `.inner` for the CAT path — the documented type change. `uv run pytest` green.
- **No config change**, no new deps, no `radio.toml.example` regen — the poll interval is a marked
  module default (guardrail 1), not a setting, until the bench says otherwise.
- **Behavioural:** a CAT-squelch uvk5/V71 box now streams RX audio to the browser and feeds DTMF the
  full capture; the gate's busy decision lags the channel by ≤0.2 s (imperceptible for voice,
  irrelevant to DTMF). Non-CAT backends (`off`/`audio`, i.e. Baofeng, kv4p pass-through, mock) are
  entirely unaffected — no poller thread, byte-identical loop.

## Out of scope

- **Chain B (TX audio, symptoms 2+4)** — the ADR 0112/0113 acceptance gate (does register-keyed TX
  carry the AIOC-injected K1 mic audio in XVFO mode). Blocked on the bench `--tx-tone` run; stays
  open in HANDOFF. It may be a hardware/firmware finding, not a deployment regression.
- **The ops fixes** — the clipping knob-down and the `radio.toml` frequency the bench runs used —
  are recorded in `docs/server-notes.md`, not here (config/level, not code).
- Moving the CAT read to an async `to_thread` task instead of a dedicated thread, or a larger ALSA
  buffer, were considered and rejected as bigger changes than the bench-proven decoupling needs.
