# 0127 — `systemctl stop` never completed: bound uvicorn's graceful shutdown

Status: Accepted

## Context

The deployed bench service could not be stopped. Every `systemctl --user stop radio-server` sat for
the full `TimeoutStopSec=20`, then got SIGKILLed:

```
systemd: Stopping radio-server.service...
uv[164907]: INFO:     Shutting down
uv[164907]: INFO:     Waiting for background tasks to complete. (CTRL+C to force quit)
systemd: radio-server.service: State 'stop-sigterm' timed out. Killing.
systemd: radio-server.service: Killing process 164907 (python3) with signal SIGKILL.
systemd: radio-server.service: Main process exited, code=killed, status=9/KILL
systemd: radio-server.service: Failed with result 'timeout'.
```

**Seven times in 14 hours.** One of those stops is the live blocker that started this mission: the
service went down at 12:36:30 and was not started again until 12:44:33 — an **8-minute window with
nothing listening on 8090**, which is the two `ConnectionRefused`. `Restart=on-failure` does not
cover it, because an explicit `stop` is not a failure.

This is also *why* the "stop the service around single-open `doctor` tests, never leave it stopped"
ops rule kept getting violated: stop appeared to hang, so it got abandoned mid-way.

### Two measured causes, not one

**1. `uvicorn.run()` had no `timeout_graceful_shutdown`** (`radio_server/__main__.py`), and
uvicorn's default is `None` — *wait forever*. In `Server._wait_tasks_to_complete()` the loop is
`while self.server_state.tasks and not self.force_exit: await asyncio.sleep(0.1)`. A browser
holding `/audio/rx` and `/events` open keeps those handler tasks alive indefinitely, so the wait
never ends. Confirmed at the moment of a stop:

```
ESTAB 192.168.1.62:8090  192.168.1.30:35632  users:(("python3",pid=892535,fd=21))   # /audio/rx
ESTAB 192.168.1.62:8090  192.168.1.30:44964  users:(("python3",pid=892535,fd=22))   # /events
```

**2. The blocking sound-card read runs on the asyncio event loop.** A `py-spy dump` taken 6 s into
a hung stop:

```
Thread 892535 (idle): "MainThread"
    _raw_read (sounddevice.py:1213)
    receive (radio_server/backends/uvk5/radio.py:517)
    run (radio_server/rx/pump.py:225)
    _run_once (asyncio/base_events.py:2057)
    run_forever (asyncio/base_events.py:677)
    run (uvicorn/server.py:74)
```

`RxPump.run` is `asyncio.create_task`-ed (`rx/pump.py:315`) and calls `self._radio.receive()`
*synchronously*, so on real hardware the loop thread is inside ALSA for the chunk duration on
every iteration. The docstring at `rx/pump.py:190` already flags this as an open bring-up decision
("whether to run it in a thread executor rather than directly in the event loop"). It is not the
cause of the *hang* — the loop still turns between chunks — but it degrades every deadline the
loop owns. **Left open here deliberately; it is measured and decided on its own evidence in the RX
work, not folded into a shutdown fix.**

The consequence of (1) was worse than a slow stop: because uvicorn never got past the wait, the
**lifespan teardown never ran at all**. The carefully bounded teardown (ADR 0104) — drop PTT, stop
the scan, halt the pump, reap the `multimon-ng` child, flush the event log, close the radio — was
dead code on every real shutdown. SIGKILL is also exactly what ADR 0104 warns severs a USB vocoder.

## Decision

**1. Bound the graceful phase** — `timeout_graceful_shutdown=5.0` in `uvicorn.run()`
(`GRACEFUL_SHUTDOWN_SECONDS`, `radio_server/__main__.py`). uvicorn then cancels the stragglers and
**still runs `lifespan.shutdown()`** — `force_exit` is only set by a *second* SIGINT, so the
teardown is preserved, not skipped. Kept well under `TimeoutStopSec=20` so the bounded teardown
fits in the remaining budget.

**2. Declare `SuccessExitStatus=143` in the unit.** A clean SIGTERM stop exits **143** (128+15),
not 0: uvicorn's `capture_signals()` restores the default handlers and re-raises the captured
SIGTERM on purpose, so the process dies with the signal's disposition. Without the declaration
systemd reports `Failed with result 'exit-code'` on a perfectly good stop.

**This is a truthful declaration, not the fix.** Adding `SuccessExitStatus=143` *before* fixing the
hang would have masked nothing anyway — the failure result then was `timeout` with `status=9/KILL`,
which 143 does not match. It only becomes correct once the process genuinely exits on SIGTERM.

## Consequences — measured

| | stop, 2 WS clients attached | `Result` | lifespan teardown |
|---|---|---|---|
| Before | **20.0 s**, then SIGKILL | `timeout` (`status=9/KILL`) | **never ran** |
| After | **5.48 s** | `success` (`status=143`) | ran |

`restart` is 0.07 s; an idle stop is 0.50 s. The journal now shows the designed path end to end:

```
INFO:     Waiting for background tasks to complete. (CTRL+C to force quit)
ERROR:    Cancel 4 running task(s), timeout graceful shutdown exceeded
INFO:     Waiting for application shutdown.
INFO:     Finished server process [921772]
```

`Waiting for application shutdown` is the proof the teardown now executes.

- Deployed units (`radio-server`, `radio-server-kv4p`) patched with `SuccessExitStatus=143`; the
  unit template in [`docs/deployment.md`](../deployment.md) carries it and the reasoning.
- Cost: a client with an open WebSocket is cancelled ~5 s into a stop rather than waited on
  forever. For a LAN radio gateway that is the right trade — an idle browser tab must not be able
  to hold the station up.
- Boot survival (`Linger=yes`, both units `enabled`) is verified by an actual reboot in the
  acceptance runner (ADR 0129), not assumed.
