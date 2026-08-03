#!/usr/bin/env python3
"""Does ONE control exchange straddling a key-up damage the transmission? (ADR 0177)

ADR 0176's audit found that both cadences guard the shared AIOC wire with a check-then-act pause:
`poll_once` reads ``paused()`` and then, holding nothing, puts an exchange on the wire. A poll that
passes the check can still be on that wire when the transmitter comes up.

**Waiting for it is a bad instrument.** The window is a few tens of milliseconds against a 0.5 s
cadence, so the natural rate is a couple of percent — hundreds of key-ups to see a handful of
events, and a clean run would prove very little. So this makes the collision **certain** instead:
it fires exactly one register exchange timed to be in flight as the line goes high, and measures
what reaches the witness.

**Exactly one, deliberately.** Speeding the cadence up to force collisions would confound "does one
exchange hurt" with "does more traffic hurt", and ADR 0175/0176 already answered the second. The
question left over is only whether a single leading-edge exchange is enough.

The control arm is the same script with ``--forced 0``: same process, same port, same tone, same
witness, minutes apart. That is the ADR 0175 bisect shape, which is the one that worked.

**It reports the first second in 100 ms bins**, because a leading-edge collision would land at the
*start* of the over and ``RxCapture.active`` (first byte → last byte) cannot resolve that.

**The overlap is evidenced, not assumed.** Each exchange records when it started and ended, and the
key-up records when the line actually went high (off the backend's own no-I/O flag, polled — not off
a serial read, which would be more traffic on the wire under test). A run that failed to straddle
says so instead of quietly reporting a clean result.

ADR 0127: the service is stopped to borrow the port, and restarted in a ``finally``.

Usage (on the bench box)::

    RADIO_API_TOKEN=... .venv/bin/python scripts/bench/keyup_race.py --i-will-transmit --forced 1
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from acceptance import (  # noqa: E402
    BENCH_TX_HZ,
    CANONICAL_RATE,
    KV4P_BASE,
    _collect_rx,
    api,
    rms,
    tone_power,
)
from aioc_ptt_gate0 import AIOC_PORT, SERVICE, user_systemctl, wait_until_settled  # noqa: E402

import radio_server.backends.aioc_baofeng as aioc  # noqa: E402
from radio_server.audio.tone import synth_tone  # noqa: E402
from radio_server.backends import create_radio  # noqa: E402

#: The bench pair, both on the witness's only band (`docs/server-notes.md`: the kv4p is a UHF-only
#: SA818, so a station outside 400-480 MHz reads as "no RF" whether or not it keyed).
BENCH_HZ = 445_800_000
#: Long enough that a truncated over is unmistakable against a whole one, and short enough that a
#: dozen trials is a couple of minutes of RF. ADR 0176's arms used the same shape.
TONE_SECONDS = 5.0
#: Bin width for the leading-edge profile. 100 ms is about three cadence round trips, so a collision
#: that ate the front of the over cannot hide inside one bin.
BIN_MS = 100.0


def _bins(cap, bin_ms: float = BIN_MS, span_s: float = 1.0) -> list[float]:
    """RMS per ``bin_ms`` across the first ``span_s`` of received audio.

    Positional in the *captured stream*, which starts when the first byte arrives — so bin 0 is the
    start of the over as the witness heard it, which is exactly where a leading-edge collision would
    land.
    """
    per_bin = int(CANONICAL_RATE * bin_ms / 1000.0) * 2
    out = []
    for start in range(0, min(len(cap.pcm), int(span_s * CANONICAL_RATE) * 2), per_bin):
        out.append(rms(cap.pcm[start : start + per_bin]))
    return out


class Collider:
    """Fires ``count`` register exchanges timed to straddle the DTR assert."""

    def __init__(self, radio, count: int, offset_ms: float, spacing_ms: float) -> None:
        self._radio = radio
        self._count = count
        self._offset = offset_ms / 1000.0
        self._spacing = spacing_ms / 1000.0
        self.exchanges: list[tuple[float, float, object]] = []
        self.keyed_at: float | None = None
        self._threads: list[threading.Thread] = []

    def _watch_for_the_line(self) -> None:
        """Timestamp the assert off the backend's own no-I/O flag.

        Deliberately not `ptt_line_asserted()`: that reads the kernel's line state off the very
        handle under test, and an instrument that adds traffic to the wire it is measuring is the
        ADR 0176 mistake one level out.
        """
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if self._radio.transmitting:
                self.keyed_at = time.monotonic()
                return
            time.sleep(0.0002)

    def _exchange(self, index: int) -> None:
        time.sleep(self._offset + index * self._spacing)
        started = time.monotonic()
        try:
            answer = self._radio._tuner.read_rssi(timeout=1.0, wire_timeout=0.0)
        except Exception as exc:  # noqa: BLE001 - the answer is not the point; the traffic is
            answer = f"raised {exc!r}"
        self.exchanges.append((started, time.monotonic(), answer))

    def arm(self) -> None:
        """Start the watcher and the exchange threads. Called as the key-up opens its stream."""
        self._threads = [threading.Thread(target=self._watch_for_the_line, daemon=True)]
        self._threads += [
            threading.Thread(target=self._exchange, args=(i,), daemon=True)
            for i in range(self._count)
        ]
        for thread in self._threads:
            thread.start()

    def join(self) -> None:
        for thread in self._threads:
            thread.join(timeout=20.0)

    def straddled(self) -> int:
        """How many exchanges were genuinely in flight as the line went high."""
        if self.keyed_at is None:
            return 0
        return sum(1 for start, end, _ in self.exchanges if start <= self.keyed_at <= end)


def one_over(radio, *, forced: int, offset_ms: float, spacing_ms: float, seconds: float):
    """Key a 1000 Hz tone with ``forced`` exchanges timed at the leading edge; capture at the kv4p."""
    tone = synth_tone(1000.0, (seconds - 1.0) * 1000.0, amplitude=0.6)
    collider = Collider(radio, forced, offset_ms, spacing_ms)

    original = aioc.open_playout_stream

    def opening_the_stream(*args, **kwargs):
        # The commit point: the key-up's own dock frames are done and the wire is free, the stream
        # is about to open and start, and the line goes high a few statements later. A cadence poll
        # landing here is the race — so this is where the forced one is armed.
        if forced:
            collider.arm()
        return original(*args, **kwargs)

    async def run():
        started = asyncio.Event()
        collector = asyncio.create_task(_collect_rx(KV4P_BASE, seconds + 2.0, started))
        await started.wait()
        await asyncio.sleep(0.3)
        await asyncio.to_thread(radio.transmit, tone)
        return await collector

    aioc.open_playout_stream = opening_the_stream
    try:
        cap = asyncio.run(run())
    finally:
        aioc.open_playout_stream = original
        collider.join()
    return cap, collider


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--i-will-transmit", action="store_true", help="required: this keys the radio")
    ap.add_argument("--forced", type=int, default=1,
                    help="exchanges to fire across the key-up (0 = the control arm)")
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--offset-ms", type=float, default=0.0,
                    help="delay from the stream opening to the first forced exchange")
    ap.add_argument("--spacing-ms", type=float, default=30.0)
    ap.add_argument("--seconds", type=float, default=TONE_SECONDS)
    ap.add_argument("--frequency", type=int, default=BENCH_HZ)
    ap.add_argument("--port", default=AIOC_PORT)
    ap.add_argument("--gap", type=float, default=6.0, help="seconds between trials")
    args = ap.parse_args(argv)

    if not args.i_will_transmit:
        print("refusing: pass --i-will-transmit (this keys the radio)", file=sys.stderr)
        return 2
    if args.frequency not in BENCH_TX_HZ:
        print(f"refusing: {args.frequency} Hz is not a bench frequency", file=sys.stderr)
        return 2

    code, witness = api(KV4P_BASE, "GET", "/status")
    if code != 200:
        print(f"the witness did not answer /status ({code})", file=sys.stderr)
        return 2
    if abs(int(witness.get("frequency") or 0) - args.frequency) > 25_000:
        print(f"refusing: the witness is on {witness.get('frequency')}, not {args.frequency} — "
              "a UHF-only SA818 elsewhere reads as 'no RF' whether or not we keyed", file=sys.stderr)
        return 2

    wait_until_settled(SERVICE)
    print(f"stopping {SERVICE} to borrow {args.port} (ADR 0127: restarted in a finally)")
    user_systemctl("stop", SERVICE)
    time.sleep(2.0)

    rows = []
    try:
        radio = create_radio(
            "baofeng",
            serial_port=args.port,
            uvk5_tuner="hybrid",
            uvk5_tune_persist=True,
            uvk5_power="low",
        )
        try:
            radio.set_frequency(args.frequency)
            print(f"station on {args.frequency / 1e6:.4f} MHz, witness confirmed on the same "
                  f"channel; {args.trials} trials at forced={args.forced}\n")
            for trial in range(args.trials):
                cap, collider = one_over(
                    radio,
                    forced=args.forced,
                    offset_ms=args.offset_ms,
                    spacing_ms=args.spacing_ms,
                    seconds=args.seconds,
                )
                tone = tone_power(cap.pcm, 1000.0)
                profile = _bins(cap)
                rows.append((tone, cap.active, len(cap.pcm), collider.straddled(), profile))
                print(f"trial {trial + 1}: 1000 Hz {tone:.3f} | {len(cap.pcm)} B over "
                      f"{cap.active:.2f}s active | RMS {rms(cap.pcm):.0f} | "
                      f"straddled {collider.straddled()}/{args.forced}")
                if collider.exchanges and collider.keyed_at is not None:
                    for start, end, answer in collider.exchanges:
                        print(f"    exchange {(start - collider.keyed_at) * 1000:+.1f}ms → "
                              f"{(end - collider.keyed_at) * 1000:+.1f}ms  answer={answer}")
                print("    first second, 100ms bins: "
                      + " ".join(f"{value:.0f}" for value in profile))
                if trial + 1 < args.trials:
                    time.sleep(args.gap)
        finally:
            radio.close()
    finally:
        print(f"\nrestarting {SERVICE}")
        user_systemctl("start", SERVICE)

    if not rows:
        return 2
    tones = [row[0] for row in rows]
    spans = [row[1] for row in rows]
    straddles = sum(row[3] for row in rows)
    print(f"\nforced={args.forced}  n={len(rows)}")
    print(f"  1000 Hz recovery : median {statistics.median(tones):.3f}  "
          f"min {min(tones):.3f}  max {max(tones):.3f}")
    print(f"  active span      : median {statistics.median(spans):.2f}s  "
          f"min {min(spans):.2f}s  max {max(spans):.2f}s")
    print(f"  exchanges genuinely straddling the assert: {straddles}"
          f"/{args.forced * len(rows)}")
    if args.forced and straddles == 0:
        print("  NOTE: nothing straddled — this arm proves nothing about the race. Retry with a "
              "larger --offset-ms.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
