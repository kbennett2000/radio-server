"""Capture the kv4p's received audio and look for the UV-K5's 1000 Hz tone.

The carrier watch (busy/COS) proved the UV-K5 radiates when keyed. This answers the next question:
is that carrier MODULATED with the AIOC-injected tone, or is it bare? With the kv4p tuned to the
UV-K5's TX frequency (445.800), its demodulated RX audio is the modulation. A strong ~1000 Hz peak
during the key = the whole TX audio chain works; silence during a known carrier = carrier present but
no modulation (the AIOC audio never reaches the modulator).

Reports per-window RMS and the dominant frequency so a 1000 Hz tone stands out unambiguously. Pure
RX on the kv4p — nothing keys.
"""

import asyncio
import json
import os
import ssl
import sys

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
#: Credentials/endpoints come from the environment so this works on any bench and no
#: token is baked into the repo: RADIO_API_TOKEN, and RADIO_HOST (default 127.0.0.1).
URL = (f"wss://{os.environ.get('RADIO_HOST', '127.0.0.1')}:8091"
       f"/audio/rx?token={os.environ.get('RADIO_API_TOKEN', '')}")


def analyze(pcm: bytes, rate: int) -> None:
    import numpy as np

    a = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    if a.size == 0:
        print("  no audio frames arrived (squelch stayed closed — no modulation passed the gate)")
        return
    win = int(rate * 0.5)  # 0.5 s windows
    print(f"  captured {a.size} samples = {a.size / rate:.1f}s of gated audio")
    print(f"  {'window':>8}  {'RMS':>8}  {'peakHz':>8}  {'note'}")
    for i in range(0, a.size - win, win):
        w = a[i : i + win]
        rms = float(np.sqrt(np.mean(w * w)))
        if rms < 50:
            continue  # silent window, skip
        # dominant frequency via rFFT
        spec = np.abs(np.fft.rfft(w * np.hanning(w.size)))
        freqs = np.fft.rfftfreq(w.size, 1.0 / rate)
        peak = float(freqs[int(np.argmax(spec))])
        note = "<-- ~1000 Hz TONE" if 900 <= peak <= 1100 else ""
        print(f"  {i / rate:7.1f}s  {rms:8.0f}  {peak:8.0f}  {note}")


async def main() -> None:
    import websockets

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    rate = 48000
    pcm = bytearray()
    async with websockets.connect(URL, max_size=None, ssl=ctx) as ws:
        hello = await asyncio.wait_for(ws.recv(), timeout=10)
        if isinstance(hello, str):
            try:
                rate = int(json.loads(hello).get("rate", 48000))
            except Exception:
                pass
        print(f"kv4p /audio/rx open, format rate={rate} Hz — capturing {DURATION:.0f}s")
        t0 = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - t0 < DURATION:
            remaining = DURATION - (asyncio.get_event_loop().time() - t0)
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if isinstance(msg, (bytes, bytearray)):
                pcm.extend(msg)
    analyze(bytes(pcm), rate)


asyncio.run(main())
