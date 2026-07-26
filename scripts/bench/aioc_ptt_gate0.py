"""Gate 0 (ADR 0137): does the AIOC's serial PTT line key the UV-K5 at all?

This one answer decides the architecture. radio-server currently suspends the radio's firmware and
hand-writes BK4819 registers to re-create what ``RADIO_SetTxParameters`` already does correctly --
three cycles of debugging that re-creation, against repeaters a stock radio opens with a thumb. If
the AIOC's DTR (or RTS) line keys this radio, none of that is needed for repeater work: the operator
picks the channel on the radio, ``AiocBaofeng`` keys it, and the radio's own firmware does every
register the way it does when a human presses PTT. That backend already exists and is bench-proven
on a UV-5R (ADR 0029).

**The witness makes this objective.** The synthesised verdict proposed watching the radio's TX LED;
an LED says "something happened", not "RF appeared on the right frequency at a usable level". The
kv4p runs as a separate service on :8091, so it stays up while the uvk5 service is stopped, and it
answers the actual question.

SAFETY, and this is the whole reason for the operator instruction:

* Keying over DTR transmits on **whatever the radio's front panel is set to** -- the host cannot see
  it and cannot override it. There is no read-back path for the front-panel VFO in this repo. So the
  radio MUST be on the bench frequency before this runs, and that is the one thing a human has to do.
* The witness cross-checks it: if the carrier does not appear on the bench frequency, this reports
  NO RF rather than "it worked", and a radio parked on some other frequency reads as a clean failure
  rather than as a silent out-of-band transmission that looked like success.
* ADR 0127: stopping the uvk5 service around a single-open test is the sanctioned pattern -- what is
  forbidden is *leaving* it stopped. Restart is in a ``finally``.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import deviation_probe as dp  # noqa: E402
from acceptance import BENCH_TX_HZ, KV4P_BASE, api, rms  # noqa: E402

SERVICE = "radio-server.service"
AIOC_PORT = "/dev/serial/by-id/usb-AIOC_All-In-One-Cable_da3441ac-if04"
#: Above this RMS the witness definitely heard a carrier; the silence floor in deviation_probe is
#: the same constant, kept in step deliberately.
CARRIER_RMS = dp.SILENCE_RMS


def user_systemctl(*args: str) -> tuple[int, str]:
    proc = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def key_via_line(port: str, line: str, seconds: float) -> None:
    """Assert one serial control line for ``seconds``, then drop it. Mirrors AiocBaofeng."""
    import serial

    with serial.Serial(port, 9600, timeout=1) as ser:
        # Both low first: an inherited-high line would mean the "off" capture is really a keyed one.
        ser.dtr = False
        ser.rts = False
        time.sleep(0.2)
        try:
            setattr(ser, line, True)
            time.sleep(seconds)
        finally:
            ser.dtr = False
            ser.rts = False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--i-will-transmit", action="store_true", required=False,
                    help="required: this keys the radio")
    ap.add_argument("--frequency", type=int, default=445_800_000,
                    help="where the operator has parked the radio (must be a bench frequency)")
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--port", default=AIOC_PORT)
    ap.add_argument("--lines", default="dtr,rts",
                    help="control lines to try in order (default: dtr,rts)")
    args = ap.parse_args(argv)

    if not args.i_will_transmit:
        print("refusing: pass --i-will-transmit (this keys the radio)", file=sys.stderr)
        return 2
    if args.frequency not in BENCH_TX_HZ:
        print(f"refusing: {args.frequency} Hz is not a bench frequency {sorted(BENCH_TX_HZ)}",
              file=sys.stderr)
        return 2

    print(f"\n  witness -> {args.frequency} Hz")
    code, body = api(KV4P_BASE, "POST", "/frequency", body={"hz": args.frequency})
    if code != 200:
        print(f"  could not tune the witness ({code} {body!r:.80}) — is the kv4p service up?",
              file=sys.stderr)
        return 2
    time.sleep(2.0)

    print(f"  baseline (nothing keyed)")
    baseline = rms(dp.listen(3.0))
    print(f"    RMS {baseline:.1f}")
    if baseline >= CARRIER_RMS:
        print("\n  the witness already hears a carrier with nothing keyed. Something else is\n"
              "  transmitting on this frequency; every result below would be that, not us.",
              file=sys.stderr)
        return 2

    rc, out = user_systemctl("stop", SERVICE)
    print(f"\n  stopped {SERVICE} (rc={rc}) {out}")
    if rc != 0:
        print("  could not stop the service; not keying blind", file=sys.stderr)
        return 2

    results: dict[str, float] = {}
    try:
        time.sleep(1.5)  # let the port actually close
        for line in [x.strip() for x in args.lines.split(",") if x.strip()]:
            print(f"\n  asserting {line.upper()} for {args.seconds:.0f}s...")
            import threading
            captured: list[bytes] = []
            listener = threading.Thread(
                target=lambda: captured.append(dp.listen(args.seconds + 2.0)))
            listener.start()
            time.sleep(0.5)  # the witness must be listening before the line goes high
            try:
                key_via_line(args.port, line, args.seconds)
            except Exception as exc:  # noqa: BLE001 — a serial failure is a result, not a crash
                print(f"    could not drive {line.upper()}: {exc}")
            listener.join()
            got = rms(captured[0]) if captured else 0.0
            results[line] = got
            print(f"    witness RMS {got:.1f}   ({'CARRIER' if got >= CARRIER_RMS else 'no RF'})")
    finally:
        rc, out = user_systemctl("start", SERVICE)
        print(f"\n  restarted {SERVICE} (rc={rc}) {out}")
        # ADR 0127: never leave it stopped. Say so out loud either way.
        rc2, state = user_systemctl("is-active", SERVICE)
        print(f"  {SERVICE} is now: {state}")

    print()
    winner = max(results, key=results.get) if results else None
    if winner and results[winner] >= CARRIER_RMS:
        print(f"  => THE AIOC KEYS THIS RADIO on {winner.upper()} "
              f"(witness RMS {results[winner]:.1f} vs baseline {baseline:.1f}).")
        print("     The dock register path is OPTIONAL for repeater work: the radio can hold the")
        print("     channel and its own firmware can set up TX, exactly as it does by hand.")
        return 0
    print(f"  => NO RF from any of {list(results)}. Baseline {baseline:.1f}, "
          f"best {max(results.values(), default=0.0):.1f}.")
    print("     Either the AIOC's PTT line does not reach this radio's PTT input, or the radio")
    print("     was not on this frequency. Both are worth knowing; neither is a reason to keep")
    print("     guessing at registers.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
