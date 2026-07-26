"""Read — and optionally change — the radio's own settings over the cable that is already plugged in.

ADR 0137 refused this path twice, and both reasons were wrong for what is needed:

* *"a 6-second TX lockout"* — ``gSerialConfigCountDown_500ms = 12``, which only bites if you key
  immediately after writing. For a one-time config change followed by a settle it is irrelevant.
* *"no channel-select opcode exists"* — true, and beside the point. You do not select a channel;
  you write the VFO.

So the firmware **already on this radio** can be read and reconfigured: ``0x051B`` reads EEPROM,
``0x051D`` writes it in 8-byte chunks, ``0x05DD`` reboots. No flash, and nobody has to touch the radio.

The immediate target is one byte. ``gEeprom.DUAL_WATCH`` (``0x0E7C``) makes ``gRxVfo`` alternate, and
``RADIO_SelectCurrentVfo`` makes ``gCurrentVfo`` follow it — so with dual watch on, *which VFO the
station transmits from is decided by a timer*. That is the measured fault: the key-up success rate is
a function of the gap between keys (9/10 at 6 s, 5/12 at 19 s, strict alternation at ~21 s).

**Default is read-only.** ``--set-dual-watch off`` is the only thing that writes, and it:

1. dumps the whole 16-byte block to a file BEFORE touching anything,
2. read-modify-writes so only ``0x0E7C`` changes,
3. reads back and refuses to reboot unless the block matches byte for byte,
4. reboots (``CMD_051D`` only auto-reloads settings for ``0x0F30..0x0F40``, so this one needs it).

**On reversibility, honestly:** the flash is NOR, so a write can only clear bits. Putting ``0xFF``
back needs a sector erase, which would wipe every setting — so ``--restore`` cannot undo an
``0xFF -> 0x00`` write. It does not need to: the thing being changed is a normal menu setting the
operator can put back from the front panel in seconds, and the radio's own save path rewrites the
block properly (with an erase) whenever any setting changes.

ADR 0127: the service is stopped to borrow the port and restarted in a ``finally``.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from aioc_ptt_gate0 import AIOC_PORT, SERVICE, user_systemctl, wait_until_settled  # noqa: E402

from radio_server.backends.uvk5 import frames as f  # noqa: E402
from radio_server.backends.uvk5.transport import Uvk5Transport  # noqa: E402

#: Any value works — the firmware stores whatever the host sends in CMD_0514 and then requires the
#: same value on every EEPROM frame. Fixed rather than time-derived so a run is reproducible.
SESSION_TS = 0x12345678
BLOCK = f.EEPROM_SETTINGS_BLOCK
BLOCK_LEN = 16

#: Byte meanings inside the block, from settings.c's save routine. Printed so a human can sanity
#: check the dump against the radio's menus before anything is written.
FIELDS = {
    0xA001: "SQUELCH_LEVEL",
    0xA002: "TX_TIMEOUT_TIMER",
    0xA004: "KEY_LOCK / F4HWN bits",
    0xA007: "MIC_SENSITIVITY",
    0xA008: "BACKLIGHT_MIN/MAX",
    0xA009: "CHANNEL_DISPLAY_MODE",
    0xA00A: "CROSS_BAND_RX_TX",
    0xA00B: "BATTERY_SAVE",
    0xA00C: "DUAL_WATCH   <-- the one that alternates the TX VFO (0xFF reads as ON)",
}


def hello(tp: Uvk5Transport) -> None:
    """Establish the session timestamp every EEPROM frame is checked against."""
    tp.send(f.Hello(timestamp=SESSION_TS))
    time.sleep(0.4)


def read_block(tp: Uvk5Transport, offset: int, size: int) -> bytes:
    reply = tp.request(
        f.EepromRead(offset=offset, size=size, timestamp=SESSION_TS),
        match=lambda m: isinstance(m, f.EepromReadReply) and m.offset == offset,
        timeout=4.0,
    )
    if reply is None:
        raise SystemExit(f"no reply to EEPROM read at {offset:#06x} — is the radio listening?")
    return bytes(reply.data)


def show(block: bytes) -> None:
    print(f"  {BLOCK:#06x}: " + " ".join(f"{b:02X}" for b in block))
    for addr, name in sorted(FIELDS.items()):
        print(f"    {addr:#06x}  {block[addr - BLOCK]:3d}  {name}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--set-dual-watch", choices=["off"],
                    help="write DUAL_WATCH=0 (read-modify-write, verified, then reboot)")
    ap.add_argument("--restore", type=Path, help="write a previously saved 16-byte dump back")
    ap.add_argument("--dump", type=Path,
                    default=Path("eeprom-A000.bak"), help="where to save the block before writing")
    ap.add_argument("--offset", type=lambda s: int(s, 0), default=None,
                    help="probe a different block base (read-only exploration)")
    ap.add_argument("--port", default=AIOC_PORT)
    args = ap.parse_args(argv)

    global BLOCK
    if args.offset is not None:
        if args.set_dual_watch or args.restore:
            print("refusing: --offset is for read-only probing", file=sys.stderr)
            return 2
        BLOCK = args.offset
    writing = bool(args.set_dual_watch or args.restore)
    stopped = False
    tp: Uvk5Transport | None = None
    try:
        wait_until_settled(SERVICE)
        rc, out = user_systemctl("stop", SERVICE)
        if rc != 0:
            print(f"refusing: could not stop {SERVICE}: {out}", file=sys.stderr)
            return 2
        stopped = True
        time.sleep(6.0)

        tp = Uvk5Transport(serial_port=args.port)
        tp.connect()
        hello(tp)

        before = read_block(tp, BLOCK, BLOCK_LEN)
        print("current settings block:")
        show(before)

        if not writing:
            dw = before[f.EEPROM_DUAL_WATCH - BLOCK]
            print()
            if dw != f.DUAL_WATCH_OFF:
                how = "unprogrammed, which settings.c:173 loads as ON" if dw == 0xFF else "ON"
                print(f"  ==> DUAL_WATCH = {dw} ({how}). The transmit VFO alternates on a timer,")
                print("      which is exactly the measured gap-dependent key-up rate. Root cause confirmed.")
            else:
                print("  ==> DUAL_WATCH = 0 (already OFF). The alternation is NOT dual watch, and")
                print("      that diagnosis is wrong — do not build on it. Go back to the timing data.")
            return 0

        if args.restore:
            payload = args.restore.read_bytes()
            if len(payload) != BLOCK_LEN:
                print(f"refusing: {args.restore} is {len(payload)} bytes, expected {BLOCK_LEN}",
                      file=sys.stderr)
                return 2
            want = payload
        else:
            args.dump.write_bytes(before)
            print(f"\n  saved original block to {args.dump}")
            want = bytearray(before)
            want[f.EEPROM_DUAL_WATCH - BLOCK] = f.DUAL_WATCH_OFF
            want = bytes(want)
            if want == before:
                print("  nothing to do: DUAL_WATCH is already 0.")
                return 0

        # Write only the 8-byte chunk that actually changed, so an unrelated half of the block is
        # never rewritten on the strength of a read that might have been short.
        lo = f.EEPROM_SETTINGS_CHUNK - BLOCK
        chunk = want[lo:lo + f.EEPROM_CHUNK]
        print(f"  writing {f.EEPROM_SETTINGS_CHUNK:#06x}: " + " ".join(f"{b:02X}" for b in chunk))
        ack = tp.request(
            f.EepromWrite(offset=f.EEPROM_SETTINGS_CHUNK, data=chunk, timestamp=SESSION_TS),
            match=lambda m: isinstance(m, f.EepromWriteReply),
            timeout=4.0,
        )
        if ack is None:
            print("  no write acknowledgement — nothing verified, refusing to reboot.",
                  file=sys.stderr)
            return 2
        time.sleep(1.0)

        after = read_block(tp, BLOCK, BLOCK_LEN)
        print("\nread back:")
        show(after)
        if after != want:
            print("\n  ==> READ-BACK MISMATCH. The radio does not hold what we wrote; NOT rebooting.")
            print(f"      Original block is saved at {args.dump}; restore with --restore.")
            return 1

        print("\n  verified. rebooting the radio so it re-reads EEPROM at boot...")
        tp.send(f.Reset())
        time.sleep(1.0)
        return 0
    finally:
        if tp is not None:
            try:
                tp.close()
            except Exception:  # noqa: BLE001
                pass
        if stopped:
            time.sleep(6.0)          # let the radio finish rebooting before the service grabs the port
            rc, out = user_systemctl("start", SERVICE)
            print(f"  restarted {SERVICE}: rc={rc}")
            time.sleep(18.0)


if __name__ == "__main__":
    raise SystemExit(main())
