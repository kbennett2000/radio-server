"""Does a dock tune survive 0x0871, or does the radio snap back to its own VFO?

The "tune with the dock, then key with the AIOC" idea rests on one assumption: that the frequency
the host wrote to REG_38/39 is still there after the dock hands control back. Reading the fork's
source says it is not -- `Dock_EnterFullControl` ends with `RADIO_SetupRegisters(true)`
(App/app/uart.c:774), which does `BK4819_SetFrequency(gRxVfo->pRX->Frequency)` (App/radio.c:807-809),
i.e. it retunes from the radio's OWN VFO on the way out.

This project has been burned by source-derived conclusions before -- ADR 0135 killed three of them,
and the fork's own ADR guessed "would only mis-scale power" and was wrong on the bench. So measure it.

## Two paths were tried before this one; both failed for reasons worth recording

1. `POST /radio/select {"backend":"uvk5"}` -- **segfaulted the server** (status 139) 49 s into the
   rebuild on 2026-07-26. `close()` never ran, so `0x0871` was never sent.
2. Stop the service, then key a bare DTR carrier with `aioc_ptt_gate0.key_via_line`. The control
   keyed once (2.71 s carrier) and then never again in that window, while keying through the
   running service worked immediately before and after, every time. Whatever the stopped-service
   window costs, the measurement must not depend on it.

## So: measure with the path that is known reliable

Keying goes through `POST /services/01` against the **running** service in baofeng mode -- the path
measured at 1.20-1.21 s of carrier on four separate occasions. The service is stopped only long
enough to borrow the serial port for the dock tune, and the dock session is owned by this process
with `close()` in a `finally`, so `0x0871` is guaranteed.

Restarting the service between the tune and the reading is safe *because* the baofeng backend has
no CAT: it cannot set a frequency, so it cannot disturb what the dock left behind. That is the one
property that makes this experiment possible at all.

  A. service up, witness on 445.800, key -> POSITIVE CONTROL (radio transmits, panel unmoved).
  B. stop service; dock in; tune 446.000; hold; close() -> 0x0871; restart service.
  C. key, alternating the witness between 446.000 and 445.800 until one answers.

Exactly one of the two should speak. Both silent => INCONCLUSIVE, never a guess (the ADR
0136/0137/0138 error class).

`busy` is the metric, not audio RMS: `busy` is the SA818's SQ pin, a hardware carrier detect.
Every over is the station ID, so all of this is Part 97-identified by construction.

ADR 0127: stopping the service around a single-open test is the sanctioned pattern; what is
forbidden is *leaving* it stopped. Restart is in the `finally` too.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from acceptance import BENCH_TX_HZ, KV4P_BASE, RADIO_BASE, api  # noqa: E402
from aioc_ptt_gate0 import AIOC_PORT, SERVICE, user_systemctl, wait_until_settled  # noqa: E402
from repeater_openup import BusyWatch, busy_seconds, tune_witness  # noqa: E402

from radio_server.backends.uvk5.radio import Uvk5Radio  # noqa: E402

PANEL_HZ = 445_800_000      # where ADR 0139 found the front panel
DOCK_HZ = 446_000_000       # where we will command the dock to tune
DOCK_HOLD_S = 40.0          # ADR 0138: a session torn down at ~8 s wedged; ~40 s keyed first try
SERVICE_UP_S = 20.0         # let the service bind :8090 and open the AIOC before asking it to key
RECOVERY_BUDGET_S = 540.0   # post-dock deafness measured in MINUTES (2026-07-26), not seconds
LOUD_S = 0.30

for _hz in (PANEL_HZ, DOCK_HZ):
    if _hz not in BENCH_TX_HZ:
        raise SystemExit(f"refusing: {_hz} Hz is not a bench frequency {sorted(BENCH_TX_HZ)}")


def wait_for_api(timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if api(RADIO_BASE, "GET", "/radio/backends", timeout=5.0)[0] == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2.0)
    return False


def key_and_measure(watch: BusyWatch, hz: int, label: str) -> float:
    """Point the witness at `hz`, key one station-ID over, return carrier-seconds heard."""
    tune_witness(hz)
    time.sleep(2.0)
    start = time.monotonic()
    api(RADIO_BASE, "POST", "/services/01", timeout=90.0)
    end = time.monotonic()
    time.sleep(0.5)
    carrier = busy_seconds(watch.samples, start, end)
    verdict = "CARRIER" if carrier >= LOUD_S else "silent "
    print(f"  {label:<28} {hz/1e6:9.4f}  keyed {end-start:.2f}s  carrier {carrier:5.2f}s  {verdict}")
    return carrier


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--i-will-transmit", action="store_true", help="required: this keys the radio")
    args = ap.parse_args(argv)
    if not args.i_will_transmit:
        print("refusing: pass --i-will-transmit (this keys the radio)", file=sys.stderr)
        return 2

    active = api(RADIO_BASE, "GET", "/radio/backends")[1].get("active")
    if active != "baofeng":
        print(f"refusing: active backend is {active!r}, needs to be 'baofeng' "
              f"(this experiment relies on it having no CAT)", file=sys.stderr)
        return 2

    watch = BusyWatch(KV4P_BASE)
    watch.start()
    time.sleep(0.5)
    if not watch.samples:
        print("refusing: the kv4p witness on :8091 is not answering", file=sys.stderr)
        watch.stop()
        return 2

    radio: Uvk5Radio | None = None
    stopped = False
    try:
        print("A -- positive control: does it transmit, and is the panel still on 445.800?")
        if key_and_measure(watch, PANEL_HZ, "baseline, panel frequency") < LOUD_S:
            print("\n  INCONCLUSIVE: no carrier before we changed anything. Nothing below would")
            print("  mean anything, so the run stops here rather than guessing.")
            return 2

        print(f"\nB -- borrow the port: dock in, tune {DOCK_HZ/1e6:.4f}, hold, exit cleanly")
        wait_until_settled(SERVICE)
        rc, out = user_systemctl("stop", SERVICE)
        if rc != 0:
            print(f"  INCONCLUSIVE: could not stop {SERVICE}: {out}", file=sys.stderr)
            return 2
        stopped = True
        time.sleep(8.0)

        radio = Uvk5Radio(serial_port=AIOC_PORT, frequency=DOCK_HZ)
        got = radio.status().frequency
        print(f"  dock reports frequency {got!r}")
        if got != DOCK_HZ:
            print("  INCONCLUSIVE: the dock did not take the tune; nothing downstream is valid.")
            return 2
        time.sleep(DOCK_HOLD_S)
        radio.close()          # ExitHwMode / 0x0871
        radio = None
        print("  0x0871 sent, transport closed")

        rc, out = user_systemctl("start", SERVICE)
        stopped = False
        print(f"  restarted {SERVICE}: rc={rc} {out}".rstrip())
        time.sleep(SERVICE_UP_S)
        if not wait_for_api():
            print("  INCONCLUSIVE: the service did not come back on :8090.")
            return 2

        # Alternate the two candidates until one answers. Immune to however long the radio takes
        # to start responding again after a dock session -- whichever speaks first IS the result.
        print("\nC -- where does the carrier come out? (alternating until the radio answers)")
        deadline = time.monotonic() + RECOVERY_BUDGET_S
        heard_dock = heard_panel = False
        rounds = 0
        while time.monotonic() < deadline:
            rounds += 1
            if key_and_measure(watch, DOCK_HZ, f"r{rounds} dock-commanded") >= LOUD_S:
                heard_dock = True
                # Confirm the other is quiet, so "heard" is a choice and not just a detection.
                heard_panel = key_and_measure(watch, PANEL_HZ, f"r{rounds} panel (confirm)") >= LOUD_S
                break
            if key_and_measure(watch, PANEL_HZ, f"r{rounds} front-panel") >= LOUD_S:
                heard_panel = True
                heard_dock = key_and_measure(watch, DOCK_HZ, f"r{rounds} dock (confirm)") >= LOUD_S
                break
            print("      both silent -- radio still recovering from the dock session")

        print()
        if heard_dock and not heard_panel:
            print("  ==> THE DOCK TUNE SURVIVED 0x0871.")
            print("      The cheap hybrid is alive and no firmware change is needed.")
        elif heard_panel and not heard_dock:
            print("  ==> THE DOCK TUNE DID NOT SURVIVE 0x0871.")
            print("      The radio is back on its own VFO, exactly as RADIO_SetupRegisters(true)")
            print("      reads. Tuning has to go through the firmware's VFO, which needs the opcode.")
        elif heard_dock and heard_panel:
            print("  ==> INCONCLUSIVE: carrier on BOTH -- the witness is not selective enough at")
            print("      this range (desense / front-end overload from a transmitter inches away).")
            return 2
        else:
            print(f"  ==> INCONCLUSIVE: no carrier anywhere in {RECOVERY_BUDGET_S:.0f}s of polling,")
            print("      though the baseline was loud. The radio did not come back from the dock")
            print("      session within the budget. That is a finding about recovery, not tuning.")
            return 2
        return 0
    finally:
        if radio is not None:
            try:
                radio.close()          # never leave the radio inside the 0x0870 loop
                print("  (dock session closed on the way out)")
            except Exception as exc:   # noqa: BLE001
                print(f"  WARNING: dock close failed: {exc!r} -- radio may be wedged")
        watch.stop()
        if stopped:
            rc, out = user_systemctl("start", SERVICE)
            print(f"  restarted {SERVICE}: rc={rc} {out}".rstrip())
            time.sleep(SERVICE_UP_S)
        tune_witness(PANEL_HZ)
        print(f"  witness restored to {PANEL_HZ/1e6:.4f}")


if __name__ == "__main__":
    raise SystemExit(main())
