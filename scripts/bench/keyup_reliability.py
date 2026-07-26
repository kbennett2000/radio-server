"""Does this station key EVERY time? Run before believing anything else measured here.

On 2026-07-26 the bench was found keying about half the time (ADR 0140) — 0/4 from cold, 4/4 warm —
and it had been doing so, undetected, through two ADRs' worth of single-shot RF numbers. A run of
this script would have caught it in ninety seconds.

So this is the pre-flight for the pre-flights: ten identical overs on a bench frequency, counted.
It is the first consumer of ``trials.py``, and the shape every other RF check here should copy —
the verdict is a fraction, not a reading.

Two failure shapes it distinguishes, which a single over cannot:

* **not unanimous** — the station is intermittent, and any other measurement taken today is a coin
  toss. Stop and fix this first.
* **unanimous but wide** — it keys every time, but the carrier length is moving, which usually means
  something upstream (audio lead, pacer, squelch) is not settled.

Run it cold (after the radio has been idle a few minutes) *and* warm, because ADR 0140's fault only
appears cold and a warm-only run reports a clean bench that is not one::

    RADIO_API_TOKEN=... .venv/bin/python scripts/bench/keyup_reliability.py --i-will-transmit
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from acceptance import BENCH_TX_HZ, KV4P_BASE, RADIO_BASE, api  # noqa: E402
from repeater_openup import BusyWatch, busy_seconds, tune_witness  # noqa: E402
from trials import DEFAULT_GAP_S, DEFAULT_N, require_unanimous, run_trials  # noqa: E402

LOUD_S = 0.30


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--i-will-transmit", action="store_true", help="required: this keys the radio")
    ap.add_argument("--frequency", type=int, default=445_800_000)
    ap.add_argument("-n", type=int, default=DEFAULT_N)
    ap.add_argument("--gap", type=float, default=DEFAULT_GAP_S)
    args = ap.parse_args(argv)
    if not args.i_will_transmit:
        print("refusing: pass --i-will-transmit (this keys the radio)", file=sys.stderr)
        return 2
    if args.frequency not in BENCH_TX_HZ:
        print(f"refusing: {args.frequency} Hz is not a bench frequency {sorted(BENCH_TX_HZ)}",
              file=sys.stderr)
        return 2

    watch = BusyWatch(KV4P_BASE)
    watch.start()
    time.sleep(0.5)
    if not watch.samples:
        print("refusing: the kv4p witness on :8091 is not answering", file=sys.stderr)
        watch.stop()
        return 2
    tune_witness(args.frequency)
    time.sleep(2.0)

    def probe() -> tuple[bool, float]:
        """One station-ID over; carrier seconds heard by the witness.

        `busy` is the SA818's SQ pin — a hardware carrier detect, not a threshold this project
        chose — so nothing of ours stands between the transmitter and the verdict. The over is
        `StationId.identify()`, so every transmission is Part 97-identified by construction.
        """
        start = time.monotonic()
        api(RADIO_BASE, "POST", "/services/01", timeout=90.0)
        end = time.monotonic()
        time.sleep(0.5)
        return (carrier := busy_seconds(watch.samples, start, end)) >= LOUD_S, carrier

    try:
        ts = run_trials(f"key-up on {args.frequency / 1e6:.4f}", probe, n=args.n, gap=args.gap)
    finally:
        watch.stop()

    print()
    print(ts.report())
    rc = require_unanimous(ts)
    if rc == 0:
        print("\n  ==> the station keys every time. Other measurements here can be believed.")
    else:
        print("\n  ==> NOT UNANIMOUS. Every other RF result taken today is a coin toss until this")
        print("      is understood — see ADR 0140 for what has already been ruled out.")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
