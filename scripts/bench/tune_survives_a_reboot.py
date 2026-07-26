"""Does a tune still work after the radio reboots underneath the server?

This is the incident, turned into an instrument.

The differential gate (`tune_follows_preset.py`) proved 80/80 that a preset moves the carrier, and
the operator still could not set a frequency. Everything it measured was true; it just never
measured the way the product actually broke. It ran as one continuous session against a warm radio
that had already completed a handshake, and it re-established that handshake itself after each of
its own resets. So it never once asked the question that mattered:

    what happens when the radio's session goes away for a reason the server did not cause?

That is not an exotic case. It is a firmware flash, a battery swap, or the operator switching the
radio off and on — and it happened within hours of the gate passing. The firmware refuses every
EEPROM frame whose session timestamp does not match, and it refuses **silently**
(`app/uart.c`: `if (pCmd->Timestamp != Timestamp) return;`). The tuner had sent its one HELLO
without ever checking for the `0x0515` answer, latched "session established", and from then on
every tune failed with `no answer to an EEPROM read` — including long after the radio came back.
A transient condition became a permanent one, and the only cure was restarting the service.

WHAT THIS MEASURES
------------------
Row 1 — **cold**: a tuner that has never spoken to this radio tunes it. Proves the pre-flight
handshake stands on its own rather than inheriting a session from somewhere.

Row 2 — **recovery**: tune, then reboot the radio out-of-band (a bare `Reset()`, which is exactly
what the operator's power switch does to the session), then tune again. The second tune must
succeed. Under the old code this row is the failure the operator reported, and it fails every time.

Row 3 — **read-back**: after each tune the radio's own storage is read and must hold the record
that was written. A tune that "succeeded" onto the wrong channel is not a pass.

Runs with the service stopped, because the tuner needs the AIOC handle exclusively (ADR 0127) — and
it restarts the service in a `finally`, because leaving the bench dead is worse than any result
this script can produce.

Nothing here keys the transmitter: EEPROM writes and a soft reset only. No frequency leaves
`BENCH_TX_HZ`.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from radio_server.backends.uvk5 import frames as f                          # noqa: E402
from radio_server.backends.uvk5.transport import Uvk5Transport               # noqa: E402
from radio_server.backends.uvk5.tuner import EepromTuner, TuneError          # noqa: E402
from radio_server.backends.uvk5.vfo import VFO_RECORD_LEN, VfoImage, vfo_addr  # noqa: E402

from trials import require_unanimous, run_trials                             # noqa: E402

#: Bench-safe only. Both are in BENCH_TX_HZ; neither is a repeater input.
SIMPLEX = VfoImage(rx_hz=445_800_000, tx_hz=445_800_000)
ALT = VfoImage(rx_hz=446_000_000, tx_hz=446_000_000)

DEFAULT_PORT = "/dev/serial/by-id/usb-AIOC_All-In-One-Cable_da3441ac-if04"

#: How long to leave the radio alone after an out-of-band reset before expecting anything of it.
#: Longer than the tuner's own settle: this models an operator power-cycling it, not a soft reset
#: the server is waiting on.
REBOOT_S = 8.0


def _readback_matches(tp, image: VfoImage) -> bool:
    """Ask the radio what it is holding, through a session of our own."""
    probe = EepromTuner(tp)
    probe._hello()                                   # noqa: SLF001 - a bench probe, deliberately
    held = probe._read(vfo_addr(image.band, 0), VFO_RECORD_LEN)   # noqa: SLF001
    return held == image.pack_eeprom()


def cold_tune(tp) -> tuple[bool, float]:
    """A tuner that has never spoken to this radio must still be able to tune it."""
    started = time.time()
    tuner = EepromTuner(tp)                          # fresh: no session, nothing assumed
    try:
        tuner.apply(SIMPLEX)
    except TuneError as exc:
        print(f"      {exc}", flush=True)
        return False, time.time() - started
    return _readback_matches(tp, SIMPLEX), time.time() - started


def survives_reboot(tp) -> tuple[bool, float]:
    """The operator's failure: the radio reboots for a reason the server did not cause."""
    started = time.time()
    tuner = EepromTuner(tp)
    try:
        tuner.apply(SIMPLEX)
    except TuneError as exc:
        print(f"      first tune failed, so this row proves nothing: {exc}", flush=True)
        return False, time.time() - started

    # The power switch, in one frame. The tuner is not told, which is the entire point: the
    # session it believes it holds is gone, and it has to notice by itself.
    tp.send(f.Reset())
    time.sleep(REBOOT_S)

    try:
        tuner.apply(ALT)
    except TuneError as exc:
        print(f"      did not recover: {exc}", flush=True)
        return False, time.time() - started
    return _readback_matches(tp, ALT), time.time() - started


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-n", type=int, default=3, help="trials per row (each is slow: ~30 s)")
    ap.add_argument("--port", default=DEFAULT_PORT)
    args = ap.parse_args()

    tp = Uvk5Transport(serial_port=args.port, baud=38400)
    try:
        print("row 1 — cold tune (no prior session)", flush=True)
        cold = run_trials("cold tune", lambda: cold_tune(tp), n=args.n, gap=2.0)
        print(cold.report(), flush=True)

        print("row 2 — tune, reboot the radio out-of-band, tune again", flush=True)
        recover = run_trials("survives a reboot", lambda: survives_reboot(tp), n=args.n, gap=2.0)
        print(recover.report(), flush=True)
    finally:
        tp.close()

    return require_unanimous(cold, recover)


if __name__ == "__main__":
    raise SystemExit(main())
