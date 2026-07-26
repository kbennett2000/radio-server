"""Which end is broken? Measure both, on the same over, and let the answer name a component.

Every "the radio did not transmit" on this bench has rested on one instrument answering one
question: the kv4p's ``busy``, which is a single pin (the SA818's squelch output). Two things were
never measured, and they are exactly the two that make "no signal" and "no measurement" identical:

* **the PTT line itself** — ``pyserial``'s ``.dtr`` is write-only, so a key-up that never reached the
  hardware reads back as a flawless success (``GET /diagnostics/ptt-line`` asks the kernel instead);
* **demodulated audio on a FAILED over** — every probe so far read ``busy`` alone, so a deaf receiver
  and a dead transmitter produce the same 0.00 s, and "carrier nowhere in 400-480 MHz" is equally
  explained by a witness that could not hear.

So sample all three on one over and print the truth table:

    DTR   audio  busy   what it names
    ----  -----  ----   -----------------------------------------------------------------
    low   -      -      radio-server never keyed. A software fault in _key_on, and ours.
    high  yes    no     the WITNESS's squelch. The transmitter was never the problem.
    high  no     no     keyed, no RF: the AIOC PTT path or the radio itself.
    high  yes    yes    that over was fine.

``null`` from the PTT probe means "could not ask", never "low" — an unavailable measurement reported
as a failure is the error class this script exists to end.

The over is held open with ``POST /ptt`` rather than ``POST /services/01``: a one-shot transmit runs
on the event loop and blocks the whole API for its duration, so the PTT line could not be polled
during it. A held carrier is unmodulated, so the audio channel is judged against the pre-key noise
floor sampled moments earlier, not against an absolute threshold.

Part 97: bracketed by station IDs; PTT release in a ``finally``.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import deviation_probe as dp  # noqa: E402
from acceptance import BENCH_TX_HZ, KV4P_BASE, RADIO_BASE, api  # noqa: E402
from repeater_openup import BusyWatch, busy_seconds, tune_witness  # noqa: E402
from trials import TrialSet, Trial  # noqa: E402

HOLD_S = 4.0
FLOOR_S = 2.0
LOUD_FRACTION = 0.30      # of the hold, for `busy`
#: An unmodulated carrier *quiets* an FM receiver, so "audio changed" is the signal — in either
#: direction. Judged as a ratio against the floor sampled seconds earlier on the same hardware,
#: because an absolute threshold cannot tell a quiet band from a deaf radio.
AUDIO_RATIO = 1.6


def ptt_line() -> bool | None:
    try:
        return api(RADIO_BASE, "GET", "/diagnostics/ptt-line", timeout=10.0)[1].get("asserted")
    except Exception:  # noqa: BLE001
        return None


def one_over(watch: BusyWatch, index: int) -> dict:
    """Hold a carrier; sample the PTT pin, the witness's squelch, and its audio."""
    floor = dp.rms(dp.listen(FLOOR_S)) or 0.0

    audio_box: list = []
    t = threading.Thread(target=lambda: audio_box.append(dp.listen(HOLD_S)), daemon=True)

    api(RADIO_BASE, "POST", "/ptt", body={"on": True}, timeout=30.0)
    start = time.monotonic()
    dtr_samples: list[bool | None] = []
    try:
        t.start()
        while time.monotonic() - start < HOLD_S:
            dtr_samples.append(ptt_line())
            time.sleep(0.25)
    finally:
        api(RADIO_BASE, "POST", "/ptt", body={"on": False}, timeout=30.0)
    end = time.monotonic()
    t.join(timeout=HOLD_S + 6.0)

    busy = busy_seconds(watch.samples, start, end)
    keyed_rms = dp.rms(audio_box[0]) if audio_box and audio_box[0] else 0.0
    readable = [s for s in dtr_samples if s is not None]
    return {
        "index": index,
        "dtr": (all(readable) and bool(readable)) if readable else None,
        "dtr_samples": f"{sum(1 for s in readable if s)}/{len(dtr_samples)}",
        "busy_s": busy,
        "busy": busy >= HOLD_S * LOUD_FRACTION,
        "floor": floor,
        "keyed_rms": keyed_rms,
        "audio": bool(floor and (keyed_rms > floor * AUDIO_RATIO or keyed_rms * AUDIO_RATIO < floor)),
    }


def verdict(rows: list[dict]) -> str:
    if not rows:
        return "INCONCLUSIVE: no overs ran."
    unreadable = [r for r in rows if r["dtr"] is None]
    if len(unreadable) == len(rows):
        return ("INCONCLUSIVE: the PTT line could never be read, so nothing here separates "
                "'server never keyed' from 'radio ignored it'. Fix the probe first.")
    low = [r for r in rows if r["dtr"] is False]
    if low:
        return (f"radio-server DID NOT KEY on {len(low)}/{len(rows)} overs — the kernel says the PTT "
                f"line stayed low. That is a software fault in AiocBaofeng._key_on, and it is ours.")
    heard = [r for r in rows if r["busy"]]
    audible = [r for r in rows if r["audio"]]
    if len(heard) == len(rows):
        return f"all {len(rows)} overs keyed and were heard. The station is healthy right now."
    if len(audible) > len(heard):
        return (f"THE WITNESS'S SQUELCH IS THE FAULT: audio changed on {len(audible)}/{len(rows)} "
                f"overs but `busy` only fired on {len(heard)}. The transmitter was never the problem, "
                f"and every 'no carrier' measured with `busy` alone is suspect.")
    return (f"PTT line high on every over, but only {len(heard)}/{len(rows)} produced a carrier and "
            f"{len(audible)}/{len(rows)} moved the audio. The server keyed and no RF appeared — "
            f"next discriminator is aioc_ptt_gate0.key_via_line (a FRESH port open).")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--i-will-transmit", action="store_true", help="required: this keys the radio")
    ap.add_argument("--frequency", type=int, default=445_800_000)
    ap.add_argument("-n", type=int, default=10)
    ap.add_argument("--gap", type=float, default=6.0)
    args = ap.parse_args(argv)
    if not args.i_will_transmit:
        print("refusing: pass --i-will-transmit (this keys the radio)", file=sys.stderr)
        return 2
    if args.frequency not in BENCH_TX_HZ:
        print(f"refusing: {args.frequency} Hz is not a bench frequency {sorted(BENCH_TX_HZ)}",
              file=sys.stderr)
        return 2

    probe = api(RADIO_BASE, "GET", "/diagnostics/ptt-line", timeout=10.0)[1]
    print(f"PTT-line probe: readable={probe.get('readable')} backend={probe.get('backend')!r}\n")

    watch = BusyWatch(KV4P_BASE)
    watch.start()
    time.sleep(0.5)
    tune_witness(args.frequency)
    time.sleep(2.0)

    rows: list[dict] = []
    try:
        api(RADIO_BASE, "POST", "/services/01", timeout=90.0)          # opening ID
        time.sleep(2.0)
        print(f"  {'#':>3}  {'DTR':<10} {'audio':<22} {'busy':<12}")
        for i in range(1, args.n + 1):
            r = one_over(watch, i)
            rows.append(r)
            dtr = {True: "HIGH", False: "LOW ", None: "?   "}[r["dtr"]]
            print(f"  {i:>3}  {dtr} {r['dtr_samples']:<5} "
                  f"floor {r['floor']:7.1f} -> {r['keyed_rms']:7.1f} {'CHG' if r['audio'] else '   '}  "
                  f"{r['busy_s']:5.2f}s {'BUSY' if r['busy'] else '    '}", flush=True)
            if i < args.n:
                time.sleep(args.gap)
    finally:
        try:
            api(RADIO_BASE, "POST", "/ptt", body={"on": False}, timeout=30.0)
        except Exception:  # noqa: BLE001
            pass
        watch.stop()
        tune_witness(args.frequency)
        try:
            api(RADIO_BASE, "POST", "/services/01", timeout=90.0)      # closing ID
        except Exception:  # noqa: BLE001
            pass

    ts = TrialSet(
        name="keyed and heard",
        trials=tuple(Trial(index=r["index"], ok=r["busy"], value=r["busy_s"]) for r in rows),
        gap_seconds=args.gap,
    )
    print()
    print(ts.report())
    floors = [r["floor"] for r in rows if r["floor"]]
    if floors:
        print(f"    witness noise floor: min {min(floors):.1f} mean {statistics.fmean(floors):.1f} "
              f"max {max(floors):.1f}")
    print(f"\n  ==> {verdict(rows)}")
    return 0 if ts.unanimous else 1


if __name__ == "__main__":
    raise SystemExit(main())
