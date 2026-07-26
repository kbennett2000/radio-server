"""The three measurements that killed the CTCSS-deviation hypothesis (ADR 0137).

Everything before this was reasoning about registers. These are numbers off the air, taken through
the kv4p witness against the UV-K5 driven by radio-server exactly as a repeater preset drives it.

Each mode is built around a **control**, because the previous cycle's instrument had none and would
have confirmed whatever it was pointed at:

``--tone-control``
    Key with the tone OFF, then ON, then OFF again. A tone amplitude on its own means nothing; if
    switching it off collapses the band and switching it back on restores it, the number is our
    CTCSS and not an artefact of the receive chain. The second OFF is the drift control.

``--tone-accuracy``
    A +/-5 Hz band filter proves a tone is present, not that it is *right* -- and a repeater's
    decoder is tighter than that filter. Estimates the actual generated frequency by parabolic
    interpolation on the FFT peak. Reports a proportional error separately from an absolute one,
    because a constant *percentage* across every tone is the signature of a clock, not of a bad
    tone code -- and the witness has a clock of its own (``kv4p.sample_rate_correction``).

``--split``
    Repeater presets differ from simplex in exactly one way: they transmit somewhere else. Arms a
    synthetic split between two BENCH frequencies and asks the witness which leg the carrier is
    actually on. Never keys a repeater input; the math is identical.

``--presets``
    Applies all 41 presets and reads back what the radio is armed with. SOFTWARE ONLY -- never keys
    -- which is what makes it safe against presets whose transmit legs are live machines' inputs.
    Hunting the case where ``POST /frequency`` silently clears an armed split.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import deviation_probe as dp  # noqa: E402
from acceptance import BENCH_TX_HZ, KV4P_BASE, RADIO_BASE, api  # noqa: E402
from radio_server.audio.format import CANONICAL_RATE  # noqa: E402

BENCH_HZ = 445_800_000
SPLIT_TX_HZ = 446_000_000
TONES = (88.5, 100.0, 103.5, 107.2, 123.0, 141.3)


def _require_bench(hz: int) -> None:
    if hz not in BENCH_TX_HZ:
        raise SystemExit(f"refusing: {hz} Hz is not a bench frequency {sorted(BENCH_TX_HZ)}")


def _witness(hz: int) -> None:
    code, body = api(KV4P_BASE, "POST", "/frequency", body={"hz": hz})
    if code != 200:
        raise SystemExit(f"could not tune the witness to {hz} ({code} {body!r:.80})")
    time.sleep(2.0)


def _key(tone: float | None, seconds: float = 6.0) -> dict:
    api(RADIO_BASE, "POST", "/tone", body={"tone": tone or 0})
    time.sleep(0.5)
    pcm = dp.key_and_listen(seconds, 0.0)
    _, m = dp.measure_transmission(pcm, tone or TONES[-1], seconds)
    return m


def peak_hz(pcm: bytes, expect: float, half_width: float = 12.0) -> float:
    """Dominant frequency near ``expect``, resolved well inside one FFT bin."""
    a = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    if a.size < 8:
        return float("nan")
    a = a - a.mean()
    spec = np.abs(np.fft.rfft(a * np.hanning(a.size)))
    freqs = np.fft.rfftfreq(a.size, 1.0 / CANONICAL_RATE)
    band = np.where((freqs >= expect - half_width) & (freqs <= expect + half_width))[0]
    if band.size < 3:
        return float("nan")
    k = band[int(np.argmax(spec[band]))]
    if k <= 0 or k >= spec.size - 1:
        return float(freqs[k])
    y0, y1, y2 = (np.log(spec[j] + 1e-9) for j in (k - 1, k, k + 1))
    denom = y0 - 2 * y1 + y2
    delta = 0.5 * (y0 - y2) / denom if denom else 0.0
    return float(freqs[k] + delta * (freqs[1] - freqs[0]))


def tone_control(args) -> int:
    _require_bench(args.frequency)
    _witness(args.frequency)
    api(RADIO_BASE, "POST", "/frequency", body={"hz": args.frequency})
    got = {}
    for label, tone in (("tone OFF (control)", None), ("tone ON", args.tone),
                        ("tone OFF again", None)):
        m = _key(tone)
        got[label] = m
        print(f"  {label:20} rms={m['rms']:9.1f}  tone@{args.tone}={m['tone_rms']:9.2f}")
    api(RADIO_BASE, "POST", "/tone", body={"tone": 0})
    off = max(got["tone OFF (control)"]["tone_rms"], got["tone OFF again"]["tone_rms"])
    on = got["tone ON"]["tone_rms"]
    print(f"\n  ON/OFF ratio: {on / off:.0f}x" if off else "\n  control read zero")
    print("  => the tone is present and strong" if off and on > off * 20 else
          "  => the tone is NOT clearly above the floor")
    return 0


def tone_accuracy(args) -> int:
    _require_bench(args.frequency)
    _witness(args.frequency)
    api(RADIO_BASE, "POST", "/frequency", body={"hz": args.frequency})
    print(f"  {'asked':<9}{'measured':<12}{'error':<10}{'ratio':<10}")
    ratios = []
    for tone in TONES:
        api(RADIO_BASE, "POST", "/tone", body={"tone": tone})
        time.sleep(0.5)
        pcm = dp.key_and_listen(6.0, 0.0)
        best, _ = dp.measure_transmission(pcm, tone, 6.0)
        got = peak_hz(best, tone)
        ratios.append(got / tone)
        print(f"  {tone:<9.1f}{got:<12.3f}{got - tone:<+10.3f}{got / tone:<10.5f}")
    api(RADIO_BASE, "POST", "/tone", body={"tone": 0})
    spread = max(ratios) - min(ratios)
    mean = sum(ratios) / len(ratios)
    print(f"\n  mean ratio {mean:.5f} ({(mean - 1) * 100:+.3f}%), spread {spread:.5f}")
    if spread < 0.001:
        print("  => a CONSTANT proportional error across every tone. That is a clock, not a bad")
        print("     tone code -- and the witness has one (kv4p.sample_rate_correction). Calibrate")
        print("     the witness before attributing any of this to the radio.")
    else:
        print("  => the error is NOT a constant ratio, so it is not purely a clock. Real per-tone")
        print("     error is present and worth chasing.")
    return 0


def split(args) -> int:
    _require_bench(args.frequency)
    _require_bench(args.split_tx)
    api(RADIO_BASE, "POST", "/frequency", body={"hz": args.frequency})
    api(RADIO_BASE, "POST", "/split", body={"tx_hz": args.split_tx})
    api(RADIO_BASE, "POST", "/tone", body={"tone": args.tone})
    code, st = api(RADIO_BASE, "GET", "/status")
    print(f"  armed: rx={st.get('frequency')} tx={st.get('tx_frequency')} tone={st.get('tone')}")
    if st.get("tx_frequency") != args.split_tx:
        print("  the API did not arm the split; not keying")
        return 1
    legs = {}
    for label, hz in (("TX leg", args.split_tx), ("RX leg", args.frequency)):
        _witness(hz)
        pcm = dp.key_and_listen(6.0, 0.0)
        _, m = dp.measure_transmission(pcm, args.tone, 6.0)
        legs[label] = m["rms"]
        print(f"  witness on {label} {hz}: rms={m['rms']:9.1f}  tone={m['tone_rms']:9.2f}")
    api(RADIO_BASE, "POST", "/tone", body={"tone": 0})
    api(RADIO_BASE, "POST", "/split", body={"tx_hz": None})
    if legs["TX leg"] > legs["RX leg"] * 3:
        print("\n  => SPLIT WORKS: the carrier is on the transmit leg.")
        return 0
    print("\n  => SPLIT IS BROKEN: it transmitted on the RECEIVE frequency. No repeater would key,")
    print("     and simplex would look perfect -- exactly the reported symptom.")
    return 1


def presets(args) -> int:
    """Never keys. Safe against real repeater presets."""
    code, body = api(RADIO_BASE, "GET", "/presets")
    rows = body if isinstance(body, list) else body.get("presets", [])
    bad = []
    for p in rows:
        want_rx, want_tx = p.get("frequency"), p.get("tx_frequency")
        offset, want_tone = p.get("offset"), p.get("tx_tone")
        expect_tx = want_tx or ((want_rx + offset) if (offset and want_rx) else None)
        api(RADIO_BASE, "POST", "/presets/apply", body={"name": p.get("name")})
        time.sleep(0.25)
        _, st = api(RADIO_BASE, "GET", "/status")
        problems = []
        if st.get("frequency") != want_rx:
            problems.append(f"rx {st.get('frequency')} != {want_rx}")
        if expect_tx and st.get("tx_frequency") != expect_tx:
            problems.append(f"TX LEG {st.get('tx_frequency')} != {expect_tx}")
        if want_tone and (st.get("tone") is None or abs(st["tone"] - want_tone) > 0.05):
            problems.append(f"tone {st.get('tone')} != {want_tone}")
        if problems:
            bad.append((p.get("name"), "; ".join(problems)))
        print(f"  {'!!' if problems else '  '} {str(p.get('name'))[:30]:<30} "
              f"rx={st.get('frequency')} tx={st.get('tx_frequency')} tone={st.get('tone')}")
    print(f"\n  {len(rows)} presets, {len(bad)} armed wrong")
    for name, why in bad:
        print(f"    {name}: {why}")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tone-control", action="store_true")
    ap.add_argument("--tone-accuracy", action="store_true")
    ap.add_argument("--split", action="store_true")
    ap.add_argument("--presets", action="store_true")
    ap.add_argument("--i-will-transmit", action="store_true")
    ap.add_argument("--frequency", type=int, default=BENCH_HZ)
    ap.add_argument("--split-tx", type=int, default=SPLIT_TX_HZ)
    ap.add_argument("--tone", type=float, default=141.3)
    args = ap.parse_args(argv)

    if args.presets:
        return presets(args)
    keys = args.tone_control or args.tone_accuracy or args.split
    if not keys:
        ap.print_help()
        return 2
    if not args.i_will_transmit:
        print("refusing: pass --i-will-transmit (this keys the radio)", file=sys.stderr)
        return 2
    if args.tone_control:
        return tone_control(args)
    if args.tone_accuracy:
        return tone_accuracy(args)
    return split(args)


if __name__ == "__main__":
    raise SystemExit(main())
