#!/usr/bin/env python3
"""Does asking for less power actually produce less power? Two questions, kept apart (ADR 0146).

Power was plumbed to the radio from the very first tuner and hardcoded to HIGH above the `VfoImage`
seam. Making it settable produces two claims of very different strength, and collapsing them would
let a weak one ride on a strong one's evidence.

**Claim 1 — the level reaches the radio.** Decisive, and the radio itself is the witness: `0x0873`
is answered by `0x0874` carrying `v->OUTPUT_POWER` read out of `gEeprom.VfoInfo[0]` *after* the
firmware applied it (`App/app/uart.c`). Its scale is ``USER, LOW1..LOW5, MID, HIGH`` and the dock
maps the wire's low/mid/high onto ``1 / 6 / 7``. Before this ADR the bench read **7 on all 186
tunes**. So the read-back column is the claim, `GET /status.power` is where it surfaces, and there
is no interpretation involved.

**Claim 2 — it changes what goes out.** Weaker, because the instrument is a witness radio sitting
inches away, and near-field coupling does not care much about a few dB. Measured as the kv4p's
``status.rssi`` while the DUT keys — the *kv4p's* RSSI, which moves (108 -> 309 measured, ADR 0132);
the UV-K5's own RSSI reads 0 on this firmware even while cleanly demodulating, and reading it is
what produced two confident-and-wrong "FLAT" verdicts from `uvk5_pa_sweep.py`.

**If claim 2 does not resolve, that is a real answer and it is reported as one.** The column is
printed either way, and the exit code reflects claim 1 only. A run that proves the setting reaches
the radio and cannot resolve the radiated difference has established exactly that much, and saying
otherwise would be the sort of thing this project keeps having to correct.

Only frequencies in ``BENCH_TX_HZ`` are ever keyed. Requires the kv4p service on :8091 as the
witness, and ``baofeng.uvk5_tuner`` set to something other than 'off'::

    RADIO_API_TOKEN=... .venv/bin/python scripts/bench/power_levels.py --i-will-transmit
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from acceptance import (  # noqa: E402
    BENCH_TX_HZ,
    KV4P_BASE,
    RADIO_BASE,
    api,
)
from repeater_openup import tune_witness  # noqa: E402

#: A simplex bench channel: the witness listens where the DUT transmits, with no split to reason
#: about while the only variable under test is how hard it is transmitting.
TEST_PRESET = "Bench Simplex 445.800"
TEST_HZ = 445_800_000

#: The radio's own OUTPUT_POWER_* index for each level (App/settings.h:91-99, DOCK_POWER_MAP).
#: This is the claim-1 expectation and it is a firmware fact, not a preference.
EXPECTED_FIRMWARE = {"low": 1, "mid": 6, "high": 7}

KEY_S = 3.0
SETTLE_S = 1.5
GAP_S = 4.0
SAMPLES = 12

#: How many RSSI counts high must clear low by before the difference is called resolved. Not tuned
#: to make a row pass — it is the idle spread this bench shows between runs, so anything under it
#: is indistinguishable from the receiver's own wander.
RESOLVED_MARGIN = 4


def set_power(level: str) -> None:
    code, body = api(RADIO_BASE, "POST", "/power", body={"level": level}, timeout=90.0)
    if code == 501:
        raise SystemExit(
            "refusing: this backend cannot set power. Set baofeng.uvk5_tuner to 'setvfo', "
            "'eeprom' or 'hybrid' and restart, or this measures a radio nothing is steering."
        )
    if code != 200:
        raise SystemExit(f"could not set power to {level!r} (HTTP {code} {body!r:.200})")


def apply_preset(name: str) -> None:
    code, body = api(RADIO_BASE, "POST", "/presets/apply", body={"name": name}, timeout=90.0)
    if code != 200:
        raise SystemExit(f"could not apply preset {name!r} (HTTP {code} {body!r:.200})")


def reported_power() -> str | None:
    code, body = api(RADIO_BASE, "GET", "/status", timeout=15.0)
    if code != 200 or not isinstance(body, dict):
        raise SystemExit(f"could not read status (HTTP {code} {body!r:.200})")
    return body.get("power")


def witness_rssi() -> int | None:
    try:
        code, st = api(KV4P_BASE, "GET", "/status", timeout=5.0)
    except Exception:  # noqa: BLE001
        return None
    return st.get("rssi") if code == 200 and isinstance(st, dict) else None


def station_id() -> None:
    try:
        api(RADIO_BASE, "POST", "/services/01", timeout=90.0)
    except Exception:  # noqa: BLE001
        pass


def keyed_rssi() -> tuple[int | None, list[int]]:
    """Key the DUT and sample the witness's RSSI. Returns (median, samples)."""
    api(RADIO_BASE, "POST", "/ptt", body={"on": True}, timeout=30.0)
    samples: list[int] = []
    try:
        time.sleep(0.6)                       # let the carrier and the receiver's AGC settle
        deadline = time.monotonic() + KEY_S
        while time.monotonic() < deadline:
            value = witness_rssi()
            if value is not None:
                samples.append(value)
            time.sleep(0.15)
    finally:
        api(RADIO_BASE, "POST", "/ptt", body={"on": False}, timeout=30.0)
    return (statistics.median(samples) if samples else None), samples


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--i-will-transmit", action="store_true", help="required: this keys the radio")
    ap.add_argument("-n", type=int, default=3, help="passes over the three levels")
    args = ap.parse_args(argv)
    if not args.i_will_transmit:
        print("refusing: pass --i-will-transmit (this keys the radio)", file=sys.stderr)
        return 2

    if TEST_HZ not in BENCH_TX_HZ:
        raise SystemExit(f"refusing: {TEST_HZ} Hz is not a bench frequency")

    code, caps = api(RADIO_BASE, "GET", "/capabilities", timeout=15.0)
    if code != 200 or not isinstance(caps, list):
        print(f"INCONCLUSIVE: /capabilities answered {code}, not a capability list.", file=sys.stderr)
        print("          401 means RADIO_API_TOKEN is unset or wrong in this shell; a connection",
              file=sys.stderr)
        print("          error means the service is not up. Neither is a fact about the radio.",
              file=sys.stderr)
        return 2
    if "set_power" not in set(caps):
        print("refusing: the active backend cannot set power (no set_power capability).",
              file=sys.stderr)
        return 2

    station_id()
    time.sleep(1.0)
    tune_witness(TEST_HZ)
    time.sleep(SETTLE_S)

    idle = [v for v in (witness_rssi() for _ in range(8)) if v is not None]
    idle_floor = statistics.median(idle) if idle else None
    print(f"\n  witness idle floor: {idle_floor}  (samples {idle})\n", flush=True)

    readback: dict[str, list[str | None]] = {"low": [], "mid": [], "high": []}
    levels: dict[str, list[int]] = {"low": [], "mid": [], "high": []}

    try:
        for pass_no in range(1, args.n + 1):
            # low first, so a run that is going to key hard does it last and any thermal drift
            # works AGAINST the expected ordering rather than for it.
            for level in ("low", "mid", "high"):
                set_power(level)
                apply_preset(TEST_PRESET)
                got = reported_power()
                readback[level].append(got)

                median, samples = keyed_rssi()
                if median is not None:
                    levels[level].append(median)
                print(f"    pass {pass_no}  {level:<4}  status.power={str(got):<5}  "
                      f"rssi median {median}  (n={len(samples)})", flush=True)
                time.sleep(GAP_S)
    finally:
        try:
            api(RADIO_BASE, "POST", "/ptt", body={"on": False}, timeout=30.0)
            api(KV4P_BASE, "POST", "/ptt", body={"on": False}, timeout=30.0)
        except Exception:  # noqa: BLE001
            pass
        set_power("high")          # leave the bench where it was found
        station_id()

    # --- claim 1: the level reached the radio ---------------------------------------------
    print("\n  CLAIM 1 — the level reaches the radio (status.power, from the 0x0874 read-back)")
    claim1 = True
    for level in ("low", "mid", "high"):
        got = readback[level]
        ok = bool(got) and all(g == level for g in got)
        claim1 &= ok
        print(f"    asked {level:<4} -> {got}   expected OUTPUT_POWER "
              f"{EXPECTED_FIRMWARE[level]}   {'OK' if ok else 'FAIL'}")

    # --- claim 2: it changed what went out ------------------------------------------------
    print("\n  CLAIM 2 — the witness sees the difference (kv4p rssi while keyed)")
    medians: dict[str, float | None] = {}
    for level in ("low", "mid", "high"):
        vals = levels[level]
        medians[level] = statistics.median(vals) if vals else None
        print(f"    {level:<4}  median {medians[level]}  from {vals}")

    lo, mid, hi = medians["low"], medians["mid"], medians["high"]
    if None in (lo, mid, hi):
        verdict = ("UNRESOLVED — the witness did not report RSSI for every level; nothing here "
                   "says anything about radiated power.")
    elif hi >= mid >= lo and (hi - lo) >= RESOLVED_MARGIN:
        verdict = (f"RESOLVED — high {hi} > mid {mid} > low {lo}, a spread of {hi - lo} counts "
                   f"against a {RESOLVED_MARGIN}-count floor. Asking for less produced less.")
    else:
        verdict = (
            f"UNRESOLVED — high {hi}, mid {mid}, low {lo}: no monotonic spread of at least "
            f"{RESOLVED_MARGIN} counts. The witness is inches away, where near-field coupling "
            "swamps a few dB, so this does NOT show the setting had no effect — it shows this "
            "bench cannot see it. The setting is confirmed AT THE RADIO by claim 1; the radiated "
            "difference stays unmeasured, beside absolute power (ADR 0137)."
        )
    print(f"\n  ==> {verdict}")

    # Exit on claim 1 alone, deliberately. Claim 2 is reported, never gated on: failing the run
    # because a witness inches away could not resolve 1 W from 5 W would be scoring the bench's
    # geometry as a defect in the radio.
    print(f"\n  ==> CLAIM 1 {'PASSED' if claim1 else 'FAILED'} "
          f"(the exit code reflects this claim only)\n")
    return 0 if claim1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
