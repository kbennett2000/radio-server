"""DTR, RTS, or both — which control line actually keys this radio? N-of-M, not one sample each.

`ptt_line = "dtr"` is the single most load-bearing config value in Baofeng mode, and it was chosen
in ADR 0138 gate 0 on **one keying per line**: DTR 1926.8 / 1970.5 RMS, RTS 0.0. By the rule ADR 0140
had to introduce, that is not a measurement — and this bench has since been shown to key only ~30 %
of the time, which means a single RTS sample landing in a dead stretch is exactly what "RTS does
nothing" would look like.

The stakes are concrete. `GET /diagnostics/ptt-line` now proves the kernel drives DTR high for the
full hold on 100 % of overs while RF appears on ~30 %. Everything from the pin outward is suspect,
and "we are driving the wrong pin, and DTR only works by leakage" is the one remaining explanation
that is ours to fix rather than the operator's to solder.

So drive each candidate the same number of times and count:

* **dtr**  — the configured line.
* **rts**  — the one ADR 0138 dismissed on a single sample.
* **both** — some cables gate PTT on one line and mute/enable audio on the other, so either alone is
  a partial key that can look intermittent.

The service is stopped for the duration so this process can own the port, and restarted in a
`finally` (ADR 0127: stopping around a single-open test is sanctioned; *leaving* it stopped is not).
A fresh `serial.Serial` is opened per keying, which also removes the long-lived handle from the
picture.

Part 97: bare carriers on a bench frequency, bracketed by a station ID once the service is back.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from acceptance import BENCH_TX_HZ, KV4P_BASE, RADIO_BASE, api, open_tty  # noqa: E402
from aioc_ptt_gate0 import AIOC_PORT, SERVICE, user_systemctl, wait_until_settled  # noqa: E402
from repeater_openup import BusyWatch, busy_seconds, tune_witness  # noqa: E402
from trials import TrialSet, Trial  # noqa: E402

KEY_S = 2.5
GAP_S = 4.0
LOUD_S = 0.60          # of a 2.5 s key


def key_lines(port: str, lines: tuple[str, ...], seconds: float) -> None:
    """Assert every line in `lines` together for `seconds`, from a freshly opened port."""
    import serial

    with open_tty(port, 9600, timeout=1) as ser:
        ser.dtr = False
        ser.rts = False
        time.sleep(0.2)
        try:
            for line in lines:
                setattr(ser, line, True)
            time.sleep(seconds)
        finally:
            ser.dtr = False
            ser.rts = False


def run_interleaved(watch: BusyWatch, candidates: list[tuple[str, tuple[str, ...]]],
                    n: int) -> list[TrialSet]:
    """Round-robin the candidates instead of running each to completion.

    Run block-wise, the first candidate gets the healthiest stretch of the bench and the last gets
    the worst — and this station's key rate demonstrably drifts across a run. A block-ordered
    shootout would therefore rank the candidates by *when they ran*, which is precisely the mistake
    that produced "RTS does nothing" from one sample in ADR 0138. Interleaving spreads every
    candidate over the same conditions, so a difference between them is a difference between them.
    """
    per: dict[str, list[Trial]] = {name: [] for name, _ in candidates}
    for i in range(1, n + 1):
        for name, lines in candidates:
            start = time.monotonic()
            key_lines(AIOC_PORT, lines, KEY_S)
            end = time.monotonic()
            time.sleep(0.5)
            carrier = busy_seconds(watch.samples, start, end)
            per[name].append(Trial(index=i, ok=carrier >= LOUD_S, value=carrier))
            print(f"    round {i:2d}  {name:<5}: carrier {carrier:5.2f}s  "
                  f"{'OK' if carrier >= LOUD_S else 'DEAF'}", flush=True)
            time.sleep(GAP_S)
    return [TrialSet(name=name, trials=tuple(per[name]), gap_seconds=GAP_S)
            for name, _ in candidates]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--i-will-transmit", action="store_true", help="required: this keys the radio")
    ap.add_argument("--frequency", type=int, default=445_800_000)
    ap.add_argument("-n", type=int, default=12)
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

    stopped = False
    results: list[TrialSet] = []
    try:
        wait_until_settled(SERVICE)
        rc, out = user_systemctl("stop", SERVICE)
        if rc != 0:
            print(f"refusing: could not stop {SERVICE}: {out}", file=sys.stderr)
            return 2
        stopped = True
        time.sleep(8.0)

        results = run_interleaved(
            watch,
            [("dtr", ("dtr",)), ("rts", ("rts",)), ("both", ("dtr", "rts"))],
            args.n,
        )
    finally:
        watch.stop()
        if stopped:
            rc, out = user_systemctl("start", SERVICE)
            print(f"\n  restarted {SERVICE}: rc={rc}")
            time.sleep(18.0)
        tune_witness(args.frequency)
        try:
            api(RADIO_BASE, "POST", "/services/01", timeout=90.0)
        except Exception:  # noqa: BLE001
            pass

    print()
    for ts in results:
        print(ts.report())
    best = max(results, key=lambda t: t.passed) if results else None
    print()
    if best is None:
        print("  ==> INCONCLUSIVE: nothing ran.")
        return 2
    if best.passed == 0:
        print("  ==> NO LINE KEYS THIS RADIO right now. Not a config choice — the path from the")
        print("      AIOC's connector to the radio is not making PTT at all.")
        return 1
    if best.unanimous and any(t.passed < t.total for t in results):
        print(f"  ==> '{best.name}' KEYS EVERY TIME while the others do not. ADR 0138 picked the")
        print(f"      line on one sample each; set ptt_line = \"{best.name}\" and the intermittency")
        print("      was a config choice all along.")
        return 0
    print("  ==> ALL CANDIDATES ARE INTERMITTENT. The line choice is not the variable; the fault")
    print("      is downstream of the connector, and no config value fixes it.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
