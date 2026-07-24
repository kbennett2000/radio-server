# 0123 — UV-K5 V3: the `--rx-firststart-loop` harness fix + the real 20-count

Status: Accepted

## Context

ADR 0122 shipped the first-start dead-RX fix (`_enter_hw_mode_verified`: send `0x0870`, settle, read
back `REG_47`, re-send if the firmware force-open did not latch — ADR 0120) and an instrument to prove
it, `doctor --rx-firststart-loop N`: N× open the uvk5 stack → register-dump → measure the AIOC → close,
printing a per-iteration leg verdict (`ALIVE` / `DEAD/HOST-AUDIO` / `DEAD/RADIO`).

The 0122 live validation confirmed the **fix** works — across a warm and a cold-boot run, **10/10
completed opens were `ALIVE`**, `REG_47=0x6142` (FM/unmute) every iteration, step-0 `F3 CONFIRMED`,
zero dead-RX. But it could **not** reach the `0 dead / 20` acceptance, because the **instrument itself
crashed** at the ~6th open, twice. That is a harness defect, not a firmware/hardware fault (a plain
connect probe passed immediately after each crash). This cycle fixes the instrument and runs the real
acceptance. No firmware change; host-side (`doctor`) only.

## The defect

In [`_rx_firststart_loop`](../../radio_server/doctor.py), construction ran **outside** the
per-iteration `try`:

```python
for i in range(iterations):
    radio = _build_backend(cfg)   # <-- outside the try
    try:
        regs = {...}
        levels = measure_rx_levels(radio, seconds=seconds)
    finally:
        radio.close()
```

`_build_backend` → `Uvk5Radio.__init__` seeds its register model with plain, **non-retransmitting**
`_read_register` calls (radio.py `__init__`, the 0x30/0x33/0x38/0x39 reads). Rapid back-to-back reopen
compounds the reset-on-open boot race; on the ~6th open a seeding read lands in a reset window and
raises `Uvk5Timeout`. Because construction sat outside the `try`, that timeout **took down the whole
run** instead of being recorded as one retryable iteration.

## Decision 1 — construction is a counted, leg-attributed iteration result, never a crash

Move construction inside the per-iteration `try`. A construction-time `Uvk5Timeout` that survives the
retry below is counted as a dead start and attributed to the **RADIO leg** — the dock never completed
the enter/seed handshake, a transport-level failure with no audio path yet, the same family as
"`0x0870` lost in the boot race." It prints `REG_47=------  construct timeout after N attempts
DEAD/RADIO`, increments the dead count, and the loop continues to the next iteration.

## Decision 2 — bounded construction retry, the harness analogue of `connect()`'s retransmit

A new helper `_build_backend_settled(cfg, *, attempts, interval, sleep)` catches **only** `Uvk5Timeout`
(so a real backend bug still surfaces) and retries the whole construction up to `attempts`, settling
`interval` between tries. This is the **harness-level** analogue of what the transport's
[`connect()`](../../radio_server/backends/uvk5/transport.py) already does internally — retransmit a
benign elicit through the reset-on-open boot race until it answers (ADR 0111). Same tolerance, same
family of bound: `_RX_FIRSTSTART_CONSTRUCT_ATTEMPTS = 3` mirrors `_ENTER_HW_MODE_RETRIES = 3`, and the
interval reuses `connect()`'s `_ELICIT_RETRANSMIT_INTERVAL` (0.25 s). Both the step-0 F3 probe and the
loop build through this helper, so a reset-window open anywhere in the instrument is tolerated, not
fatal. It is a *harness* retry — the byte-frozen transport (ADR 0119) and the backend are untouched.

## Decision 3 — an inter-iteration settle defines what "N-clean" means (the realism knob)

Rapid back-to-back reopen is **not** a first-start — it is a reset pile-up the real scenario never
produces. A genuine first-start is a single reset-on-open from a **settled** radio. So the loop settles
`_RX_FIRSTSTART_SETTLE_S = 1.0 s` before every iteration **except the first**, letting one
reset-on-open boot finish before the next open resets the radio again. This makes each iteration a
faithful single-reset first-start, which is what makes a clean count *mean* something — it is **more**
faithful to the real scenario (which already carried a settle before the open), not less.

This settle is a **realism decision, not a knob to twist until green.** The 1.0 s value is marked
`VERIFY ON BENCH`: the exact reset-on-open boot time is a judge-on-the-chip fact (guardrail 1 / ADR
0111) and no measured number is asserted — 1.0 s is a defensible "one boot completes" estimate, to be
tightened against hardware if desired. Crucially: **after this fix, any non-clean count is a real
finding** — reported with per-iteration verdicts, never re-run until it looks clean. The FIDELITY
caveat from 0122 still holds (in-process reopen ≠ a freshly-enumerated USB device settling; run right
after a cold boot / replug between batches).

## Live result — 0 dead / 20 (2026-07-24, bench)

`doctor --rx-firststart-loop 20` on the dev-PC UV-K5 (AIOC `da3441ac-if04`, 445.800):

- Step 0: **F3 firmware CONFIRMED** (`REG_47=0x6142` after entry).
- **20 / 20 iterations completed, `summary: 0/20 dead starts`** — every iteration `ALIVE`,
  `REG_47=0x6142` (FM/unmute) throughout, peak RMS ~7.3–8.2 k (far above the 200 floor). The loop that
  previously crashed at open 6 now runs clean to 20.

**This is the acceptance ADR 0122 deferred (item 1): `0 dead / 20`, met on live hardware.**

Observed, benign: a single `uvk5: reader thread stopped on SerialException('...returned no data...')`
line printed to stderr at teardown before the loop output — a reader thread noticing the port closed
under it during a `close()`. Every loop open succeeded; it is teardown-time noise, not a failure.
(Idle `RSSI` read jitter — e.g. one `RSSI=0` sample at iter 8 — is the already-documented 0122
idle-floor jitter and is irrelevant to the peak-RMS ALIVE verdict.)

## Consequences

- `doctor.py`: `_build_backend_settled` helper; constants `_RX_FIRSTSTART_SETTLE_S`,
  `_RX_FIRSTSTART_CONSTRUCT_ATTEMPTS`, `_RX_FIRSTSTART_CONSTRUCT_INTERVAL_S`; the loop and the step-0
  probe both build through the helper; a construct-timeout iteration is counted `DEAD/RADIO`.
- Tests (`tests/test_doctor.py`, +5): a droppable-construction builder — retry-then-`ALIVE` (a tolerated
  reset-race open is not counted dead), exhausted-attempts → counted `DEAD/RADIO` with the loop still
  finishing, the inter-iteration settle firing `iterations - 1` times (not before the first), and two
  `_build_backend_settled` unit cases (returns after K<attempts drops; re-raises after exhaustion).
  Full suite: **1533 passed, 4 skipped.**
- ADR 0122's addendum folds in the `0 dead / 20` result and points here.
- No firmware change; the transport and backend are untouched. `radio.toml` is bench-local (gitignored)
  and unchanged.

### Verify on hardware (guardrail 1 — no fabricated bench numbers)

1. The reset-on-open boot time behind `_RX_FIRSTSTART_SETTLE_S = 1.0 s`. 1.0 s is a marked estimate,
   not a measurement; tighten it against the hardware if a faster genuine-restart cadence is confirmed.
