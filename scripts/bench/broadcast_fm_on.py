#!/usr/bin/env python3
"""Send the dock frames radio-server deliberately cannot build, and print the RAW reply bytes.

**Why this exists, and why it is not the "no code changes in a bench cycle" rule being broken.**
That rule bars *fixes* during a bench run: repair the host or the firmware mid-cycle and no
measurement afterwards can tell you which change produced the result. This is not a fix. It is a
test instrument for two frames the fork's ``PROTOCOL.md`` already defines and that the production
code cannot emit:

* ``0x0879`` **action=ON**. ``frames.ClearBroadcastFm`` can only build OFF, on purpose — ADR 0157
  made "this server cannot turn broadcast FM on" a property of the code rather than a rule someone
  has to keep re-checking. Items 5, 7 and 8 of the bench acceptance need the radio *in* that state
  to observe what the host does about it, so the ON frame has to come from outside the server.
* ``0x0877`` set-modulation, sent here only so the **literal ``0x0878`` reply bytes** can be
  printed. The service logs the decoded fields at INFO and never the wire.

Nothing in ``radio_server/`` imports this, no route reaches it, and it is not wired into
``acceptance.py``. It talks to the AIOC serial directly, so **the service must be stopped first** —
the port is single-open (``docs/server-notes.md``).

It never keys. ``0x0879`` drives the BK1080, a receiver; ``0x0877`` chooses a demodulator. Both are
receive-side. Turning broadcast FM ON does, however, make the station **deaf on its own channel**
while it is on, and at F8 the firmware will still transmit — so clear it again when you are done
(``--off``), and never leave a station in this state.

Usage (on the bench box, service stopped)::

    systemctl --user stop radio-server
    .venv/bin/python scripts/bench/broadcast_fm_on.py --status
    .venv/bin/python scripts/bench/broadcast_fm_on.py --on 104300000
    .venv/bin/python scripts/bench/broadcast_fm_on.py --off
    .venv/bin/python scripts/bench/broadcast_fm_on.py --modulation AM
    systemctl --user start radio-server
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from radio_server.backends.uvk5.frames import (  # noqa: E402
    BroadcastFmReply,
    DockCommand,
    SetModulationReply,
    Uvk5Decoder,
    build_frame,
    parse_frame,
)
from radio_server.backends.uvk5.transport import _default_serial_factory  # noqa: E402

#: The wire's three actions (fork ``PROTOCOL.md``). The production codec names only OFF.
ACTION_OFF, ACTION_ON, ACTION_TUNE = 0, 1, 2
#: ``FM_Band`` 0 is 87.5-108 MHz — the band nearly every host wants, and a **two-bit** field.
BAND_87_5_108 = 0
#: The BK1080 tuning step. The firmware refuses anything off it with ``ERR_FIELD`` — refuse, never
#: round: the next raster step is a whole adjacent station.
RASTER_HZ = 100_000

DEFAULT_PORT = "/dev/serial/by-id/usb-AIOC_All-In-One-Cable_da3441ac-if04"


def _exchange(port: str, command: int, params: bytes, *, wait: float) -> bytes:
    """Write one frame, return every raw byte that came back within *wait* seconds.

    Opens through the transport's own ``_default_serial_factory`` rather than ``serial.Serial(...)``
    — **this line carries PTT**. pyserial asserts DTR on a plain open, DTR is the AIOC's key line
    (``aioc_baofeng.DEFAULT_PTT_LINE``), and a plain open here would key the transmitter on a
    receive-only probe. The factory sets both lines low *before* ``open()``; reusing it means this
    instrument cannot drift from the discipline the backend is held to (ADR 0111, guardrail 2).
    """
    frame = build_frame(command, params)
    ser = _default_serial_factory(port, 38400)
    try:
        ser.reset_input_buffer()
        print("TX raw : %s" % frame.hex())
        sent = time.monotonic()
        ser.write(frame)
        ser.flush()
        got, last, first_at = bytearray(), time.monotonic(), None
        while time.monotonic() - last < wait:
            chunk = ser.read(256)
            if chunk:
                if first_at is None:
                    first_at = time.monotonic()
                got.extend(chunk)
                last = time.monotonic()  # keep reading while bytes still arrive
        # Time-to-first-byte is the number that matters for the OFF leg: `Dock_FmOff` writes flash,
        # and how long that erase stalls the link is a `⚠ CONFIRM AT BENCH` item in the fork whose
        # only source is a firmware comment. The read window (`--wait`) is not that number.
        print("ttfb   : %s"
              % ("%.3f s" % (first_at - sent) if first_at else "<no reply>"))
        return bytes(got)
    finally:
        ser.close()


def _report(raw: bytes) -> None:
    print("RX raw : %s" % (raw.hex() if raw else "<nothing — timed out>"))
    print("RX len : %d bytes" % len(raw))
    if not raw:
        return
    for payload in Uvk5Decoder().feed(raw):
        print("payload: %s" % payload.hex())
        msg = parse_frame(payload)
        print("decoded: %r" % (msg,))
        if isinstance(msg, BroadcastFmReply):
            print(
                "         status=%s state=%d raw_hz=%d band=%d flags=0x%02x -> on=%r tx_ok=%r"
                % (msg.status.name, msg.state, msg.raw_hz, msg.raw_band, msg.flags,
                   msg.on, msg.tx_ok)
            )
        elif isinstance(msg, SetModulationReply):
            print(
                "         status=%s modulation=%d raw=%d flags=0x%02x -> name=%r tx_ok=%r"
                % (msg.status.name, msg.modulation, msg.raw, msg.flags, msg.name, msg.tx_ok)
            )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", default=DEFAULT_PORT)
    ap.add_argument("--wait", type=float, default=3.5,
                    help="seconds to keep reading (the OFF leg writes flash and stalls)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--on", type=int, metavar="HZ", help="0x0879 action=ON at HZ")
    g.add_argument("--tune", type=int, metavar="HZ", help="0x0879 action=TUNE to HZ")
    g.add_argument("--off", action="store_true", help="0x0879 action=OFF")
    g.add_argument("--status", action="store_true", help="0x0879 action=TUNE to 0 — a read probe")
    g.add_argument("--modulation", choices=["FM", "AM", "USB"], help="0x0877, prints raw 0x0878")
    args = ap.parse_args(argv)

    if args.modulation:
        value = {"FM": 0, "AM": 1, "USB": 2}[args.modulation]
        t0 = time.monotonic()
        raw = _exchange(args.port, DockCommand.SET_MODULATION, bytes([value]), wait=args.wait)
        print("elapsed: %.3f s" % (time.monotonic() - t0))
        _report(raw)
        return 0

    if args.on is not None:
        hz, action = args.on, ACTION_ON
    elif args.tune is not None:
        hz, action = args.tune, ACTION_TUNE
    elif args.status:
        hz, action = 0, ACTION_TUNE
    else:
        hz, action = 0, ACTION_OFF

    if hz and hz % RASTER_HZ:
        print("refusing: %d Hz is off the %d Hz raster (the firmware answers ERR_FIELD; the next "
              "step is a whole adjacent station)" % (hz, RASTER_HZ), file=sys.stderr)
        return 2

    params = struct.pack("<BIB", action, hz, BAND_87_5_108)
    t0 = time.monotonic()
    raw = _exchange(args.port, DockCommand.SET_BROADCAST_FM, params, wait=args.wait)
    print("elapsed: %.3f s" % (time.monotonic() - t0))
    _report(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
