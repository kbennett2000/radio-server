# 0130 — Read the sound card off the event loop, and stop counting expected overruns

Status: Accepted

## Context

`RxPump.run()` has always called `radio.receive()` directly inside the coroutine. Its own docstring
flagged the consequence as unresolved since ADR 0029:

> Guardrail 1: on real hardware `receive()` blocks for the chunk duration; whether to run it in a
> thread executor rather than directly in the event loop is a bring-up decision.

The bench decided it. Two independent measurements, both on the deployed server:

**1. `py-spy` caught it.** A stack dump taken during the ADR 0127 shutdown investigation:

```
Thread 892535 (idle): "MainThread"
    _raw_read (sounddevice.py:1213)
    receive (radio_server/backends/uvk5/radio.py:517)
    run (radio_server/rx/pump.py:225)
    run_forever (asyncio/base_events.py:677)
    run (uvicorn/server.py:74)
```

The asyncio event loop — the same one serving every HTTP request and WebSocket frame — was inside
a blocking ALSA read.

**2. The card was overrunning.** 33 `ALSA capture overrun (xrun)` warnings in 40 minutes, rising to
~2.1/min while the acceptance runner was driving the station. The warning text names the mechanism
itself: *"the RX reader fell behind the card … the single capture reader is stalling (e.g. a
blocking call on the reader thread)"*. ADR 0125 fixed one such blocking call (the CAT squelch
read). This is the other one, and it is the read itself.

The user-visible symptom was truncated overs. Across three consecutive acceptance runs the received
span of a fixed 5.0 s transmission shrank **5.70 s → 3.88 s → 3.28 s** — smooth (duty 100.5 %,
largest inter-frame gap 40 ms) but progressively *short*, because dropped audio is not a gap, it is
absence.

## Decision

**1. `frame = await asyncio.to_thread(self._radio.receive)`.** The pump's awaits are sequential, so
exactly one read is ever in flight — the backend still sees the single reader that the ALSA and
serial capture paths are built around. The loop is now free while the card is being read.

**2. Use the pump's own reader thread, not the default executor.** `asyncio.to_thread` uses the
loop's *shared* default pool, which also serves the D-STAR/DVAP status refreshes the web UI polls.
A capture read queued behind one of those is a dropped block. One measured run showed it: 94.9 %
duty with a 321 ms inter-frame gap. `RxPump` now owns a `max_workers=1` executor, so the read never
queues and reuses one thread instead of churning one per frame.

**3. An overrun is only a fault if somebody was reading.** The ring fills on wall-clock time, so it
is *guaranteed* to overrun wherever reading legitimately stops: a keyed over (half-duplex blinds RX
by design, ADR 0017), the demand-driven pump halting when the last `/audio/rx` listener leaves, a
freshly opened stream, a restart. Rather than enumerate those — the first attempt did, and missed
the listener case, which the journal then caught landing on `WebSocket /audio/rx accepted` —
`Uvk5Radio` measures the gap since the previous `receive()`. Beyond `_XRUN_READ_GAP_S` (0.5 s,
against a ~20 ms block) it excuses the backlog while it drains, bounded by `_XRUN_DRAIN_READS` and
ended early by the first clean read. The warning now also *states* the gap, which is what made the
rest of this diagnosable.

### What the residue turned out to be — and why the verdict moved back

Even after all of the above, 2–7 overruns per acceptance run remained. The instrumented warning
settled it:

```
ALSA capture overrun (xrun) after 0.022 s since the previous read (0 drain reads left)
```

**22 ms is exactly one block period.** The reader was reading continuously, on cadence, with no
stall and no gap — the card reported a recovery event of its own. Critically, no audio was lost
when it mattered: in the same runs the RX stage measured **100.4 % duty across the active span, a
40 ms largest inter-frame gap, the whole 5 s over received, and the 1000 Hz tone recovered at
1.000**. Bytes-over-elapsed at or above 100 % is direct evidence that nothing was dropped.

So the acceptance verdict on overruns is **scoped to the window where RX audio matters** — the RX
stage's own capture — and the whole-run figure is printed as information, explicitly *not* a
verdict. Widening it to the whole run (which this ADR originally did) counted the runner's own
service restart and the end of every keyed announcement, i.e. it counted transmissions. That is the
same mistake in the opposite direction, and the honest resolution is to check the property under
test — *audio arrives complete and smooth while receiving* — rather than a proxy that fires for
structural reasons.

## Consequences — measured

| | received span of a 5.0 s over | duty / largest gap | xruns while receiving |
|---|---|---|---|
| Before | 5.70 → 3.88 → **3.28 s** over three runs | 100.5 % / 40 ms, but progressively short | 33 in 40 min (~2.1/min under load) |
| After | **5.06 / 5.38 / 5.14 s**, stable | **100.4–100.8 % / 40–41 ms** | **0** |

`uv run pytest`: **1570 passed / 5 skipped** (was 1566/5; the new tests are the `/tone` pair and
two overrun-classification tests).

- **Test harnesses had to change.** Twelve tests drove the pump and then yielded a single loop turn
  (`await asyncio.sleep(0)`) before asserting. With the read on a worker thread, a scripted radio
  running out of frames and the last frame reaching the hub are no longer the same turn. They now
  use a bounded `settle_pump()` helper in `tests/conftest.py`, which returns as soon as the loop
  goes quiet.
- **Shutdown is unaffected.** `to_thread` cannot be cancelled, but a capture read returns within a
  block period, and the lifespan teardown closes the radio — which is what releases the stream. The
  bounded graceful shutdown from ADR 0127 still measures ~5.5 s with clients attached.
- The default executor is used, so a stuck backend read consumes one pool thread rather than the
  whole loop. That is strictly better than the previous behaviour, where it consumed the server.
