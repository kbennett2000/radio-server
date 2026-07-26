"""Does the radio actually go where the SERVER sends it? Differential, and nobody touches anything.

The operator's objection to the previous acceptance was exactly right: *"you want me to set the
uv-k6 to a repeater? if so, WTF does that even test? I can do that without radio-server entirely."*
Keying a real machine and waiting for its tail measures the antenna, the band and the repeater's
mood at least as much as it measures this code, and when it fails it cannot tell "the tuning is
broken" from "nobody heard us".

So this measures the only thing that is ours: **apply a preset, and check the carrier moved.**

WHY THE SILENCE ROWS ARE THE MEASUREMENT
----------------------------------------
A radio stuck on one frequency passes every "is there a carrier?" row by accident. It fails the
moment you ask where the carrier is NOT. Each preset is therefore checked twice — once on the
frequency it should be transmitting on, once on a frequency it should have left — and both have to
come out right. Only a real retune passes both.

Row 4 is the repeater case in full: `Bench Split` receives on 445.800 and transmits on 446.400, so
the carrier appearing on the **transmit** leg is proof the offset was armed and applied, which is
the entire mechanism a repeater needs.

RX IS TESTED THE SAME WAY, BY REVERSING THE ROLES
--------------------------------------------------
The witness transmits and the radio under test listens. Tuned to the same frequency it must hear a
carrier; retuned away, with the witness unchanged, it must go deaf. "TX works" and "RX works" are
different claims and this makes both of them, because a preset that only moved the transmitter
would leave the operator able to talk to a repeater and never hear it.

Every frequency used is in ``BENCH_TX_HZ``. Nothing here keys a repeater input.

Part 97: bracketed by station IDs through the production identification path.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from acceptance import (  # noqa: E402
    BENCH_TX_HZ,
    KV4P_BASE,
    RADIO_BASE,
    Stage,
    _collect_rx,
    api,
    synth_tone,
    tone_power,
    transmit,
)
from repeater_openup import BusyWatch, busy_seconds, covered_seconds, tune_witness  # noqa: E402
from trials import Trial, TrialSet, require_unanimous  # noqa: E402

KEY_S = 2.5
GAP_S = 4.0
#: A carrier has to occupy most of the over to count, and its absence has to be near-total. The gap
#: between them is deliberate: a marginal reading is not allowed to satisfy either side.
HEARD_FRACTION = 0.40
SILENT_FRACTION = 0.10

#: RX rows: the witness sends a 1000 Hz tone and we measure how much of the received spectrum sits
#: on it. `acceptance.stage_rx` treats > 0.30 as "recovered"; broadband noise with no tone lands
#: near 0.005, so the two thresholds are three orders of magnitude apart and nothing sensible falls
#: between them.
TONE_MS = 3000.0
RX_WINDOW_S = 9.0
TONE_FOUND = 0.30
TONE_ABSENT = 0.05

#: (preset name, witness frequency, expect carrier). The pairs are what make it differential.
TX_ROWS: tuple[tuple[str, int, bool], ...] = (
    ("Bench Simplex 445.800", 445_800_000, True),
    ("Bench Simplex 445.800", 446_000_000, False),
    ("Bench Alt 446.000",     446_000_000, True),
    ("Bench Alt 446.000",     445_800_000, False),
    # The repeater case: receive on 445.800, transmit 600 kHz up. The carrier must appear on the
    # TRANSMIT leg, and must not appear on the receive leg.
    ("Bench Split",           446_400_000, True),
    ("Bench Split",           445_800_000, False),
)

#: (preset name, witness transmit frequency, expect the DUT to hear it).
RX_ROWS: tuple[tuple[str, int, bool], ...] = (
    ("Bench Simplex 445.800", 445_800_000, True),
    ("Bench Alt 446.000",     445_800_000, False),
)


#: (preset name, witness frequency) for the persistence row — does the channel survive the radio
#: being switched off? Deliberately the SPLIT preset: it is the case with the most to lose (offset,
#: direction and tone all have to come back), and the witness sits on the transmit leg.
PERSIST_ROW: tuple[str, int] = ("Bench Split", 446_400_000)

#: How long to leave the radio alone after an out-of-band reboot. Generous: this stands in for an
#: operator's power switch, and a boot that is merely slow must not be scored as a lost channel.
RADIO_BOOT_S = 10.0


def apply_preset(name: str) -> None:
    code, body = api(RADIO_BASE, "POST", "/presets/apply", body={"name": name}, timeout=90.0)
    if code != 200:
        raise SystemExit(f"could not apply preset {name!r} (HTTP {code} {body!r:.200})")


def station_id() -> None:
    try:
        api(RADIO_BASE, "POST", "/services/01", timeout=90.0)
    except Exception:  # noqa: BLE001
        pass


def tx_probe(watch: BusyWatch, expect_carrier: bool) -> tuple[bool, float]:
    """Key the radio under test and measure the witness. Returns (row passed, carrier seconds)."""
    start = time.monotonic()
    api(RADIO_BASE, "POST", "/ptt", body={"on": True}, timeout=30.0)
    time.sleep(KEY_S)
    api(RADIO_BASE, "POST", "/ptt", body={"on": False}, timeout=30.0)
    end = time.monotonic()
    time.sleep(0.4)

    carrier = busy_seconds(watch.samples, start, end)
    covered = covered_seconds(watch.samples, start, end)
    if covered < (end - start) * 0.5:
        # "We were not watching" must never be scored as "there was no carrier" — that conflation
        # is what made a deaf witness look like a dead transmitter for three cycles.
        raise RuntimeError(f"the witness only covered {covered:.2f}s of a {end - start:.2f}s over")
    if expect_carrier:
        return carrier >= KEY_S * HEARD_FRACTION, carrier
    return carrier <= KEY_S * SILENT_FRACTION, carrier


def rx_probe(dut_should_hear: bool) -> tuple[bool, float]:
    """Witness transmits a tone; measure whether the radio under test actually recovered it.

    Deliberately NOT ``/status.busy``: `AiocBaofeng.status` hardwires ``busy=False`` because the
    UV-5R has no carrier-detect line (ADR 0015), so polling it would return "deaf" for a perfectly
    working receiver and score the tuned and detuned cases identically. The first cut of this
    script did exactly that and "passed" the detuned row for the wrong reason.

    The discriminator is `tone_power` at 1000 Hz — the fraction of spectral energy at the tone the
    witness is sending. Broadband noise sits near 0.005, so unlike a level threshold this cannot be
    satisfied by a hissy squelch tail or an AGC hunting on an empty channel.
    """
    tone = synth_tone(1000.0, TONE_MS, amplitude=0.6).samples
    stage = Stage("rx")

    async def run():
        started = asyncio.Event()
        collector = asyncio.create_task(_collect_rx(RADIO_BASE, RX_WINDOW_S, started))
        await started.wait()
        await asyncio.sleep(0.5)
        await asyncio.to_thread(transmit, KV4P_BASE, tone, stage, "tune-rx")
        return await collector

    capture = asyncio.run(run())
    if not stage.ok:
        raise RuntimeError(f"the witness could not transmit: {'; '.join(stage.notes)}")
    power = tone_power(capture.pcm, 1000.0)
    return (power >= TONE_FOUND) if dut_should_hear else (power <= TONE_ABSENT), power


def run_tx(watch: BusyWatch, n: int) -> list[TrialSet]:
    sets: list[TrialSet] = []
    for name, witness_hz, expect in TX_ROWS:
        if witness_hz not in BENCH_TX_HZ:
            raise SystemExit(f"refusing: {witness_hz} Hz is not a bench frequency")
        apply_preset(name)
        tune_witness(witness_hz)
        time.sleep(2.0)
        label = f"TX  {name:<22} @ {witness_hz / 1e6:7.3f}  expect {'CARRIER' if expect else 'SILENCE'}"
        trials: list[Trial] = []
        for i in range(1, n + 1):
            ok, value = tx_probe(watch, expect)
            trials.append(Trial(index=i, ok=ok, value=value))
            print(f"    {label}  #{i:2d}  {value:5.2f}s  {'OK' if ok else 'FAIL'}", flush=True)
            time.sleep(GAP_S)
        sets.append(TrialSet(name=label, trials=tuple(trials), gap_seconds=GAP_S))
    return sets


def run_rx(n: int) -> list[TrialSet]:
    sets: list[TrialSet] = []
    for name, witness_hz, expect in RX_ROWS:
        if witness_hz not in BENCH_TX_HZ:
            raise SystemExit(f"refusing: {witness_hz} Hz is not a bench frequency")
        apply_preset(name)
        tune_witness(witness_hz)
        time.sleep(2.0)
        label = f"RX  {name:<22} <- {witness_hz / 1e6:7.3f}  expect {'HEARD' if expect else 'DEAF'}"
        trials: list[Trial] = []
        for i in range(1, n + 1):
            ok, value = rx_probe(expect)
            trials.append(Trial(index=i, ok=ok, value=value))
            print(f"    {label}  #{i:2d}  tone {value:5.3f}  {'OK' if ok else 'FAIL'}", flush=True)
            time.sleep(GAP_S)
        sets.append(TrialSet(name=label, trials=tuple(trials), gap_seconds=GAP_S))
    return sets


def run_persistence(watch: BusyWatch, n: int) -> list[TrialSet]:
    """Does the channel the server chose survive the radio being switched off?

    Apply a preset, reboot the radio out-of-band, and then — **without re-tuning** — key it and ask
    the witness where the carrier came out. That last clause is the whole design: re-tuning first
    would test the tuner again, which already passed, and reading the channel back out of EEPROM
    would prove only that the bytes we wrote are the bytes we wrote. The question is whether the
    radio boots onto that channel and radiates there.

    This row is what separates the three tuners. `hybrid` and `eeprom` put the channel in storage
    and must pass it. `setvfo` writes RAM and must **fail** it — and if it does not, the row is
    measuring nothing and should be believed about nothing.
    """
    name, witness_hz = PERSIST_ROW
    if witness_hz not in BENCH_TX_HZ:
        raise SystemExit(f"refusing: {witness_hz} Hz is not a bench frequency")

    tune_witness(witness_hz)
    label = f"KEEP {name:<22} @ {witness_hz / 1e6:7.3f}  after a reboot, NOT re-tuned"
    trials: list[Trial] = []
    for i in range(1, n + 1):
        apply_preset(name)
        code, body = api(RADIO_BASE, "POST", "/diagnostics/reboot-radio", body={}, timeout=30.0)
        if code != 200:
            raise SystemExit(f"could not reboot the radio (HTTP {code} {body!r:.200})")
        time.sleep(RADIO_BOOT_S)

        ok, value = tx_probe(watch, expect_carrier=True)
        trials.append(Trial(index=i, ok=ok, value=value))
        print(f"    {label}  #{i:2d}  {value:5.2f}s  {'OK' if ok else 'FAIL'}", flush=True)
        time.sleep(GAP_S)
    return [TrialSet(name=label, trials=tuple(trials), gap_seconds=GAP_S)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--i-will-transmit", action="store_true", help="required: this keys the radio")
    ap.add_argument("-n", type=int, default=10, help="trials per row")
    ap.add_argument("--skip-rx", action="store_true", help="TX rows only")
    ap.add_argument("--persist", action="store_true",
                    help="also run the persistence row (reboots the radio; slow)")
    ap.add_argument("--only-persist", action="store_true",
                    help="ONLY the persistence row — used to prove it fails under setvfo")
    args = ap.parse_args(argv)
    if not args.i_will_transmit:
        print("refusing: pass --i-will-transmit (this keys the radio)", file=sys.stderr)
        return 2

    # `/capabilities` answers a bare JSON list of strings. Distinguish "the server would not tell
    # us" from "it told us the backend cannot tune" — collapsing the two reported a forgotten
    # RADIO_API_TOKEN as a misconfigured backend, and sent the reader off to edit radio.toml over
    # an auth failure. Exactly the fault class ADR 0143 exists to correct, one layer up.
    code, state = api(RADIO_BASE, "GET", "/capabilities", timeout=15.0)
    if code != 200 or not isinstance(state, list):
        print(f"INCONCLUSIVE: /capabilities answered {code}, not a capability list.",
              file=sys.stderr)
        print("          401 means RADIO_API_TOKEN is unset or wrong in this shell; a connection",
              file=sys.stderr)
        print("          error means the service is not up. Neither is a fact about the radio.",
              file=sys.stderr)
        return 2
    if "set_frequency" not in set(state):
        print("refusing: the active backend cannot tune (no set_frequency capability).",
              file=sys.stderr)
        print("          Set baofeng.uvk5_tuner to 'setvfo' or 'eeprom' and restart, or this",
              file=sys.stderr)
        print("          measures a radio nothing is steering.", file=sys.stderr)
        return 2

    watch = BusyWatch(KV4P_BASE)
    watch.start()
    time.sleep(0.5)
    if not watch.samples:
        print("refusing: the kv4p witness on :8091 is not answering", file=sys.stderr)
        watch.stop()
        return 2

    sets: list[TrialSet] = []
    try:
        station_id()
        time.sleep(1.0)
        if not args.only_persist:
            sets += run_tx(watch, args.n)
            if not args.skip_rx:
                sets += run_rx(args.n)
        if args.persist or args.only_persist:
            sets += run_persistence(watch, args.n)
    finally:
        try:
            api(RADIO_BASE, "POST", "/ptt", body={"on": False}, timeout=30.0)
            api(KV4P_BASE, "POST", "/ptt", body={"on": False}, timeout=30.0)
        except Exception:  # noqa: BLE001
            pass
        watch.stop()
        station_id()

    print()
    for ts in sets:
        print(ts.report())
    print()
    rc = require_unanimous(*sets)
    if rc == 0:
        print("  ==> THE SERVER PICKS THE CHANNEL. Every preset put the carrier where it belongs")
        print("      and nowhere else, transmit and receive, with nobody touching the radio.")
    elif rc == 1:
        carriers = [s for s in sets if "CARRIER" in s.name or "HEARD" in s.name]
        if all(s.unanimous for s in carriers):
            print("  ==> THE RADIO IS NOT MOVING. Carrier rows pass and silence rows fail, which is")
            print("      what a radio stuck on one frequency looks like — the tune is not taking.")
        else:
            print("  ==> A preset did not land. Read the rows above: which frequency, which"
                  " direction.")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
