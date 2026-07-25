#!/usr/bin/env python3
"""Dump the UV-K5 V3's BK4819 TX-path registers idle / keyed / un-keyed, over the dock.

The question this answers: **when radio-server keys the UV-K5 in dock mode, does the power
amplifier actually come up?** Received-signal tests cannot answer it on a bench where the two
radios are inches apart — a bare modulator with no PA still couples enough near-field energy for
the far end to see "a carrier" (F4 measured RMS 7427 that way, ADR 0126). The register state is
the ground truth.

What to read in the output:

* ``0x30`` — TX enable. ``0xC1FE`` keyed, the RX word un-keyed. Proves the *modulator*.
* ``0x33`` — the BK4819 GPIO-output byte, written whole (``bk4829.c:434``, mask ``0x40 >> pin``):
  - ``0x40`` ``GPIO0_PIN28_RX_ENABLE``  — stock **clears** this on TX (``radio.c:987``)
  - ``0x20`` ``GPIO1_PIN29_PA_ENABLE``  — stock **sets** this on TX (``radio.c:1017``) ← the PA rail
  - ``0x08``/``0x04`` UHF/VHF LNA path  — ``PickRXFilterPathBasedOnFrequency``
* ``0x36`` — PA bias/gain, ``(bias << 8) | 0x80 | (0x08 VHF : 0x22 UHF)`` (``bk4829.c:729``).

If ``0x20`` is **set while keyed** the PA rail is up. If it is set without radio-server writing it,
the firmware is doing it (an F5-style ``Dock_ForceTx`` is flashed and firing). If it is clear while
keyed, the PA never came up and no meaningful RF is radiating regardless of what the far end sees.

**This keys the transmitter.** Pass ``--i-will-transmit`` to arm it. Unlike ``doctor``'s typed
CONFIRM prompt, this is an explicit flag because the whole point is unattended bench acceptance —
the guard is deliberate opt-in, not the presence of a human. Dummy load, please.

The running service owns the serial port, so stop it first and start it after::

    systemctl --user stop radio-server
    .venv/bin/python scripts/bench/uvk5_tx_regs.py --i-will-transmit
    systemctl --user start radio-server
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from radio_server.doctor import _build_backend, _uvk5_config  # noqa: E402

#: (register, what it is) — the whole TX path, in the order stock firmware touches it.
REGISTERS: tuple[tuple[int, str], ...] = (
    (0x30, "system control / TX enable (0xC1FE = keyed)"),
    (0x33, "GPIO out: 0x40 RX_ENABLE, 0x20 PA_ENABLE, 0x08/0x04 LNA"),
    (0x36, "PA bias<<8 | 0x80 | gain (0x22 UHF / 0x08 VHF)"),
    (0x37, "TxOn_Beep housekeeping (stock writes 0x9D1F)"),
    (0x47, "AF selector (0x6142 FM / 0x6042 mute)"),
    (0x50, "AF/TX path un-mute (stock writes 0x3B20)"),
    (0x52, "TxOn_Beep housekeeping (stock writes 0x028F)"),
)

PA_ENABLE = 0x20
RX_ENABLE = 0x40


def snapshot(radio) -> dict[int, int]:
    return {reg: radio._read_register(reg) for reg, _ in REGISTERS}


def show(label: str, snap: dict[int, int]) -> None:
    print(f"\n  {label}")
    for reg, what in REGISTERS:
        val = snap[reg]
        flags = ""
        if reg == 0x33:
            bits = []
            bits.append("PA_ENABLE" if val & PA_ENABLE else "pa_off")
            bits.append("RX_ENABLE" if val & RX_ENABLE else "rx_off")
            flags = f"   [{' '.join(bits)}]"
        print(f"    0x{reg:02X} = 0x{val:04X}{flags:<26} {what}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--i-will-transmit", action="store_true",
                    help="required: this keys the transmitter (use a dummy load)")
    ap.add_argument("--seconds", type=float, default=2.0, help="how long to hold TX (default 2)")
    args = ap.parse_args(argv)
    if not args.i_will_transmit:
        print("refusing: pass --i-will-transmit (this keys the radio)", file=sys.stderr)
        return 2

    cfg = _uvk5_config()
    print(f"uvk5 {cfg['serial_port']} @ {cfg['frequency']} Hz")
    try:
        radio = _build_backend(cfg)
    except Exception as exc:
        print(f"[FAIL] could not open the UV-K5 backend: {exc}", file=sys.stderr)
        print("       (is radio-server.service still running and holding the port?)", file=sys.stderr)
        return 1

    try:
        idle = snapshot(radio)
        show("IDLE (before keying)", idle)
        radio.ptt(True)
        try:
            time.sleep(0.4)  # let the writes settle before reading back
            keyed = snapshot(radio)
        finally:
            radio.ptt(False)
        show("KEYED", keyed)
        time.sleep(0.4)
        after = snapshot(radio)
        show("AFTER UN-KEY", after)

        print("\n  verdict")
        tx_on = keyed[0x30] == 0xC1FE
        pa_on = bool(keyed[0x33] & PA_ENABLE)
        print(f"    modulator keyed (0x30)        {'YES' if tx_on else 'NO'}")
        print(f"    PA rail up while keyed (0x20) {'YES' if pa_on else 'NO'}")
        print(f"    PA bias (0x36 high byte)      {keyed[0x36] >> 8}")
        print(f"    PA rail dropped after un-key  {'YES' if not (after[0x33] & PA_ENABLE) else 'NO'}")
        if tx_on and not pa_on:
            print("    => modulator only. No PA rail: near-field carrier, no radiated power.")
        elif tx_on and pa_on:
            print("    => full TX chain up.")
        return 0
    finally:
        try:
            radio.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
