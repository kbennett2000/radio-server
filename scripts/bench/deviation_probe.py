#!/usr/bin/env python3
"""Compare our CTCSS deviation against a KNOWN-GOOD transmitter (ADR 0135).

The field failure this exists for: none of 37 repeater presets key up, while simplex TX works and
the operator opens those same repeaters on the same antenna with a handheld at microwatts. Power,
feedline and antenna are therefore ruled out, and every register in the TX path reads correct in
source. What has never been measured, in any units, is **deviation** — how far the tone actually
swings the carrier.

Why the existing instrument cannot answer it. ``acceptance.tone_power`` returns a *fraction* of
total spectral energy, deliberately level-independent so it survives a change of power or geometry.
That is the wrong tool twice over here:

* A tone at a tenth of its proper deviation occupies the same fraction of a quiet capture as a
  correct one, and sails through the 0.01 floor.
* On a **dead carrier** the fraction saturates: with only tone and receiver noise present it tends
  to ``P_tone / (P_tone + P_noise)``, which pins near 1.0 as soon as the tone clears the noise. It
  has resolution only where the tone is comparable to the noise floor — not the regime under test.

So this measures the **absolute** recovered amplitude in the tone band. FM demodulation recovers an
amplitude proportional to deviation, and there is no AGC anywhere in the witness chain (the only
gain multiplier in the kv4p backend, ``_apply_tx_gain``, is TX-only), so that amplitude is a
faithful stand-in for deviation.

**The method.** A radio that demonstrably opens these repeaters is a calibration reference. Have it
send a dead carrier plus CTCSS; capture it on the kv4p. Then have radio-server send the same thing
on the same frequency into the same witness at the same geometry, and compare. Every loss in the
receive chain — the SA818's de-emphasis and LF roll-off, Opus, the resampler — is common to both
captures and divides out. The RATIO is trustworthy even though no single absolute number from this
bench is. This reports a ratio and never pretends to report deviation in Hz; the bench has no
instrument that can measure that.

**Three legs, because two cannot localise a fault.** Capture the known-good handheld, then the
UV-K5 keyed from *its own front panel*, then the UV-K5 keyed by radio-server. The middle leg is
free and it is what separates "our software under-deviates" from "this radio under-deviates":

===================  ==================================================================
A ≈ B ≈ C            deviation is fine on all three — the fault is elsewhere
A ≈ B >> C           our dock register writes under-deviate the tone (a bug we own)
A >> B ≈ C           the UV-K5 itself under-deviates in both firmwares (not a code bug)
B >> A               the rig is broken; stop and fix it before believing anything
===================  ==================================================================

**141.3 Hz, not 100.0 Hz.** Opus/SILK applies a high-pass whose corner moves through roughly
60-100 Hz as a function of signal content, so a measurement at 100 Hz confounds "the tone got
weaker" with "the codec's corner moved". 141.3 Hz — the highest tone in the operator's set — sits
above it, and our REG_51 gain is the same constant for every tone, so a result there generalises.

Four invocations, because a human keys the first two. Those two open a long window and measure the
loudest stretch inside it, so there is no cue to hit and no way to be too slow::

    # A: operator keys the known-good handheld ~8 s, saying nothing, any time in the window
    .venv/bin/python scripts/bench/deviation_probe.py --reference baofeng-mini

    # B: operator keys the UV-K5 itself from its own front panel, same deal
    .venv/bin/python scripts/bench/deviation_probe.py --reference uvk5-front-panel

    # C: radio-server does the identical thing
    RADIO_API_TOKEN=... scripts/bench/deviation_probe.py --under-test --i-will-transmit

    # read the answer
    .venv/bin/python scripts/bench/deviation_probe.py --compare

``--sweep`` then answers the second question: does our tone collapse once real audio drives the
modulator? See :func:`reached_the_limiter` for why that sweep is unfalsifiable without a control.

Runs against the live API; the service stays up (ADR 0127 — stopping it is what broke this bench).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from acceptance import (  # noqa: E402
    BENCH_TX_HZ,
    KV4P_BASE,
    RADIO_BASE,
    TOKEN,
    _collect_rx,
    api,
    rms,
    tone_power,
)

from radio_server.audio import synth_tone  # noqa: E402
from radio_server.audio.format import CANONICAL_RATE  # noqa: E402

#: The tone every leg is measured at. See the module docstring for why this is not 100.0.
DEFAULT_TONE_HZ = 141.3
#: Narrow enough to exclude DC and the voice band. At 48 kHz over several seconds the FFT bin
#: spacing is far below 1 Hz, so this is real resolution rather than wishful arithmetic.
TONE_WIDTH_HZ = 5.0
#: Wide enough for the audible test tones, whose exact frequency does not matter.
AUDIO_WIDTH_HZ = 60.0
#: Below this RMS a capture is silence, not a measurement — see :func:`witness_heard_anything`.
SILENCE_RMS = 100.0
#: How long a ``--reference`` run listens. Sized for an operator who is not sitting at the bench:
#: the radios live in a different room from the terminal, so this has to cover walking there,
#: keying, and walking back. Listening costs nothing — only the loudest slice is measured — so the
#: default is generous on purpose and ``--window`` raises it further.
DEFAULT_OPERATOR_WINDOW = 180.0

#: The sweep's stimulus: a tone that rises, and a pilot that does not.
SWEEP_TONE_HZ = 1600.0
PILOT_TONE_HZ = 1000.0
PILOT_AMPLITUDE = 0.10
#: Dense at the bottom because the limiter's knee is the interesting part, and capped so the SUM
#: with the pilot never clips in int16 — a clipped STIMULUS would be misread as a limiting MODULATOR.
SWEEP_AMPLITUDES = (0.0, 0.05, 0.10, 0.20, 0.35, 0.50, 0.65, 0.85)
#: How far below a linear extrapolation the top step must fall before we call it compression.
COMPRESSION_MARGIN = 0.8

#: Where captures accumulate between invocations — the human-keyed legs are separate runs.
DEFAULT_STORE = Path("/tmp/deviation-probe.json")


def band_rms(pcm: bytes, freq: float, width: float = TONE_WIDTH_HZ,
             rate: int = CANONICAL_RATE) -> float:
    """Time-domain RMS of just the ``freq ± width`` band, in PCM LSB.

    The mean is removed and a Hann window applied before masking, so a strong carrier or DC offset
    cannot leak into the band and masquerade as tone; dividing by the window's own RMS puts the
    result back into the same units as :func:`acceptance.rms`.

    Absolute, not normalised, ON PURPOSE — the fraction saturates on a dead carrier (module
    docstring). Comparable only between captures taken through the same receive chain at the same
    geometry, which is exactly the comparison this script makes.
    """
    a = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    if a.size < 2:
        return 0.0
    a = a - a.mean()
    window = np.hanning(a.size)
    spec = np.fft.rfft(a * window)
    freqs = np.fft.rfftfreq(a.size, 1.0 / rate)
    spec[(freqs < freq - width) | (freqs > freq + width)] = 0.0
    band = np.fft.irfft(spec, n=a.size)
    return float(np.sqrt(np.mean(band**2)) / np.sqrt(np.mean(window**2)))


def witness_heard_anything(pcm: bytes) -> bool:
    """Did the witness actually deliver audio?

    The RX pump gates frames on activity (``rx/pump.py``). A carrier whose only modulation is a
    sub-audible tone can fall UNDER a level-driven gate and deliver nothing — which is byte for
    byte what zero deviation looks like. Refusing to record that capture is the difference between
    "no tone" and "no data", and this rig exists to tell those apart.
    """
    return len(pcm) > 0 and rms(pcm) >= SILENCE_RMS


def loudest_slice(pcm: bytes, seconds: float, rate: int = CANONICAL_RATE) -> bytes:
    """The ``seconds``-long stretch of ``pcm`` carrying the most energy.

    Every capture here contains more window than transmission: the operator keys by hand somewhere
    inside a long listening window, and the script-keyed leg brackets its own carrier with lead-in
    and tail. Measuring the whole window instead divides the transmission's energy across the dead
    air around it, and **the two legs do not carry the same amount of dead air** — so the ratio the
    verdict is read from would move with the operator's reaction time rather than with deviation.
    Slicing both legs to their loudest equal-length stretch is what makes them comparable.
    """
    frame = 2  # int16 mono
    usable = pcm[: len(pcm) - (len(pcm) % frame)]
    want = int(seconds * rate) * frame
    if want <= 0 or len(usable) <= want:
        return usable
    a = np.frombuffer(usable, dtype="<i2").astype(np.float64)
    n = want // frame
    # A cumulative sum makes every candidate offset O(1), so this stays linear over a long window.
    energy = np.concatenate(([0.0], np.cumsum(a * a)))
    start = int(np.argmax(energy[n:] - energy[:-n]))
    return usable[start * frame: start * frame + want]


def measure(pcm: bytes, tone: float) -> dict:
    """Every number we can honestly extract from one capture."""
    return {
        "bytes": len(pcm),
        "rms": round(rms(pcm), 1),
        "tone_rms": round(band_rms(pcm, tone), 2),
        "tone_fraction": round(tone_power(pcm, tone, width=TONE_WIDTH_HZ), 6),
        "swept_rms": round(band_rms(pcm, SWEEP_TONE_HZ, AUDIO_WIDTH_HZ), 2),
        "pilot_rms": round(band_rms(pcm, PILOT_TONE_HZ, AUDIO_WIDTH_HZ), 2),
    }


def measure_transmission(pcm: bytes, tone: float, seconds: float) -> tuple[bytes, dict]:
    """Slice a capture down to the transmission inside it, then measure THAT.

    Both legs go through here and nothing else measures a raw capture, because the comparison is a
    ratio: identical treatment is the whole reason the receive chain's unknowns divide out.
    """
    best = loudest_slice(pcm, seconds)
    return best, measure(best, tone)


@dataclass
class Record:
    label: str
    kind: str            # "reference" | "under-test"
    tone: float
    amplitude: float     # 0.0 == dead carrier
    frequency: int
    measured: dict


def stimulus(amplitude: float, seconds: float) -> bytes:
    """The swept tone at ``amplitude`` summed with the fixed pilot, as canonical PCM.

    The pilot is what makes the sweep readable: it is the only part of the stimulus that does not
    change, so if IT starts shrinking the modulator is compressing everything, not just the tone.
    """
    ms = max(seconds - 0.5, 0.5) * 1000.0
    swept = np.frombuffer(synth_tone(SWEEP_TONE_HZ, ms, amplitude=amplitude).samples, dtype="<i2")
    pilot = np.frombuffer(synth_tone(PILOT_TONE_HZ, ms, amplitude=PILOT_AMPLITUDE).samples,
                          dtype="<i2")
    n = min(swept.size, pilot.size)
    summed = swept[:n].astype(np.int32) + pilot[:n].astype(np.int32)
    if np.abs(summed).max() > 32767:
        print(f"    !! stimulus clipped at amplitude {amplitude:.2f} — the measurement below is "
              f"about OUR audio, not the radio's modulator", file=sys.stderr)
    return np.clip(summed, -32768, 32767).astype("<i2").tobytes()


def listen(seconds: float) -> bytes:
    """Capture from the kv4p witness. Transmits nothing."""
    async def go():
        started = asyncio.Event()
        collector = asyncio.create_task(_collect_rx(KV4P_BASE, seconds, started))
        await started.wait()
        return await collector

    return asyncio.run(go()).pcm


def key_and_listen(seconds: float, amplitude: float) -> bytes:
    """Key the radio under test for ``seconds`` and capture what the witness hears.

    ``amplitude`` 0 keys a DEAD CARRIER through ``/ptt`` — no audio at all, so the CTCSS tone is the
    only modulation and the measurement is not competing with speech energy.
    """
    async def go():
        started = asyncio.Event()
        collector = asyncio.create_task(_collect_rx(KV4P_BASE, seconds + 1.0, started))
        await started.wait()
        await asyncio.sleep(0.3)
        if amplitude <= 0:
            await asyncio.to_thread(api, RADIO_BASE, "POST", "/ptt", {"on": True}, None, 10.0)
            try:
                await asyncio.sleep(seconds)
            finally:
                await asyncio.to_thread(api, RADIO_BASE, "POST", "/ptt", {"on": False}, None, 10.0)
        else:
            await asyncio.to_thread(api, RADIO_BASE, "POST", "/transmit", None,
                                    stimulus(amplitude, seconds), 60.0)
        return await collector

    return asyncio.run(go()).pcm


def reached_the_limiter(points: list[Record]) -> bool:
    """Did the sweep actually drive the modulator into compression?

    Without this the sweep is unfalsifiable. If the playback level is set low the modulator stays
    linear, the CTCSS amplitude comes out perfectly flat, and that reads as "audio does not squash
    the tone" when it actually means "we never tested it". False here means INCONCLUSIVE — raise
    the level and re-run — and must never be reported as a refutation.

    Compression is called when the loudest step's swept-tone amplitude falls more than
    ``COMPRESSION_MARGIN`` below the line through the small-signal steps, where the modulator is
    still linear.
    """
    small = [p for p in points if 0 < p.amplitude <= 0.20 and p.measured["swept_rms"] > 0]
    loud = [p for p in points if p.amplitude >= 0.35]
    if len(small) < 2 or not loud:
        return False
    slope = sum(p.measured["swept_rms"] / p.amplitude for p in small) / len(small)
    top = max(loud, key=lambda p: p.amplitude)
    return top.measured["swept_rms"] < slope * top.amplitude * COMPRESSION_MARGIN


def load(store: Path) -> list[Record]:
    try:
        return [Record(**r) for r in json.loads(store.read_text())]
    except (OSError, ValueError, TypeError):
        return []


def save(store: Path, records: list[Record]) -> None:
    store.write_text(json.dumps([asdict(r) for r in records], indent=2))


def show(m: dict) -> None:
    print(f"    RMS {m['rms']:>8.1f}   tone RMS {m['tone_rms']:>8.2f}   "
          f"fraction {m['tone_fraction']:.6f}")


def compare(store: Path) -> int:
    records = load(store)
    if not records:
        print(f"nothing recorded in {store}", file=sys.stderr)
        return 2

    print(f"\n  captures in {store}\n")
    print(f"    {'label':<24} {'kind':<11} {'tone':>6} {'audio':>6} {'RMS':>9} "
          f"{'tone RMS':>9} {'fraction':>10}")
    for r in records:
        audio = "dead" if r.amplitude <= 0 else f"{r.amplitude:.2f}"
        m = r.measured
        print(f"    {r.label:<24} {r.kind:<11} {r.tone:>6.1f} {audio:>6} "
              f"{m['rms']:>9.1f} {m['tone_rms']:>9.2f} {m['tone_fraction']:>10.6f}")

    # Compare like with like: dead-carrier captures only. With voice present the absolute level
    # depends on how hard each radio was driven, which is a different question.
    dead = [r for r in records if r.amplitude <= 0]
    refs = [r for r in dead if r.kind == "reference"]
    duts = [r for r in dead if r.kind == "under-test"]
    if not refs or not duts:
        print("\n  need a dead-carrier capture from both a --reference and --under-test to compare.")
        return 1

    dut = max(r.measured["tone_rms"] for r in duts)
    print(f"\n  dead-carrier tone amplitude, radio-server: {dut:.2f}\n")
    verdicts = []
    for r in refs:
        ref = r.measured["tone_rms"]
        if ref <= 0:
            print(f"    vs {r.label:<22} reference has no tone in it — check its settings")
            continue
        ratio = dut / ref
        db = 20 * np.log10(ratio) if ratio > 0 else float("-inf")
        print(f"    vs {r.label:<22} {ref:>8.2f}   ratio {ratio:>5.2f}x  ({db:+.1f} dB)")
        verdicts.append((r.label, ratio))

    if not verdicts:
        return 1
    worst = min(ratio for _, ratio in verdicts)
    best = max(ratio for _, ratio in verdicts)
    print()
    if worst < 0.5:
        print("  VERDICT: our CTCSS is materially weaker than a transmitter that opens these\n"
              "  repeaters. Under-deviation is a live cause of the field failure.")
        return 1
    if best > 2.0:
        print("  VERDICT: our CTCSS is materially STRONGER than the reference. Over-deviation is\n"
              "  its own failure mode — some decoders reject a tone that swings too far.")
        return 1
    print("  VERDICT: our CTCSS deviation is comparable to a known-good transmitter.\n"
          "  Deviation is NOT the fault. Close this line of inquiry and look elsewhere.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--reference", metavar="LABEL",
                      help="listen only while YOU key a known-good radio by hand (use this for "
                           "both the handheld and the UV-K5's own front panel)")
    mode.add_argument("--under-test", action="store_true",
                      help="key the radio through radio-server and measure the same way")
    mode.add_argument("--sweep", action="store_true",
                      help="key at rising audio levels: does real audio squash our tone?")
    mode.add_argument("--compare", action="store_true", help="print the comparison and a verdict")
    ap.add_argument("--i-will-transmit", action="store_true",
                    help="required for --under-test and --sweep (these key the radio)")
    ap.add_argument("--frequency", type=int, default=445_800_000, help="Hz (a bench frequency)")
    ap.add_argument("--tone", type=float, default=DEFAULT_TONE_HZ,
                    help=f"CTCSS tone to measure, Hz (default {DEFAULT_TONE_HZ})")
    ap.add_argument("--seconds", type=float, default=6.0, help="measured length (default 6)")
    ap.add_argument("--window", type=float, default=DEFAULT_OPERATOR_WINDOW,
                    help="how long a --reference run listens while you key by hand (default 180); "
                         "raise it if the radio is a walk away — listening is free")
    ap.add_argument("--store", type=Path, default=DEFAULT_STORE, help="where captures accumulate")
    args = ap.parse_args(argv)

    if args.compare:
        return compare(args.store)

    keys = args.under_test or args.sweep
    if keys and not args.i_will_transmit:
        print("refusing: pass --i-will-transmit (this keys the radio)", file=sys.stderr)
        return 2
    # The runner keys bench frequencies only. A repeater's input is the operator's to key by hand,
    # never this script's — that rule is what keeps an automated probe off live machines.
    if keys and args.frequency not in BENCH_TX_HZ:
        print(f"refusing: {args.frequency} Hz is not a bench frequency ({sorted(BENCH_TX_HZ)})",
              file=sys.stderr)
        return 2
    if not TOKEN:
        print("set RADIO_API_TOKEN", file=sys.stderr)
        return 2

    code, body = api(KV4P_BASE, "POST", "/frequency", body={"hz": args.frequency})
    if code != 200:
        print(f"could not tune the witness to {args.frequency} ({code} {body!r:.80})",
              file=sys.stderr)
        return 2
    # Do NOT touch the witness's mode/bandwidth anywhere in this script: it changes the demodulator
    # constant, and every ratio here depends on that constant being identical across legs.
    time.sleep(2.0)  # a kv4p retune cycles its receiver; let it settle

    records = load(args.store)

    if args.reference:
        window = max(args.window, args.seconds + 1.0)
        mhz = args.frequency / 1e6
        print(f"\n  ==> PICK UP THE RADIO AND HOLD ITS PTT FOR ABOUT "
              f"{args.seconds + 2:.0f} SECONDS. SAY NOTHING.\n")
        print(f"      The radio must be on {mhz:.4f} MHz, {args.tone:.1f} Hz CTCSS (TX tone), "
              f"wide FM, no offset.")
        print(f"      Any time in the next {window:.0f} seconds — there is no cue to hit. The "
              f"loudest {args.seconds:.0f} seconds")
        print("      of the window is what gets measured, so take your time.")
        # The label is a filename, not an instruction. It read as one, and the operator stopped and
        # asked what the script wanted from them — so it goes last and says what it is.
        print(f"\n      (filing this capture as '{args.reference}'; listening on the kv4p now)\n",
              flush=True)
        pcm = listen(window)
        heard = len(pcm) / (CANONICAL_RATE * 2)
        best, m = measure_transmission(pcm, args.tone, args.seconds)
        print(f"  witness delivered {heard:.1f} s of audio; measured its loudest "
              f"{len(best) / (CANONICAL_RATE * 2):.1f} s")
        show(m)
        if not witness_heard_anything(best):
            print("\n  that capture is silence, not a measurement. Either the radio was not keyed "
                  "in the\n  window, or it was on the wrong frequency, or the witness's activity "
                  "gate dropped a\n  carrier modulated only by a sub-audible tone. Not recording "
                  "it — a silent capture and\n  a zero-deviation transmitter look identical, and "
                  "this rig exists to tell them apart.")
            return 1
        records.append(Record(args.reference, "reference", args.tone, 0.0, args.frequency, m))
        save(args.store, records)
        print(f"\n  recorded to {args.store}")
        return 0

    code, body = api(RADIO_BASE, "POST", "/frequency", body={"hz": args.frequency})
    if code != 200:
        print(f"could not tune the radio under test ({code} {body!r:.80})", file=sys.stderr)
        return 2
    code, body = api(RADIO_BASE, "POST", "/tone", body={"tone": args.tone})
    if code != 200:
        print(f"could not set the tone ({code} {body!r:.80})", file=sys.stderr)
        return 2
    time.sleep(0.5)

    fresh: list[Record] = []
    try:
        # The sweep repeats its silent step LAST as a drift control: if the two disagree, the
        # receive chain moved during the run and every absolute number between them is void.
        amplitudes = list(SWEEP_AMPLITUDES) + [0.0] if args.sweep else [0.0]
        for i, amp in enumerate(amplitudes):
            what = "dead carrier" if amp <= 0 else f"{SWEEP_TONE_HZ:.0f} Hz at {amp:.2f} + pilot"
            print(f"\n  keying {args.frequency} Hz, {args.tone:.1f} Hz CTCSS, {what}...", flush=True)
            pcm = key_and_listen(args.seconds, amp)
            # Sliced exactly like the operator-keyed leg: key_and_listen brackets the carrier with
            # a 0.3 s lead-in and a 1 s tail, and dead air the other leg does not carry would drag
            # this leg's amplitude down and read as under-deviation.
            pcm, m = measure_transmission(pcm, args.tone, args.seconds)
            show(m)
            if amp <= 0 and not witness_heard_anything(pcm):
                print("\n  the witness delivered silence for a dead carrier. Fix that before "
                      "believing any\n  number here: a gated capture and a dead transmitter are "
                      "the same bytes.")
                return 1
            label = f"uvk5-{'sweep' if args.sweep else 'dut'}-{i}-{amp:.2f}"
            rec = Record(label, "under-test", args.tone, amp, args.frequency, m)
            fresh.append(rec)
            records.append(rec)
            save(args.store, records)
    finally:
        api(RADIO_BASE, "POST", "/tone", body={"tone": 0})

    print(f"\n  recorded to {args.store}")
    if args.sweep:
        return report_sweep(fresh)
    return 0


def report_sweep(points: list[Record]) -> int:
    """Read the sweep out loud. See :func:`reached_the_limiter` for why the control comes first."""
    silent = [p for p in points if p.amplitude <= 0]
    print(f"\n  {'audio':>6} {'CTCSS':>9} {'swept':>9} {'pilot':>9}")
    for p in points:
        m = p.measured
        audio = "dead" if p.amplitude <= 0 else f"{p.amplitude:.2f}"
        print(f"  {audio:>6} {m['tone_rms']:>9.2f} {m['swept_rms']:>9.2f} {m['pilot_rms']:>9.2f}")

    if len(silent) >= 2:
        first, last = silent[0].measured["tone_rms"], silent[-1].measured["tone_rms"]
        drift = abs(last - first) / first if first > 0 else float("inf")
        print(f"\n  drift control: dead carrier {first:.2f} at the start, {last:.2f} at the end "
              f"({drift * 100:.0f}%)")
        if drift > 0.25:
            print("  => the receive chain moved during the run. The sweep is VOID; re-run it.")
            return 1

    if not reached_the_limiter(points):
        print("\n  INCONCLUSIVE: the swept tone never compressed, so this run never drove the "
              "modulator\n  into limiting and cannot say whether audio squashes the tone. Raise "
              "the playback level\n  and re-run. This is NOT a refutation.")
        return 1

    dead = silent[0].measured["tone_rms"] if silent else 0.0
    loud = max(points, key=lambda p: p.amplitude).measured["tone_rms"]
    if dead <= 0:
        return 1
    print(f"\n  CTCSS amplitude: dead carrier {dead:.2f} -> loudest step {loud:.2f} "
          f"({loud / dead:.2f}x)")
    if loud < dead * 0.5:
        print("  => real audio IS squashing the access tone. A repeater sees the tone at half "
              "strength\n     or less exactly when someone is talking, which is the only time it "
              "matters.")
        return 1
    print("  => the tone holds up under audio that provably reached the limiter. Not the fault.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
