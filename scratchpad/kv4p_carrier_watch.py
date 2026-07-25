"""Watch the kv4p's hardware carrier-detect (COS) as a software RF sniffer for the UV-K5.

kv4p `status().busy` is the SA818 SQ pin (radio.py:562: "a real carrier detect… an open squelch
(carrier present) reads busy"). With the kv4p tuned to the UV-K5's TX frequency (both on 445.800,
inches apart on the bench), busy going True means the UV-K5 is radiating a CARRIER — modulated or
not. That is the split Kris's HT could not give: does register-keyed dock-mode TX produce RF at all?

Polls /status on 8091 as fast as it answers, logs every busy transition with elapsed time, and prints
a verdict. Pure RX on the kv4p — nothing here keys anything.
"""

import http.client
import json
import ssl
import sys
import time

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
TOKEN = "password1"

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

_conn = None


def sample():
    """One /status read over a persistent HTTPS connection (reconnect if the socket drops)."""
    global _conn
    if _conn is None:
        _conn = http.client.HTTPSConnection("127.0.0.1", 8091, context=ctx, timeout=2)
    try:
        _conn.request("GET", "/status", headers={"Authorization": f"Bearer {TOKEN}"})
        d = json.loads(_conn.getresponse().read())
    except Exception:
        try:
            _conn.close()
        except Exception:
            pass
        _conn = None
        raise
    return bool(d.get("busy")), d.get("frequency"), d.get("rssi")


def main() -> int:
    busy0, freq, _ = sample()
    print(f"kv4p on {freq} Hz — watching carrier-detect for {DURATION:.0f}s")
    print(f"  baseline busy = {busy0}")
    t0 = time.monotonic()
    last = busy0
    transitions = []
    ever_busy = busy0
    polls = 0
    while time.monotonic() - t0 < DURATION:
        try:
            busy, _, _ = sample()
        except Exception:
            continue
        polls += 1
        if busy != last:
            el = time.monotonic() - t0
            transitions.append((el, busy))
            print(f"  t={el:6.2f}s  busy -> {busy}  {'*** CARRIER ***' if busy else '(cleared)'}")
            last = busy
        ever_busy = ever_busy or busy
    el = time.monotonic() - t0
    print(f"\n  {polls} polls in {el:.1f}s")
    print(f"  carrier ever detected: {ever_busy}")
    print(f"  transitions: {len(transitions)}")
    if ever_busy:
        print("  => the UV-K5 RADIATED a carrier on this frequency at some point.")
    else:
        print("  => NO carrier detected the entire window (kv4p SQ pin never opened).")
    return 0


sys.exit(main())
