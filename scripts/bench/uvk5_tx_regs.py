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
* ``0x38``/``0x39`` — the synthesiser, in 10 Hz units. The host owns these in dock mode; the
  firmware's ``Dock_ForceTx`` deliberately does not touch them. Read them keyed to prove the
  carrier is where the server thinks it is.
* ``0x67`` — raw RSSI (low 9 bits), the input to the CAT squelch gate. Idle, this is the noise
  floor to size ``uvk5.squelch_threshold`` against — and it is band-dependent.
* ``0x51``/``0x07`` — the CTCSS access tone (with ``--tone``). A repeater needs this and simplex
  does not, so it is the difference between "works on the bench" and "opens nothing in the field".
  Read them KEYED: source says ``Dock_ForceTx`` never touches them, but the identical assumption
  was wrong for ``0x33``/``0x36`` (ADR 0132), and source is not a measurement.
* ``0x40`` — TX **deviation**. ``BK4819_Init`` writes ``0x3516`` once (``bk4829.c:151``) and this
  firmware has no per-band or per-bandwidth deviation calibration at all, so this one constant
  governs every transmission from both the front panel and the dock.
* ``0x43`` — filter bandwidth, ``0x49A8`` wide / ``0x4808`` narrow. ``Dock_ForceTx`` **omits** the
  ``SetFilterBandwidth`` call stock ``RADIO_SetTxParameters`` makes (``radio.c:999``), so this is
  whatever radio-server's ``set_mode`` last wrote — or whatever the front panel left behind.

If ``0x20`` is **set while keyed** the PA rail is up. If it is set without radio-server writing it,
the firmware is doing it (an F5-style ``Dock_ForceTx`` is flashed and firing). If it is clear while
keyed, the PA never came up and no meaningful RF is radiating regardless of what the far end sees.

**The band questions** (``--frequency``, ADR 0132). ``Dock_ForceTx`` sets the PA up from
``gCurrentVfo`` — the radio's own boot VFO — not from the frequency the host tuned
(``uart.c:729-732``). So on a host frequency in the *other* band from the radio's VFO, expect the
keyed ``0x36`` low byte and the ``0x33`` LNA bits to name the wrong band, and expect ``0x33`` to
still name the wrong band AFTER UN-KEY — which is what leaves receive deaf until the next
``set_frequency``. That last snapshot is the one to read.

**This keys the transmitter.** Pass ``--i-will-transmit`` to arm it. Unlike ``doctor``'s typed
CONFIRM prompt, this is an explicit flag because the whole point is unattended bench acceptance —
the guard is deliberate opt-in, not the presence of a human. Dummy load, please.

The running service owns the serial port, so stop it first and start it after::

    systemctl --user stop radio-server
    .venv/bin/python scripts/bench/uvk5_tx_regs.py --i-will-transmit
    systemctl --user start radio-server

To answer the repeater question — does a keyed carrier actually carry its access tone? — arm one::

    .venv/bin/python scripts/bench/uvk5_tx_regs.py --i-will-transmit \\
        --frequency 445800000 --tone 100.0
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
    (0x40, "TX DEVIATION (BK4819_Init writes 0x3516 once; never recalibrated)"),
    (0x43, "filter bandwidth (0x49A8 wide FM / 0x4808 narrow)"),
    (0x47, "AF selector (0x6142 FM / 0x6042 mute)"),
    (0x50, "AF/TX path un-mute (firmware ExitTxMute writes 0x3B18)"),
    (0x51, "CTCSS control (0x9040 firmware / 0x904A ours / 0x0000 = OFF)"),
    (0x07, "CTCSS tone code word (CTC1); tone_hz = code / 20.6488"),
    (0x52, "TxOn_Beep housekeeping (stock writes 0x028F)"),
    (0x38, "synthesiser low word (10 Hz units)"),
    (0x39, "synthesiser high word (10 Hz units)"),
    (0x67, "raw RSSI, low 9 bits (CAT squelch input)"),
)

PA_ENABLE = 0x20
RX_ENABLE = 0x40
UHF_LNA = 0x08
VHF_LNA = 0x04
#: The pin output-ENABLE bits (``gBK4819_GpioOutState = 0x9000``, ``bk4829.c:198``). Without these
#: the low bits below name pin drivers that are switched off, so "PA_ENABLE is set" can read TRUE
#: while the PA rail is not actually driven. This probe reported exactly that — a confident
#: ``PA rail up while keyed (0x20) YES`` on a transmitter putting out almost nothing. Check the
#: enables before believing any of the pin bits.
GPIO_ENABLES = 0x9000

#: The firmware's VHF/UHF split, in 10 Hz units: 280 MHz (``bk4829.c:743``, ``bk4829.c:892``).
BAND_SPLIT_10HZ = 28_000_000

#: reg 0x51 <15> enables TxCTCSS/CDCSS and <12> selects CTCSS over CDCSS
#: (``bk4829.c:SetCTCSSFrequency``). BOTH must be set or no tone leaves the transmitter.
REG51_TX_SUBAUDIBLE = 0x8000
REG51_CTCSS_MODE = 0x1000
#: reg 0x51 <6:0> is "CTCSS/CDCSS Tx Gain1 Tuning", 0 = min, 127 = max. The firmware writes **64**;
#: radio-server writes **74**. The firmware's own inline comment claims 74 while its code writes 64,
#: which is where the host's value came from. Read it rather than believing either.
REG51_GAIN_MASK = 0x7F
#: reg 0x07 <12:0> is the CTC1 frequency control word — tone in 0.1 Hz units x 2.06488
#: (``bk4829.c:586``). Inverting it recovers the tone the chip is really generating.
REG07_CODE_MASK = 0x1FFF
CTC1_COUNTS_PER_HZ = 20.6488
#: What ``BK4819_Init`` leaves in reg 0x40 (``bk4829.c:151``). This firmware has **no** per-band and
#: **no** per-bandwidth deviation calibration, so every transmission — front panel or dock — uses
#: this one constant. If it reads back as anything else, something rewrote it and we want to know.
REG40_INIT = 0x3516
#: reg 0x43 values radio-server writes for its two modes (``_BANDWIDTH_REG43``).
BANDWIDTH_NAMES = {0x49A8: "wide FM", 0x4808: "narrow"}


def band_of(hz: int) -> str:
    return "VHF" if hz // 10 < BAND_SPLIT_10HZ else "UHF"


def tone_hz_of(reg07: int) -> float:
    """The CTCSS tone the chip is actually generating, decoded back from reg 0x07."""
    return (reg07 & REG07_CODE_MASK) / CTC1_COUNTS_PER_HZ


def read_reg(radio, reg: int, attempts: int = 4) -> int:
    """Read one register, retrying a dropped request.

    The dock link drops frames (ADR 0131) and the first read after opening the port is the most
    likely to go — this probe runs seconds after the service released the device. A dropped read is
    missing information, not a measurement; retry it rather than reporting a hole as a finding.
    """
    from radio_server.backends.uvk5.transport import Uvk5Timeout

    for attempt in range(attempts):
        try:
            return radio._read_register(reg)
        except Uvk5Timeout:
            if attempt == attempts - 1:
                raise
            time.sleep(0.1)
    raise AssertionError("unreachable")


def snapshot(radio) -> dict[int, int]:
    return {reg: read_reg(radio, reg) for reg, _ in REGISTERS}


def snap_freq_hz(snap: dict[int, int]) -> int:
    """The frequency the synthesiser is actually on, from the 0x38/0x39 read-back."""
    return ((snap[0x39] << 16) | snap[0x38]) * 10


def show(label: str, snap: dict[int, int]) -> None:
    print(f"\n  {label}")
    for reg, what in REGISTERS:
        val = snap[reg]
        flags = ""
        if reg == 0x33:
            bits = [
                "PA_ENABLE" if val & PA_ENABLE else "pa_off",
                "RX_ENABLE" if val & RX_ENABLE else "rx_off",
            ]
            if val & GPIO_ENABLES != GPIO_ENABLES:
                bits.insert(0, "!!NO-PIN-ENABLES!!")
            lna = [n for n, m in (("UHF_LNA", UHF_LNA), ("VHF_LNA", VHF_LNA)) if val & m]
            bits.append("+".join(lna) if lna else "no_lna")
            flags = f"   [{' '.join(bits)}]"
        elif reg == 0x36:
            gain = val & 0xFF
            named = {0xA2: "UHF", 0x88: "VHF"}.get(gain, "?")
            flags = f"   [bias {val >> 8}, gain 0x{gain:02X} {named}]"
        elif reg == 0x51:
            if val == 0:
                flags = "   [SUB-AUDIBLE OFF — no tone transmitted]"
            else:
                on = val & REG51_TX_SUBAUDIBLE and val & REG51_CTCSS_MODE
                flags = (f"   [{'CTCSS on' if on else '!!NOT ARMED!!'}, "
                         f"gain {val & REG51_GAIN_MASK}/127]")
        elif reg == 0x07:
            flags = f"   [{tone_hz_of(val):.1f} Hz]"
        elif reg == 0x40:
            flags = f"   [dev {val & 0x0FFF}{'' if val == REG40_INIT else '  ✘ NOT the init value'}]"
        elif reg == 0x43:
            flags = f"   [{BANDWIDTH_NAMES.get(val, '?')}]"
        elif reg == 0x67:
            flags = f"   [rssi {val & 0x1FF}]"
        print(f"    0x{reg:02X} = 0x{val:04X}{flags:<34} {what}")
    print(f"    synth  = {snap_freq_hz(snap)} Hz ({band_of(snap_freq_hz(snap))})")


def tune_loop(radio, hz: int, rounds: int) -> int:
    """Tune to ``hz`` ``rounds`` times, reading the synthesiser back each time.

    ``set_frequency`` writes 0x38/0x39 fire-and-forget. The dock link drops frames (ADR 0131), and a
    dropped *tune* is invisible: the API returns 200, ``/status`` reports the requested frequency,
    and the radio sits wherever it was — which, after a full-control exit, is the radio's OWN VFO.
    This counts how often that happens. No transmission.
    """
    misses = 0
    for i in range(rounds):
        radio.set_frequency(hz)
        time.sleep(0.15)
        try:
            got = ((read_reg(radio, 0x39) << 16) | read_reg(radio, 0x38)) * 10
        except Exception as exc:  # a dropped read is not a dropped tune — say which it was
            print(f"    {i + 1:3d}  read failed: {type(exc).__name__}")
            continue
        if got != hz:
            misses += 1
            print(f"    {i + 1:3d}  asked {hz} Hz, radio is on {got} Hz  ✘")
    print(f"\n  tune verification: {rounds - misses}/{rounds} took, {misses} silently did not")
    return 1 if misses else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--i-will-transmit", action="store_true",
                    help="required: this keys the transmitter (use a dummy load)")
    ap.add_argument("--seconds", type=float, default=2.0, help="how long to hold TX (default 2)")
    ap.add_argument("--frequency", type=int, default=None,
                    help="tune here first, in Hz (default: whatever radio.toml configures)")
    ap.add_argument("--tone", type=float, default=None, metavar="HZ",
                    help="arm this CTCSS tone before keying and read 0x51/0x07 back while keyed. "
                         "This is the only way to prove from HARDWARE — rather than from reading "
                         "firmware source — that the access tone survives the key edge.")
    ap.add_argument("--tune-loop", type=int, default=0, metavar="N",
                    help="do not key: tune N times, reading 0x38/0x39 back each time, and report "
                         "how often the tune did not take (a dropped dock write leaves the radio "
                         "on some other frequency while the API reports success)")
    args = ap.parse_args(argv)
    if not args.i_will_transmit and not args.tune_loop:
        print("refusing: pass --i-will-transmit (this keys the radio)", file=sys.stderr)
        return 2

    cfg = _uvk5_config()
    if args.frequency is not None:
        cfg["frequency"] = args.frequency
    print(f"uvk5 {cfg['serial_port']} @ {cfg['frequency']} Hz ({band_of(cfg['frequency'])})")
    try:
        radio = _build_backend(cfg)
    except Exception as exc:
        print(f"[FAIL] could not open the UV-K5 backend: {exc}", file=sys.stderr)
        print("       (is radio-server.service still running and holding the port?)", file=sys.stderr)
        return 1

    try:
        if args.tune_loop:
            return tune_loop(radio, cfg["frequency"], args.tune_loop)
        if args.tone is not None:
            # Arm BEFORE the idle snapshot, so the idle read shows what we asked for and the keyed
            # read shows what survived. Comparing the two is the whole point.
            radio.set_tone(args.tone)
            time.sleep(0.2)
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
        enabled = keyed[0x33] & GPIO_ENABLES == GPIO_ENABLES
        # The PA rail is up only if the bit is set AND the pin driver is enabled. Reporting the
        # bit alone is how this probe once certified a transmitter that was radiating almost
        # nothing (ADR 0132).
        pa_on = bool(keyed[0x33] & PA_ENABLE) and enabled
        want = band_of(cfg["frequency"])
        print(f"    modulator keyed (0x30)        {'YES' if tx_on else 'NO'}")
        print(f"    GPIO pin drivers enabled      {'YES' if enabled else 'NO  <- reg 0x33 is missing '
              f'{GPIO_ENABLES:#06x}; every pin bit below is inert'}")
        print(f"    PA rail up while keyed (0x20) {'YES' if pa_on else 'NO'}")
        print(f"    PA bias (0x36 high byte)      {keyed[0x36] >> 8}")
        print(f"    PA rail dropped after un-key  {'YES' if not (after[0x33] & PA_ENABLE) else 'NO'}")
        if tx_on and not enabled:
            print("    => the GPIO output-enable bits are gone, so the PA-rail pin is not driven "
                  "whatever its bit says: modulator only, near-field carrier, no radiated power.")
        elif tx_on and not pa_on:
            print("    => modulator only. No PA rail: near-field carrier, no radiated power.")
        elif tx_on and pa_on:
            print("    => full TX chain up.")

        # --- the band questions (ADR 0132) -------------------------------------------------
        print(f"\n  band  (tuned {cfg['frequency']} Hz = {want})")
        synth_ok = snap_freq_hz(keyed) == cfg["frequency"]
        print(f"    synth stays on the tuned freq while keyed  "
              f"{'YES' if synth_ok else 'NO — ' + str(snap_freq_hz(keyed)) + ' Hz'}")
        gain_band = {0xA2: "UHF", 0x88: "VHF"}.get(keyed[0x36] & 0xFF, "?")
        print(f"    keyed PA gain byte names                   {gain_band}"
              f"{'  ✔' if gain_band == want else f'  ✘ WRONG BAND (want {want})'}")
        for label, snap in (("keyed", keyed), ("after un-key", after)):
            lna = [n for n, m in (("UHF", UHF_LNA), ("VHF", VHF_LNA)) if snap[0x33] & m]
            named = "+".join(lna) if lna else "none"
            ok = lna == [want]
            print(f"    LNA path {label:<13}                     {named}"
                  f"{'  ✔' if ok else f'  ✘ (want {want})'}")
        print(f"    idle RSSI (0x67) on this band              {idle[0x67] & 0x1FF}")

        # --- the CTCSS question (ADR 0135) --------------------------------------------------
        # Every claim that the access tone survives `Dock_ForceTx` has so far come from READING
        # firmware source. Source says the hook never touches 0x51/0x07 — but the identical
        # ordering assumption was wrong for 0x33/0x36 (ADR 0132), so read it off the chip.
        if args.tone is not None:
            print(f"\n  CTCSS  (armed {args.tone:.1f} Hz)")
            for label, snap in (("idle, armed, not keyed", idle), ("KEYED", keyed)):
                reg51, reg07 = snap[0x51], snap[0x07]
                armed = bool(reg51 & REG51_TX_SUBAUDIBLE) and bool(reg51 & REG51_CTCSS_MODE)
                got = tone_hz_of(reg07)
                ok = armed and abs(got - args.tone) < 0.5
                print(f"    {label:<23} 0x51=0x{reg51:04X} 0x07=0x{reg07:04X}  {got:6.1f} Hz  "
                      f"gain {reg51 & REG51_GAIN_MASK:3d}/127{'  ✔' if ok else '  ✘'}")
            survived = (bool(keyed[0x51] & REG51_TX_SUBAUDIBLE)
                        and bool(keyed[0x51] & REG51_CTCSS_MODE))
            print(f"    tone still armed while keyed               "
                  f"{'YES' if survived else 'NO — reg 0x51 was cleared after we wrote it'}")
            if not survived:
                print("    => the transmitter is radiating a carrier with NO access tone. No "
                      "repeater will open on it.")

        # Deviation is fixed at init in this firmware and is the same constant for the front panel
        # and the dock, so a surprise here is itself the finding.
        print("\n  deviation / bandwidth")
        dev_note = "  (firmware init value)" if keyed[0x40] == REG40_INIT else "  ✘ CHANGED from init"
        print(f"    reg 0x40 TX deviation while keyed          {keyed[0x40] & 0x0FFF}{dev_note}")
        print(f"    reg 0x43 bandwidth while keyed             "
              f"{BANDWIDTH_NAMES.get(keyed[0x43], f'0x{keyed[0x43]:04X}  ✘ unrecognised')}")
        return 0
    finally:
        try:
            radio.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
