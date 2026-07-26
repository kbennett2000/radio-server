"""Can the AIOC carry dock frames while its sound card is streaming? Measure it before designing on it.

The whole tuning feature rests on one unverified assumption: that `AiocBaofeng` can speak the UV-K5's
UART **on the port it already owns**, without stopping anything. Every EEPROM round-trip so far
(ADR 0141) ran with the service stopped and the sound card closed, so it proves the wire works — not
that it works while the station is running.

That matters because the AIOC is one USB composite device: the CDC serial and the audio interface
share a cable, a controller, and (at the radio's K1 jack) the same physical contacts. "It answered
when nothing else was going on" is exactly the kind of evidence that has been wrong here before.

So reproduce the real condition — ONE process holding the serial handle and the capture stream at the
same time — and compare against the quiet baseline:

    A. serial only                     (what ADR 0141 actually proved)
    B. serial + capture running        (what the backend will do)
    C. serial + capture + playback     (what the backend does mid-transmission)

Each round-trip is a read-only `EepromRead` of the settings block: it has a reply, so a lost frame is
visible, and it writes nothing. Interleaved rather than block-ordered, for the reason ADR 0140 had to
learn twice — a run whose conditions drift ranks the arms by when they ran.

Exit 0 = every arm answered every time. Exit 1 = a measured failure (streaming breaks the UART, so
the tune has to pause capture). Exit 2 = inconclusive; nothing was proven either way.

ADR 0127: the service is stopped to borrow the port, and restarted in a `finally`.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from aioc_ptt_gate0 import AIOC_PORT, SERVICE, user_systemctl, wait_until_settled  # noqa: E402

from radio_server.backends.soundcard import (  # noqa: E402
    load_sounddevice,
    open_capture_stream,
    open_playout_stream,
)
from radio_server.backends.uvk5 import frames as f  # noqa: E402
from radio_server.backends.uvk5.transport import Uvk5Transport  # noqa: E402

SESSION_TS = 0x12345678
BLOCK = f.EEPROM_SETTINGS_BLOCK
BLOCK_LEN = 16
AIOC_CARD = "AIOC_K6"
BLOCKSIZE = 960


def round_trip(tp: Uvk5Transport) -> bytes | None:
    """One read-only EEPROM read. Returns the bytes, or None if the radio did not answer."""
    reply = tp.request(
        f.EepromRead(offset=BLOCK, size=BLOCK_LEN, timestamp=SESSION_TS),
        match=lambda m: isinstance(m, f.EepromReadReply) and m.offset == BLOCK,
        timeout=3.0,
    )
    return bytes(reply.data) if reply is not None else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-n", type=int, default=6, help="round-trips per arm")
    ap.add_argument("--port", default=AIOC_PORT)
    args = ap.parse_args(argv)

    stopped = False
    tp: Uvk5Transport | None = None
    capture = None
    playback = None
    results: dict[str, list[bool]] = {"serial only": [], "+capture": [], "+capture+playback": []}
    baseline: bytes | None = None

    try:
        wait_until_settled(SERVICE)
        rc, out = user_systemctl("stop", SERVICE)
        if rc != 0:
            print(f"refusing: could not stop {SERVICE}: {out}", file=sys.stderr)
            return 2
        stopped = True
        time.sleep(6.0)

        sd = load_sounddevice(None, extra_hint="hardware")
        tp = Uvk5Transport(serial_port=args.port)
        tp.connect()
        tp.send(f.Hello(timestamp=SESSION_TS))
        time.sleep(0.4)

        baseline = round_trip(tp)
        if baseline is None:
            print("refusing: the radio does not answer even with nothing else running.",
                  file=sys.stderr)
            print("          Nothing here can distinguish 'streaming breaks it' from "
                  "'it was never up'.", file=sys.stderr)
            return 2
        print(f"radio answers. settings block: {' '.join(f'{b:02X}' for b in baseline)}\n")

        for i in range(1, args.n + 1):
            # A — serial only
            results["serial only"].append(round_trip(tp) == baseline)

            # B — capture running, exactly as the backend opens it
            capture = open_capture_stream(sd, device=AIOC_CARD, blocksize=BLOCKSIZE)
            time.sleep(0.3)
            results["+capture"].append(round_trip(tp) == baseline)

            # C — and playback on top, which is what a transmission looks like. Opened through
            # the same helper the backend uses: `AIOC_K6` is an ALSA *card* name, which
            # `resolve_device` maps via sysfs — sounddevice's own name matching never finds it.
            playback = open_playout_stream(sd, device=AIOC_CARD, blocksize=BLOCKSIZE)
            time.sleep(0.3)
            results["+capture+playback"].append(round_trip(tp) == baseline)

            for stream in (playback, capture):
                try:
                    stream.stop(); stream.close()
                except Exception:  # noqa: BLE001
                    pass
            playback = capture = None
            time.sleep(0.4)

            row = "  ".join(f"{k}:{'OK ' if v[-1] else 'LOST'}" for k, v in results.items())
            print(f"  round {i:2d}   {row}", flush=True)

    finally:
        for stream in (playback, capture):
            if stream is not None:
                try:
                    stream.stop(); stream.close()
                except Exception:  # noqa: BLE001
                    pass
        if tp is not None:
            try:
                tp.close()
            except Exception:  # noqa: BLE001
                pass
        if stopped:
            rc, out = user_systemctl("start", SERVICE)
            print(f"\n  restarted {SERVICE}: rc={rc}")
            time.sleep(18.0)

    print()
    worst = 1.0
    for name, hits in results.items():
        passed, total = sum(hits), len(hits)
        worst = min(worst, passed / total if total else 0.0)
        print(f"  {name:<20} {passed}/{total}")

    print()
    if not results["serial only"] or worst == 1.0:
        print("  ==> The AIOC carries dock frames WHILE its sound card streams, in both directions.")
        print("      The tuner can run in-process on the port the backend already owns; nothing")
        print("      has to be stopped to change channel.")
        return 0
    if all(hits and all(hits) for name, hits in results.items() if name == "serial only"):
        print("  ==> STREAMING BREAKS THE UART. The serial-only arm is clean and a streaming arm is")
        print("      not, so the tune must pause the audio streams around the frame exchange.")
        return 1
    print("  ==> INCONCLUSIVE: even the serial-only arm dropped frames, so this measures the link's")
    print("      general reliability, not the effect of streaming. Fix that first.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
