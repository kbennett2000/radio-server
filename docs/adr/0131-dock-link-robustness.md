# 0131 — The dock link drops frames, and three places assumed it never would

Status: Accepted

## Context

With TX working (ADR 0128) and RX smooth (ADR 0130), the acceptance runner (ADR 0129) still failed
roughly one run in three — never the same stage twice. Running it repeatedly turned three separate
intermittent faults into reproducible ones. They share a root cause worth stating plainly:

**The dock link is not reliable, and the firmware is single-threaded.** `Dock_EnterFullControl` is
a *blocking* loop; while the firmware is inside `Dock_ForceTx` doing the PA sequence (~25 ms of
`SYSTEM_DelayMs`) it is not servicing its UART. A request that arrives then is not answered late —
it is **dropped**. Register writes are fire-and-forget, so a lost write is silent. Three call sites
were written as if neither could happen.

## The three faults, each caught in the act

### 1. The key-up read-back raced the firmware — HTTP 500, nothing on air

```
Uvk5KeyingError: radio did not report TX enabled (reg 0x30=0xbff1, want 0xc1fe)
```

This is the "first-key settle flake" F4 saw and [ADR 0126](0126-uvk5-v3-dock-force-tx-chain-b-close.md)
carried forward as unconfirmed. It is real: it failed a live `/services/02` announcement. `_key_on`
wrote the TX-enable and immediately read reg 0x30 back — straight into the window where
`BK4819_PrepareTransmit()` has just written `REG_30 = 0` mid-sequence. The radio answers truthfully
with the RX word for a transmitter that is coming up correctly.

### 2. Polling harder made it worse — a 2-second stall

Adding a bounded read-back retry then produced:

```
Uvk5Timeout: no matching reply within 2.0s
```

Because the retry read *into* the busy window, where the request is dropped outright and the
transport waits out its whole timeout.

### 3. A dropped write cannot be recovered by reading

Even after settling first, one over failed with reg 0x30 reading `0xBFF1` after five read-backs —
the RX word, meaning the radio was never keyed at all. The **write** was the casualty. No amount of
re-reading finds a TX bit that was never set.

### 4. A dropped squelch poll reported the channel clear — and chopped the over

`status().busy` drives the CAT squelch gate (ADR 0121) and answered `False` whenever its RSSI read
timed out. So a poll lost in the busy window closed the RX gate on a transmission *in progress*.
Measured: **4.19 s of a 6.0 s over** arriving, perfectly continuous (98.2 % duty, 119 ms largest
gap, zero overruns) — just missing its head. Every symptom pointed at the receiver; the fault was a
missing measurement being read as silence.

## Decision

**Wait the firmware out; retry the operation, not the observation; never treat missing data as
information.**

1. **Settle before confirming** (`_KEY_SETTLE_S = 50 ms`). Wait out the PA sequence rather than
   polling into it. This alone removes faults 1 and 2.
2. **Re-send the whole key-up, up to `_KEY_ATTEMPTS = 3`**, confirming each attempt. The register
   writes are idempotent. This is the only thing that can recover fault 3.
3. **Tolerate a timed-out read-back** as one failed attempt rather than an aborted transmission.
4. **Hold the last known squelch state** across a failed RSSI read (`_BUSY_HOLD_READS = 3`) instead
   of reporting "clear".

**None of this weakens the ADR 0112 RF-safety invariant.** A radio that genuinely never keys still
exhausts every attempt, still raises `Uvk5KeyingError`, and `_key_on` still unwinds completely — a
silent no-key never becomes dead air. What changed is that a *dropped frame* no longer masquerades
as a *broken radio*. Likewise the squelch hold is bounded, so a genuinely dead link cannot latch the
RX gate open.

## Consequences — measured

Three consecutive full acceptance runs, the first immediately after a cold boot, on the deployed
tree:

```
RESULT: PASS      RESULT: PASS      RESULT: PASS      (exit 0, 0, 0)
```

All eight stages green in all three, where the same runner previously failed ~1 in 3 on a rotating
cast of stages. `uv run pytest`: **1574 passed / 5 skipped**, including a regression test per fault
(settle window, dropped read, dropped write, held squelch), each driven through a fake that
reproduces the specific link failure.

- **Cost:** a key-up now takes ~50 ms longer in the happy path. Against a TX lead-in already
  measured in hundreds of ms, that is not observable.
- **This is a link-layer weakness being masked at the call sites**, which is the right place for
  now — the alternative is an ack/retry layer in the dock protocol, and that would break the
  byte-compatibility invariant the whole fork rests on (ADR 0119). If dropped frames get worse,
  that is the escalation.
- The `Uvk5Timeout` path is now exercised in three distinct places; the transport's 2.0 s default
  is worth revisiting, since nothing the dock does legitimately takes that long.
