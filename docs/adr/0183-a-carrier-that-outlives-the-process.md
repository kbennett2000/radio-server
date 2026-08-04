# ADR 0183 — A carrier that outlives the process, and the SIGSEGV that exposed it

Status: Accepted

## Context

[ADR 0182](0182-the-tx-lockout-wait-owns-the-loop.md) recorded six `status=139` exits and filed them
as "recorded, not fixed", with one sentence of consequence attached: *"a SIGSEGV skips the rest of
the teardown including the unkey."* That sentence conflates two different defects, and they want
separating before either can be judged:

1. **The crash.** A segfault is a bug in or around a C library.
2. **The consequence.** A process that dies abruptly never runs its unkey — true of SIGSEGV, of
   SIGKILL, of a power cut, and of the `TimeoutStopSec` SIGKILL that ADR 0181 established follows a
   blocked loop.

Defect 2 is the one that matters, it is fixable without fixing 1, and it turns out to have been
answered all along by something nobody in this repo had ever looked at.

## Defect 2 — the line already falls, but by inheritance rather than by design

On the AIOC, **PTT *is* DTR** (`ptt_line = "dtr"`, bench-confirmed; the AIOC's DTR drives the same
`GPIO_PIN_PTT` the rubber PTT button does), asserted with a bare `setattr` on a pyserial handle. So
what happens to the carrier when the process dies is a **kernel** question, not a Python one — and
nothing in the tree runs at all after a SIGSEGV or SIGKILL: no `close()`, no `atexit`, and no
`TotRadio`, whose watchdog is a `daemon` `threading.Timer` that dies with the process and therefore
offers **zero** coverage here. ADR 0117 said as much when it named an out-of-process supervisor as a
follow-on and did not build it.

What is left is the tty layer: `tty_port_close` lowers DTR/RTS at last close **iff `HUPCL` is set**.

Measured on the station:

```
$ stty -F /dev/serial/by-id/usb-AIOC_All-In-One-Cable_da3441ac-if04 -a
termios: hupcl          # set
```

And in the installed pyserial 3.5, `HUPCL` appears **zero** times. `_reconfigure_port` reads the
device's existing `cflag` and edits only `CLOCAL`/`CREAD`, `CSIZE`, `CSTOPB`, parity and `CRTSCTS`;
`Serial.close()` closes the fd and does not touch the modem lines. So the flag is **inherited
verbatim** from whatever last configured the tty.

**That is the finding.** The station has been protected from an abandoned carrier this whole time,
by a kernel default that no code in this repo sets, asserts, tests, measures or documents. One
`stty -hupcl`, a getty or ModemManager touching that tty, a different distro's tty defaults, or a
future pyserial change silently converts "the kernel unkeys you" into a carrier that runs until
someone power-cycles the radio — and **no code path anywhere would notice**. The same rig measured
last-close behaviour before, for `TIOCEXCL` (ADR 0166 B0, *"verified across three restarts and a
`kill -9`"*); DTR was never asked.

### The three candidates the brief asked me to weigh

| candidate | covers | costs |
|---|---|---|
| **Unkey at open** | a death *followed by a restart* (`Restart=on-failure`, `RestartSec=2`) | **already built** — `AiocBaofeng.__init__` forces both lines low post-open. Not a new remedy, and it does nothing for a crash during a `stop`, which is five of the six |
| **Out-of-process supervisor** | host death, including SIGKILL and a wedged machine | a second unit that can fail silently, and it cannot hold the port while the server does — `TIOCEXCL` is asserted, confirmed live (`stty` → `Device or resource busy`) — so it must poll-then-open, which is racy. Named in ADR 0117, never built |
| **The radio's own TOT** | *everything*, including power loss | a radio-menu setting the server cannot read, set or verify; coarse; an operator instruction rather than an engineered remedy |

**Chosen: assert `HUPCL` at open.** It is the only option that satisfies the brief's own criterion —
a remedy that does not depend on the process surviving — *without* adding a process, because the
actor is the kernel. `ensure_hangup_on_close()` sits beside `claim_port_exclusive()` (the same
port-hygiene concern, the same "returns whether it took, never raises" shape), and the backend warns
if it found the flag **clear**, which is the case worth knowing about because it means something on
the host has been quietly removing the only backstop.

The supervisor and the radio's TOT remain the only things that cover power loss and host death.
Neither is built; both are named again below.

**Wired at one call site, deliberately.** The helper lives beside `claim_port_exclusive` in the uvk5
transport because that is where port hygiene lives, but only `_default_serial_factory` (the AIOC)
calls it. The standalone `uvk5` dock backend keys over the wire protocol rather than through a modem
control line, so `HUPCL` would buy it nothing: a process that dies mid-transmission there leaves the
radio keyed whatever the tty does, and only the radio's own TOT ends that over. Stated so the single
caller reads as a decision rather than an oversight.

### Corrected by measurement: the open does not key

`_default_serial_factory`'s docstring claims *"pyserial applies `.rts`/`.dtr` set before `open()` as
the initial line state, so we set both low first and only then open."* It was worth checking, since
pyserial can only apply that **after** `os.open` has already created the fd, and the tty layer
raises DTR on open. Measured on the station with a **stable observer fd**, so the reading itself
never opens the port:

```
baseline (observer forced DTR low)        : False
after a RAW os.open of the same port      : True    <- a bare open DOES key it
re-baselined                              : False
after pyserial open with dtr=False preset : False   <- the station's own open does NOT
```

So the existing idiom works and the station's own open does not key the transmitter. Sampled at
200 ms, so a sub-200 ms pulse would not have been seen — stated rather than glossed.

### What could not be measured, said plainly

Whether DTR actually falls at last-close is **not observable from userspace on the same port**: any
observer must open the tty, and a bare open raises DTR (above), while holding a second fd open
prevents last-close from happening at all. A two-arm `kill -9` test was run with an idle-line
calibration arm precisely to detect this, and it did:

```
[ARM-A keyed] child set DTR=True  | after SIGKILL + last-close = True
[ARM-B  idle] child set DTR=False | after SIGKILL + last-close = True   <- calibration
VERDICT: INCONCLUSIVE - the reader's own open perturbs the line; trust the termios flag
```

Both arms read high, so the reader's own open dominated and the direct test proves nothing. The
evidence for the drop is therefore the `HUPCL` flag plus documented kernel behaviour, **not** a
direct observation — and this ADR says so rather than claiming a measurement it does not have.

## Defect 1 — root-caused, not merely characterised

The kernel names the crashing thread, and it is the same one every time:

```
Jul 26 02:33:11 rx-read_0[832360]:  segfault ... in libasound.so.2.0.0
Jul 31 23:20:31 rx-read_0[3492039]: segfault ... in libportaudio.so.2.0.0
Aug 01 02:58:02 rx-read_0[3701957]: segfault ... in libasound.so.2.0.0
Aug 01 03:53:22 rx-read_0[3783747]: segfault ... in libc.so.6
Aug 03 20:12:23 rx-read_0[3571626]: segfault ... in libportaudio.so.2.0.0
Aug 04 16:59:06 rx-read_0[680377]:  segfault ... in libasound.so.2.0.0
```

`rx-read_0` is the pump's dedicated capture reader ([ADR 0130](0130-rx-read-off-the-event-loop.md)).
Six for six, `error 4` (a user-mode read of an unmapped page), across **three different libraries**
at varying addresses. One thread faulting in three libraries is a **use-after-free**, not one bad
pointer — which is also why it is intermittent.

**What they have in common:** five of six are during a shutdown, after uvicorn logs *"Waiting for
application shutdown."* and before *"Application shutdown complete."*; four had an `/audio/rx`
WebSocket opened seconds earlier. That correlation is causal, not coincidental — the pump is
demand-driven, so **no listener means no reader thread and no race**. The sixth (2026-07-26) is a
`POST /radio/select` backend swap, which runs the same `stop()` → `close()` sequence through
`holder.rebuild`; `scripts/bench/tune_survives_exit.py` already recorded it as *"segfaulted the
server (status 139) … `close()` never ran"*, and `docs/deployment.md` documents it as an open bug.
No transmission was in progress in any of them: these are RX-path crashes.

**The mechanism, with the load-bearing step measured rather than reasoned.** `RxPump.stop()`
cancelled the task and then called `reader.shutdown(wait=False)`. Cancelling a task parked in
`run_in_executor` cancels only the *asyncio* wrapper — the underlying `concurrent.futures.Future` is
already RUNNING, so its `cancel()` fails silently and the worker reads on:

```
stop() returned after           : 0.06 ms
reader STILL inside receive()   : True
live rx-read threads after stop : ['rx-read_0']
```

So the 2 s bounded join **never engaged**. Abandoning a live reader was not the exceptional path
[ADR 0104](0104-shutdown-stop-budget.md) contemplated — it was the **only** path, on every stop.
`holder.stop()` then reaches `radio.close()` three lines later, which calls `Pa_CloseStream` →
`snd_pcm_close` and frees the very stream the parked read is dereferencing. The benign form of the
same race is already in the journal beside the crashes: `Expression 'alsa_snd_pcm_mmap_begin(
self->pcm, ...)' failed` is a read that lands *after* the stop but *before* the free.

**The asymmetry is the finding.** `SoundCardTxPacer.stop()` **joins** its writer thread before the
stream is closed, and its docstring names this exact hazard — *"the thread is parked in one blocking
chunk write against a stream the backend is closing."* TX understood it; RX never did. ADR 0130
considered and dismissed it by timing alone — *"a capture read returns within a block period, and
the lifespan teardown closes the radio — which is what releases the stream"* — with nothing
enforcing that ordering. That sentence is the defect.

### Decision

`RxPump` now `submit()`s the read so it holds the concurrent future (which `run_in_executor` does not
expose), and `stop()` does a **bounded** `concurrent.futures.wait` on it before
`shutdown(wait=False)`. `READER_JOIN_TIMEOUT_S = 0.25` is derived: a capture block is
`DEFAULT_BLOCKSIZE / CANONICAL_RATE` = 960/48000 = **20 ms**, so this is ~12 of them — long enough
that a healthy read is never abandoned, short enough to be invisible against uvicorn's 5 s graceful
window and the unit's `TimeoutStopSec=20`. ADR 0104's escape is deliberately kept: a read that never
returns is still abandoned rather than holding shutdown open.

## The acceptance baseline's real state

"9/10, the known witness 404" has been repeated for several cycles as though the one failure were
understood and static. It is not. Over the journal's window (2026-07-16 → 2026-08-04):

| | count |
|---|---|
| stops requested | **422** |
| reached `Application shutdown complete` | **249** |
| `stop-sigterm` timed out → **SIGKILL** | **133** |
| uvicorn graceful window exceeded | 171 |
| SIGSEGV | 6 |

**About 41 % of stops never complete teardown, and 133 ended in SIGKILL** — roughly seven a day.
That, not the segfault, is the dominant route to a process that dies without unkeying, and it is
exactly the path ADR 0181 predicted. It also means `stage_systemd` is **intermittently red on its
own**, so a `systemd` failure must not be read as a regression without checking history first. This
is worth recording because the arc has twice leaned on `acceptance.py` to catch what pytest
structurally could not: the net is real, and it has a hole in it that has been read as a constant.

## Evidence

### Fail-first

`tests/test_teardown_never_abandons_the_reader.py`. The behavioural red was measured directly on
master (0.06 ms, above); the test's sensitivity was then demonstrated by neutralising the new wait,
which reproduces master's behaviour exactly:

```
with the wait neutralised: stop() returned after 0.10 ms; reader still parked = True
-> the test's assertion (elapsed >= 50 ms) would FAIL (red)
```

Green after, together with the invariants that keep the fix honest: the reader is finished **before**
`close()` runs; a genuinely wedged read is **still abandoned** so ADR 0104's stop budget holds; the
bound is many capture block periods; `stop()` stays idempotent and leaks no reader threads; and
`HUPCL` is asserted on both the inherited-set and the someone-cleared-it paths.

### Counts

pytest **2453 passed / 5 skipped**, from 2446/5 — the seven new tests exactly, with
`test_shutdown_budget.py` still green. vitest **14 files / 163 tests**, unchanged; no web change.

## Findings — recorded, not fixed

* **The out-of-process supervisor** (ADR 0117's named-not-built follow-on) and **the radio's own
  TOT** remain the only remedies that cover power loss and host death. `HUPCL` covers a dying
  process; it cannot cover a dying host.
* **`holder.stop()`'s middle is unguarded** — `scan_runner.stop()` and `rx_pump.stop()` are the only
  two steps without a `try/except`, and they sit *before* `radio.close()`, which contains the only
  **unconditional** unkey. A raise in either skips it. The earlier `ptt(False)` is
  arbiter-conditional and its own comment admits it misses the direct `POST /ptt` path.
* **`TotRadio` gives zero coverage for an abrupt death** — a `daemon` `threading.Timer` dies with the
  process. Restated so it is not mistaken for a backstop for this class of fault.
* **The `POST /radio/select` segfault** (`docs/deployment.md`) is this same use-after-free reached
  through `holder.rebuild`, so it should now be fixed too — but it was not re-tested this cycle and
  is not claimed fixed.
* **`Recorder.write`** and the **~1.3 s synchronous key-up** — excluded by the brief, unchanged.

## Out of scope

The firmware fork, the witness checkout, the ledger, the recording format, and the value of
`uvk5_tune_persist` — reported as found (`true`), not flipped.
