"""Is the first key after an idle period lost -- and does holding PTT longer recover it?

`carrier_hunt` settled two things: when an over goes silent the carrier is NOWHERE in 400-480 MHz
(so the radio does not move frequency, it genuinely does not radiate), and the failures cluster at
the START of a run -- rounds 1-2 silent, rounds 3-8 all good. Every other run today fits that shape
too: keying repeatedly works, keying after a few idle minutes often does not.

That is a warm-up effect, and the UV-K5's battery-save duty-cycles the radio. A 1.5 s over
(1.0 s tx_lead + a CW ident) can fall entirely inside a sleep window; a longer PTT assertion cannot.

If that is what this is, the fix is pure software -- hold PTT longer -- and nobody has to touch the
radio. So test exactly that, cold each time:

    idle IDLE_S  ->  LONG hold (8 s)   -> heard?
                 ->  SHORT over (1.5 s, immediately after, radio now warm) -> heard?

and on alternate trials do the short one first, so "long works" cannot be confused with "the second
attempt always works".

Part 97: each trial is bracketed by a station ID. PTT release in a `finally`.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from acceptance import KV4P_BASE, RADIO_BASE, api  # noqa: E402
from repeater_openup import BusyWatch, busy_seconds, tune_witness  # noqa: E402

PANEL_HZ = 445_800_000
LOUD_S = 0.30
IDLE_S = 150.0
LONG_HOLD_S = 8.0


def long_hold(watch: BusyWatch, seconds: float = LONG_HOLD_S) -> float:
    start = time.monotonic()
    api(RADIO_BASE, "POST", "/ptt", body={"on": True}, timeout=30.0)
    try:
        time.sleep(seconds)
    finally:
        api(RADIO_BASE, "POST", "/ptt", body={"on": False}, timeout=30.0)
    end = time.monotonic()
    time.sleep(0.5)
    return busy_seconds(watch.samples, start, end)


def short_over(watch: BusyWatch) -> float:
    start = time.monotonic()
    api(RADIO_BASE, "POST", "/services/01", timeout=90.0)
    end = time.monotonic()
    time.sleep(0.5)
    return busy_seconds(watch.samples, start, end)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--i-will-transmit", action="store_true", help="required: this keys the radio")
    ap.add_argument("--trials", type=int, default=4)
    ap.add_argument("--idle", type=float, default=IDLE_S)
    args = ap.parse_args(argv)
    if not args.i_will_transmit:
        print("refusing: pass --i-will-transmit (this keys the radio)", file=sys.stderr)
        return 2

    watch = BusyWatch(KV4P_BASE)
    watch.start()
    time.sleep(0.5)
    tune_witness(PANEL_HZ)
    time.sleep(2.0)

    cold_long: list[float] = []
    cold_short: list[float] = []
    warm_long: list[float] = []
    warm_short: list[float] = []

    try:
        for t in range(1, args.trials + 1):
            print(f"\ntrial {t}: idling {args.idle:.0f}s to go cold...")
            time.sleep(args.idle)
            long_first = t % 2 == 1
            if long_first:
                c = long_hold(watch)
                cold_long.append(c)
                print(f"  COLD  long  {LONG_HOLD_S:.0f}s hold : carrier {c:5.2f}s  "
                      f"{'OK' if c >= LOUD_S else 'DEAF'}")
                time.sleep(4.0)
                w = short_over(watch)
                warm_short.append(w)
                print(f"  warm  short over      : carrier {w:5.2f}s  "
                      f"{'OK' if w >= LOUD_S else 'DEAF'}")
            else:
                c = short_over(watch)
                cold_short.append(c)
                print(f"  COLD  short over      : carrier {c:5.2f}s  "
                      f"{'OK' if c >= LOUD_S else 'DEAF'}")
                time.sleep(4.0)
                w = long_hold(watch)
                warm_long.append(w)
                print(f"  warm  long  {LONG_HOLD_S:.0f}s hold : carrier {w:5.2f}s  "
                      f"{'OK' if w >= LOUD_S else 'DEAF'}")
    finally:
        try:
            api(RADIO_BASE, "POST", "/ptt", body={"on": False}, timeout=30.0)
        except Exception:  # noqa: BLE001
            pass
        watch.stop()
        tune_witness(PANEL_HZ)

    def rate(v: list[float]) -> str:
        if not v:
            return "n/a"
        return f"{sum(1 for x in v if x >= LOUD_S)}/{len(v)}"

    print("\n  heard rates")
    print(f"    COLD long hold  : {rate(cold_long)}")
    print(f"    COLD short over : {rate(cold_short)}")
    print(f"    warm long hold  : {rate(warm_long)}")
    print(f"    warm short over : {rate(warm_short)}")

    cl = sum(1 for x in cold_long if x >= LOUD_S)
    cs = sum(1 for x in cold_short if x >= LOUD_S)
    if cold_long and cold_short and cl == len(cold_long) and cs < len(cold_short):
        print("\n  ==> HOLDING PTT LONGER FIXES IT. A short over from cold is lost; a long one is")
        print("      not. Fix is software -- lengthen the keying lead. Nobody touches the radio.")
    elif cold_long and cl < len(cold_long):
        print("\n  ==> LENGTH IS NOT THE ANSWER: even a long hold is lost from cold. The radio is")
        print("      asleep or the path is intermittent; a wake-up key or a hardware look is next.")
    else:
        print("\n  ==> No cold failure reproduced this run; the warm-up effect did not appear.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
