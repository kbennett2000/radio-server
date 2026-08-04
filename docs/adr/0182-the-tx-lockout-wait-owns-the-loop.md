# ADR 0182 — The TX lockout wait owns the event loop

Status: Accepted

## The redirect, recorded

This cycle was briefed to fix `Recorder.write`, carried forward by name from
[ADR 0181](0181-a-hung-disk-stalls-the-transmitter.md). It also asked for the loop-blocking sweep to
be run a third time, because "two cycles have each found one more; the third should establish whether
there is a fourth."

There is a fourth, and it outranked the assigned target, so the cycle was redirected to it. **That is
the sweep working, not a deviation.** The mechanism the last two ADRs built — look for synchronous
work on the event loop, and follow it to the keying path — found something larger than the thing it
was pointed at. Recording it that way matters, because the alternative reading is that the brief was
wrong, and it was not: the brief asked the question whose answer moved the cycle.

`Recorder.write` is carried, with **why it can wait** written down rather than left implied: it is
genuinely on the loop ([`rx/pump.py:267`](../../radio_server/rx/pump.py),
[`tx/session.py:244`](../../radio_server/tx/session.py)) — the [ADR 0130](0130-rx-read-off-the-event-loop.md)
executor wraps `self._radio.receive` and nothing else, so the write never followed the read onto the
capture thread — but `recording.enabled = false` and `recording.tx = false` on the deployed station,
so `build_recorder` returns `None` and the pump gets `null_recorder`. **A latent hazard behind a
disabled flag is not the same as one firing on the ordinary retune-then-talk path today.** The
completed analysis is in Findings, so the next cycle inherits it rather than re-deriving it.

## Context

A UV-K5 mutes its own transmitter for six seconds after any EEPROM conversation. Not as a consequence
of *writing*: [ADR 0142](0142-the-server-picks-the-repeater.md) read the firmware and found
`gSerialConfigCountDown_500ms` armed by the HELLO and by every EEPROM *read* as well as the write
(`uart.c:355, 393, 447`), and [ADR 0145](0145-instant-by-default.md) corrected ADR 0144 to arm on
every commit, "including the run that finds the channel already there and writes nothing."

The wait for it is **necessary and is not in question here**. ADR 0142 shipped without it and the
bench failed every carrier row on attempt #1 and passed on every attempt after — a radio reported
ready while its firmware ignored PTT. `AiocBaofeng._await_tx_lockout` exists to stop that, and
`SerialConfigInProgress()` both refuses a key-up *and* terminates an over already in progress, so
keying early does not mean keying late, it means transmitting nothing.

What was wrong is **where the wait was spent**:

```python
def _await_tx_lockout(self) -> None:
    remaining = self.tx_ready_in()
    if remaining is None:
        return
    logger.info("aioc: holding key-up %.1fs for the radio's serial TX lockout", remaining)
    time.sleep(min(remaining, SERIAL_TX_LOCKOUT_S))     # SERIAL_TX_LOCKOUT_S = 6.5
```

`time.sleep`, reached from `ptt(True)` → `_key_on`, and the loop-side keying paths call into that
**synchronously from the event loop**.

## Severity — established before anything was designed, and it is not ADR 0181's

### It is an availability fault, not a stuck carrier

This governs everything else, so it goes first. `_key_on`'s order is:

```
_reserve_the_wire() → _clear_if_deafened() → _reassert_channel()
→ _refuse_if_tx_disabled() → _await_tx_lockout()   ← the sleep
→ _key_on_locked()                                  ← opens the stream, THEN asserts the line
```

The line assert — `setattr(self._serial, self._ptt_line.value, True)` — is inside `_key_on_locked`,
**after** the sleep, and the playout stream is not even open yet. So for the whole wait **the PTT line
is low and the transmitter is not keyed.** There is no carrier to be unable to drop.

ADR 0181's "a keyed transmitter the server cannot unkey" is **not** what this is, and this ADR does
not borrow that framing. Two secondary effects are real and are not a stuck key: `TxSession._key_up`
calls `acquire_tx()` *before* `radio.ptt(True)`, so the arbiter is latched `TRANSMITTING` for the
duration; and `_reserve_the_wire()` runs before the sleep with `_release_the_wire()` only in the
`finally`, so the RSSI and broadcast-FM cadence pollers are muted across it too.

### It fires on the deployed station — measured, not inferred

`tx_ready_at` has exactly one assignment site (`HybridTuner._arm_lockout`), reached only from the
persist branch of `HybridTuner.apply()`. The existing tests already name the trigger: `# storing costs
the lockout` against `assert tuner.tx_ready_at is None  # so the radio will key immediately`. So the
config decides it, and the station is on the arming side — `uvk5_tuner = "hybrid"`,
`uvk5_tune_persist = true` (reported as found, not flipped). The shipped **default** is
`persist = false`, which arms nothing; this station runs the non-default value that arms on every tune.

The station's own journal settles the rate. Across 2026-07-16 → 2026-08-04:

* **78** `aioc: holding key-up …` lines — every one a key-up that sat out a lockout,
* durations from **2.7 s to the full 6.5 s** (ten at 6.5 s),
* **six of them in the three hours before this cycle started.**

`POST /frequency`, `/split`, `/tone`, `/mode` and `/power` each arm it **per call**, so the UI
sequence frequency→tone→mode pays it three times; `apply_preset` collapses up to four setters into
one commit via `tuning_batch`. Scan does not arm it (`Capability.SCAN` is not in the AIOC's caps) and
no DTMF service tunes at all, so this is an operator-retune and preset-apply hazard rather than a
per-scan-hop one. Naming that bound matters as much as naming the hazard.

### What is unavailable, measured on hardware

Two arms on the deployed station, **the same lockout in each**, differing only in which route drives
the key-up. A concurrent poller asked `GET /status` every 50 ms throughout.

| arm | route dispatch | lockout | key-up | probes served | median | **worst** |
|---|---|---|---|---|---|---|
| control | `POST /ptt` — plain `def` → threadpool | 6.488 s | 7.03 s | **142** | 5.8 ms | **12.9 ms** |
| loop | `POST /transmit` — `async def` → event loop | 6.487 s | 7.13 s | **17** | 11.1 ms | **7123.5 ms** |

Same wait, same duration, same radio. One route kept the server answering; the other stopped it dead
for 7.1 seconds and served 17 probes where the control served 142. That isolates the fault to **loop
ownership**, not to the wait — which is the entire argument for the fix below.

The loop-side keying paths, enumerated: `/audio/tx` (`session.feed` inline in `async def audio_tx`),
`POST /transmit`, the Mumble relay task, the D-STAR relay task, `POST /services/{digit}` and
`POST /auth/session`. `POST /ptt` is a plain `def` and is the control above.

## The design — the wait stays, it stops owning the loop

### ADR 0177's bounded barrier does not reuse, and the reason is the point

`_reserve_the_wire` → `transport.wait_for_quiet(timeout)` is the obvious in-tree precedent: a bounded
wait on the same key-up path. It does not transfer. That barrier is bounded *and on expiry the station
keys anyway*, for the reason its own docstring gives — "a transmitter must not wait on a diagnostic" —
and `_key_on` states the converse directly: "Capping them turns a delayed key-up into a REFUSED one…
A late station ID is a worse-but-legal transmission; a refused one is no transmission at all, and
guardrail 5 makes it required controller behaviour."

That is a barrier against a **diagnostic** the transmitter may safely out-wait. The lockout is a
**hardware precondition** it may not: give up early and there is no RF. Same shape, opposite meaning.
The same argument rules out the tempting backend-local fix of *refusing* on the loop rather than
sleeping — refusal is exactly what `_key_on` already rejects for the station-ID path.

### So the wait is made awaitable

The seam already existed and was already public. `AiocBaofeng.tx_ready_in()` reports the remaining
seconds and is already surfaced in `status()` — exposed, its docstring says, "so `status()` can report
it and the UI can stop offering a button that will do nothing", and, pointedly, "It is not what
enforces the wait — `_key_on` is, because a browser must never be the thing keeping RF correct."

So the loop-side callers `await await_tx_ready(radio)` immediately before entering the synchronous
keying path, and **`_await_tx_lockout` is left exactly as it was.** It then finds `tx_ready_in() is
None` and returns immediately, having already been waited out — asynchronously, in full.

**This is an optimisation of where, never of whether**, and that is what makes it safe:

* The wait is not shortened, skipped, or capped differently. Same duration, same enforcement.
* The backend keeps its synchronous enforcement, so "a browser must never be the thing keeping RF
  correct" still holds, and threadpool callers are unaffected.
* **It degrades safely.** A keying path that forgets to pre-wait falls back to the old
  blocking-but-correct behaviour, never to an early key-up that transmits nothing. That property is
  worth more than a tidier refactor, and it is the argument for an added `await` at the call sites
  rather than restructuring `ptt()`.
* Awaiting is safe *here* specifically because it happens **before** the key-up: no PTT is asserted,
  so a cancellation during the wait strands nothing. `_reserve_the_wire()` still runs immediately
  before the assert, so ADR 0177's reservation-to-assert window is untouched.
* Bounded by construction — `MAX_TX_LOCKOUT_WAIT_S` mirrors the cap `_await_tx_lockout` already
  applied, pinned by test against `SERIAL_TX_LOCKOUT_S` so the duplicate cannot drift.

The cost, stated plainly: the `await` lives at each call site, so a seventh keying path could miss it.
Mitigated by the safe degradation above and by an enumeration test in `test_event_hub_bound`'s
`_EXPECTED`-pin idiom — a source scan, so a guard rather than a proof (ADR 0167 recorded that
limitation against `test_relay_subscribers`), but it catches the failure that will actually happen.

## Decision

`radio_server/tx/lockout.py`: `tx_lockout_remaining(radio)` duck-types `tx_ready_in` (the
`getattr`-and-check idiom `RxPump` uses for the gate's optional `start`/`stop`, and guardrail 3's
capability split — most backends have no lockout and pay nothing), and `await_tx_ready(radio)` awaits
it. The probe never raises: a failed probe is treated as "nothing to pre-wait", which falls through to
the backend's enforcement, the safe direction.

Wired at `/audio/tx` (guarded on `not session.keyed`, since only the key-up consults a lockout),
`POST /transmit`, and the Mumble relay task.

### Three loop-side paths deliberately still block, and are named rather than left to be rediscovered

* **The D-STAR bridge.** `_emit_rx_pcm` is sync inside two `async def` callers, so the change is
  mechanically small — but it is the crossband keying path
  [ADR 0090–0099](0099-crossband-vocoder-wedge-failsafe.md) hardened after the stuck-key incident, and that
  crossband is **disabled** pending a re-proof from a cold-booted dongle, which this cycle's bench
  does not run. An unverified change to that path is not worth a fix to a disabled feature.
* **`POST /services/{digit}` and `POST /auth/session`.** Both are `async def` with no await inside
  **on purpose**, so they serialize against the RX pump's `controller.step`. Adding an await would
  break that ordering, which is a controller-concurrency question and not this ADR's to answer.

## Evidence

### The fail-first red

`tests/test_tx_lockout_does_not_stall_the_loop.py`, staging a 500 ms lockout on a radio double that
mirrors the real ordering (sleep first, line assert after — getting that backwards would test a stuck
carrier, which this is not). Red on master:

```
an unrelated GET /healthz waited 396 ms while the radio sat out a 500 ms TX lockout
— the wait is being spent on the event loop
```

Green after, along with the invariants that keep the fix honest: the lockout is still waited out **in
full** (a shortened wait is the ADR 0142 fault returning), an un-pre-waited key-up is still enforced by
the backend, a pre-waited one leaves the backend nothing to sleep on, a radio with no lockout is never
waited on, and the cap is not tighter than `SERIAL_TX_LOCKOUT_S`.

### On hardware: green, and an honest accounting of what is left

Same script, same station, after deploying `ab0daf0`:

| arm | probes served | median | **worst** |
|---|---|---|---|
| red, lockout armed | 17 | 11.1 ms | **7123.5 ms** |
| green, lockout armed | **119** | 11.1 ms | **1227.9 ms** |
| green, **no lockout armed** (control) | 17 | 11.1 ms | **1298.8 ms** |

The third row is the one that matters, and it was measured precisely because "it got better" is not an
accounting. With **no lockout armed at all**, the same loop route still blocks **1298.8 ms** — so the
1227.9 ms remaining in the green arm is not the lockout. It is the rest of the synchronous key-up: the
wire barrier, the `0x0879` deafness frame and the ALSA playout open, all still inside `ptt(True)` on
the loop, and all of it there before this cycle, hidden underneath the six seconds.

So: **the lockout's contribution to the loop block went from ~5.9 s to within the noise of zero**, and
a ~1.3 s synchronous key-up remains on the loop that this ADR did not cause and does not fix. It is
recorded in Findings as its own cycle.

### Key-up latency — the instrument refused, and was not substituted for

`keyup_race.py --i-will-transmit --forced 0` stopped at its own precondition:

```
the witness did not answer /status (401)
```

The kv4p witness on 8091 carries its own API token. That check runs *before* the script stops the
station to borrow the port, so nothing was disrupted. Reported rather than worked around, and no
substitute number was invented in its place.

It was also not worth chasing, for a reason stronger than ADR 0181's: `keyup_race` stops the service
and drives `create_radio("baofeng", …)` directly, so it never imports `api/app.py`,
`link/bridge.py` or `tx/` — which is the **entire** diff. It would have measured byte-identical code.
The one way this diff could touch key-up latency is the added `await` before `ptt(True)`, and that
await is entirely *outside* `_key_on`: `_reserve_the_wire()` still runs immediately before the line
assert, so ADR 0177's reservation-to-assert window is unchanged.

### `acceptance.py` — the gate this cycle actually turned on

This arc has taken the Part 97 station ID down twice through keying-path changes, **both times with a
green pytest suite**, and `acceptance.py` caught both. So it is the acceptance criterion here, not a
formality. Full run after deploying `ab0daf0`:

```
systemd FAIL   web FAIL   presets PASS   rx PASS   dtmf PASS
auth PASS      tx PASS    split PASS     split-minus SKIP   services PASS
```

`tx`, `services` and `auth` — the stages that transmit and that exercise the station ID — all pass.
`web` is the known baseline failure (`kv4p GET /healthz 404`).

**`systemd` is not in the baseline, so it was chased rather than accepted.** The failing check was
`stop under WS load: result = exit-code`, and the journal named it: `Main process exited,
code=exited, status=139` — **SIGSEGV**, a native crash during teardown, with PortAudio/ALSA
`alsa_snd_pcm_mmap_begin … failed` lines beside it. Re-run alone, the stage passed **3/3**. The
station's own journal then settled ownership without needing a redeploy: **six** `status=139` exits
all time, and **five of them predate this branch** — 2026-07-26, 07-31, 08-01 (×2) and 08-03, against
a branch first deployed at 16:51 on 08-04.

So it is a **pre-existing, intermittent ALSA teardown segfault**, recorded below. Two things follow
that are worth stating: the "9/10 PASS" baseline is itself flaky, so a `systemd` failure must not be
read as a regression without this check; and a SIGSEGV skips the rest of the lifespan teardown —
including the unkey — which is exactly the shape ADR 0181 called strictly worse than a bounded stop.

## Findings — recorded, not fixed

* **`Recorder.write` is on the event loop**, at `rx/pump.py:267` (inside the pump's `create_task` body)
  and `tx/session.py:244` (inline in `/audio/tx`'s stack, in the same call as `ptt(True)`). It waits
  because `recording.enabled` and `recording.tx` are both `false` on the station. Work already done
  for whoever picks it up: `wave.Wave_write.writeframes` calls `_patchheader()` on **every** frame, and
  `seek()` on a `BufferedWriter` flushes, so nothing amortizes — instrumented at the raw fd,
  **2.7 `write()` + 2.7 `seek()` syscalls per 1920-byte frame**, i.e. ~270 syscalls/s per active
  recorder in the worst possible shape for a network filesystem. `wave.open()` — the most expensive
  call — happens lazily on the *first frame of a segment*, which for TX is the key-up frame. Three
  things that will bite a naive fix: `end_segment` must travel through the same queue as an **ordered
  marker**, or every segment boundary lands in the wrong file (so `ThreadedSink` cannot be reused as
  it stands); the capture timestamp must travel **in-band**, or the filename records the drain time
  rather than the capture time; and `max_seconds` must be re-derived from bytes rather than the wall
  clock once writes are asynchronous. A FIFO cannot stage the wedge — measured,
  `OSError: [Errno 29] Illegal seek` on the second frame — so ADR 0181's rig does not transfer.
* **A ~1.3 s synchronous key-up remains on the loop**, measured above with the lockout disarmed:
  `_reserve_the_wire` (bounded 0.3 s) plus `_clear_if_deafened`'s ~0.1 s serial round trip plus the
  ALSA playout open. `_key_on`'s own comment already says these "still block for the wire without a
  bound of their own".
* **The rest of the sweep**, on the loop and unfixed: `urllib.request.urlopen(timeout=3.0)`
  (`services/fetch.py:54`) and piper ONNX synthesis (`services/tts.py:116`) reached from the RX pump
  task via `controller.step`; `subprocess.Popen` for the multimon respawn (`audio/dtmf.py:533`, only in
  the non-default `streaming` decode mode); `save_settings`' read-modify-write from `async def
  select_backend` (`api/app.py:2379`); and `Uvk5Radio.status()`'s 2 s-timeout serial register read,
  reached from every `async def` route on that backend.
* **The TX recorder never records station-ID audio** — all three `_station_id` transmits in
  `tx/session.py` go straight to `radio.transmit` without touching the recorder, so a `tx-` WAV is
  already not a faithful record of the emission. Found by this cycle's trace; relevant to whoever
  fixes the recorder.
* **The station intermittently SIGSEGVs on shutdown**, in ALSA/PortAudio teardown — six `status=139`
  exits in the 19 days of journal, five of them before this branch existed, with
  `alsa_snd_pcm_mmap_begin … failed` beside them. `acceptance.py`'s `stop under WS load` catches it
  non-deterministically (it failed once and then passed 3/3), which makes the 9/10 baseline flaky and
  means a `systemd` failure needs this check before it is called a regression. It also matters on its
  own terms: a SIGSEGV skips the rest of the lifespan teardown, including the unkey.
* **`PolledGate` still has no staleness expiry.** Restated, unchanged.
* **`keyup_race.py` cannot run as written** — its witness precondition 401s because the kv4p witness
  on 8091 has a separate token from the station's. Any future cycle wanting ADR 0177's instrument has
  to pass the witness its own token first.

## Out of scope

The firmware fork, the witness checkout, the recording format, the ledger, and the value of
`uvk5_tune_persist` — reported as found (`true`), not flipped.
