# 0099 — The crossband must fail safe when the DV Dongle wedges (teardown unkeys first; wedge fails the over fast; recover without a reader race)

Status: Accepted

## Context

The second joint dummy-load re-proof of the module-A crossband (2026-07-20, after ADR 0097 + 0098
merged and deployed) **failed**: real off-air D-STAR keyed the FM transmitter but put **dead air** on the
air, and the transmitter **stayed keyed** — and the graceful `systemctl stop` then **hung ~15 s**
(sigterm → SIGKILL) before PTT finally dropped.

The decode fix itself is sound — the same bench, Phase 1, decoded the same reflector audio to
**intelligible voice in the browser** on a `MockRadio` backend (zero RF). The failure is a **wedged DV
Dongle** and, more importantly, a crossband that **did not fail safe** around it. The DV Dongle was left
in a bad state by the Phase 1→Phase 2 service restart (Phase 1 held a decode stream open on the same
shared dongle). From the journal + a source-cited trace, one wedge cascaded through three separate
defects:

1. **`_recover()` has a reader-thread race** (`vocoder/dvdongle.py`). To wake a sleeping/wedged AMBE2000
   it `_stop.set()`s, closes the port, `old_reader.join(timeout=1.5)`, then **reassigns** `_serial` /
   `_stop` / `_reader`. On a wedged dongle the old reader does not exit within 1.5 s, so it survives the
   reassignment; `_read_loop` dereferences `self._serial` by attribute each iteration, so the **zombie
   reader reads the now-closed port** (pyserial `close()` nulls the fd) → `TypeError("'NoneType' object
   cannot be interpreted as an integer")` from inside `read()`. `_fail` records it, and
   `_raise_if_failed` then converts **every** subsequent exchange to `VocoderUnavailable` — "every decode
   threw." (Journal: `reader thread stopped on TypeError(...)` at 03:28:33, then 47 × `AMBE decode failed`.)

2. **The streaming decode never recovers a wedge** (`_DvDongleDecodeStream.decode`, ADR 0098). The legacy
   per-frame `_exchange` calls `_recover()` once on a `VocoderTimeout` and retries; the streaming path
   added in ADR 0098 dropped that — it just `serial.write`s into a full FTDI buffer, hits the 1 s
   `_WRITE_TIMEOUT` **every frame**, and re-raises forever. That is the **dead air**, and each ~1 s write
   also parks `_reflector_to_rf` (the inbound drain) one frame at a time.

3. **Teardown blocks the event loop, starving the unkey** (`dstar/bridge.py` `_teardown`). It calls
   `self._vocoder.close()` **synchronously on the event loop** *before* `_force_unkey()`. `close()` sends
   `REQ_STOP` under `_io_lock`; if the vocoder executor thread is inside `_recover()` it holds `_io_lock`
   for the **whole** recovery — `old_reader.join(1.5 s)` + 3 handshake attempts × ~5 s START-timeout ≈
   **15–16 s** — so `close()` (and therefore the direct `radio.ptt(False)` in `_force_unkey`, and every
   task join) is **stalled on the event-loop thread**. PTT stays asserted until the process is SIGKILLed
   and the OS closes the AIOC fd (dropping DTR). This is the "still transmitting" the operator saw during
   the stop.

The during-*over* keyed dead air was bounded (~1.5 s: the independent `_rx_watchdog`, ADR 0092, still
runs on the event loop and drops PTT `tx_hang` after the last real feed). The load-bearing failure is
**teardown**: the one path where the vocoder is allowed to block the loop is the one path that must
never delay the unkey.

This is guardrail-2 / ADR 0090-0092 territory: **a wedged codec must never hold PTT, and the unkey must
never depend on the codec being healthy.**

## Decision

Make the crossband fail safe around a wedged vocoder, in three tightly-coupled fixes (one concern):

1. **Teardown drops PTT FIRST and never blocks the event loop on the vocoder** (`bridge._teardown`).
   Reorder so `_force_unkey()` (the direct, codec-independent `radio.ptt(False)`) runs **before**
   `self._vocoder.close()`. Then close the vocoder **off the event loop** with a bounded wait
   (`run_in_executor` + `asyncio.wait_for`, timeout ≤ `tx_hang + 2`), so a slow/wedged `close()` can
   never stall SIGTERM, the task joins, or the unkey. PTT is already down before the vocoder is touched.

2. **`DVDongleVocoder.close()` is non-blocking on a contended lock.** `close()` must not wait ~15 s for
   `_io_lock` to send a courtesy `REQ_STOP`. Try the lock with a short timeout; if a `_recover` holds it,
   **skip the graceful stop**, set `_stop`, `notify_all`, and close the port directly. Closing the port
   is what actually unwedges an in-flight `_recover` (its blocked I/O errors out), so teardown converges
   instead of waiting on it. Recovery is also made cheaper (fewer/*shorter* handshake attempts on the
   teardown path) so no single `_recover` can hold the lock for many seconds.

3. **`_recover()` cannot leave a zombie reader** (`dvdongle._read_loop` / `_recover`). Bind each reader
   thread to **its own** `serial` + `stop` (passed as args, not read from `self`), and tag it with a
   monotonic **generation**; `_dispatch`/`_fail` ignore a reader whose generation is no longer current.
   A stale reader on a closed port then only ever touches its own (closed) handle, exits on its own
   error, and **cannot** clobber the recovered transport's `_reader_error`. This removes the `TypeError`
   crash and the "every decode threw after recover."

4. **The streaming decode fails the over fast and self-heals on the next over** (`_DvDongleDecodeStream`
   + `bridge`). On a write timeout / reader error, the stream raises a clear terminal error **once** and
   latches "wedged" so subsequent `decode()`/`flush()` calls short-circuit (no more 1 s write attempts) —
   `_play_ambe` already catches it and the `_rx_watchdog` ends the over, now without the per-frame
   parking storm. The dongle is **not** recovered mid-over (a recover resets the pipeline and drops the
   rest of the over anyway); instead `open_decode_stream()` recovers a failed dongle at the **start of
   the next over** (and the idle `_keepalive_loop` already recovers via `_exchange`). So a transient
   wedge costs one dropped over, not a dead-air lockup.

L, the write/reply/handshake timeouts, and the recover attempt counts stay marked tunable bench facts
(guardrail 1).

## Consequences

- **PTT can never outlive a teardown**, wedged codec or not: the unkey is first, direct, and off the
  vocoder's lock. SIGTERM/stop completes promptly (no 15 s hang, no reliance on SIGKILL closing the fd).
- A wedged dongle yields **one clean dropped over** (fast fail + watchdog end) and **self-heals** on the
  next over, instead of dead-air-keyed-until-killed with a log storm.
- The `_recover` reader race — the `TypeError` that turned a recoverable sleep into "every decode threw"
  — is gone; recovery is generation-safe.
- **Fakes model the wedge without hardware:** a vocoder/decode-stream fake that starts raising a
  write-timeout / reader-error mid-over proves (a) the bridge unkeys promptly (`MockRadio.ptt(False)`),
  (b) `stop()` returns quickly and unkeyed even mid-wedge, (c) the streaming decode fails fast rather than
  parking N seconds, and (d) a stale reader generation is ignored. The zero-latency `FakeVocoder` and the
  ADR 0098 `PipelinedFakeVocoder` could not catch any of this.
- **This does not re-enable the crossband.** It stays disabled on the live radios; re-enable is still
  gated on the joint dummy-load re-proof (operator watching, no-keying listen step first) per ADR
  0091-0094 / 0097 / 0098 — now to be run from a **cold-booted dongle** (never reuse it across a restart;
  power-cycle `ttyUSB1` first).
- Scope: the reflector→RF decode teardown/wedge path and the DV Dongle driver's recover/reader. The
  decode *correctness* (ADR 0098), the content gate + over cap (ADR 0097), encode/RF→reflector, the
  browser paths, and DVAP are untouched.
