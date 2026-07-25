#!/usr/bin/env python3
"""Measure what a *running* radio-server hears, on any band, with no second radio on the bench.

Why this exists: the bench's objective receiver is the kv4p, and it is a single-module SA818-**UHF**
board (400-480 MHz). Every RF stage in ``acceptance.py`` leans on it, so on 2 m there is simply
nothing on the box that can hear. Until a VHF board arrives, the only VHF signal source is a human
with a handheld — and a human's time is the scarce resource. This makes the one key-up they give
you produce every number at once.

It samples the deployed API for ``seconds``:

* ``/status.rssi`` — the raw reg-0x67 reading behind ``busy`` (ADR 0132). This is the number that
  sizes ``uvk5.squelch_threshold``: run it once with nobody transmitting for the noise floor, once
  with a signal for the ceiling, and put the threshold between them.
* ``/status.busy`` — whether the CAT squelch gate opened, i.e. whether the current threshold
  actually works on this band.
* ``/audio/rx`` — the received audio, so "the gate opened" can be checked against "audio arrived",
  which are not the same claim.

Usage::

    RADIO_API_TOKEN=... .venv/bin/python scripts/bench/rf_listen.py --seconds 12
    RADIO_API_TOKEN=... .venv/bin/python scripts/bench/rf_listen.py --seconds 12 --tone 1000

Read the output as: **floor** (quiet) vs **peak** (keyed). Nothing here transmits.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from acceptance import (  # noqa: E402
    RADIO_BASE,
    TOKEN,
    _collect_rx,
    api,
    rms,
    speech_band_ratio,
    tone_power,
)


def poll_rssi(base: str, seconds: float, out: list) -> None:
    """Sample ``/status`` until ``seconds`` elapse, appending ``(rssi, busy)``."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            st = api(base, "GET", "/status", timeout=5.0)
        except Exception:
            time.sleep(0.1)
            continue
        out.append((st.get("rssi"), bool(st.get("busy"))))
        time.sleep(0.2)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seconds", type=float, default=12.0, help="listening window (default 12)")
    ap.add_argument("--base", default=RADIO_BASE, help=f"radio under test (default {RADIO_BASE})")
    ap.add_argument("--tone", type=float, default=None,
                    help="also report how much of the received energy sits at this frequency")
    args = ap.parse_args(argv)
    if not TOKEN:
        print("set RADIO_API_TOKEN", file=sys.stderr)
        return 2

    st = api(args.base, "GET", "/status")
    print(f"listening on {st.get('frequency')} Hz for {args.seconds:.0f} s — key now\n")

    samples: list = []
    import threading

    poller = threading.Thread(target=poll_rssi, args=(args.base, args.seconds, samples))
    poller.start()
    cap = asyncio.run(_collect_rx(args.base, args.seconds))
    poller.join()

    readings = [r for r, _ in samples if r is not None]
    busy = [b for _, b in samples]
    print(f"  RSSI samples        {len(readings)} of {len(samples)} polls answered")
    if readings:
        print(f"  RSSI floor / peak   {min(readings)} / {max(readings)}")
        readings.sort()
        print(f"  RSSI median         {readings[len(readings) // 2]}")
    print(f"  squelch open        {sum(busy)}/{len(busy)} polls "
          f"({100.0 * sum(busy) / max(1, len(busy)):.0f}% of the window)")
    print(f"  audio received      {len(cap.pcm)} bytes over {cap.active:.2f} s active span")
    if cap.pcm:
        print(f"  audio duty / gap    {cap.duty:.1f}% / {cap.max_gap * 1000:.0f} ms")
        print(f"  audio RMS           {rms(cap.pcm):.0f}")
        print(f"  speech-band energy  {speech_band_ratio(cap.pcm):.2f}")
        if args.tone:
            print(f"  {args.tone:.0f} Hz recovered   {tone_power(cap.pcm, args.tone):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
