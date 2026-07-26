"""When an over goes silent on 445.800, WHERE did the carrier actually go?

Every probe on this bench so far has asked "is it on the one frequency I expect?" -- which cannot
tell "not transmitting" from "transmitting somewhere else." That ambiguity is why the station's
~50% key rate (measured 2026-07-26) is still unexplained.

So: key normally on 445.800. If it is heard, that was a good burst -- wait and try again. If it is
SILENT, immediately hold PTT with `POST /ptt` and sweep the witness across a candidate list until
the carrier turns up. A transmitter inches from the witness is unmissable when the witness is
pointed anywhere near it, so this finds it if it is anywhere in the kv4p's 400-480 MHz.

Three outcomes, all actionable:
  * found on 445.800 every time      -> the earlier silences were something else entirely
  * found on some OTHER frequency    -> the radio moves (dual VFO / dual watch is the suspect:
                                        RADIO_SelectCurrentVfo, App/radio.c:715-721)
  * found nowhere in 400-480         -> either not radiating, or it is on 2 m (the deployed
                                        `uvk5.frequency` is 147 555 000) which this bench cannot
                                        hear. Say so; do not close the gap by assertion.

Part 97: every held carrier is bracketed by a station ID, and PTT release is in a `finally`.
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
KV4P_MIN_HZ, KV4P_MAX_HZ = 400_000_000, 480_000_000
LOUD_S = 0.30
HOLD_S = 14.0          # max continuous carrier per sweep leg
DWELL_S = 0.35         # per candidate: retune + settle + read
ROUNDS = 8
GAP_S = 8.0


def candidates() -> list[tuple[int, str]]:
    """Bench frequencies first, then every UHF preset leg, then the usual suspects."""
    out: list[tuple[int, str]] = [
        (445_800_000, "bench / panel"),
        (446_000_000, "bench alt (dock was told this)"),
        (446_400_000, "bench alt"),
    ]
    seen = {hz for hz, _ in out}
    try:
        presets = api(RADIO_BASE, "GET", "/presets")[1].get("presets", [])
    except Exception:  # noqa: BLE001
        presets = []
    for p in presets:
        for key, what in (("frequency", "preset out"), ("tx_frequency", "preset in")):
            hz = p.get(key)
            if hz and KV4P_MIN_HZ <= hz <= KV4P_MAX_HZ and hz not in seen:
                seen.add(hz)
                out.append((hz, f"{what} {p.get('name')}"))
    for hz in (446_006_250, 462_562_500, 433_500_000, 440_000_000, 470_000_000):
        if KV4P_MIN_HZ <= hz <= KV4P_MAX_HZ and hz not in seen:
            seen.add(hz)
            out.append((hz, "common default"))
    return out


def heard_now(watch: BusyWatch, dwell: float) -> bool:
    """Is the witness seeing a carrier right now?"""
    start = time.monotonic()
    time.sleep(dwell)
    return busy_seconds(watch.samples, start, time.monotonic()) >= dwell * 0.4


def normal_over(watch: BusyWatch) -> float:
    tune_witness(PANEL_HZ)
    time.sleep(2.0)
    start = time.monotonic()
    api(RADIO_BASE, "POST", "/services/01", timeout=90.0)
    end = time.monotonic()
    time.sleep(0.5)
    return busy_seconds(watch.samples, start, end)


def sweep(watch: BusyWatch, cands: list[tuple[int, str]]) -> tuple[int, str] | None:
    """Hold a carrier and walk the candidates. Returns the frequency it was found on."""
    found: tuple[int, str] | None = None
    i = 0
    while i < len(cands) and found is None:
        api(RADIO_BASE, "POST", "/ptt", body={"on": True}, timeout=30.0)
        leg_end = time.monotonic() + HOLD_S
        try:
            while i < len(cands) and time.monotonic() < leg_end:
                hz, why = cands[i]
                i += 1
                tune_witness(hz)
                if heard_now(watch, DWELL_S):
                    found = (hz, why)
                    print(f"      >>> CARRIER at {hz/1e6:.4f} ({why})")
                    break
        finally:
            api(RADIO_BASE, "POST", "/ptt", body={"on": False}, timeout=30.0)
        if found is None and i < len(cands):
            time.sleep(2.0)
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--i-will-transmit", action="store_true", help="required: this keys the radio")
    ap.add_argument("--rounds", type=int, default=ROUNDS)
    args = ap.parse_args(argv)
    if not args.i_will_transmit:
        print("refusing: pass --i-will-transmit (this keys the radio)", file=sys.stderr)
        return 2

    cands = candidates()
    print(f"{len(cands)} candidate frequencies in the kv4p's 400-480 MHz\n")

    watch = BusyWatch(KV4P_BASE)
    watch.start()
    time.sleep(0.5)
    if not watch.samples:
        print("refusing: the kv4p witness on :8091 is not answering", file=sys.stderr)
        watch.stop()
        return 2

    good = 0
    hits: list[tuple[int, str]] = []
    misses = 0
    try:
        api(RADIO_BASE, "POST", "/services/01", timeout=90.0)   # opening ID
        for r in range(1, args.rounds + 1):
            carrier = normal_over(watch)
            if carrier >= LOUD_S:
                good += 1
                print(f"  round {r}: 445.8000 carrier {carrier:.2f}s  OK")
            else:
                print(f"  round {r}: 445.8000 SILENT -- hunting...")
                where = sweep(watch, cands)
                if where is None:
                    misses += 1
                    print("      nothing anywhere in 400-480 MHz")
                else:
                    hits.append(where)
            time.sleep(GAP_S)
    finally:
        try:
            api(RADIO_BASE, "POST", "/ptt", body={"on": False}, timeout=30.0)
        except Exception:  # noqa: BLE001
            pass
        watch.stop()
        tune_witness(PANEL_HZ)
        try:
            api(RADIO_BASE, "POST", "/services/01", timeout=90.0)   # closing ID
        except Exception:  # noqa: BLE001
            pass

    print(f"\n  {good}/{args.rounds} overs heard on 445.800")
    if hits:
        print("  carrier found off-frequency on:")
        for hz, why in hits:
            print(f"    {hz/1e6:9.4f}  {why}")
        print("\n  ==> THE RADIO MOVES. It is not always on the frequency we key it for.")
    elif misses:
        print(f"  {misses} silent over(s), carrier found NOWHERE in 400-480 MHz")
        print("\n  ==> IT IS NOT RADIATING (or it is on 2 m, which this bench cannot hear).")
        print("      Physical path is the leading suspect: the AIOC sits in the K5's jacks.")
    else:
        print("\n  ==> Every over was heard. No silent over occurred to hunt this time.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
