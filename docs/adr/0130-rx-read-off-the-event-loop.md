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

**2. Stop reporting *expected* overruns as faults.** Not every overrun is a stall. The ring keeps
filling whenever nobody is reading it, and there are two places where nobody legitimately is:

- **A keyed over.** Half-duplex (ADR 0017) deliberately blinds the receiver: the pump does not pull
  `receive()` at all while transmitting. The ring is guaranteed to overrun by the time RX resumes.
- **A freshly opened stream.** The card starts filling the instant the stream opens; the first read
  is several hundred ms later (device settle, dock hand-shake).

`Uvk5Radio` now arms `_expect_xrun` at those two points and consumes **exactly one** overrun
quietly (at DEBUG). A genuine stall overruns again on the very next read and is reported as before.
Without this the warning count was really a transmission counter, which is worse than useless as a
health signal — it drowns the real thing in expected noise.

**3. The acceptance runner counts xruns over the whole run**, not over the stage that happens to be
measuring. The per-stage window reported a comfortable `0` while the journal held 33; that
instrument bug is why the problem survived the first pass.

## Consequences — measured

| | received span of a 5.0 s over | xruns |
|---|---|---|
| Before | 5.70 → 3.88 → 3.28 s over three runs | 33 in 40 min (~2.1/min under load) |
| After | 5.38 s, stable | 0 unexpected |

`uv run pytest`: **1568 passed / 5 skipped** (was 1566/5; the new tests are the `/tone` pair and
the overrun-classification test).

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
