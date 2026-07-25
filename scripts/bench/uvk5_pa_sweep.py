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

The reading is only meaningful *relative* to itself: the kv4p's AGC and the inches-apart geometry
make the absolute number meaningless. Compare the column, not the value.

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

from acceptance import KV4P_BASE, TOKEN, _collect_rx, api, rms, tone_power  # noqa: E402

from radio_server.audio import synth_tone  # noqa: E402
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

    print(f"sweeping reg 0x36 bias on {args.frequency} Hz, gain byte {gain:#04x}\n")
    print(f"  {'bias':>5}  {'0x36':>7}  {'kv4p RMS':>9}  {'1 kHz':>6}  {'busy':>5}")
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
            busy_seen = False
            end = time.monotonic() + args.seconds
            while time.monotonic() < end:
                _, kst = api(KV4P_BASE, "GET", "/status", timeout=5.0)
                busy_seen = busy_seen or bool(kst.get("busy"))
                time.sleep(0.25)
            got = radio._read_register(0x36)
            radio.ptt(False)
            t.join()

            cap = captured[0]
            level, recovered = rms(cap.pcm), tone_power(cap.pcm, 1000.0)
            results.append((bias, level))
            flag = "" if got == want else f"  (read back {got:#06x}!)"
            print(f"  {bias:>5}  {got:#06x}  {level:>9.0f}  {recovered:>6.3f}  "
                  f"{'YES' if busy_seen else 'no':>5}{flag}", flush=True)
            time.sleep(1.0)
    finally:
        try:
            radio.ptt(False)
        except Exception:
            pass
        radio.close()

    if len(results) >= 2:
        lo, hi = results[0][1], max(r[1] for r in results)
        print(f"\n  lowest-bias RMS {lo:.0f}, best {hi:.0f} — ratio {hi / max(lo, 1):.2f}x")
        print("  => bias is the knob" if hi > lo * 1.5 else
              "  => FLAT: bias is not what is limiting radiated power. Look at the radio's own "
              "OUTPUT_POWER, the PA rail, or the antenna path.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
