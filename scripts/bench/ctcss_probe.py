#!/usr/bin/env python3
"""Can this bench measure a CTCSS tone over the air at all? Measure before asserting (ADR 0133).

The split acceptance stage would like to prove the repeater tone is really on the carrier, not just
in a register. Whether it *can* is an empirical question about the witness, not about our
transmitter: the kv4p is an SA818 module whose receive chain very likely high-passes sub-audible
tone out before we ever see the audio. If it does, no amount of FFT recovers it, and a stage that
hard-checks the tone would fail for a reason that is not a defect.

So: transmit the same audio twice on the same frequency, once with CTCSS off and once with it on,
and compare the energy near the tone. Two failure modes this is built to avoid —

* ``tone_power``'s default ``width=60.0`` spans -40..160 Hz around 100 Hz, i.e. it swallows DC and
  the whole LF rumble region and would report "tone present" for a carrier with no tone at all.
  This uses a narrow window and reports the *ratio* between the two runs.
* A single absolute number proves nothing without its control. Both runs are printed.

Runs against the live API, so the service stays up (ADR 0127: stopping it is what broke this bench)::

    RADIO_API_TOKEN=... .venv/bin/python scripts/bench/ctcss_probe.py --i-will-transmit
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from acceptance import KV4P_BASE, RADIO_BASE, TOKEN, _collect_rx, api, rms, tone_power  # noqa: E402

from radio_server.audio import synth_tone  # noqa: E402

#: Narrow enough that the window around a 100 Hz CTCSS tone excludes DC and the voice band. At
#: 48 kHz over a multi-second capture the FFT bin spacing is well under 1 Hz, so this is real
#: resolution, not wishful arithmetic.
TONE_WIDTH_HZ = 5.0


def run_over(hz: int, tone: float | None, seconds: float) -> tuple[bytes, list[bool]]:
    """Key the radio under test with ``tone`` and return what the kv4p heard."""
    code, body = api(RADIO_BASE, "POST", "/tone", body={"tone": tone or 0})
    if code != 200:
        print(f"  could not set tone={tone} ({code} {body!r:.80})", file=sys.stderr)
    time.sleep(0.5)
    pcm = synth_tone(1000.0, (seconds - 1.0) * 1000.0, amplitude=0.6).samples
    polls: list[bool] = []

    async def watch_and_key():
        started = asyncio.Event()
        collector = asyncio.create_task(_collect_rx(KV4P_BASE, seconds + 1.0, started))

        async def poll():
            t0 = time.monotonic()
            while time.monotonic() - t0 < seconds + 1.0:
                try:
                    s = await asyncio.to_thread(api, KV4P_BASE, "GET", "/status", None, None, 4.0)
                    polls.append(bool(s[1].get("busy")))
                except Exception:
                    pass
                await asyncio.sleep(0.25)

        await started.wait()
        poller = asyncio.create_task(poll())
        await asyncio.sleep(0.3)
        await asyncio.to_thread(api, RADIO_BASE, "POST", "/transmit", None, pcm, 60.0)
        capture = await collector
        poller.cancel()
        return capture

    capture = asyncio.run(watch_and_key())
    return capture.pcm, polls


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--i-will-transmit", action="store_true", help="required: this keys the radio")
    ap.add_argument("--frequency", type=int, default=445_800_000, help="both radios, Hz")
    ap.add_argument("--tone", type=float, default=100.0, help="the CTCSS tone to look for (Hz)")
    ap.add_argument("--seconds", type=float, default=6.0, help="over length (default 6)")
    args = ap.parse_args(argv)
    if not args.i_will_transmit:
        print("refusing: pass --i-will-transmit (this keys the radio)", file=sys.stderr)
        return 2
    if not TOKEN:
        print("set RADIO_API_TOKEN", file=sys.stderr)
        return 2

    for base, label in ((RADIO_BASE, "radio under test"), (KV4P_BASE, "witness")):
        code, body = api(base, "POST", "/frequency", body={"hz": args.frequency})
        if code != 200:
            print(f"could not tune the {label} to {args.frequency} ({code} {body!r:.80})",
                  file=sys.stderr)
            return 2
    time.sleep(2.0)

    results = {}
    for label, tone in (("no tone", None), (f"{args.tone} Hz", args.tone)):
        print(f"\n  keying with CTCSS {label}...", flush=True)
        pcm, polls = run_over(args.frequency, tone, args.seconds)
        energy = tone_power(pcm, args.tone, width=TONE_WIDTH_HZ)
        results[label] = energy
        print(f"    kv4p heard {len(pcm)} B, RMS {rms(pcm):.0f}, "
              f"carrier on {sum(polls)}/{len(polls)} polls")
        print(f"    energy within +/-{TONE_WIDTH_HZ:.0f} Hz of {args.tone} Hz: {energy:.6f}")

    api(RADIO_BASE, "POST", "/tone", body={"tone": 0})
    off, on = results["no tone"], results[f"{args.tone} Hz"]
    print(f"\n  control {off:.6f} -> with tone {on:.6f}", end="")
    if off > 0:
        print(f"  (ratio {on / off:.2f}x)")
    else:
        print()
    if on > off * 3 and on > 0.01:
        print("\n  VERDICT: the tone survives the witness's audio path — an RF-level CTCSS check "
              "is measurable on this bench.")
        return 0
    print("\n  VERDICT: the tone does NOT survive the witness's audio path (the SA818 receive chain "
          "\n  filters sub-audible tone out before we see it). An RF-level CTCSS check is NOT "
          "\n  available here; prove the tone at the register level and say so plainly.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
