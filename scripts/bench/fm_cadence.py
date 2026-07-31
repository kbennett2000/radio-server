#!/usr/bin/env python3
"""Does the broadcast-FM probe work in the state it exists to detect?

ADR 0162 proved the ``0x0879`` out-of-band TUNE probe reads cleanly and mutates nothing. What it
never did was watch that read **over time** while the second receiver was running, and the worry is
concrete: ``Dock_SetFm`` refuses with ``ERR_TX`` while ``gCurrentFunction`` is ``FUNCTION_TRANSMIT``
or ``FUNCTION_MONITOR``, and if the radio parked in ``MONITOR`` for the duration of broadcast FM a
poll built on this frame could never observe it. A cadence must not be designed on a read that
cannot see.

Four legs, and **M3 is the one that decides the gate's meaning**:

* ``m1`` — host-side ON, then probe on a cadence for 30+ s. Every status byte, not a summary.
* ``m2`` — ``0x0874`` and ``0x0878`` in both states, byte for byte. ADR 0162 found them identical;
  a null result recorded is the point, because it is what leaves ``0x0879`` the only tell.
* ``m3`` — a **real signal** on the station's own channel while broadcast FM plays. The firmware
  tears the BK1080 down in ``APP_StartListening`` (``app.c:712-715``) and passes channel audio
  **without clearing ``gFmRadioMode``** — so the probe should keep saying "FM" while the operator is
  in fact hearing the channel. Measured with a 1000 Hz tone from the witness so "which audio is
  this" is a number rather than an impression.
* ``m4`` — the front-panel ordering. Polls continuously so the OFF→ON transition is caught live;
  this is the one the relay gate exists for, and it is also the only leg needing a human.

**The service must be stopped for all four.** The AIOC exposes one tty and it carries PTT as well as
dock frames, so a second opener would both steal replies and risk keying. ADR 0127's rule applies:
stopping around a single-open test is sanctioned, leaving it stopped is not — hence the ``finally``,
which restores the receiver even on a crash.

Usage (on the bench box, service stopped)::

    .venv/bin/python scripts/bench/fm_cadence.py m1 --seconds 35
    .venv/bin/python scripts/bench/fm_cadence.py m2
    RADIO_API_TOKEN=... .venv/bin/python scripts/bench/fm_cadence.py m3 --i-will-transmit
    .venv/bin/python scripts/bench/fm_cadence.py m4 --seconds 90
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_ROOT))

from radio_server.backends.uvk5.frames import (  # noqa: E402
    BroadcastFmReply,
    DockCommand,
    SetModulationReply,
    SetVfoReply,
    Uvk5Decoder,
    build_frame,
    parse_frame,
)
from radio_server.backends.uvk5.transport import _default_serial_factory  # noqa: E402

import broadcast_fm_on as bfm  # noqa: E402

DEFAULT_PORT = bfm.DEFAULT_PORT
DOCK_BAUD = 38400

#: Where the BK1080 is parked for these legs. A receiver on a dead carrier still holds the speaker
#: line, but a strong local station is what makes "this audio is the broadcast" measurable.
DEFAULT_FM_HZ = 104_300_000

#: M3 runs on the UHF bench pair: the kv4p witness is an SA818 and cannot transmit on 147.555, so
#: the leg that needs a real signal on the station's own channel has to happen where the witness
#: can make one. The station is put back on 147.555 by the caller, not by this script.
M3_CHANNEL_HZ = 445_800_000
M3_TONE_HZ = 1000.0


class Dock:
    """One long-lived dock session: the port opens once and every leg reuses it.

    Opening per exchange (what ``broadcast_fm_on.py`` does, correctly, for a one-shot) would mean
    30+ open/close cycles inside a cadence leg, which is not what a poller does and would measure
    the wrong thing. Opens through the transport's own factory because **this line carries PTT** —
    pyserial asserts DTR on a plain open and DTR is the AIOC's key line (ADR 0111, guardrail 2).
    """

    def __init__(self, port: str) -> None:
        self._ser = _default_serial_factory(port, DOCK_BAUD)
        self._decoder = Uvk5Decoder()

    def close(self) -> None:
        try:
            self._ser.close()
        except Exception:
            pass

    def exchange(self, command: int, params: bytes, *, wait: float = 2.0):
        """Send one frame; return ``(msg, payload_hex, latency_s)`` for the first decoded reply."""
        frame = build_frame(command, params)
        self._ser.reset_input_buffer()
        self._decoder = Uvk5Decoder()  # a fresh decoder per exchange: no carry-over from a stall
        sent = time.monotonic()
        self._ser.write(frame)
        self._ser.flush()
        deadline = sent + wait
        while time.monotonic() < deadline:
            chunk = self._ser.read(256)
            if not chunk:
                continue
            for payload in self._decoder.feed(chunk):
                return parse_frame(payload), payload.hex(), time.monotonic() - sent
        return None, "", time.monotonic() - sent

    # -- the three broadcast-FM actions ---------------------------------------------------------

    def probe(self, *, wait: float = 2.0):
        """The ADR 0162 read: TUNE at 0 Hz, band 0 — under every band floor, so it always refuses."""
        return self.exchange(
            DockCommand.SET_BROADCAST_FM,
            struct.pack("<BIB", bfm.ACTION_TUNE, 0, bfm.BAND_87_5_108),
            wait=wait,
        )

    def fm_on(self, hz: int, *, wait: float = 3.5):
        return self.exchange(
            DockCommand.SET_BROADCAST_FM,
            struct.pack("<BIB", bfm.ACTION_ON, hz, bfm.BAND_87_5_108),
            wait=wait,
        )

    def fm_off(self, *, wait: float = 4.0):
        """OFF writes flash and stalls the link, hence the longer window (ADR 0160)."""
        return self.exchange(
            DockCommand.SET_BROADCAST_FM,
            struct.pack("<BIB", bfm.ACTION_OFF, 0, 0),
            wait=wait,
        )


def _status_name(msg) -> str:
    if msg is None:
        return "<no reply>"
    status = getattr(msg, "status", None)
    return status.name if status is not None else repr(msg)


def _poll(dock: Dock, seconds: float, interval: float, *, label: str) -> list[str]:
    """Probe every ``interval`` for ``seconds``, printing EVERY reply. Returns the status names."""
    seen: list[str] = []
    started = time.monotonic()
    n = 0
    while time.monotonic() - started < seconds:
        msg, payload, latency = dock.probe()
        n += 1
        name = _status_name(msg)
        seen.append(name)
        print(f"  [{label} {n:>3}] t={time.monotonic() - started:6.2f}s  "
              f"{name:<10} {latency * 1000:6.1f} ms  payload={payload or '-'}")
        time.sleep(max(0.0, interval - latency))
    return seen


def _verdict(seen: list[str]) -> None:
    tally: dict[str, int] = {}
    for name in seen:
        tally[name] = tally.get(name, 0) + 1
    print(f"  tally  : {tally}")
    if not seen:
        print("  VERDICT: no reads — nothing was measured")
    elif set(seen) == {"ERR_TX"}:
        print("  VERDICT: BLIND — the probe returned ERR_TX for the whole window. A cadence built "
              "on this frame can never observe broadcast FM.")
    elif "ERR_BAND" in tally:
        print(f"  VERDICT: the probe SEES broadcast FM ({tally['ERR_BAND']}/{len(seen)} reads "
              f"answered ERR_BAND).")
    else:
        print("  VERDICT: no ERR_BAND in the window — the receiver was not running, or the probe "
              "did not reach it.")


# --- legs ---------------------------------------------------------------------------------------


def leg_m1(args) -> int:
    """Host-side ON, then a sustained cadence. The brief's gating item."""
    print(f"M1 — probe every {args.interval}s for {args.seconds}s with broadcast FM ON\n")
    dock = Dock(args.port)
    try:
        print("  baseline (FM off):")
        msg, payload, _ = dock.probe()
        print(f"    {_status_name(msg):<10} payload={payload or '-'}")

        print(f"  switching the second receiver on at {args.fm_hz / 1e6:.1f} MHz")
        msg, payload, _ = dock.fm_on(args.fm_hz)
        print(f"    {_status_name(msg):<10} payload={payload or '-'}  -> {msg!r}")
        if _status_name(msg) != "APPLIED":
            print("  the receiver did not come up; the leg measures nothing. STOP.")
            return 2

        seen = _poll(dock, args.seconds, args.interval, label="m1")
        _verdict(seen)
        return 0
    finally:
        print("\n  restoring: broadcast FM OFF")
        msg, payload, _ = dock.fm_off()
        print(f"    {_status_name(msg):<10} payload={payload or '-'}  -> {msg!r}")
        dock.close()


def _read_controls(dock: Dock) -> dict[str, str]:
    """The two frames the brief asks about, as raw payload hex."""
    out: dict[str, str] = {}
    msg, payload, _ = dock.exchange(DockCommand.SET_VFO, b"")  # empty 0x0873 = the read-back probe
    out["0x0874"] = payload or "<no reply>"
    out["0x0874_decoded"] = repr(msg) if isinstance(msg, SetVfoReply) else repr(msg)
    msg, payload, _ = dock.exchange(DockCommand.SET_MODULATION, bytes([0]))  # re-assert FM
    out["0x0878"] = payload or "<no reply>"
    out["0x0878_decoded"] = repr(msg) if isinstance(msg, SetModulationReply) else repr(msg)
    return out


def leg_m2(args) -> int:
    """Is there a tell on any frame that is NOT Dock_SetFm? ADR 0162 says no; re-measure and report."""
    print("M2 — 0x0874 / 0x0878 with broadcast FM off, then on. Report the bytes either way.\n")
    dock = Dock(args.port)
    try:
        off = _read_controls(dock)
        print("  FM off:")
        for key in ("0x0874", "0x0878"):
            print(f"    {key}: {off[key]}")

        msg, _, _ = dock.fm_on(args.fm_hz)
        if _status_name(msg) != "APPLIED":
            print(f"  could not switch the receiver on ({_status_name(msg)}); STOP.")
            return 2
        on = _read_controls(dock)
        print("  FM on:")
        for key in ("0x0874", "0x0878"):
            print(f"    {key}: {on[key]}")

        print("\n  diff:")
        same = True
        for key in ("0x0874", "0x0878"):
            if off[key] == on[key]:
                print(f"    {key}: IDENTICAL")
            else:
                same = False
                print(f"    {key}: DIFFERS  off={off[key]}  on={on[key]}")
        print(f"    {off['0x0878_decoded']}")
        if same:
            print("\n  NULL RESULT — neither frame carries a broadcast-FM tell, which is what "
                  "leaves 0x0879 the only read. (ADR 0162 measured the same.)")
        else:
            print("\n  A TELL EXISTS on a frame that is not Dock_SetFm. That is a better gate than "
                  "the probe and the cadence design changes.")
        return 0
    finally:
        print("\n  restoring: broadcast FM OFF")
        msg, payload, _ = dock.fm_off()
        print(f"    {_status_name(msg):<10} payload={payload or '-'}")
        dock.close()


def leg_m3(args) -> int:
    """A real signal on the station's own channel while broadcast FM plays.

    Predicted from source: the firmware drops the BK1080 and passes channel audio, but leaves
    ``gFmRadioMode`` set — so the probe keeps answering ERR_BAND while the operator is in fact
    hearing the channel. If that holds, ERR_BAND means "FM mode is selected", not "deaf right now",
    and the gate's semantics follow from it.
    """
    import sounddevice as sd  # noqa: PLC0415 — hardware extra, imported only for this leg

    from acceptance import KV4P_BASE, api, rms, tone_power  # noqa: PLC0415
    from radio_server.audio import synth_tone  # noqa: PLC0415

    if not args.i_will_transmit:
        print("refusing: m3 keys the witness on air. Pass --i-will-transmit.", file=sys.stderr)
        return 2

    print(f"M3 — a real over on {M3_CHANNEL_HZ / 1e6:.3f} MHz while broadcast FM plays\n")

    def capture(seconds: float) -> bytes:
        with sd.RawInputStream(device=args.input_device, samplerate=48000, channels=1,
                               dtype="int16", blocksize=960) as stream:
            frames, want = [], int(48000 * seconds)
            got = 0
            while got < want:
                data, _ = stream.read(min(960, want - got))
                frames.append(bytes(data))
                got += 960
            return b"".join(frames)

    def describe(label: str, pcm: bytes) -> None:
        print(f"    {label:<28} RMS {rms(pcm):8.1f}   {M3_TONE_HZ:.0f} Hz power "
              f"{tone_power(pcm, M3_TONE_HZ):7.3f}")

    code, body = api(KV4P_BASE, "POST", "/frequency", body={"hz": M3_CHANNEL_HZ})
    if code != 200:
        print(f"  could not tune the witness ({code} {body!r:.80})", file=sys.stderr)
        return 2
    print(f"  witness tuned to {M3_CHANNEL_HZ / 1e6:.3f} MHz")

    tone = synth_tone(M3_TONE_HZ, 6000).data
    dock = Dock(args.port)
    try:
        msg, _, _ = dock.fm_on(args.fm_hz)
        print(f"  broadcast FM on at {args.fm_hz / 1e6:.1f} MHz -> {_status_name(msg)}")
        if _status_name(msg) != "APPLIED":
            return 2
        time.sleep(1.5)

        print("\n  a) before the over — the broadcast should be what this station hears")
        describe("audio (FM only)", capture(2.0))
        msg, payload, _ = dock.probe()
        print(f"    probe                        {_status_name(msg):<10} payload={payload}")

        print("\n  b) during the over — the witness keys a 1000 Hz tone")
        import threading  # noqa: PLC0415

        result: dict[str, object] = {}

        def _key() -> None:
            result["code"], result["body"] = api(
                KV4P_BASE, "POST", "/transmit", raw=tone, timeout=60
            )

        keyer = threading.Thread(target=_key, daemon=True)
        keyer.start()
        time.sleep(1.5)  # let the carrier come up and the UV-K5's squelch open
        during = capture(2.0)
        describe("audio (over in progress)", during)
        msg, payload, _ = dock.probe()
        print(f"    probe                        {_status_name(msg):<10} payload={payload}")
        keyer.join(timeout=30)
        print(f"    witness POST /transmit -> {result.get('code')}")

        print("\n  c) straight after the over — FM has not come back yet (5 s restore timer)")
        describe("audio (0-2 s after)", capture(2.0))
        msg, payload, _ = dock.probe()
        print(f"    probe                        {_status_name(msg):<10} payload={payload}")

        print("\n  d) 6 s after the over — the firmware should have restored the broadcast")
        time.sleep(4.0)
        describe("audio (6 s after)", capture(2.0))
        msg, payload, _ = dock.probe()
        print(f"    probe                        {_status_name(msg):<10} payload={payload}")
        return 0
    finally:
        print("\n  restoring: broadcast FM OFF")
        msg, payload, _ = dock.fm_off()
        print(f"    {_status_name(msg):<10} payload={payload or '-'}")
        dock.close()


def leg_m4(args) -> int:
    """The front-panel ordering. Polls continuously so the OFF->ON transition is caught live."""
    print(f"M4 — polling every {args.interval}s for {args.seconds}s.\n")
    print("  *** PRESS F+0 ON THE RADIO NOW (and EXIT before the window ends, or the")
    print("      restore below will switch it off for you). ***\n")
    dock = Dock(args.port)
    saw_on = False
    try:
        seen = _poll(dock, args.seconds, args.interval, label="m4")
        saw_on = "ERR_BAND" in seen
        _verdict(seen)
        transitions = [f"{a}->{b}" for a, b in zip(seen, seen[1:]) if a != b]
        print(f"  changes: {transitions or 'none — the state never moved during the window'}")
        return 0
    finally:
        if saw_on:
            print("\n  restoring: broadcast FM OFF")
            msg, payload, _ = dock.fm_off()
            print(f"    {_status_name(msg):<10} payload={payload or '-'}")
        else:
            print("\n  no ON was observed; leaving the receiver untouched")
        dock.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", default=DEFAULT_PORT)
    ap.add_argument("--fm-hz", type=int, default=DEFAULT_FM_HZ)
    ap.add_argument("--seconds", type=float, default=35.0)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--input-device", default="AIOC_K6")
    ap.add_argument("--i-will-transmit", action="store_true",
                    help="m3 only: acknowledge that this leg puts a carrier on the air")
    ap.add_argument("leg", choices=["m1", "m2", "m3", "m4"])
    args = ap.parse_args(argv)
    return {"m1": leg_m1, "m2": leg_m2, "m3": leg_m3, "m4": leg_m4}[args.leg](args)


if __name__ == "__main__":
    raise SystemExit(main())
