#!/usr/bin/env python3
"""Bench self-test: prove the reflector→{browser,FM} decode path END-TO-END, no HT, no RF needed.

Injects a synthetic DSRP over — header, N AMBE voice frames at 20 ms, end frame — straight into
radio-server's gateway socket (UDP, `dstar.local_port`, default 20012; the DSRP client trusts any
source address), while subscribing to ``/audio/dstar/rx`` and counting the decoded PCM the bridge
publishes there. That hub publish happens BEFORE the content gate, so even a silence over must
appear: a dead decode pipeline reads 0. In ``real`` mode the over mimics a live reflector stream —
a re-header every 21-frame superframe (the observed cadence) and pseudo-random AMBE, which the
AMBE2000 decodes to full-scale noise (bench-measured RMS ~3300 ≫ the vad_on_rms 500 default), so
the run also opens the content gate and keys the FM crossband: watch ``/status`` flip
``transmitting: true`` during the over (this run's proof on 2026-07-20 nailed the whole chain).

Usage (on the bench host, from the deployed checkout):
    RADIO_API_TOKEN=<token> .venv/bin/python scripts/bench/dstar_decode_selftest.py [N] [clean|real]

Exit code 0 on PASS (≥80% of injected frames decoded out), 1 on FAIL. History: built during the
2026-07-20 "nothing works" hunt (ADR 0108) after three layer-level "fixed" claims that were never
end-to-end verified — this script is the end-to-end verification. Run it after EVERY deploy that
touches the dstar/vocoder path, before handing the bench back.
"""

import asyncio
import json
import os
import random
import socket
import ssl
import sys
import time

sys.path.insert(0, os.getcwd())

from radio_server.dstar import dsrp  # noqa: E402
from radio_server.dstar.header import build_voice_header  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 200  # frames (20 ms each)
MODE = sys.argv[2] if len(sys.argv) > 2 else "real"  # clean | real
TOKEN = os.environ.get("RADIO_API_TOKEN", "")
BASE = os.environ.get("RADIO_BASE", "wss://127.0.0.1:8090")
DSRP_PORT = int(os.environ.get("RADIO_DSRP_PORT", "20012"))
if not TOKEN:
    sys.exit("set RADIO_API_TOKEN (the API bearer token) in the environment")
URI = f"{BASE}/audio/dstar/rx?token={TOKEN}"


def inject() -> None:
    rnd = random.Random(1234)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dst = ("127.0.0.1", DSRP_PORT)
    hdr = build_voice_header(callsign="AE9S", module="C")
    sid = 0xBEEF
    sock.sendto(dsrp.build_header_packet(hdr, sid), dst)
    seq = 0
    for i in range(N):
        if MODE == "real" and i > 0 and i % 21 == 0:
            sock.sendto(dsrp.build_header_packet(hdr, sid), dst)  # superframe re-header
        ambe = bytes(rnd.randrange(256) for _ in range(9)) if MODE == "real" else dsrp.NULL_AMBE
        frame = dsrp.build_dv_frame(ambe, dsrp.slow_data_for_seq(seq))
        sock.sendto(dsrp.build_data_packet(frame, sid, seq), dst)
        seq = dsrp.next_seq(seq)
        time.sleep(0.02)
    end = dsrp.build_dv_frame(dsrp.NULL_AMBE, dsrp.slow_data_for_seq(seq))
    sock.sendto(dsrp.build_data_packet(end, sid, seq, end=True), dst)


async def main() -> int:
    import array

    import websockets

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    msgs = total = peak = sq = ns = 0
    async with websockets.connect(URI, ssl=ctx, max_size=None) as ws:
        ready = json.loads(await ws.recv())
        loop = asyncio.get_running_loop()
        task = loop.run_in_executor(None, inject)
        deadline = loop.time() + N * 0.02 + 10
        while loop.time() < deadline:
            try:
                m = await ws.recv()
            except Exception:
                break
            if not isinstance(m, (bytes, bytearray)):
                continue
            msgs += 1
            total += len(m)
            a = array.array("h")
            a.frombytes(bytes(m[: len(m) - (len(m) % 2)]))
            if a:
                peak = max(peak, max(abs(s) for s in a))
                sq += sum(s * s for s in a)
                ns += len(a)
            if task.done() and msgs and loop.time() > deadline - 8:
                break
        await task
    rms = (sq / ns) ** 0.5 if ns else 0.0
    ok = msgs >= N * 0.8
    print(
        f"injected={N} mode={MODE} decoded_out_frames={msgs} decoded_out_bytes={total} "
        f"rms={rms:.0f} peak={peak} format={ready.get('format')}"
    )
    print("RESULT: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
