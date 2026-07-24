# 0122 — UV-K5 V3 F3 bench loose ends: reproduce, fix, instrument

Status: Accepted

## Context

Five loose ends surfaced by the UV-K5 V3 F3 bench (ADRs 0118–0121), all addressable RX-side /
register-side with **no HT keying**. Bundled into one cycle because each is small and they share the
`doctor` / uvk5-backend surface. No firmware change — everything here is host-side (radio-server).

## Decision 1 — first-start dead RX: diagnose-before-fixing, with a bounded host-side fix

**Symptom:** some first server starts after a serial-port open have dead RX; a restart cures it.
**Mechanics (from the code):** the RX-audio force-open is *firmware-side* (`Dock_ForceRxAudioAlive`,
ADR 0120), triggered by radio-server's single **fire-and-forget, un-retried, unverified**
`send(EnterHwMode())` (0x0870) in `Uvk5Radio.__init__`. Two indistinguishable dead legs: **(a) the
radio leg** — 0x0870 lost in the reset-on-open boot race → the firmware never runs the force-open →
GPIOA8 (the un-dockable audio-amp gate) stays low → dead RX; **(b) the host-audio leg** — the ALSA
capture stream, opened lazily on the first `receive()`, is opened against a still-USB-settling device.

**The diagnostic lever.** GPIOA8 is un-readable over the dock, but the same firmware routine sets
`REG_47`=`0x6142` (AF=FM/unmute), so a `REG_47` read-back is a host-visible **proxy** for "the
force-open ran." Split against the downstream AIOC RMS it separates the legs: `REG_47`=FM + floor RMS
⇒ firmware ran ⇒ host-audio leg; `REG_47`=mute (`0x6042`) + floor RMS ⇒ 0x0870 lost ⇒ radio leg.

**Harness — `doctor --rx-firststart-loop N`.** N× open the uvk5 stack → register-dump
(`REG_30/47/48/67`) → `measure_rx_levels` → tear down, printing a per-iteration leg verdict
(`ALIVE` / `DEAD/HOST-AUDIO` / `DEAD/RADIO`) and a `dead/N` summary. A **step-0 F3 probe** reads
`REG_47` after a settled entry and stamps the run `F3 CONFIRMED` or `NOT F3` — because a pre-F3 dock
leaves `REG_47` at idle mute forever, so the RADIO/HOST split is only trustworthy on F3. A printed
**fidelity caveat** records that in-process reopen does not reproduce a freshly-*enumerated* USB
device settling: run it right after a cold boot / cable replug.

**Fix — host-side, in `radio.py` (never the F2-frozen `frames.py`/`transport.py`):**
- **Radio leg (shipped unconditionally):** `_enter_hw_mode_verified()` replaces the bare
  `send(EnterHwMode())` — send 0x0870, settle, read `REG_47`; if not FM, re-send (bounded, 3 tries).
  A no-op on a healthy F3 start (FM after the first send); **bounded** on a pre-F3 dock (REG_47 never
  FM → exits after the retries with a warning, never hangs, never falsely claims a fix). This is a
  strict robustness upgrade to a known-fragile fire-and-forget on a command proven lost in the boot
  race — worth shipping regardless of whether the loop reproduces.
- **Host-audio leg (shipped, default OFF):** `capture_reopen_on_floor` → `_open_capture()` primes one
  block and reopens the stream once if it reads floor (device still settling). Off by default keeps
  `receive()` byte-identical; enabled only if the live repro shows this leg.

## Decision 2 — shutdown tidy (CancelledError)

The `?token=` WebSocket handlers parked on `await queue.get()` / `await asyncio.wait_for(receive…)`
caught only `WebSocketDisconnect`, so uvicorn's shutdown cancellation escaped as a traceback. All of
them now catch `asyncio.CancelledError` alongside it and exit quietly (cleanup stays in `finally`) —
the same `contextlib.suppress(asyncio.CancelledError)` idiom the lifespan already uses. Applied to
**every** exposed handler (`/events`, `/audio/rx`, and the `mumble`/`dstar` `rx`+`tx` siblings), since
a partial fix leaves the same spam on the others.

## Decision 3 — doctor stopwatch

`measure_rx_levels` started its clock *before* the first `receive()` lazily opens the capture stream,
so ~13 blocks of stream-spin-up latency inflated `elapsed` → the true-rate estimate read low (the
−0.9/−0.2/−2.7% bench figures). Fix: **prime one `receive()` (discard) before starting the clock.**
`RxLevels.elapsed` feeds only the two pure rate formatters, whose kv4p **+2%** assertions use hardcoded
`frames`/`elapsed` and never touch `measure_rx_levels` — so this **cannot** soften that load-bearing
finding; removing the bias sharpens the live read toward it.

## Decision 4 — RSSI readout — `doctor --rssi`

A new uvk5-only live meter (no TX): stream the raw RSSI counts (`reg 0x67 & 0x1FF`, the same register
the busy path reads) with a per-sample busy verdict against `uvk5.squelch_threshold`, plus a min/mean/
max/busy summary — so the threshold is tuned from numbers, not guesswork.

## Decision 5 — the "HELLO not answered" quirk: document, no firmware change

Derived from the fork: ADR 0119 Decision 4 shows the V3 dock fork **hard-defines the link as
always-encrypted and removed the classic plaintext-`0x0514` toggle**, so a plaintext HELLO cannot be
answered on V3 — "not answered" is **correct**, not a fault. `doctor` already treats the version read
as best-effort (`pas`, never `fail`); the misleading "unread (HELLO not answered)" message and its
comment are reworded to state the derived truth (dock-alive is already proven by the register elicit;
ADR 0119). The separate **stock-firmware** branch, where the unguarded HELLO *does* answer, is
untouched. No side is "wrong" → **no pre-release bin.**

## Live bench validation — attempted, deferred (honest record)

Items 1 and 4 have a live component. The dev-PC UV-K5 (AIOC `da3441ac`) was driven this cycle but
**did not answer the dock probe:** the AIOC serial port opens and its **sound-card capture leg is
healthy** (`arecord` on the AIOC card succeeds), but the HT returned **zero bytes** and the connect
probe's register elicit timed out across **4 attempts** — the same result every time, i.e. not a
transient boot-race but a persistently unresponsive HT (powered off, or not on responsive dock
firmware). Powering on / waking / reflashing the radio is a physical bench action unavailable to a
headless cycle. So:

- **No live first-start repro before/after counts** (item 1) and **no live RSSI readings** (item 4)
  were captured. Those remain a **bench acceptance for Kris** (the F1/F2/F3a pattern).
- Everything is nonetheless proven **hardware-free**: the harness leg-split, the step-0 F3 probe, the
  radio-leg retry (incl. the dropped-0x0870 → re-send → REG_47-alive path via an extended
  `FirmwareFakeSerial` that models the F3 force-open), the capture reopen-on-floor, the stopwatch
  gap-exclusion, the RSSI stream, the shutdown-cancel swallow, and the HELLO reword all have tests.
  `uv run pytest` **1528 passed, 4 skipped** (was 1510/4).

**Bench acceptance (Kris):** with the F3 build flashed and the HT on —
`doctor --backend uvk5 --rx-firststart-loop 20` right after a cold boot / replug (record the F3
verdict + dead/N; re-run after and confirm 0 dead where it failed), and
`doctor --backend uvk5 --rssi` unkeyed (RSSI counts stream, busy tracks the threshold).

## Consequences

- **New `doctor` modes:** `--rx-firststart-loop N`, `--rssi` (both uvk5-only, no TX; both skip cleanly
  on other backends). No new config keys, no new deps.
- **`radio.py`:** `_enter_hw_mode_verified` (replaces the bare 0x0870 send), `_open_capture` reopen,
  the `capture_reopen_on_floor` kwarg (default OFF), a `_block_rms` local helper (the backend still
  does not import the `activity` layer). `frames.py` / `transport.py` unchanged (F2 invariant holds).
- **`app.py`:** every `?token=` WS handler swallows shutdown `CancelledError`.
- **Tests:** `FirmwareFakeSerial` gains an F3 force-open model + a droppable-0x0870 boot-race knob;
  new radio-leg/capture-leg tests, doctor harness/RSSI tests, a stopwatch gap regression, and a
  WS-shutdown-cancel regression. The `measure_rx_levels` MockRadio tests absorb the +1 prime read.

## Out of scope

No firmware change. D-STAR's independent per-link gate stays independent. Enabling
`capture_reopen_on_floor` by default (or wiring it to a config key) is deferred until the live repro
shows the host-audio leg. Powering/flashing the bench radio is Kris's bench step.
