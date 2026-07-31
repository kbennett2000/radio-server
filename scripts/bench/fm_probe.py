#!/usr/bin/env python3
"""Does an out-of-band ``0x0879`` TUNE **refuse**, or does it **clamp**? Measure before building.

**This instrument gates ADR 0162.** The host has no way to ask a UV-K5 whether its BK1080 second
receiver is running: ``ClearBroadcastFm`` builds only OFF (ADR 0157) and the firmware reports state
*after* acting, which is why ADR 0161 concluded — wrongly — that "the wire offers no read that is
not also a repair".

Reading ``Dock_SetFm`` (fork ``App/app/uart.c``) shows the guards run in this order::

    gCurrentFunction == TRANSMIT || MONITOR      -> ERR_TX,   return
    action == TUNE && !gFmRadioMode              -> ERR_OFF,  return   <-- FM is off
    raster32 < lo || raster32 > hi               -> ERR_BAND, return   <-- FM is on
    ... only now does anything happen

So a TUNE at a frequency **no band can accept** returns before touching the radio, and *which*
refusal comes back is a complete, non-mutating answer:

===============  ==========================================  =========
reply            proves                                      mutates
===============  ==========================================  =========
``ERR_OFF`` (9)  ``gFmRadioMode == false`` — station hears    nothing
``ERR_BAND`` (6) ``gFmRadioMode == true``  — station is deaf  nothing
===============  ==========================================  =========

**0 Hz, not 64 MHz, and the choice is load-bearing.** ``BK1080_GetFreqLoLimit`` is
``{875, 760, 760, 640}`` in 100 kHz units, so 64.0 MHz is out of band **only under band 0** — it is
a legal band-3 frequency. 0 Hz is below every floor in the table, so the probe stays a probe even if
a future firmware ignores or defaults the band field. It is on-raster (``0 % 100000 == 0``), so
``dock.c`` passes it through to the HAL rather than answering ``ERR_FIELD`` on the host side.

**The claim being tested is that the firmware REFUSES rather than CLAMPS.** If any build silently
clamped an out-of-band tune to the nearest legal channel, this "probe" would retune the operator's
broadcast receiver and would not be a read at all. The fork's own host tests never exercise the real
band-limit branch — ``test_dock.c:1176`` forces the status with ``g_fm_force_status`` — so the
branch has never executed anywhere, on host or radio, until this script runs it.

Talks to the AIOC serial directly, so **stop the service first** (the port is single-open). It never
keys: ``0x0879`` drives a receiver and ``0x0873``/``0x0877`` are refused or receive-side. It turns
broadcast FM on for the middle legs and **always** turns it off again in a ``finally``.

Usage (on the bench box, service stopped)::

    systemctl --user stop radio-server
    .venv/bin/python scripts/bench/fm_probe.py --fm-hz 104300000
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
    BroadcastFmStatus,
    DockCommand,
    SetModulationReply,
    SetVfoProbe,
    SetVfoReply,
    Uvk5Decoder,
    build_frame,
    parse_frame,
)
from radio_server.backends.uvk5.transport import _default_serial_factory  # noqa: E402

ACTION_OFF, ACTION_ON, ACTION_TUNE = 0, 1, 2
#: ``FM_Band`` 0 is 87.5-108 MHz, floor 875 in 100 kHz units.
BAND_87_5_108 = 0
#: Below every floor in ``BK1080_GetFreqLoLimit`` ``{875, 760, 760, 640}``, and on the 100 kHz
#: raster so ``dock.c`` forwards it instead of answering ``ERR_FIELD`` itself.
PROBE_HZ = 0

DEFAULT_PORT = "/dev/serial/by-id/usb-AIOC_All-In-One-Cable_da3441ac-if04"


def _exchange(port: str, command: int, params: bytes, *, wait: float) -> bytes:
    """Write one frame, return every raw byte back within *wait* seconds.

    Opens through the transport's own factory rather than ``serial.Serial(...)`` — **this line
    carries PTT**. pyserial asserts DTR on a plain open and DTR is the AIOC's key line, so a plain
    open would key the transmitter on a receive-only probe (ADR 0111, guardrail 2).
    """
    ser = _default_serial_factory(port, 38400)
    try:
        ser.reset_input_buffer()
        ser.write(build_frame(command, params))
        ser.flush()
        got, last = bytearray(), time.monotonic()
        while time.monotonic() - last < wait:
            chunk = ser.read(256)
            if chunk:
                got.extend(chunk)
                last = time.monotonic()
        return bytes(got)
    finally:
        ser.close()


def _decode(raw: bytes):
    for payload in Uvk5Decoder().feed(raw):
        return parse_frame(payload), payload
    return None, b""


def _fm(port: str, action: int, hz: int, *, wait: float):
    raw = _exchange(port, DockCommand.SET_BROADCAST_FM,
                    struct.pack("<BIB", action, hz, BAND_87_5_108), wait=wait)
    msg, payload = _decode(raw)
    return (msg if isinstance(msg, BroadcastFmReply) else None), payload


def _modulation(port: str, *, wait: float):
    raw = _exchange(port, DockCommand.SET_MODULATION, bytes([0]), wait=wait)  # 0 = FM
    msg, payload = _decode(raw)
    return (msg if isinstance(msg, SetModulationReply) else None), payload


def _vfo_probe(port: str, *, wait: float):
    raw = _exchange(port, DockCommand.SET_VFO, SetVfoProbe().pack(), wait=wait)
    msg, payload = _decode(raw)
    return (msg if isinstance(msg, SetVfoReply) else None), payload


def _show(label: str, reply, payload: bytes) -> None:
    print("  %-28s %-10s  payload=%s"
          % (label,
             reply.status.name if reply is not None else "<silent>",
             payload.hex() if payload else "-"))


def _keyed_probe(args) -> int:
    """B4: what does the probe answer while the radio is actually transmitting?

    `Dock_SetFm` refuses with `ERR_TX` when `gCurrentFunction` is `FUNCTION_TRANSMIT` **or
    `FUNCTION_MONITOR`**, and that refusal is the one ADR 0161 met on this exact code path — it
    treated it as a fault and took the Part 97 station ID off the air twice in four minutes. So the
    fall-through has to be measured against a *real* refusal rather than a stub that returns it.

    **This radiates.** DTR is the AIOC's PTT line, so the radio must already be on a bench frequency
    with a witness. The key is held for well under a second and dropped in a `finally`.
    """
    ser = _default_serial_factory(args.port, 38400)
    try:
        ser.reset_input_buffer()
        print("B4   asserting DTR — THE TRANSMITTER IS NOW KEYED")
        ser.dtr = True
        time.sleep(0.3)                    # let FUNCTION_Select land on FUNCTION_TRANSMIT
        ser.write(build_frame(DockCommand.SET_BROADCAST_FM,
                              struct.pack("<BIB", ACTION_TUNE, PROBE_HZ, BAND_87_5_108)))
        ser.flush()
        got, last = bytearray(), time.monotonic()
        while time.monotonic() - last < 1.0:
            chunk = ser.read(256)
            if chunk:
                got.extend(chunk)
                last = time.monotonic()
    finally:
        ser.dtr = False
        ser.close()
        print("     DTR dropped — unkeyed")

    msg, payload = _decode(bytes(got))
    reply = msg if isinstance(msg, BroadcastFmReply) else None
    _show("probe while keyed", reply, payload)
    if reply is None:
        print("\nRESULT: INCONCLUSIVE — no reply while keyed")
        return 1
    if reply.status is not BroadcastFmStatus.ERR_TX:
        print("\nRESULT: INCONCLUSIVE — expected ERR_TX, got %s. The refusal this measures is the\n"
              "one the host must fall through on; a different status does not exercise it."
              % reply.status.name)
        return 1
    print("\nRESULT: PASS — the radio refuses the probe mid-over with ERR_TX, which\n"
          "`probe_broadcast_fm` maps to None and `_clear_if_deafened` ignores entirely.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", default=DEFAULT_PORT)
    ap.add_argument("--wait", type=float, default=3.5)
    ap.add_argument("--fm-hz", type=int, default=104_300_000,
                    help="where to park the BK1080 for the FM-on legs")
    ap.add_argument("--keyed", action="store_true",
                    help="B4: assert DTR (KEYS THE TRANSMITTER) and probe mid-over, to\nmeasure the ERR_TX refusal that ADR 0161 met on this path. Put the radio on a bench\nfrequency with a witness FIRST — this radiates.")
    args = ap.parse_args(argv)

    if args.keyed:
        return _keyed_probe(args)

    fails: list[str] = []
    notes: list[str] = []
    try:
        # ---- B1a: the probe against a receiver that is OFF -------------------------------
        print("B1a  broadcast FM off — probe must answer ERR_OFF")
        off_probe, off_payload = _fm(args.port, ACTION_TUNE, PROBE_HZ, wait=args.wait)
        _show("probe (TUNE, 0 Hz)", off_probe, off_payload)
        if off_probe is None:
            fails.append("B1a: no reply — pre-F8 firmware, or the radio is not listening")
        elif off_probe.status is BroadcastFmStatus.APPLIED:
            fails.append("B1a: APPLIED — THE FIRMWARE CLAMPED. This is not a read. Ship no probe.")
        elif off_probe.status is not BroadcastFmStatus.ERR_OFF:
            fails.append("B1a: expected ERR_OFF, got %s" % off_probe.status.name)

        # ---- B2 controls: what else moves while the BK1080 runs? --------------------------
        print("B2   controls with FM off")
        mod_a, mod_a_payload = _modulation(args.port, wait=args.wait)
        _show("0x0877 FM -> 0x0878", mod_a, mod_a_payload)
        vfo_a, vfo_a_payload = _vfo_probe(args.port, wait=args.wait)
        _show("0x0873 empty -> 0x0874", vfo_a, vfo_a_payload)

        # ---- B1b: switch the receiver on -------------------------------------------------
        print("B1b  broadcast FM ON at %.1f MHz" % (args.fm_hz / 1e6))
        on_reply, on_payload = _fm(args.port, ACTION_ON, args.fm_hz, wait=args.wait)
        _show("0x0879 ON", on_reply, on_payload)
        if on_reply is None or not on_reply.ok or on_reply.on is not True:
            fails.append("B1b: could not put the radio into broadcast FM — later legs prove nothing")

        # ---- B1c: the probe against a receiver that is ON --------------------------------
        print("B1c  probe must answer ERR_BAND, twice, changing nothing")
        on_probe, on_probe_payload = _fm(args.port, ACTION_TUNE, PROBE_HZ, wait=args.wait)
        _show("probe (TUNE, 0 Hz)", on_probe, on_probe_payload)
        again, again_payload = _fm(args.port, ACTION_TUNE, PROBE_HZ, wait=args.wait)
        _show("probe again", again, again_payload)
        for tag, rep in (("B1c", on_probe), ("B1c-repeat", again)):
            if rep is None:
                fails.append("%s: no reply while FM was on" % tag)
            elif rep.status is BroadcastFmStatus.APPLIED:
                fails.append("%s: APPLIED — THE FIRMWARE CLAMPED and just retuned the receiver."
                             % tag)
            elif rep.status is not BroadcastFmStatus.ERR_BAND:
                fails.append("%s: expected ERR_BAND, got %s" % (tag, rep.status.name))
        if (on_probe_payload and again_payload and on_probe_payload != again_payload):
            fails.append("B1c: two identical probes answered differently — the probe is not pure")

        # ---- B2 legs: the same two reads, with the BK1080 running -------------------------
        print("B2   the same reads with FM on")
        mod_b, mod_b_payload = _modulation(args.port, wait=args.wait)
        _show("0x0877 FM -> 0x0878", mod_b, mod_b_payload)
        vfo_b, vfo_b_payload = _vfo_probe(args.port, wait=args.wait)
        _show("0x0873 empty -> 0x0874", vfo_b, vfo_b_payload)
        notes.append("0x0878 differs with FM on: %s" % (
            "YES  %s -> %s" % (mod_a_payload.hex(), mod_b_payload.hex())
            if mod_a_payload != mod_b_payload else "no (byte-identical)"))
        notes.append("0x0874 differs with FM on: %s" % (
            "YES  %s -> %s" % (vfo_a_payload.hex(), vfo_b_payload.hex())
            if vfo_a_payload != vfo_b_payload else "no (byte-identical)"))

        # ---- B1d: did the probes move the receiver? --------------------------------------
        # The OFF reply reports `gEeprom.FM_FrequencyPlaying` as it was at the moment of the OFF.
        # If any probe had tuned the BK1080, this is where it shows.
        print("B1d  the OFF reply reports where the receiver actually was")
        off_reply, off_reply_payload = _fm(args.port, ACTION_OFF, 0, wait=args.wait)
        _show("0x0879 OFF", off_reply, off_reply_payload)
        if off_reply is None or not off_reply.ok:
            fails.append("B1d: the OFF leg did not answer APPLIED — cannot confirm the frequency")
        else:
            print("       receiver was on %s" % (
                "%.1f MHz" % (off_reply.hz / 1e6) if off_reply.hz else "an unreported frequency"))
            if off_reply.hz != args.fm_hz:
                fails.append("B1d: receiver was on %r, not the %r it was put on — A PROBE MOVED IT"
                             % (off_reply.hz, args.fm_hz))
            if off_reply.on is not False:
                fails.append("B1d: the OFF leg did not report the receiver off")
    finally:
        # Never leave the station deaf, whatever happened above.
        parting, _ = _fm(args.port, ACTION_OFF, 0, wait=args.wait)
        print("teardown: broadcast FM %s"
              % ("off" if parting is not None and parting.on is False else "STATE UNKNOWN — CHECK"))

    print()
    for note in notes:
        print("note: %s" % note)
    if fails:
        print()
        for f in fails:
            print("FAIL: %s" % f)
        print("\nRESULT: FAIL — do not build the probe on this firmware")
        return 1
    print("\nRESULT: PASS — an out-of-band TUNE refuses without mutating; it is a read")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
