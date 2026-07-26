"""Gate 1 (ADR 0138): drive the UV-K5 as a plain Baofeng and hear the audio come back.

Gate 0 (`aioc_ptt_gate0.py`) proved the AIOC's DTR line keys this radio. That is necessary but not
sufficient: keying is not talking. This drives the **real `AiocBaofeng` backend** -- the same class
`server.backend = "baofeng"` would instantiate -- through a full transmit, and asks the kv4p witness
whether a recognisable audio tone came back off the air.

Why that is the acceptance and not a formality: in Baofeng mode radio-server owns only two things,
the PTT line and the sound card. If a tone goes in and the same tone comes out at the far end, then
every register question this arc has been chasing for four cycles is moot for repeater work -- the
radio's own firmware did the frequency, the deviation, the CTCSS and the PA, exactly as it does when
a human presses the button.

The measurement is deliberately not a level. It is **"is the tone I sent the tone I hear"**: a
1000 Hz stimulus recovered at 1000 Hz, checked against the 1600 Hz band as a negative control so a
burst of broadband noise cannot pass as success.

SAFETY
------
* The radio transmits on whatever its **front panel** is set to -- there is no read-back for that
  anywhere in this repo -- so the operator parks it on the bench frequency first and this refuses
  any non-bench value. The witness cross-checks: no carrier on the bench frequency reads as failure,
  never as success.
* Holds the uvk5 serial port and the AIOC sound card, both of which the live service owns, so the
  service is stopped around the run and restarted in a ``finally`` (ADR 0127).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import deviation_probe as dp  # noqa: E402
from acceptance import BENCH_TX_HZ, KV4P_BASE, api, rms  # noqa: E402
from radio_server.audio import synth_tone  # noqa: E402

SERVICE = "radio-server.service"
AIOC_PORT = "/dev/serial/by-id/usb-AIOC_All-In-One-Cable_da3441ac-if04"
AIOC_CARD = "AIOC"
STIMULUS_HZ = 1000.0
CONTROL_HZ = 1600.0
STIMULUS_SECONDS = 5.0
#: The recovered stimulus must beat the (unsent) control band by this much for "we heard the tone"
#: to mean the tone rather than noise.
TONE_OVER_CONTROL = 4.0


def user_systemctl(*args: str) -> tuple[int, str]:
    proc = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--i-will-transmit", action="store_true")
    ap.add_argument("--frequency", type=int, default=445_800_000,
                    help="where the operator has parked the radio (must be a bench frequency)")
    ap.add_argument("--ptt-line", default="dtr", help="bench-confirmed by gate 0")
    ap.add_argument("--port", default=AIOC_PORT)
    ap.add_argument("--device", default=AIOC_CARD, help="ALSA device substring for the AIOC card")
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
        print(f"  could not tune the witness ({code} {body!r:.80})", file=sys.stderr)
        return 2
    time.sleep(2.0)

    baseline = rms(dp.listen(3.0))
    print(f"  baseline (nothing keyed): RMS {baseline:.1f}")
    if baseline >= dp.SILENCE_RMS:
        print("\n  something is already transmitting here; every result below would be that.",
              file=sys.stderr)
        return 2

    rc, out = user_systemctl("stop", SERVICE)
    print(f"\n  stopped {SERVICE} (rc={rc}) {out}")
    if rc != 0:
        print("  could not stop the service; not keying blind", file=sys.stderr)
        return 2

    captured: list[bytes] = []
    listen_error: list[BaseException] = []

    def _listen(seconds: float) -> None:
        # A thread that dies takes its traceback with it, and an empty capture then looks exactly
        # like a dead transmitter -- the same confusion witness_heard_anything() exists to prevent.
        try:
            captured.append(dp.listen(seconds))
        except BaseException as exc:  # noqa: BLE001
            listen_error.append(exc)

    try:
        time.sleep(1.5)  # let the port and the sound card actually close
        from radio_server.backends.aioc_baofeng import AiocBaofeng

        print(f"  opening AiocBaofeng(port={args.port}, ptt_line={args.ptt_line}, "
              f"device={args.device!r})")
        radio = AiocBaofeng(
            serial_port=args.port,
            ptt_line=args.ptt_line,
            input_device=args.device,
            output_device=args.device,
        )
        tone = synth_tone(STIMULUS_HZ, STIMULUS_SECONDS * 1000.0, amplitude=0.5)
        print(f"  transmitting a {STIMULUS_HZ:.0f} Hz tone for {STIMULUS_SECONDS:.0f}s "
              f"through the REAL backend...")

        listener = threading.Thread(target=_listen, args=(STIMULUS_SECONDS + 4.0,))
        listener.start()
        time.sleep(0.5)  # witness must be listening before the carrier appears
        t0 = time.monotonic()
        try:
            radio.transmit(tone)
        finally:
            span = time.monotonic() - t0
            print(f"  transmit() returned after {span:.2f}s "
                  f"(expected ~{STIMULUS_SECONDS:.0f}s; a fast return means it never played)")
            radio.ptt(False)
        listener.join()

        # Positive control, same process, same open port: if a bare line assert IS heard but the
        # backend's transmit is not, the fault is the audio path, not the keying.
        if not captured or rms(captured[0]) < dp.SILENCE_RMS:
            print("\n  transmit produced no RF -- re-testing a bare PTT assert from this process")
            control_cap: list[bytes] = []
            ct = threading.Thread(target=lambda: control_cap.append(dp.listen(5.0)))
            ct.start()
            time.sleep(0.5)
            radio.ptt(True)
            time.sleep(3.0)
            radio.ptt(False)
            ct.join()
            got = rms(control_cap[0]) if control_cap else 0.0
            print(f"  bare ptt(True) -> witness RMS {got:.1f}")
        close = getattr(radio, "close", None)
        if callable(close):
            close()
    except Exception as exc:  # noqa: BLE001 — a failure here is a result to report, not a crash
        print(f"\n  backend raised: {type(exc).__name__}: {exc}")
        captured = captured or [b""]
    finally:
        rc, out = user_systemctl("start", SERVICE)
        _, state = user_systemctl("is-active", SERVICE)
        print(f"\n  restarted {SERVICE} (rc={rc}) — now: {state}")

    if listen_error:
        print(f"\n  the witness listener itself failed: "
              f"{type(listen_error[0]).__name__}: {listen_error[0]}")
        return 2
    pcm = captured[0] if captured else b""
    print(f"  captured {len(pcm)} bytes")
    if not pcm:
        print("\n  => the witness delivered nothing.")
        return 1

    best = dp.loudest_slice(pcm, STIMULUS_SECONDS - 1.0)
    heard = dp.band_rms(best, STIMULUS_HZ, width=dp.AUDIO_WIDTH_HZ)
    control = dp.band_rms(best, CONTROL_HZ, width=dp.AUDIO_WIDTH_HZ)
    print(f"\n  overall RMS        {rms(best):9.1f}")
    print(f"  recovered {STIMULUS_HZ:.0f} Hz  {heard:9.2f}")
    print(f"  control  {CONTROL_HZ:.0f} Hz  {control:9.2f}   (never transmitted)")

    if rms(best) < dp.SILENCE_RMS:
        print("\n  => NO RF. The backend ran but nothing reached the witness.")
        return 1
    if heard < control * TONE_OVER_CONTROL:
        print(f"\n  => CARRIER BUT NO TONE: {STIMULUS_HZ:.0f} Hz did not beat the control band by "
              f"{TONE_OVER_CONTROL:.0f}x.\n     RF is getting out; the audio path is not.")
        return 1

    print(f"\n  => BAOFENG MODE WORKS END TO END. The tone went out through the AIOC sound card, "
          f"\n     the radio's own firmware transmitted it, and it came back {heard / control:.0f}x "
          f"over the control band.\n     No dock, no register writes, no CAT.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
