# 0062 — kv4p serial transport: appliedSequence handshake and reset-safe open

Status: Accepted

## Context

ADR 0061 landed the kv4p HT's pure wire codec (`frames.py`) and deferred the transport — the
piece that actually opens the UART, reads it, and writes to it. This cycle builds that:
`radio_server/backends/kv4p/transport.py` — the pyserial port, a daemon reader thread that
deframes and dispatches device→host frames, the flow-control window, and the reconciler's
sequence bookkeeping (`send_desired_state` / `await_applied`). It is still built and tested
against a **fake serial** (guardrail 6 — hardware bring-up is its own empirical phase); it does
not yet implement the `Radio`/`CatRadio` surface (the `Kv4pHt` class composes this with
`audio.py` and `frames.py` in a later cycle).

Two firmware facts force decisions that are neither obvious nor reversible-by-config, so they
are recorded here. Both were **read from the firmware as a specification** (kv4p-ht GPL-3.0 @
the shipped release **v2.0.0.1, `3f0e809baa02a946c3f0602681303f600c321d31`**,
`microcontroller-src/kv4p_ht_esp32_wroom_32/kv4p_ht_esp32_wroom_32.ino`), not asserted from
memory (guardrail 1); the one value we cannot read from a header is marked verify-on-bench.

> **Amended (ADR 0064).** This ADR originally pinned `e9935bd…`, an **unreleased** commit +44 ahead
> of v2.0.0.1 (both `FIRMWARE_VER = 17`). Decision 1's *mechanism* below — a sequence gate that
> silently ignores a lower `sequence`, and session flags applied "before the comparison" — is the
> `e9935bd` line. **Shipped v2.0.0.1 has no sequence gate at all:** `handleCommands` applies
> `HOST_DESIRED_STATE` unconditionally on `param_len == 22` via a whole-struct `memcpy` (no flag
> mask), so the "silently ignored" hazard does not arise on shipped. The `appliedSequence` sync
> `connect()` performs stays correct and safe (the device echoes the sequence we sent). The
> running-board timeout the bench saw (PR #116) is **edge-triggered status reports**
> (`deviceStateDirty` + `ENABLE_STATUS_REPORTS`), not a gate. See ADR 0064.

## Decision 1 — connect by syncing `DeviceState.appliedSequence`, never by waiting for a HELLO

The obvious connect design — attach, wait for the device's HELLO, read `windowSize`/version/
module/frequency-range from it, start sending — **does not work against a running device**, for
two reasons in the firmware:

- **The USB HELLO fires once, at boot.** `sendHello` for the USB session is called at the end of
  `setup()`; BT/BLE send HELLO on a connect *event*, but USB has none —
  `protocolUsbSession.connected` is hardcoded `true` at init. A host process that starts (or
  restarts) while the ESP32 is already running **never receives a HELLO**. So HELLO cannot be a
  precondition for anything.
- **The device's sequence is RAM-only and monotonic within a boot.**
  `loadPersistedRadioState()` sets `desiredState.sequence = 0` and never persists it, and
  `handleCommands` applies an incoming state only when `incoming.sequence > desiredState.sequence`.
  A freshly-started host that begins counting at 1 against a device already at, say, sequence 40
  is **silently ignored** — no error, no state change, no clue.

The firmware's own comment names the escape: hosts sync from `DeviceState.appliedSequence`. It
works because `handleCommands` applies the **session flags unconditionally, before** the sequence
comparison — so a probe carrying a stale sequence still turns on status reports and still
triggers `reconcileDesiredState()`. And `DeviceState` is only pushed when
`HOST_STATE_ENABLE_STATUS_REPORTS` is set.

**So `connect()` is:** send a probe `HostDesiredState` with `ENABLE_STATUS_REPORTS` set (the
firmware honours the flag regardless of the sequence and answers with a `DeviceState`) → read the
reported `appliedSequence` → set our counter so the next real send is `appliedSequence + 1` →
proceed. A HELLO, **if** one arrives (a device that booted after we attached), is treated as a
bonus: its `windowSize`/module type/frequency range are adopted over the defaults. Without one,
`windowSize` defaults to the firmware's `USB_BUFFER_SIZE = 2048` — the one number we cannot read
from a header, so it is a **marked default, verify-on-bench** (guardrail 1).

The probe itself is a normal `send_desired_state`, so it consumes a sequence number; that number
is discarded when we resync to `appliedSequence`. The probe uses `RADIO_CONFIG_VALID = 0`, so
even on a fresh device where the probe's sequence *is* higher and the state *is* applied, it
reconfigures nothing.

## Decision 2 — hold DTR and RTS inactive before `open()`, and do not reset-to-get-a-HELLO

On ESP32 dev boards DTR and RTS are wired to the auto-reset circuit (EN / GPIO0); the classic
Arduino-IDE trick toggles them to reset the chip and enter the bootloader. If pyserial asserts
either line as it opens the port, it can **reset the radio or drop it into the bootloader** on
every connect. The transport's `_default_serial_factory` therefore sets `.dtr = False` and
`.rts = False` **before** `open()` (pyserial applies pre-open line state at open) — the same
defensive shape as `aioc_baofeng._default_serial_factory`, for an entirely different reason
(device reset here, accidental keying there).

A tempting alternative is to *use* the reset line deliberately — pulse it on connect to reboot
the device and thereby always get a fresh HELLO (making Decision 1 unnecessary). We **reject**
that: it would reboot the radio on every server restart (dropping any in-progress activity and
adding seconds of boot latency), and the `appliedSequence` sync already solves the connect
problem without touching the device's power state. Whether pyserial's *default* line handling
actually resets **this** particular board is **verify-on-bench** — the guard is cheap and correct
either way, so we assert neither outcome.

## What the transport does (built this cycle)

- **Reader thread.** `serial.read()` → `KissDecoder.feed` → `parse_frame` → dispatch:
  `RX_AUDIO` → a bounded, drop-oldest queue (drops counted); `DEVICE_STATE` → latest state +
  `appliedSequence`; `HELLO` → adopt identity; `WINDOW_UPDATE` → credits; `DEBUG_*` → logging at
  the matching level; a KISS **DATA** frame → an inert `Ax25Frame` on a separate path (never a
  vendor sink), for the future text-over-RF arc. A read error (SerialException et al.) is
  **surfaced** — stored and re-raised to blocked writers/waiters — not swallowed into a silent
  wedge; a single malformed frame is logged and skipped without killing the reader.
- **Flow control in *encoded* bytes** (the gotcha ADR 0061 recorded). The firmware acks each
  frame with its escaped, FEND-inclusive length (`_encodedFrameLen`), so credit accounting counts
  the on-wire frame length, not the payload. `build_vendor_frame` already returns the escaped
  bytes, so `len(frame)` *is* the encoded length. A write blocks until the window has room and
  raises `Kv4pTimeout` rather than hanging a TX forever.
- **Reconciler bookkeeping.** `send_desired_state` assigns the next sequence, ORs in the session
  flags (which ride every frame — the `HOST_STATE_SESSION_FLAG_MASK` / `GLOBAL_FLAG_MASK` split
  from `frames.py`), encodes and writes; `await_applied(seq, timeout)` blocks on
  `DeviceState.appliedSequence`.
- **Lifecycle.** `close()` is idempotent and atexit-registered. Safe shutdown here is a
  *reconciled flag*, not a dropped line (there is none to drop): best-effort reconcile PTT off
  and confirm it applied (short, bounded — shutdown must never hang on a device that stopped
  answering), then stop the reader and close the port, fail-safe if the port is already gone.

## Consequences

- **Fully testable with zero hardware:** 15 fake-serial tests — the appliedSequence sync with and
  without a HELLO, sequence never regressing below the device's applied value, encoded-byte
  window accounting (block-at-zero / resume-on-`WINDOW_UPDATE` / timeout), per-command dispatch
  routing, DATA-frame inertness, and reader robustness across a mid-frame chunk boundary, a
  `b""` read, and a surfaced serial error. Full suite green.
- **Verify-on-bench (guardrail 1), recorded not asserted:** the `windowSize = 2048` default; that
  pyserial's default open does not (or does) reset this board; and the real serial device
  path/name (the CP210x/CH340 enumerates as `/dev/ttyUSB*`, unlike the AIOC's `/dev/ttyACM*`).
- **Throughput budget, an open measurement (not a problem):** one audio direction is ADPCM at
  ≈ 89 kbit/s ≈ 77% of the 115200 line, ≈ 64 blocks/sec, each block running cycle 2's pure-Python
  per-sample codec loop on the reader thread alongside the FastAPI loop. The reader must not
  stall; whether the Python codec keeps up in the composed backend is measured in the bring-up
  cycle, not here. **(Moot under shipped Opus, ADR 0064: no per-sample Python codec on the reader —
  variable-length Opus packets are decoded whole; the throughput profile is re-measured then.)**
- **Still deferred to the `Kv4pHt` backend cycle (ADR 0061):** which capabilities to advertise
  (`Capability.SCAN` for the software `ScanEngine` vs. the radio's absent hardware scan), and
  relaxing the `audio.squelch = "cat"` rejection (`api/app.py`) now that this backend has a real
  `SQUELCHED` busy line.
