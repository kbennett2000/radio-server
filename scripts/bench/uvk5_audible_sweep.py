#!/usr/bin/env python3
"""Find the PA bias that makes the UV-K5 actually audible, using a human's ears as the meter.

This exists because **there is no power meter on this bench.** The kv4p is the only witness and it
cannot measure received power: FM is constant-envelope so demodulated audio level says nothing
about transmit power, its RSSI field reads 0 on this firmware even while cleanly demodulating, and
at inches everything sits far above the squelch threshold anyway. What *is* available is a person
with a handheld across the room, and the standing observation that on 445.800 they hear the tones
while on 147.555 they hear only fragments — which is exactly the shape of "under-driven".

Why bias is the suspect: `Dock_ForceTx` sets the PA up with `gCurrentVfo->TXP_CalculatedSetting`,
computed for the RADIO's own VFO band. That number comes from per-band calibration and the firmware
uses a *different* divider table for 2 m than for 70 cm (`App/radio.c:657-661`). On this bench it
comes out **12 of 255**, derived for UHF — and it is inherited unchanged when the host tunes VHF.
The host cannot read the real VHF calibration (it lives in SPI flash the dock protocol cannot
reach), so the remaining way to find a working value is to try them and ask.

**The listener never has to watch a clock.** Each step announces its own number first: N short
beeps, then a steady tone. The report is one sentence — "I could hear step 5 onwards" — and that
names the bias directly.

**This keys the transmitter repeatedly.** Real antenna, bench frequency. Service stopped::

    systemctl --user stop radio-server
    .venv/bin/python scripts/bench/uvk5_audible_sweep.py --i-will-transmit --frequency 147555000
    systemctl --user start radio-server
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from radio_server.audio import CANONICAL_FORMAT, AudioFrame, synth_tone  # noqa: E402
from radio_server.doctor import _build_backend, _uvk5_config  # noqa: E402

GAIN_VHF, GAIN_UHF = 0x08, 0x22
BAND_SPLIT_HZ = 280_000_000

#: 12 is what the firmware picks here. 255 is the register ceiling, which is inside the range the
#: stock firmware's own calibration tables reach, so nothing here drives the PA past what the radio
#: does in normal operation.
BIASES = (12, 32, 64, 96, 128, 160, 200, 255)

BEEP_MS = 180.0
BEEP_GAP_MS = 140.0
STEADY_MS = 2500.0


def marker(n: int) -> bytes:
    """``n`` short beeps, then silence — the step announcing its own number."""
    beep = synth_tone(1200.0, BEEP_MS, amplitude=0.6).samples
    gap = b"\x00\x00" * int(CANONICAL_FORMAT.rate * BEEP_GAP_MS / 1000.0)
    return (beep + gap) * n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--i-will-transmit", action="store_true",
                    help="required: this keys the transmitter repeatedly")
    ap.add_argument("--frequency", type=int, default=147_555_000, help="Hz (default 147.555)")
    ap.add_argument("--gap", type=float, default=4.0, help="seconds between steps (default 4)")
    args = ap.parse_args(argv)
    if not args.i_will_transmit:
        print("refusing: pass --i-will-transmit (this keys the radio)", file=sys.stderr)
        return 2

    gain = GAIN_VHF if args.frequency < BAND_SPLIT_HZ else GAIN_UHF
    cfg = _uvk5_config()
    cfg["frequency"] = args.frequency
    radio = _build_backend(cfg)
    steady = synth_tone(1000.0, STEADY_MS, amplitude=0.6).samples

    print(f"audible bias sweep on {args.frequency} Hz (gain byte {gain:#04x})")
    print(f"{len(BIASES)} steps. Each is: N beeps, then a steady tone. Listen for the highest N you")
    print("can hear clearly — that names the bias.\n")
    print(f"  {'step':>4}  {'bias':>5}  {'0x36 read back':>15}")
    try:
        for i, bias in enumerate(BIASES, start=1):
            radio.ptt(True)
            want = (bias << 8) | 0x80 | gain
            radio._write_registers([(0x36, want)])
            radio.transmit(AudioFrame(marker(i) + steady, CANONICAL_FORMAT))
            try:
                got = radio._read_register(0x36)
            except Exception:
                got = -1
            radio.ptt(False)
            flag = "" if got == want else "   <- did NOT take"
            print(f"  {i:>4}  {bias:>5}  {got:#06x}{flag}", flush=True)
            time.sleep(args.gap)
    finally:
        try:
            radio.ptt(False)
        except Exception:
            pass
        radio.close()

    print("\n  step -> bias:  " + ",  ".join(f"{i}={b}" for i, b in enumerate(BIASES, start=1)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
