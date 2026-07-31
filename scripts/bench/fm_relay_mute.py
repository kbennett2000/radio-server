#!/usr/bin/env python3
"""Does broadcast FM reach the bridges, and does the ADR 0162 mute stop it? Measure both.

ADR 0161 measured a station in broadcast FM relaying a commercial station to ``/audio/rx`` at
**3841.2 RMS / 768 000 bytes** and left the consequence as finding 3: the same `AudioHub` feeds the
Mumble bridge and the D-STAR bridge, so that broadcast goes onto a link whose far end may be
somebody else's RF repeater. This is the instrument for the fix.

**What is real here and what is not, stated rather than implied.** The radio, the AIOC sound card,
`AudioHub`, `RxPump`, `MumbleBridge._rx_to_mumble` and `DStarBridge._rf_to_reflector` are the
production objects, running the production code. The *sinks* are mocks — `MockMumbleClient`, the
gateway mock, and a stub vocoder — because the thing under test is what each relay loop passes
onward, and because pointing this at a live reflector is the exact hazard it exists to prevent.

**Three legs, one variable.** The BK1080 plays a real broadcast station throughout; only the
`broadcast_fm` block the bridges are given changes:

=========================  ==================================================================
block                      expectation
=========================  ==================================================================
``None``                   every leg relays — "nobody asked" must never silence a link, the
                           `tx_ok` rule. This is also the ADR 0161 reproduction.
``{on: True}``             browser still hears it; **both bridges 0.0** — the mute
``{on: False, ...}``       every leg relays again — a measured "the station can hear itself"
=========================  ==================================================================

**The `{on: True}` leg uses a stub tuner, and that is a real limitation, not a convenience.** On F8
and F9 that block is unreachable: `Dock_FmOff()` clears `gFmRadioMode` unconditionally as its first
statement, so a radio asked to leave broadcast FM always leaves. The stub stands in for a state the
firmware cannot produce — see ADR 0162, which ships the mute knowing it cannot fire on this radio
today and says why.

The boot assert is dodged the way `server-notes.md` records: a tuner that does not advertise
`SET_MODULATION` is skipped by `_assert_boot_broadcast_fm`, so the receiver keeps playing.

Stop the service first — the serial port and the sound card are both single-open.

Usage (on the bench box)::

    systemctl --user stop radio-server
    .venv/bin/python scripts/bench/fm_relay_mute.py --fm-hz 104300000
    systemctl --user start radio-server
"""

from __future__ import annotations

import argparse
import array
import asyncio
import math
import struct
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from radio_server.arbiter import RadioArbiter  # noqa: E402
from radio_server.audio import AudioFrame  # noqa: E402
from radio_server.backends import create_radio  # noqa: E402
from radio_server.backends.base import BroadcastFm, Capability  # noqa: E402
from radio_server.backends.uvk5.frames import DockCommand, build_frame  # noqa: E402
from radio_server.backends.uvk5.transport import _default_serial_factory  # noqa: E402
from radio_server.backends.uvk5.tuner import TUNING_CAPS  # noqa: E402
from radio_server.dstar.bridge import DStarBridge  # noqa: E402
from radio_server.dstar import dsrp  # noqa: E402
from radio_server.dstar.client import MockGatewayClient  # noqa: E402
from radio_server.link import MockMumbleClient, MumbleBridge  # noqa: E402
from radio_server.rx import AudioHub, RxPump  # noqa: E402
from radio_server.tx import TxSlot  # noqa: E402
from radio_server.vocoder.base import AMBE_BYTES_PER_FRAME, PCM_FORMAT  # noqa: E402

DEFAULT_PORT = "/dev/serial/by-id/usb-AIOC_All-In-One-Cable_da3441ac-if04"
ACTION_OFF, ACTION_ON = 0, 1


def _rms(chunks: list[bytes]) -> float:
    total = b"".join(chunks)
    if not total:
        return 0.0
    samples = array.array("h")
    samples.frombytes(total[: len(total) // 2 * 2])
    if not samples:
        return 0.0
    return math.sqrt(sum(float(s) * s for s in samples) / len(samples))


def _set_fm(port: str, action: int, hz: int) -> None:
    """`0x0879` straight down the wire. Through the transport's own factory — a plain
    ``serial.Serial`` open asserts DTR, which on the AIOC is the PTT line."""
    ser = _default_serial_factory(port, 38400)
    try:
        ser.reset_input_buffer()
        ser.write(build_frame(DockCommand.SET_BROADCAST_FM, struct.pack("<BIB", action, hz, 0)))
        ser.flush()
        deadline = time.monotonic() + 3.5
        while time.monotonic() < deadline:
            if not ser.read(256):
                break
    finally:
        ser.close()


class StubTuner:
    """Holds one `BroadcastFm` block and speaks to no radio at all.

    It advertises `TUNING_CAPS` only — **no `SET_MODULATION`** — which is what makes
    `_assert_boot_broadcast_fm` skip it, so constructing the backend does not switch off the very
    receiver this run is measuring. It also has no `CLEAR_BROADCAST_FM`, so nothing here can key or
    repair anything: this run never transmits.
    """

    volatile = False
    tx_ready_at = None
    modulation = None
    tx_ok = None

    def __init__(self) -> None:
        self.broadcast_fm: BroadcastFm | None = None

    def capabilities(self) -> frozenset[Capability]:
        return TUNING_CAPS

    def apply(self, image) -> None:  # pragma: no cover - never called here
        raise NotImplementedError

    def reassert(self, image) -> None:  # pragma: no cover - never called here
        raise NotImplementedError


class StubVocoder:
    """Encodes to constant AMBE. The reflector sink only needs to count frames."""

    def encode(self, frame: AudioFrame) -> bytes:
        return b"\x01" * AMBE_BYTES_PER_FRAME

    def decode(self, ambe: bytes) -> AudioFrame:
        return AudioFrame(b"\x00\x00" * 160, PCM_FORMAT)

    def close(self) -> None:
        pass


async def _leg(radio, tuner: StubTuner, block: BroadcastFm | None, seconds: float) -> dict:
    """One measurement: real pump, real hub, real relay loops, this block."""
    tuner.broadcast_fm = block
    hub = AudioHub()
    arbiter, slot = RadioArbiter(), TxSlot()
    pump = RxPump(radio, hub)

    def read_block():
        return radio.status().broadcast_fm

    mumble = MockMumbleClient()
    gateway = MockGatewayClient()
    mbridge = MumbleBridge(mumble, radio, arbiter=arbiter, tx_slot=slot, audio_hub=hub,
                           tx_to_rf=False, broadcast_fm=read_block)
    dbridge = DStarBridge(gateway, radio, StubVocoder, arbiter=arbiter, tx_slot=slot,
                          audio_hub=hub, callsign="AE9S", module="A", tx_to_rf=False,
                          rx_to_reflector=True, tx_hang=0.5, broadcast_fm=read_block)
    browser = hub.subscribe()          # exactly what `/audio/rx` does — no policy at all

    await mbridge.start()
    await dbridge.start()
    pump.start()
    try:
        await asyncio.sleep(seconds)
    finally:
        await pump.stop()
        await asyncio.sleep(0.3)       # let the relay loops drain what the pump already published
        await mbridge.stop()
        await dbridge.stop()

    browser_frames = []
    while not browser.empty():
        browser_frames.append(browser.get_nowait())
    hub.unsubscribe(browser)

    # By `kind`, not by a `data` attribute — `DsrpMessage` carries `dv_frame`, and a
    # `getattr(m, 'data', None)` filter silently counts zero for every leg.
    dstar_audio = [m for m in gateway.sent if m.kind is dsrp.MessageKind.DATA]
    return {
        "browser_rms": _rms(browser_frames),
        "browser_bytes": sum(len(f) for f in browser_frames),
        "mumble_rms": _rms(mumble.sent_audio),
        "mumble_bytes": sum(len(f) for f in mumble.sent_audio),
        "dstar_frames": len(dstar_audio),
        "mumble_stats": mbridge.tx_stats(),
        "dstar_stats": dbridge.tx_stats(),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", default=DEFAULT_PORT)
    ap.add_argument("--fm-hz", type=int, default=104_300_000)
    ap.add_argument("--seconds", type=float, default=4.0)
    args = ap.parse_args(argv)

    print("putting the BK1080 on %.1f MHz — the station is now deaf on its own channel"
          % (args.fm_hz / 1e6))
    _set_fm(args.port, ACTION_ON, args.fm_hz)

    tuner = StubTuner()
    radio = create_radio("baofeng", tuner=tuner, serial_port=args.port)
    legs = [
        ("None        (nobody asked)", None),
        ("{on: True}  (measured deaf)", BroadcastFm(on=True, hz=args.fm_hz, blocks_tx=True)),
        ("{on: False} (measured hearing)", BroadcastFm(on=False, hz=args.fm_hz, blocks_tx=False)),
    ]
    results = []
    try:
        for label, block in legs:
            print("\nleg: %s" % label)
            r = asyncio.run(_leg(radio, tuner, block, args.seconds))
            results.append((label, block, r))
            print("  browser  RMS %8.1f  (%d bytes)" % (r["browser_rms"], r["browser_bytes"]))
            print("  mumble   RMS %8.1f  (%d bytes)" % (r["mumble_rms"], r["mumble_bytes"]))
            print("  d-star   %d AMBE frames to the reflector" % r["dstar_frames"])
            print("  counters mumble rx_deafened=%d deafened=%r | dstar tx_deafened=%d deafened=%r"
                  % (r["mumble_stats"]["rx_deafened"], r["mumble_stats"]["deafened"],
                     r["dstar_stats"]["tx_deafened"], r["dstar_stats"]["deafened"]))
    finally:
        radio.close()
        print("\nteardown: switching broadcast FM off")
        _set_fm(args.port, ACTION_OFF, 0)

    fails = []
    for label, block, r in results:
        deaf = block is not None and block.on
        if r["browser_bytes"] == 0:
            fails.append("%s: the browser heard NOTHING — the asymmetry is broken, or the "
                         "receiver was silent and this run measured nothing at all" % label)
        if deaf:
            if r["mumble_bytes"] or r["dstar_frames"]:
                fails.append("%s: a bridge relayed broadcast FM" % label)
            if r["mumble_stats"]["deafened"] is not True:
                fails.append("%s: the Mumble tri-state did not report deaf" % label)
        else:
            if not r["mumble_bytes"] or not r["dstar_frames"]:
                fails.append("%s: a bridge was muted on a block that must never mute one" % label)

    print()
    if fails:
        for f in fails:
            print("FAIL: %s" % f)
        print("\nRESULT: FAIL")
        return 1
    print("RESULT: PASS — the browser hears it, the bridges do not, and only a MEASURED "
          "on=True mutes anything")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
