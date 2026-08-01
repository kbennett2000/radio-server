# ADR 0166 — A dead serial reader is not a healthy station

**Status:** Accepted · 2026-08-01 · closes [ADR 0163](0163-a-cadence-for-the-probe.md) **finding 1**
(the concurrent-tty hazard every prompt in this arc has had to carry a manual warning about); brings
[ADR 0099](0099-crossband-vocoder-wedge-failsafe.md)'s reader-generation guard to the dock transport

## Context

`Uvk5Transport._read_loop` has exactly one fatal path: any exception out of `self._serial.read()`
calls `_fail(exc)` and **returns**. The thread ends. Nothing restarted it, nothing watched it, and
`_reader_error` had no public accessor.

Everything above it went on working, which is the whole problem. `AiocBaofeng.status()` is pure
attribute reads *by design* — that is what makes it cheap enough to call per audio frame — so
`/status` kept answering with a frequency, a `modulation`, a `broadcast_fm` block and `tx_ok: true`
from a radio that had not spoken in an hour. ADR 0163 found it by accident: the only moving symptom
was `deafened_unknown` climbing, on a cadence that exists for one cycle's worth of luck and only runs
while a bridge is relaying.

**The cause to lead with is the one with nobody behind it.** The concurrent-open case is what was
observed, but `/dev/serial/by-id/...A602RQT5` re-enumerates **hourly** on this box, and a USB
re-enumeration leaves precisely this wreckage: a stale fd, a dead reader, a healthy-looking station.
Nobody did anything wrong, and "wait for a human at a terminal" is a worse outcome than the fault
itself. That is why decision 4 exists at all.

The same `_fail`-and-return shape is in `kv4p/transport.py`, and `AiocBaofeng` inherits it by handing
its own open handle to a `Uvk5Transport`.

## Measured before designing

### pyserial's `exclusive=True` is not what this repo believed

`TIOCEXCL` appears **zero** times in pyserial 3.5. `serialposix.py:382-389` calls
`fcntl.flock(LOCK_EX | LOCK_NB)` and nothing else, and flock is *advisory* — it stops only another
process that also flocks. As a normal user:

| what is held | a naive second `open()` |
|---|---|
| nothing | OPENED |
| `flock(LOCK_EX)` | **OPENED** |
| `serial.Serial(..., exclusive=True)` | **OPENED** |
| `fcntl.ioctl(fd, termios.TIOCEXCL)` | **EBUSY** |
| after `TIOCNXCL` | OPENED |

Two docstrings in this repo said otherwise and are corrected here: `aioc_baofeng.py` claimed
*"pyserial takes the device exclusively, so a second open would fail"* — it does not, and this
backend never even passed the flag — and `dvdongle.py` described `exclusive=True` as *"POSIX
`TIOCEXCL` + advisory lock"* when it is the advisory lock alone. **That false sentence is why a bench
script silently killing the reader was a surprise instead of an expectation.**

### The lockout question, and why a pty could not answer it

Adopting `TIOCEXCL` risks something worse than the fault: if the flag outlived the holding process, a
crashed service would lock the station out of its own radio until the device re-enumerated.

On a **pty**, the flag persists after the holder closes its fd — a control run (set, clear with
`TIOCNXCL`, close) proves `TIOCEXCL` is the cause. That result is an artefact: a pty slave never
reaches last-close while its master is open. Believing it would have wrongly refused the whole
prevention half of this cycle.

Measured on `/dev/ttyUSB0`, a **real USB serial device with no holder** (B0, and nothing ships past
it):

```
1. baseline, nothing held           : OPENED
2. holder alive, TIOCEXCL set       : EBUSY
3. holder closed, flag never cleared: OPENED     <- the lockout question
4. second process holds it          : EBUSY
5. that process SIGKILLed           : OPENED     <- the restart case
```

The flag clears on last close, including after `SIGKILL`. Safe to adopt.

## Decision

### 1. Ask the thread, never the radio

`Uvk5Transport` and `Kv4pTransport` gain `alive` and `reader_error`. `alive` is
`thread.is_alive() and self._reader_error is None` — a local check with no I/O, so it is safe on the
per-frame `status()` path and works when the serial handle raises on every touch.

Polling the radio to discover that the radio has stopped answering is the failure mode, not the
detector, and it would be the one call guaranteed to block.

A **closed** transport is not alive and is not a fault. Teardown is deliberate; reporting it as a
dead reader would make every clean shutdown look like the defect. `reader_error` separates them.

### 2. `/status` keeps the whole truth; `/healthz` is the verdict

`RadioStatus` gains `transport: TransportHealth | None` — `alive`, `error`, `port` — following
`BroadcastFm`'s block-not-loose-fields and tri-state discipline. `None` means *there is no serial
link here to report on*, never "fine".

**`/status` stays 200 with a full body, and the reason is diagnosability:** it is where a broken
station gets read, and naive clients discard a 503 body. A `/status` that 503'd would say something
is wrong and then refuse to say what. New **`GET /healthz`** answers **503** instead.

That split is only worth anything because of what the audit found. **Everything in this repo that
used `/status` as an up-signal:**

- `acceptance.py:287` `wait_healthy` — the readiness loop, and the only one. **Now `/healthz`.**
- `acceptance.py:571` `"restarted healthy"` — its only caller.
- `acceptance.py:581` `stage_web`'s `GET /status == 200` — **stays**, correctly: it asserts the read
  endpoint answers, a different claim. A `/healthz` check joins it on both stations.
- Eleven other bench scripts read `/status` for **data** (`rssi`, `busy`), not readiness.
- The web UI never calls it: `client.status()` (`web/src/api.js`) has **zero call sites** and the UI
  is pure-push over `/events`. Recorded as dead code and a latent second up-signal.
- No CI, no cron, no timer.

Because `status_event()` serialises `RadioStatus`, the block rides the `/events` **on-connect
snapshot** too — so a tab opened *after* the reader died is told, rather than shown a healthy station
until someone presses something.

### 3. A watcher that costs nothing and only speaks on the edge

An asyncio task in the lifespan at **2.0 s**, calling the local liveness closure. It is handed a
callable, not a radio — a watcher that polled the radio would be the failure mode wearing a monitor's
hat. It publishes on a **transition** only: a dead reader is a state, not an event, and alarming
every tick would put 43 200 lines a day in the journal and train everyone to filter them.

### 4. Recovery: one shot, on request, saying what it did

**`POST /diagnostics/reconnect`** — exactly one bounded reopen, reusing ADR 0099's reader-generation
guard so a straggler from the superseded reader can neither fill a reply slot nor store its own death
in `_reader_error` after the new reader starts. Without that tag a recovered transport reports itself
broken the moment the old thread finally notices, and never looks healthy again.

| outcome | HTTP |
|---|---|
| `already_healthy` | 200 |
| `reopened` | 200 |
| failure, with the reason | **503** |

**Never a loop.** The concurrent-open case means another process holds the tty, and a retry loop
would spend the station's life fighting it for the port — a louder version of the race that caused
the defect. **Never a 200 saying "failed"**: an outcome reported in a body that everything reads as
success is the fault class this repo keeps closing.

### 5. TIOCEXCL — the blast radius, stated rather than discovered

One `fcntl.ioctl(handle.fileno(), termios.TIOCEXCL)` after every serial open.

**Every bench script and every `doctor` dock probe against that tty now fails EBUSY while the service
runs.** That is the rule `troubleshooting.md` already gave ("stop the server before running doctor"),
finally enforced instead of merely written down — but it will surprise someone, so the message names
the remedy rather than leaving a bare errno:

```
[FAIL] could not open the serial port — /dev/serial/by-id/usb-AIOC_All-In-One-Cable_… is held by
another process: … The radio-server service claims the dock tty exclusively while it runs (ADR
0166), so a bench script or `doctor` has to have it to itself: `systemctl --user stop radio-server`
first, and start it again afterwards.
```

**Root bypasses `TIOCEXCL`.** A `sudo`-ed script still gets in and still kills the reader. That is
not a gap to fix — it is the reason decisions 1-4 still have to exist. Nobody should read this
section as making the liveness surface redundant.

## Fail-first

Red before implementation: **23 failed, 2203 passed, 5 skipped**, all behavioural. The first run was
an `ImportError` for symbols that did not exist yet; a no-behaviour stub replaced it and the run was
repeated before any number was written down (ADR 0162's lesson, applied even though this one would
not have cascaded). The four that passed red are guards pinning the *absence* of a claim — `None` not
rendering as healthy, a closed transport not reading as a fault — and must stay green in both
directions.

## Bench

### B1 — the defect, reproduced rather than quoted

On the pre-fix build, a second process opening `/dev/ttyACM0` was **not** enough: a bare open, and
even a reading second process, left the reader alive. The error only fires when two readers **race
for bytes**, so it needs traffic — a loop of real dock frames alongside the intruder.

At one intruder, the exception landed on **the intruder**, not the service. **Which process loses is
a race**, and ADR 0163's account of "a second process kills the service's reader" is one of two
outcomes rather than the guaranteed one. At three concurrent readers the service lost:

```
ERROR radio_server.backends.uvk5.transport: uvk5: reader thread stopped on
SerialException('device reports readiness to read but returned no data
(device disconnected or multiple access on port?)')
```

and then, the whole ADR in four lines:

```
POST /broadcast-fm  http=500      <- a real dock frame fails
GET  /status        http=200      <- and the station reports:
  frequency : 147555000   tx_ok : True   modulation : FM   transport : None
```

### B2 — deployed onto exactly that broken state

`/healthz` 200 and the block populated with the by-id path once restarted; `/status` carries
`transport` on both stations, including the witness's own CP2102N.

### B4 — prevention, against the identical attack

The same three-way race that had killed the reader ten minutes earlier:

```
second process: REFUSED: SerialException - [Errno 16] Device or resource busy
reader deaths in this window: 0
GET /healthz http=200 · POST /broadcast-fm http=200
```

`doctor --backend uvk5` against the live station now reports the FAIL quoted in decision 5 instead of
getting in.

### B5 — the restart case, measured

Three restarts in a row and then a `SIGKILL` of the running process, each followed by
`/healthz` — **200 every time**. A dying process does not lock the station out of its own radio.

### B3 — recovery, and the leg that could not run

`already_healthy` measured on the station: `POST /diagnostics/reconnect` → 200, no action taken, dock
link still answering afterwards.

**The `reopened` leg was not run on the station, and the reason is the prevention working.**
`TIOCEXCL` removed the only non-root way to kill the reader, and the box has no passwordless sudo, so
there is no way to reach a dead reader on the protected build without physically interfering with the
hardware. It is proven instead against a **real `Uvk5Transport` with a real reader thread and a real
reopen** in `tests/test_transport_health.py`, together with the 503 failure path and the
reader-generation shedding. That is a real-transport test, not a station measurement, and it is
recorded as such rather than described as covered.

### B6 — `acceptance.py`

Two runs. The first: `systemd` **FAIL**, everything else pass — run seconds after B5's SIGKILL/restart
storm, and not reproducible; the stage passes in isolation and passed on the re-run. Recorded rather
than dropped. The second, clean:

**9 of 9 attempted PASS · `split-minus` SKIP · `RESULT: INCOMPLETE` · exit 3**, with the new lines:

```
ok radio GET /healthz   200    radio serial reader   alive
ok kv4p  GET /healthz   200    kv4p  serial reader   alive
```

### B7 — restored

**147.555**, `tune_persist` off, broadcast FM off, `rescues` 0, not transmitting, both units active on
`f82a657`, `radio.toml` byte-identical (`ead78a44…` / `f9be6bb5…`).

### What the bench found that pytest could not

1. **`doctor`'s own error path crashed.** A blanket edit gave every
   `"could not open the serial port"` site the new explanation — and two live in
   `*_connect_probe(report, cfg, ...)`, which has no `port` local. The result was a `NameError`
   *while reporting an error*. Found by running `doctor` against a live station; now pinned, along
   with a guard that the five call sites still pass a real port expression.
2. **The "say stop the service first" requirement only reached our own opens.** Three bench scripts
   call `serial.Serial()` directly and got pyserial's bare EBUSY — exactly the surprise this was
   meant not to be. They now share an `open_tty()` that names the remedy and exits cleanly.
3. **The witness backend never surfaced its transport.** `Kv4pTransport` got `alive` in the first
   commit but `Kv4pHt.status()` never asked it, so `/healthz` on 8091 reported `transport: null` and
   was blind to the same defect — on the instrument every other measurement here is taken against.
4. **A bare open does not kill the reader; a race for bytes does.** Worth knowing before someone
   tries to reproduce this and concludes it is fixed.

## Consequences

- **The manual "don't touch the tty" warning every prompt in this arc has carried is now enforced by
  the kernel**, and when it fires it says what to do. That was the actual ask.
- **Auth, tuning and audio are untouched.** This cycle adds two read/diagnostic routes, a status
  field and a background tick.
- **Carried:** the `Bench Split Minus` preset (still the only SKIP); `client.status()` is dead code
  in the web client and a latent second up-signal; the witness unit's `ExecStart` still names extras
  that exclude `kv4p` (ADR 0165).

## Verification

`uv run pytest` — **2229 passed, 5 skipped** (baseline 2199/5). `npx vitest run` — **14 files, 138
tests** (baseline 13/131). Red before implementation: **23 failed, 2203 passed, 5 skipped**.
