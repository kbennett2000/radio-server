#!/usr/bin/env python3
"""Sweep the UV-K5's PA bias and measure what comes out, with the kv4p as the witness.

The question this answers: **is the PA bias the thing limiting radiated power, or is something
else?** Dock TX comes up at whatever `TXP_CalculatedSetting` the firmware computed for the radio's
own VFO — measured on this bench, **bias 12 out of 255** (`0x36 = 0x0CA2`, ADR 0128). The kv4p sees
a clean carrier at that setting, but the kv4p is inches away and near-field coupling is not
radiated power: F4 measured RMS 7427 from a modulator with the PA rail *off*. A handheld across the
room hears nothing.

So sweep the bias and watch the received level. Two possible shapes, and they mean opposite things:

* **Received level rises with bias** — bias is the knob, and 12 is simply too low. The fix is to
  find the radio's real calibration rather than inherit a number computed for the wrong band.
* **Received level is flat** — bias is not the limiter, and the missing power is somewhere the host
  cannot see (the radio's own OUTPUT_POWER setting, the antenna/dummy-load path, or the PA rail
  itself). Stop turning this knob and go look there.

**Measure the kv4p's RSSI, not its audio.** The first version of this compared demodulated audio
RMS and learned nothing, for two compounding reasons: FM is constant-envelope, so audio level does
not track transmit power at all, and at inches every setting sits far above the squelch threshold
where even SNR stops varying. It produced 763-5493 with no relation to bias — noise presented as a
measurement. RSSI is the receiver's actual estimate of received power and it is what moves.

The reading is only meaningful *relative* to itself: the inches-apart geometry makes the absolute
number meaningless. Compare the column, not the value.

**This keys the transmitter, repeatedly.** Dummy load. The service must be stopped (this opens the
serial port directly); the kv4p's service must be RUNNING, since it is the measuring instrument::

    systemctl --user stop radio-server
    RADIO_API_TOKEN=... .venv/bin/python scripts/bench/uvk5_pa_sweep.py --i-will-transmit
    systemctl --user start radio-server
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from acceptance import KV4P_BASE, TOKEN, _collect_rx, api, rms  # noqa: E402

from radio_server.audio import CANONICAL_FORMAT, AudioFrame, synth_tone  # noqa: E402
from radio_server.doctor import _build_backend, _uvk5_config  # noqa: E402

#: reg 0x36 = (bias << 8) | 0x80 | gain, gain 0x08 VHF / 0x22 UHF (bk4829.c:743).
GAIN_VHF, GAIN_UHF = 0x08, 0x22
BAND_SPLIT_HZ = 280_000_000

#: Bias values to try. 12 is what the firmware picks here; 255 is the register's ceiling and what
#: the stock firmware itself writes at the top of its calibration table, so this stays inside the
#: range the radio uses in normal operation.
BIASES = (12, 32, 64, 96, 128, 160, 200, 255)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--i-will-transmit", action="store_true",
                    help="required: this keys the transmitter repeatedly (use a dummy load)")
    ap.add_argument("--frequency", type=int, default=445_800_000,
                    help="Hz; default 445.800, the only frequency the kv4p can witness")
    ap.add_argument("--seconds", type=float, default=3.0, help="hold per step (default 3)")
    args = ap.parse_args(argv)
    if not args.i_will_transmit:
        print("refusing: pass --i-will-transmit (this keys the radio)", file=sys.stderr)
        return 2
    if not TOKEN:
        print("set RADIO_API_TOKEN (the kv4p is the measuring instrument)", file=sys.stderr)
        return 2

    code, st = api(KV4P_BASE, "GET", "/status")
    if code != 200:
        print(f"kv4p not answering ({code}) — it is the witness, start its service", file=sys.stderr)
        return 2
    if abs(st.get("frequency", 0) - args.frequency) > 2500:
        print(f"kv4p is on {st.get('frequency')} Hz, sweeping {args.frequency} Hz — retune it first",
              file=sys.stderr)
        return 2

    gain = GAIN_VHF if args.frequency < BAND_SPLIT_HZ else GAIN_UHF
    cfg = _uvk5_config()
    cfg["frequency"] = args.frequency
    radio = _build_backend(cfg)
    tone = synth_tone(1000.0, args.seconds * 1000.0, amplitude=0.6).samples

    # Quiet baseline, so the keyed numbers have something to be compared against.
    idle = [_kv4p_rssi() for _ in range(6)]
    idle = [r for r in idle if r is not None]
    print(f"sweeping reg 0x36 bias on {args.frequency} Hz, gain byte {gain:#04x}")
    print(f"kv4p idle RSSI (nobody transmitting): {idle}\n")
    print(f"  {'bias':>5}  {'0x36':>7}  {'kv4p RSSI peak':>15}  {'median':>7}  {'audio RMS':>9}")

    results = []
    try:
        for bias in BIASES:
            captured: list = []

            def grab() -> None:
                captured.append(asyncio.run(_collect_rx(KV4P_BASE, args.seconds + 1.0)))

            t = threading.Thread(target=grab)
            t.start()
            time.sleep(0.4)  # let the capture attach before the carrier comes up

            radio.ptt(True)
            want = (bias << 8) | 0x80 | gain
            radio._write_registers([(0x36, want)])
            # Modulate. An unmodulated carrier tells the far end nothing and quiets its receiver;
            # the first version of this keyed silence and then measured the resulting noise.
            radio.transmit(AudioFrame(tone, CANONICAL_FORMAT))
            readings = []
            end = time.monotonic() + args.seconds
            while time.monotonic() < end:
                r = _kv4p_rssi()
                if r is not None:
                    readings.append(r)
                time.sleep(0.2)
            got = radio._read_register(0x36)
            radio.ptt(False)
            t.join()

            peak = max(readings) if readings else 0
            med = sorted(readings)[len(readings) // 2] if readings else 0
            level = rms(captured[0].pcm)
            results.append((bias, peak))
            flag = "" if got == want else f"  (read back {got:#06x}!)"
            print(f"  {bias:>5}  {got:#06x}  {peak:>15}  {med:>7}  {level:>9.0f}{flag}", flush=True)
            time.sleep(1.0)
    finally:
        try:
            radio.ptt(False)
        except Exception:
            pass
        radio.close()

    if len(results) < 2:
        return 0
    lo = results[0][1]
    best_bias, hi = max(results, key=lambda r: r[1])
    if hi == 0:
        # Refuse to conclude on a dead instrument. This script has twice produced a confident
        # "FLAT: bias is not the knob" from a witness that was not measuring anything — first from
        # demodulated audio (which cannot track power in FM), then from an RSSI field this kv4p
        # firmware reports as 0 even while cleanly demodulating a carrier. A flat line from a
        # broken meter looks exactly like a flat line from a real null, and only one of those is a
        # finding.
        print("\n  => NO VERDICT: the kv4p reported RSSI 0 at every step — including while it was "
              "cleanly demodulating the carrier (see the audio RMS column). The witness is not "
              "measuring received power on this firmware, so this run says nothing at all about "
              "whether bias is the knob. Fix the meter before trusting the reading.")
        return 2
    print(f"\n  RSSI at the firmware's own bias ({results[0][0]}): {lo}")
    print(f"  best: {hi} at bias {best_bias}")
    if hi > lo + 3:
        print(f"  => BIAS IS THE KNOB — {hi - lo} counts of headroom above what dock TX uses.")
    else:
        print("  => FLAT: bias is not what limits radiated power here. Look at the radio's own "
              "OUTPUT_POWER setting, the PA rail, or the antenna path.")
    return 0


def _kv4p_rssi() -> int | None:
    try:
        code, st = api(KV4P_BASE, "GET", "/status", timeout=5.0)
    except Exception:
        return None
    return st.get("rssi") if code == 200 and isinstance(st, dict) else None


if __name__ == "__main__":
    raise SystemExit(main())
