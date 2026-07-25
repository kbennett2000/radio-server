"""Correlate the UV-K5 service's own TX state with the kv4p's carrier-detect during a browser PTT.

Two measurements disagreed: with a dummy load the kv4p saw the UV-K5's browser-PTT carrier; with an
antenna (after the radio was handled) it saw nothing. This splits the possibilities by watching both
ends at once:

  - 8090 transmitting=True  AND  8091 busy=True   -> keyed AND radiating (RF present)
  - 8090 transmitting=True  AND  8091 busy=False  -> service keyed the register but NO RF comes out
                                                     (the reg-0x30-vs-physical-TX gap)
  - 8090 transmitting=False                        -> the browser PTT never reached the TX path
                                                     (session/transport, not the radio)

8090 = UV-K5 service (its `transmitting` flag is the backend's keyed state). 8091 = kv4p (its `busy`
is the SA818 carrier-detect). Pure polling — nothing here keys anything.
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

_conns = {}


def sample(port):
    c = _conns.get(port)
    if c is None:
        c = _conns[port] = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=2)
    try:
        c.request("GET", "/status", headers={"Authorization": f"Bearer {TOKEN}"})
        return json.loads(c.getresponse().read())
    except Exception:
        try:
            c.close()
        except Exception:
            pass
        _conns[port] = None
        raise


def main() -> int:
    d90 = sample(8090)
    d91 = sample(8091)
    print(f"UV-K5(8090) freq {d90['frequency']}  |  kv4p(8091) freq {d91['frequency']}")
    print("watching:  UV-K5 transmitting  vs  kv4p carrier(busy)")
    t0 = time.monotonic()
    tx_last = bool(d90["transmitting"])
    car_last = bool(d91["busy"])
    tx_ever = tx_last
    car_ever = car_last
    both = 0
    keyed_norf = 0
    polls = 0
    while time.monotonic() - t0 < DURATION:
        try:
            tx = bool(sample(8090)["transmitting"])
            car = bool(sample(8091)["busy"])
        except Exception:
            continue
        polls += 1
        el = time.monotonic() - t0
        if tx != tx_last:
            print(f"  t={el:6.2f}s  UV-K5 transmitting -> {tx}")
            tx_last = tx
        if car != car_last:
            print(f"  t={el:6.2f}s  kv4p carrier       -> {car}  {'*** RF ***' if car else ''}")
            car_last = car
        tx_ever = tx_ever or tx
        car_ever = car_ever or car
        if tx and car:
            both += 1
        if tx and not car:
            keyed_norf += 1
    print(f"\n  {polls} polls in {time.monotonic() - t0:.1f}s")
    print(f"  UV-K5 ever keyed      : {tx_ever}")
    print(f"  kv4p ever saw carrier : {car_ever}")
    print(f"  polls keyed WITH RF   : {both}")
    print(f"  polls keyed WITHOUT RF: {keyed_norf}")
    if tx_ever and not car_ever:
        print("  => VERDICT: the service KEYS (reg 0x30) but NO RF radiates — physical-TX gap.")
    elif tx_ever and car_ever:
        print("  => VERDICT: the service keys AND RF radiates — TX works; HT-silence is HT-side.")
    elif not tx_ever:
        print("  => VERDICT: the browser PTT never keyed the service — session/transport, not the radio.")
    return 0


sys.exit(main())
