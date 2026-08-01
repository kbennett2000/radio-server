#!/usr/bin/env python3
"""Does the F9 firmware actually refuse to transmit while the radio cannot hear itself?

**This is the one claim ADR 0159 makes that had never met a radio.** F9 appends a clause to
``RADIO_PrepareTX`` so that a UV-K5 playing broadcast FM will not key — and the AIOC's DTR line
drives ``GPIO_PIN_PTT``, the same pin the rubber button drives, so that clause is supposed to gate
this backend's only keying path. Everything about it was read out of the fork; nothing was measured.

The measurement is an ABSENCE, which is the kind that is easiest to fake and hardest to trust. Three
things make it evidence rather than a shrug:

* **A positive control first, at the same frequency, through the same line.** An absence counts only
  because the identical setup had just produced a presence. The witness is a UHF-only SA818, so a
  station outside its band reads "no RF" whether or not the radio keyed — that is not weak evidence,
  it is *no* evidence (``docs/server-notes.md``).
* **A second positive control afterwards**, so a setup that quietly died half way through cannot
  masquerade as an interlock working.
* **The firmware level is read from the wire in the same run.** ``0x087A`` ``flags`` bit 1 set while
  broadcast FM is on IS the F9-with-interlock probe. If it is clear, the image has no interlock and
  the middle leg proves nothing — so this script says so and stops rather than reporting a pass.

**The service is stopped for the whole sequence, and that is not incidental.** Since ADR 0161 the
host clears broadcast FM before every key-up, so a running server would repair the very condition
under test between the instrument setting it and the line going high. Stopping it is what makes the
firmware the only thing in the way. ADR 0127's rule applies: stopping around a single-open test is
sanctioned, *leaving* it stopped is not — hence the ``finally``, which also clears the receiver so a
crashed run can never walk away from a deaf station.

Usage (on the bench box)::

    RADIO_API_TOKEN=... .venv/bin/python scripts/bench/fm_tx_interlock.py --i-will-transmit
"""

from __future__ import annotations

import argparse
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import broadcast_fm_on as bfm  # noqa: E402
import deviation_probe as dp  # noqa: E402
from acceptance import BENCH_TX_HZ, KV4P_BASE, api, rms, open_tty  # noqa: E402

from radio_server.backends.uvk5.frames import (  # noqa: E402
    BroadcastFmReply,
    DockCommand,
    Uvk5Decoder,
    parse_frame,
)

SERVICE = "radio-server.service"
#: The BK1080 has to be tuned somewhere real for the leg to be honest: a receiver parked on a dead
#: carrier still holds the speaker line, but a strong local station makes "the station is deaf"
#: audible as well as structural. 104.3 is where ADR 0160 found it.
BROADCAST_HZ = 104_300_000


def _systemctl(*args: str) -> int:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True).returncode


def _fm(action: int, hz: int, port: str) -> BroadcastFmReply | None:
    """One `0x0879`, decoded. Returns None when the radio said nothing."""
    params = struct.pack("<BIB", action, hz, bfm.BAND_87_5_108)
    raw = bfm._exchange(port, DockCommand.SET_BROADCAST_FM, params, wait=3.5)
    for payload in Uvk5Decoder().feed(raw):
        msg = parse_frame(payload)
        if isinstance(msg, BroadcastFmReply):
            return msg
    return None


def _describe(reply: BroadcastFmReply | None) -> str:
    if reply is None:
        return "<no 0x087A reply>"
    return (
        f"status={reply.status.name} state={reply.state} hz={reply.raw_hz} "
        f"flags=0x{reply.flags:02x} -> on={reply.on!r} tx_ok={reply.tx_ok!r} "
        f"fm_blocks_tx={reply.fm_blocks_tx!r} will_key={reply.will_key!r}"
    )


def _key_and_witness(port: str, seconds: float) -> tuple[float, int]:
    """Assert DTR for ``seconds`` while the witness listens. Returns (RMS, samples).

    The capture brackets the key-up on both sides so a witness that started late cannot turn a real
    carrier into an absence — the failure mode that would produce exactly the result being looked
    for, which is the one to design against.
    """
    import serial

    captured: dict[str, bytes] = {}

    def listen() -> None:
        captured["pcm"] = dp.listen(seconds + 2.0)

    t = threading.Thread(target=listen, daemon=True)
    t.start()
    time.sleep(1.0)  # let the capture be running before the line goes high
    with open_tty(port, 9600, timeout=1) as ser:
        # Both low first: an inherited-high line would make the "off" leg a keyed one.
        ser.dtr = False
        ser.rts = False
        time.sleep(0.2)
        try:
            ser.dtr = True
            time.sleep(seconds)
        finally:
            ser.dtr = False
            ser.rts = False
    t.join(timeout=seconds + 8.0)
    pcm = captured.get("pcm", b"")
    return rms(pcm), len(pcm)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--i-will-transmit", action="store_true",
                    help="required: this keys the radio")
    ap.add_argument("--frequency", type=int, default=445_800_000,
                    help="where the station is parked; must be a bench frequency the witness hears")
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--port", default=bfm.DEFAULT_PORT)
    args = ap.parse_args(argv)

    if not args.i_will_transmit:
        print("refusing: pass --i-will-transmit (this keys the radio)", file=sys.stderr)
        return 2
    if args.frequency not in BENCH_TX_HZ:
        print(f"refusing: {args.frequency} Hz is not a bench frequency {sorted(BENCH_TX_HZ)}",
              file=sys.stderr)
        return 2

    print(f"witness -> {args.frequency} Hz")
    code, body = api(KV4P_BASE, "POST", "/frequency", body={"hz": args.frequency})
    if code != 200:
        print(f"  could not tune the witness ({code} {body!r:.80})", file=sys.stderr)
        return 2
    time.sleep(2.0)

    print(f"stopping {SERVICE} — a running server would clear broadcast FM before the key-up")
    _systemctl("stop", SERVICE)
    time.sleep(2.0)

    results: dict[str, tuple[float, int]] = {}
    verdict = 1
    try:
        off = _fm(bfm.ACTION_OFF, 0, args.port)
        print(f"\n[control A] broadcast FM off: {_describe(off)}")
        results["control A (FM off)"] = _key_and_witness(args.port, args.seconds)
        print(f"  witness RMS {results['control A (FM off)'][0]:.1f}")

        on = _fm(bfm.ACTION_ON, BROADCAST_HZ, args.port)
        print(f"\n[interlock] broadcast FM ON: {_describe(on)}")
        if on is None or not on.ok or on.on is not True:
            print("  the receiver did not come on — the leg below would prove nothing", file=sys.stderr)
            return 2
        if not on.fm_blocks_tx:
            # The level probe, and it is the difference between an experiment and a shrug. An image
            # without ENABLE_DOCK_FM_TX_INTERLOCK answers 0 here and WILL key while deaf, so an
            # absence at the witness would have to mean something else entirely.
            print("  flags bit 1 is CLEAR: this image has no interlock (F8 or a non-Fusion build).",
                  file=sys.stderr)
            print("  Refusing to report an absence as a pass.", file=sys.stderr)
            return 2
        results["interlock (FM on)"] = _key_and_witness(args.port, args.seconds)
        print(f"  witness RMS {results['interlock (FM on)'][0]:.1f}")

        off2 = _fm(bfm.ACTION_OFF, 0, args.port)
        print(f"\n[control B] broadcast FM off again: {_describe(off2)}")
        results["control B (FM off)"] = _key_and_witness(args.port, args.seconds)
        print(f"  witness RMS {results['control B (FM off)'][0]:.1f}")

        a, blocked, b = (results[k][0] for k in
                         ("control A (FM off)", "interlock (FM on)", "control B (FM off)"))
        print("\n=== verdict ===")
        for label, (value, _n) in results.items():
            print(f"  {label:24s} RMS {value:9.1f}")
        if a <= dp.SILENCE_RMS or b <= dp.SILENCE_RMS:
            print("  INCONCLUSIVE: a control produced no carrier, so the absence means nothing.")
        elif blocked > dp.SILENCE_RMS:
            print("  FAIL: the radio transmitted while playing broadcast FM.")
        else:
            print("  PASS: carrier, then NO carrier while deaf, then carrier again.")
            verdict = 0
    finally:
        # Never walk away from a deaf station, whatever went wrong above.
        print("\nclearing broadcast FM and restarting the service")
        try:
            print(f"  {_describe(_fm(bfm.ACTION_OFF, 0, args.port))}")
        except Exception as exc:  # noqa: BLE001 - the restart matters more than this report
            print(f"  could not clear over the wire ({exc}); the boot assert will")
        _systemctl("start", SERVICE)
    return verdict


if __name__ == "__main__":
    raise SystemExit(main())
