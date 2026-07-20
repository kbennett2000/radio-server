# 0104 — Shutdown fits the stop budget (bounded, concurrent joins everywhere)

Status: Accepted

## Context

Stopping radio-server on the bench repeatedly overran the deployed unit's `TimeoutStopSec=10`, so
systemd escalated to **SIGKILL — which severs the DV Dongle mid-operation and wedges it** (the
"first open after an abrupt close is flaky" failure that then poisons the next crossband session;
ADR 0094/0099). A stop-path audit found three structural contributors:

1. **The D-STAR bridge joined its (up to four) cancelled tasks sequentially**, each under its own
   `wait_for(tx_hang + 2.0)` — up to ~12 s of budget on its own when a worker is wedged in the
   single-worker vocoder executor (a cancel is not deliverable until the executor call returns).
   ADR 0099 bounded each join but left them sequential.
2. **The Mumble link bridge's `stop()` awaited its cancelled tasks with NO bound** — a task parked
   in a non-cancellable blocking call could hang shutdown indefinitely.
3. **The RX pump's `stop()` likewise awaited its task unbounded** — and the pump's task can be
   parked in a blocking backend `receive()` (the ADR 0029 known limitation).

Worst-case bounded sum before this ADR ≈ 20 s (3 vocoder close + 12 bridge joins + 2 Mumble thread
+ 3 DTMF reap) **plus** the two unbounded joins.

## Decision

1. **D-STAR bridge:** join the cancelled tasks **concurrently** — one `asyncio.gather` under a
   single `wait_for(tx_hang + 2.0)`. Worst case drops ~12 s → ~3 s; a still-wedged worker is
   abandoned exactly as before (the executor is already shut down `wait=False`).
2. **Mumble link bridge & RX pump:** the joins get the same treatment as the ADR 0099-hardened
   D-STAR path — `wait_for` (2.0 s), abandon on timeout. Shutdown latency must never depend on a
   cancelled task cooperating.
3. **Document the budget in the unit:** the deployment unit now ships `TimeoutStopSec=20` with the
   rationale — the stop timeout must never be what fires first, because its SIGKILL is precisely
   what wedges the DV Dongle.

## Consequences

- Worst-case clean stop is comfortably inside 10 s even with a wedged vocoder worker; the
  routine restart → SIGKILL → wedged-dongle cycle is closed off from the software side, and the
  unit's 20 s ceiling gives margin on top.
- An abandoned task is a daemon-side leak until process exit — acceptable at shutdown, and exactly
  the trade ADR 0099 already made for the D-STAR joins.
- Deployed instances should adopt `TimeoutStopSec=20` (one-line unit edit); the code fix alone
  already fits a 10 s budget.
