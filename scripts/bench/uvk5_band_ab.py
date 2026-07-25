#!/usr/bin/env python3
"""Prove which radio a listener is actually hearing, and whether the band makes any difference.

This exists because of a premise that went unexamined for hours. Both bench radios sit on
**445.800**, so "I hear tones on 445.800" does not identify *which* radio transmitted them — and
the whole 2 m investigation rested on the contrast between "audible on 70 cm" and "only clicks on
2 m". If the 70 cm half was ever the kv4p, there is no band contrast to explain and the K6 is
simply weak everywhere.

So: **stop the kv4p service first** (this script refuses to run until the kv4p is not answering),
then transmit from the K6 alone, on one band and then the other, with each burst announcing which
leg it belongs to.

* Leg A — the configured A-band, **two** beeps then a steady tone.
* Leg B — the configured B-band, **four** beeps then a steady tone.

Two beeps vs four is deliberately coarse: a listener catching a fragment through squelch can still
count two versus four, where "was that 3 or 5" is exactly the ambiguity that made the bias sweep
hard to read.

The listener reports which legs they heard. Four outcomes, all informative:

* **A only** — the band difference is real and it is the K6.
* **A and B** — the K6 transmits fine on both; the earlier 2 m silence was something else
  (or has been fixed since).
* **neither** — the K6 has never been audible at distance on any band. Everything previously
  attributed to "70 cm works" was the kv4p, and this is one problem, not a band problem.
* **B only** — genuinely surprising; go back to the register evidence.

Runs against the live API, so the service stays up (ADR 0127: stopping it is what broke this bench)::

    RADIO_API_TOKEN=... .venv/bin/python scripts/bench/uvk5_band_ab.py --i-will-transmit
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from acceptance import KV4P_BASE, RADIO_BASE, TOKEN, api  # noqa: E402

from radio_server.audio import CANONICAL_FORMAT, synth_tone  # noqa: E402

BEEP_MS = 200.0
BEEP_GAP_MS = 160.0
STEADY_MS = 3000.0


def burst(beeps: int) -> bytes:
    """``beeps`` short beeps, then a steady tone — a transmission that names its own leg."""
    beep = synth_tone(1200.0, BEEP_MS, amplitude=0.6).samples
    gap = b"\x00\x00" * int(CANONICAL_FORMAT.rate * BEEP_GAP_MS / 1000.0)
    steady = synth_tone(1000.0, STEADY_MS, amplitude=0.6).samples
    return (beep + gap) * beeps + steady


def kv4p_is_quiet() -> bool:
    """True when the kv4p cannot transmit, i.e. its service is not answering."""
    try:
        code, _ = api(KV4P_BASE, "GET", "/status", timeout=4.0)
    except Exception:
        return True
    return code != 200


def leg(name: str, hz: int, beeps: int, rounds: int, gap: float) -> None:
    code, body = api(RADIO_BASE, "POST", "/frequency", body={"hz": hz})
    if code != 200:
        print(f"  leg {name}: could not tune to {hz} ({code} {body!r:.80}) — skipped", flush=True)
        return
    time.sleep(1.5)
    pcm = burst(beeps)
    print(f"  leg {name}: {hz} Hz, {beeps} beeps then a tone, {rounds}x", flush=True)
    for i in range(rounds):
        code, body = api(RADIO_BASE, "POST", "/transmit", raw=pcm, timeout=60)
        print(f"    {time.strftime('%H:%M:%S')}  burst {i + 1}/{rounds}  HTTP {code}", flush=True)
        time.sleep(gap)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--i-will-transmit", action="store_true",
                    help="required: this keys the transmitter repeatedly")
    ap.add_argument("--band-a", type=int, default=445_800_000, help="leg A, Hz (default 445.800)")
    ap.add_argument("--band-b", type=int, default=147_555_000, help="leg B, Hz (default 147.555)")
    ap.add_argument("--rounds", type=int, default=4, help="bursts per leg (default 4)")
    ap.add_argument("--gap", type=float, default=4.0, help="seconds between bursts (default 4)")
    ap.add_argument("--allow-kv4p", action="store_true",
                    help="skip the kv4p-is-stopped guard (you are then measuring an ambiguity)")
    args = ap.parse_args(argv)
    if not args.i_will_transmit:
        print("refusing: pass --i-will-transmit (this keys the radio)", file=sys.stderr)
        return 2
    if not TOKEN:
        print("set RADIO_API_TOKEN", file=sys.stderr)
        return 2
    if not args.allow_kv4p and not kv4p_is_quiet():
        print("refusing: the kv4p is still answering on", KV4P_BASE, file=sys.stderr)
        print("  Stop it first — otherwise a listener cannot tell which radio they heard, which is",
              file=sys.stderr)
        print("  the exact ambiguity this script exists to remove:", file=sys.stderr)
        print("    systemctl --user stop radio-server-kv4p", file=sys.stderr)
        return 2

    original = None
    code, st = api(RADIO_BASE, "GET", "/status")
    if code == 200 and isinstance(st, dict):
        original = st.get("frequency")
    print("K6 alone — the kv4p is stopped and cannot transmit.\n")
    try:
        leg("A", args.band_a, 2, args.rounds, args.gap)
        print("\n  --- 10 s pause ---\n", flush=True)
        time.sleep(10.0)
        leg("B", args.band_b, 4, args.rounds, args.gap)
    finally:
        if original is not None:
            api(RADIO_BASE, "POST", "/frequency", body={"hz": original})
            print(f"\n  restored to {original} Hz")
    print("\n  Report: which legs did you hear — A (two beeps), B (four beeps), both, or neither?")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
