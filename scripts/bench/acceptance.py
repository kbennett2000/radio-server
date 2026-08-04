#!/usr/bin/env python3
"""End-to-end bench acceptance for a *deployed, running* radio-server pair.

This is the permanent replacement for the ad-hoc ``/tmp`` bench scripts. ``/tmp`` is wiped on
reboot, so every proof written there evaporated the moment the bench rebooted — which is roughly
how this bench ended up with no reproducible acceptance at all. This lives in the repo.

**Why a script and not a ``doctor`` flag.** ``doctor`` opens the serial port and sound card
directly, so it can only run with the service *stopped*. Stopping the service to test it is the
exact anti-pattern that produced the 8-minute outage ADR 0127 documents. Acceptance has to
exercise the system as deployed: through the HTTP/WebSocket API of the live units.

**The rig** (see ``docs/server-notes.md``): two radio-server instances on one box, both on
**445.800**, antennas/dummy loads inches apart, so each is the other's objective measuring
instrument.

* ``RADIO_BASE`` — the radio under test (UV-K5 V3 over the AIOC dock), default ``:8090``
* ``KV4P_BASE`` — the kv4p SA818, used as a calibrated UHF transmitter *and* receiver, default ``:8091``

kv4p transmits → the K6 receives (RX, DTMF, auth stages). The K6 transmits → kv4p's hardware
carrier-detect and received audio measure it (TX stage). No human keys anything.

**Two fixture channels the split stages need** in the deployed ``radio.toml``. They are not in
``radio.toml.example`` on purpose — that file ships no live preset, because a preset's frequency is
the operator's own choice — so add them by hand on a bench box::

    [[presets]]
    name = "Bench Split"          # +600 kHz
    frequency = 445800000
    tx_frequency = 446400000
    tx_tone = 100.0

    [[presets]]
    name = "Bench Split Minus"    # -600 kHz, the shape 34 of the operator's 37 repeaters use
    frequency = 446400000
    tx_frequency = 445800000
    tx_tone = 100.0

Without them the ``split`` / ``split-minus`` stages SKIP, and a skipped stage is not a pass — the
run says so and exits non-zero.

Usage::

    RADIO_API_TOKEN=... .venv/bin/python scripts/bench/acceptance.py            # every stage
    RADIO_API_TOKEN=... .venv/bin/python scripts/bench/acceptance.py --only rx tx
    RADIO_API_TOKEN=... .venv/bin/python scripts/bench/acceptance.py --list

Exit codes (ADR 0165): **0** every selected stage passed · **1** a stage failed · **2** the run never
began (no ``RADIO_API_TOKEN``, unknown stage name) · **3** every attempted stage passed but at least
one could not be attempted. A skipped stage is still not a pass, so incompleteness is still non-zero
and every ``rc != 0`` caller behaves as it always did — but "a check failed" and "a check never ran"
stopped being the same word. Before this, a missing ``Bench Split Minus`` preset printed
``RESULT: FAIL`` on an otherwise clean run, which is how a real failure gets read past
(ADR 0161 finding 8, open four cycles).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import errno
import http.client
import json
import os
import socket
import ssl
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

import numpy as np  # noqa: E402
import websockets  # noqa: E402

from radio_server.audio import CANONICAL_RATE, synth_dtmf, synth_tone  # noqa: E402

TOKEN = os.environ.get("RADIO_API_TOKEN", "")
RADIO_BASE = os.environ.get("RADIO_BASE", "https://127.0.0.1:8090")
KV4P_BASE = os.environ.get("KV4P_BASE", "https://127.0.0.1:8091")
#: The operating log (``logging.path``) of the radio under test, relative to the deployment dir.
LOG_PATH = Path(os.environ.get("RADIO_LOG_PATH", _ROOT / "radio-server.jsonl"))
#: systemd --user unit names, for the boot/stop stage.
UNIT = os.environ.get("RADIO_UNIT", "radio-server.service")
KV4P_UNIT = os.environ.get("KV4P_UNIT", "radio-server-kv4p.service")

#: Wall-clock at import, formatted for `journalctl --since`. Used only to *report* the whole-run
#: overrun count; the pass/fail check is scoped to the window where RX audio actually matters (see
#: `stage_rx`). Overruns around a restart or at the end of a keyed over are structural, not faults
#: — the card fills its ring on wall-clock time and nobody is reading it then (ADR 0130).
RUN_START = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 2))

#: Self-signed bench certs (ADR 0039) — verification off on purpose, this is a LAN loopback probe.
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE

#: DTMF digit shape, **measured over this RF path**, not guessed. Sending ``123456#`` from the kv4p
#: and decoding the K6's received audio offline with the deployed decoder:
#:
#:     120 ms / 60 ms @ 0.4  ->  ''          (burst too short to even hold the RX squelch gate open)
#:     200 ms / 100 ms @ 0.4 ->  '123456#'   <- chosen
#:     200 ms / 100 ms @ 0.25 -> '56#'       (under-driven)
#:     300 ms / 150 ms @ 0.4 ->  '123456#'   (no better, just slower)
#:
#: The same capture decodes as only ``'14'`` at the stock 4 dB reverse-twist limit, which is why
#: this bench sets ``audio.dtmf_reverse_twist_db = 10.0`` (ADR 0075) — see ADR 0129.
DTMF_TONE_MS = 200.0
DTMF_GAP_MS = 100.0
DTMF_AMPLITUDE = 0.4


# --- plumbing ---------------------------------------------------------------------------------


def _host_port(base: str) -> tuple[str, int]:
    hostport = base.split("://", 1)[1]
    host, _, port = hostport.partition(":")
    return host, int(port or 443)


def api(base: str, method: str, path: str, body=None, raw: bytes | None = None, timeout=15.0):
    """One REST call. Returns ``(status, parsed_json_or_bytes)``. Bearer auth (``api/auth.py``)."""
    host, port = _host_port(base)
    conn = http.client.HTTPSConnection(host, port, context=_SSL, timeout=timeout)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    payload: bytes | None = raw
    if body is not None:
        payload = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif raw is not None:
        headers["Content-Type"] = "application/octet-stream"
    try:
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        try:
            return resp.status, json.loads(data)
        except Exception:
            return resp.status, data
    finally:
        conn.close()


def ws_url(base: str, path: str) -> str:
    return base.replace("https://", "wss://").replace("http://", "ws://") + f"{path}?token={TOKEN}"


@dataclass
class RxCapture:
    """What ``/audio/rx`` delivered, with the timing needed to judge *smoothness*.

    ``duty`` is deliberately measured across the **active** span (first byte → last byte), not the
    whole listening window: the window includes dead air before and after the far end keys, so
    window-relative duty can only ever report the transmitter's duty cycle, never the receiver's
    continuity. Gap-free delivery while a carrier is up is the property under test.
    """

    pcm: bytes
    window: float
    first: float = 0.0
    last: float = 0.0
    max_gap: float = 0.0

    @property
    def active(self) -> float:
        return max(self.last - self.first, 0.0)

    @property
    def duty(self) -> float:
        expected = self.active * CANONICAL_RATE * 2
        return 100.0 * len(self.pcm) / expected if expected > 0 else 0.0


async def _collect_rx(base: str, seconds: float, start_evt=None) -> RxCapture:
    """Subscribe to ``/audio/rx`` and collect raw PCM for ``seconds`` with arrival timing."""
    pcm = bytearray()
    first = last = 0.0
    max_gap = 0.0
    async with websockets.connect(ws_url(base, "/audio/rx"), ssl=_SSL, max_size=None) as ws:
        hello = await asyncio.wait_for(ws.recv(), timeout=10)  # {"status":"ready","format":...}
        del hello
        if start_evt is not None:
            start_evt.set()
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if not isinstance(msg, (bytes, bytearray)):
                continue
            now = time.monotonic()
            if not pcm:
                first = now
            else:
                max_gap = max(max_gap, now - last)
            last = now
            pcm.extend(msg)
        return RxCapture(bytes(pcm), time.monotonic() - t0, first, last, max_gap)


def rms(pcm: bytes) -> float:
    if not pcm:
        return 0.0
    a = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


def tone_power(pcm: bytes, freq: float, rate: int = CANONICAL_RATE, width: float = 60.0) -> float:
    """Fraction of total spectral energy within ``width`` Hz of ``freq`` (0..1).

    A clean recovered tone lands well above 0.5; broadband noise with no tone sits near
    ``2*width/(rate/2)`` ≈ 0.005. Normalising to total energy makes the number independent of
    volume, so it survives a change of power level or geometry.
    """
    if len(pcm) < 4096:
        return 0.0
    a = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    spec = np.abs(np.fft.rfft(a * np.hanning(a.size))) ** 2
    freqs = np.fft.rfftfreq(a.size, 1.0 / rate)
    band = spec[(freqs >= freq - width) & (freqs <= freq + width)].sum()
    return float(band / (spec.sum() or 1.0))


def speech_band_ratio(pcm: bytes, rate: int = CANONICAL_RATE) -> float:
    """Fraction of spectral energy in 300–3000 Hz — a voice announcement sits well above noise."""
    if len(pcm) < 2048:
        return 0.0
    a = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    spec = np.abs(np.fft.rfft(a * np.hanning(a.size))) ** 2
    freqs = np.fft.rfftfreq(a.size, 1.0 / rate)
    band = spec[(freqs >= 300) & (freqs <= 3000)].sum()
    return float(band / (spec.sum() or 1.0))


def dtmf_pcm(digits: str, amplitude: float = DTMF_AMPLITUDE) -> bytes:
    """Synthesize a DTMF string, reusing the in-tree fixture synth (``radio_server.audio``)."""
    gap = b"\x00\x00" * int(CANONICAL_RATE * DTMF_GAP_MS / 1000.0)
    out = bytearray(gap)
    for d in digits:
        out.extend(synth_dtmf(d, DTMF_TONE_MS, amplitude=amplitude).samples)
        out.extend(gap)
    return bytes(out)


#: The phrase the backend uses only when an overrun means the reader actually **fell behind** — a
#: gap longer than its own read cadence. An overrun reported one block period after the previous
#: read is the card recovering on its own and is logged separately (ADR 0130/0132); counting those
#: made this a flaky proxy that failed runs where every direct audio measure was perfect: 100.7 %
#: duty, the whole over received, tone recovered 0.999, and a red X next to it.
_XRUN_STALL_PHRASE = "RX reader fell behind"


def journal_xruns(unit: str, since: str) -> int:
    out = subprocess.run(
        ["journalctl", "--user", "-u", unit, "--since", since, "--no-pager"],
        capture_output=True, text=True,
    ).stdout
    return sum(1 for line in out.splitlines() if _XRUN_STALL_PHRASE in line)


def log_tail_since(offset: int) -> tuple[list[dict], int]:
    """New operating-log (JSONL) records appended past ``offset``, plus the new offset."""
    if not LOG_PATH.exists():
        return [], offset
    with LOG_PATH.open("rb") as fh:
        fh.seek(offset)
        blob = fh.read()
        new_offset = fh.tell()
    records = []
    for line in blob.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line:
            try:
                records.append(json.loads(line))
            except Exception:
                pass
    return records, new_offset


def log_offset() -> int:
    return LOG_PATH.stat().st_size if LOG_PATH.exists() else 0


def wait_healthy(base: str, timeout: float = 45.0) -> bool:
    """Wait for the station to be *usable*, not merely answering (ADR 0166).

    This asked `/status` for a 200 and ignored the body, which meant a station whose serial reader
    had been dead for an hour reported "restarted healthy" — every field `/status` serves comes from
    cached model state, so it answers perfectly well while the radio says nothing. That was the only
    up-signal in the repo, and it was the wrong one.

    `/healthz` is the verdict: **503** when the reader is dead. `/status` deliberately still answers
    200 with a full body, because it is where a broken station gets diagnosed.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if api(base, "GET", "/healthz", timeout=4)[0] == 200:
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def open_tty(port: str, baud: int = 9600, timeout: float = 1.0):
    """`serial.Serial(port, ...)`, but explain an EBUSY instead of leaving a bare traceback.

    Since ADR 0166 the running service claims its serial ports with `TIOCEXCL`, so every bench
    script that touches the tty while the station is up now gets **EBUSY** — which is the point (it
    used to get in, and kill the service's reader thread on the way, silently). But "Device or
    resource busy" on its own does not tell you that, and the person reading it is mid-bench.
    """
    import serial  # pyserial, the hardware extra

    try:
        return serial.Serial(port, baud, timeout=timeout)
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.EBUSY:
            sys.exit(
                f"{port} is held by another process: the radio-server service claims the tty "
                f"exclusively while it runs (ADR 0166).\n"
                f"Stop it first:  systemctl --user stop radio-server\n"
                f"and start it again when you are done:  systemctl --user start radio-server"
            )
        raise


# --- stage bookkeeping ------------------------------------------------------------------------


@dataclass
class Stage:
    name: str
    ok: bool = True
    notes: list[str] = field(default_factory=list)
    #: Set when the stage could not be *attempted* — distinct from failing. See `rf_witness`.
    skipped: str = ""

    def skip(self, why: str) -> None:
        self.skipped = why
        self.notes.append(f"  -- skipped: {why}")

    def num(self, label: str, value, want: str = "") -> None:
        self.notes.append(f"    {label:<34} {value}{('   want ' + want) if want else ''}")

    def check(self, label: str, passed: bool, value, want: str) -> None:
        self.ok = self.ok and bool(passed)
        mark = "ok " if passed else "XX "
        self.notes.append(f"  {mark}{label:<32} {value}   want {want}")

    def fail(self, msg: str) -> None:
        self.ok = False
        self.notes.append(f"  XX {msg}")


def stage_verdict(stage: "Stage") -> str:
    """One stage's word. A failed check outranks a later skip.

    The display and the tally must not use different rules: reading `skipped` first would print SKIP
    for a stage that failed a check and *then* gave up, while the run still counted it as a failure.
    No stage reaches that state today — every `skip()` call site returns immediately — which is
    exactly why it would go unnoticed if one ever did.
    """
    if not stage.ok:
        return "FAIL"
    return "SKIP" if stage.skipped else "PASS"


def overall_verdict(results: "list[Stage]") -> tuple[str, int]:
    """The banner word and the exit code (ADR 0165). Pure, so it can be proven without a station.

    Three states, because there are three things that happen: everything passed, something failed,
    or something never ran. Collapsing the third into the second is what made `RESULT: FAIL` the
    normal reading of a healthy bench for four cycles.
    """
    if any(not s.ok for s in results):
        return "FAIL", 1
    if any(s.skipped for s in results):
        return "INCOMPLETE", 3
    return "PASS", 0


#: How far the two radios' reported frequencies may differ and still be the same channel. The kv4p
#: quantises to a 2500 Hz raster (`backends/kv4p/radio.py:92-94`), so 445.800 reads back 445799988.
_SAME_CHANNEL_HZ = 2500


def rf_witness(stage: Stage) -> bool:
    """True when the kv4p can actually hear the radio under test; otherwise skip the stage, loudly.

    Every RF stage here is measured *by the kv4p*, and the kv4p is a single-module SA818-**UHF**
    board — 400-480 MHz, `set_frequency` raises outside it. So on 2 m there is no witness on this
    bench at all, and if the radio under test has been moved to its 147.555 operating frequency
    every RF stage will report zero bytes, zero duty, zero tone.

    That failure is indistinguishable from a broken receiver, and it cost real time: an early run
    of this mission read exactly that output and started bisecting a regression that did not exist.
    A stage that cannot be *attempted* must say so rather than produce a red X that looks like a
    finding. `scripts/bench/rf_listen.py` is what covers 2 m until a VHF board exists.
    """
    code_a, a = api(RADIO_BASE, "GET", "/status")
    code_b, b = api(KV4P_BASE, "GET", "/status")
    if code_a != 200 or code_b != 200:
        stage.fail(f"could not read both radios' status ({code_a}, {code_b})")
        return False
    fa, fb = a.get("frequency"), b.get("frequency")
    if fa is None or fb is None or abs(fa - fb) > _SAME_CHANNEL_HZ:
        stage.skip(
            f"no RF witness: radio under test on {fa} Hz, kv4p on {fb} Hz. The kv4p is UHF-only "
            f"(400-480 MHz), so it cannot hear 2 m — use scripts/bench/rf_listen.py there."
        )
        return False
    return True


#: Frequencies this runner is allowed to key, in Hz. Everything it transmits goes into a dummy load
#: on the bench pair; a real repeater's uplink is the operator's to key, never the runner's.
BENCH_TX_HZ = frozenset({445_800_000, 446_000_000, 446_400_000, 147_555_000})

#: The deployed ``radio.toml`` the radio under test was started with — the file-side source for the
#: preset deny-list below. On a bench box whose config lives elsewhere, set ``RADIO_CONFIG_PATH``.
RADIO_CONFIG_PATH = Path(os.environ.get("RADIO_CONFIG_PATH", _ROOT / "radio.toml"))


def _preset_frequencies() -> frozenset[int] | None:
    """Every frequency a configured preset would put on the air — RX output and TX input both.

    Returns ``None`` when the set could not be determined, and callers MUST treat that as *refuse*.
    There is no safe default here: an empty set means "nothing is forbidden", which is exactly
    backwards for a guard whose failure mode is an unattended carrier on a repeater's input.

    Two sources, **unioned** rather than first-wins:

    1. ``GET /presets`` — what the running server actually loaded.
    2. :func:`radio_server.config.load_presets` — the file, for when the API is down (the ``systemd``
       stage stops the service on purpose) or the token is wrong.

    Unioned, because a freshly restarted server that loaded *zero* presets would otherwise unlock
    every frequency the file forbids.

    Something has to be exempt, because the runner's own split fixtures are themselves presets
    living on the bench pair — a literal "refuse anything in any preset" would refuse the bench. A
    preset is exempt when **every** leg it uses is a bench frequency, which is true of the fixtures
    and false of every real repeater: no real machine has both its output and its input inside a
    set of four simplex frequencies.

    Exempting per *preset* rather than per *frequency* is what closes the interesting hole. Were
    the exemption per frequency, importing a repeater whose input happens to land on 445.800 would
    quietly exempt that leg — and keying it is precisely the accident this guard exists to prevent.
    The exemption is never by preset **name**: calling a real repeater "Bench Split" must not
    launder it.

    Never raises. A raise here would be swallowed by ``main``'s blanket handler and fail the stage,
    which is fail-closed by luck rather than by design.
    """
    found: set[int] = set()
    read_any = False

    def harvest(rows: object) -> bool:
        if not isinstance(rows, list):
            return False
        for row in rows:
            if not isinstance(row, dict):
                continue
            legs = {row.get(key) for key in ("frequency", "tx_frequency")}
            legs = {hz for hz in legs if isinstance(hz, int) and not isinstance(hz, bool)}
            if legs and not legs <= BENCH_TX_HZ:
                found.update(legs)
        return True

    try:
        code, body = api(RADIO_BASE, "GET", "/presets")
        if code == 200:
            rows = body.get("presets") if isinstance(body, dict) else body
            read_any = harvest(rows) or read_any
    except Exception:
        pass

    try:
        from radio_server.config import load_presets

        rows = load_presets(RADIO_CONFIG_PATH)
        if rows is not None:
            read_any = harvest(rows) or read_any
    except Exception:
        pass

    if not read_any:
        return None
    return frozenset(found)


def bench_frequency_only(base: str, stage: Stage, label: str) -> bool:
    """Refuse to key anything but a bench frequency. Returns False (and fails the stage) otherwise.

    This became load-bearing the moment the station's preset list stopped being three bench entries
    and became **37 real local repeaters** (ADR 0133). A stage that keys "wherever the radio happens
    to be pointing" was safe when the radio could only be pointing at the bench; now a preset stage
    that failed to restore, or a split left armed, would put an unattended carrier on a repeater's
    input. Checked at the moment of keying rather than trusted from three stages earlier.
    """
    code, state = api(base, "GET", "/status")
    if code != 200 or not isinstance(state, dict):
        stage.fail(f"{label}: could not read /status before keying (HTTP {code})")
        return False
    rx, tx = state.get("frequency"), state.get("tx_frequency")
    on_air = tx if tx is not None else rx
    # Within a channel of a bench frequency, not exactly equal: the kv4p quantises its tuning to a
    # 2500 Hz raster and reports 445799988 for 445.800, which is the same channel by every measure
    # that matters — the same tolerance `rf_witness` already applies.
    if on_air is None or not any(abs(on_air - hz) <= _SAME_CHANNEL_HZ for hz in BENCH_TX_HZ):
        stage.fail(
            f"{label}: REFUSING to key — this would transmit on {on_air} Hz, which is not a bench "
            f"frequency ({sorted(BENCH_TX_HZ)}). The radio is tuned to {rx}"
            + (f" with a split armed to {tx}" if tx is not None else "")
            + ". Something left the station on a real channel; fix that before re-running."
        )
        return False
    # The allow-list alone is blind to one hazard: a real repeater imported onto a bench frequency.
    # Then "this is a bench frequency" and "this is a live machine's channel" are both true, and
    # only the preset list can tell them apart. Deny-list AND allow-list — dropping either weakens
    # the guard (a bare deny-list would make a frequency in no preset at all keyable).
    forbidden = _preset_frequencies()
    if forbidden is None:
        stage.fail(
            f"{label}: REFUSING to key — could not read the configured presets from either "
            f"{RADIO_BASE}/presets or {RADIO_CONFIG_PATH}, so this runner cannot tell the bench "
            f"pair from a repeater's channel. Fix the read; do not bypass the guard."
        )
        return False
    clash = next((hz for hz in forbidden if abs(on_air - hz) <= _SAME_CHANNEL_HZ), None)
    if clash is not None:
        stage.fail(
            f"{label}: REFUSING to key — {on_air} Hz is within {_SAME_CHANNEL_HZ} Hz of {clash} Hz, "
            f"which a configured preset uses as a repeater output or input. The bench pair and a "
            f"real machine now share a channel; move the bench, not the guard."
        )
        return False
    return True


def transmit(base: str, pcm: bytes, stage: Stage, label: str) -> bool:
    """One-shot keyed transmission of raw PCM (``POST /transmit`` keys, plays, unkeys)."""
    if not bench_frequency_only(base, stage, label):
        return False
    status, body = api(base, "POST", "/transmit", raw=pcm, timeout=60)
    if status != 200:
        stage.fail(f"{label}: POST /transmit -> {status} {body!r:.120}")
        return False
    return True


# --- stages -----------------------------------------------------------------------------------


def stage_systemd() -> Stage:
    """DoD 1 — the units are up, and a stop under WebSocket load completes cleanly (ADR 0127)."""
    st = Stage("systemd")
    for unit in (UNIT, KV4P_UNIT):
        active = subprocess.run(
            ["systemctl", "--user", "is-active", unit], capture_output=True, text=True
        ).stdout.strip()
        st.check(f"{unit} active", active == "active", active, "active")
        enabled = subprocess.run(
            ["systemctl", "--user", "is-enabled", unit], capture_output=True, text=True
        ).stdout.strip()
        st.check(f"{unit} enabled at boot", enabled == "enabled", enabled, "enabled")
    linger = subprocess.run(
        ["loginctl", "show-user", os.environ.get("USER", "kb"), "-p", "Linger"],
        capture_output=True, text=True,
    ).stdout.strip()
    st.check("user lingering (boot start)", linger.endswith("yes"), linger, "Linger=yes")

    # A stop must stay clean with clients attached — an idle browser tab holding /audio/rx open is
    # exactly what used to wedge the stop for 20 s and earn a SIGKILL (ADR 0127).
    #
    # The client shape is the whole check, and it used to be the wrong one (ADR 0184). This held the
    # sockets with the `websockets` library, whose background reader answers the server's close frame
    # the moment it arrives — so the stop finished in ~0.3 s and the graceful window was never
    # touched. Measured on the deployed station, 20 stops per arm:
    #
    #     no client at all                        median 0.35 s  (0.25-0.48)
    #     `websockets` client (what this was)     median 0.34 s  (0.21-0.45)
    #     handshake completed, then never reads   median 5.36 s  (5.20-5.42)  <- the whole window
    #
    # A browser tab is the third one, not the second, which is why 133 SIGKILLs happened under a
    # check of this shape. So the hold is now a raw socket: complete the WS handshake, then read
    # nothing and answer nothing. `_hold_ws_unresponsive` is deliberately not a library client.
    def _hold_ws_unresponsive(path: str) -> socket.socket:
        host, port = _host_port(RADIO_BASE)
        sock = _SSL.wrap_socket(socket.create_connection((host, port), timeout=10), server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(
            f"GET {path}?token={TOKEN} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        sock.settimeout(10)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        if not buf.startswith(b"HTTP/1.1 101"):
            raise RuntimeError(f"websocket handshake refused: {buf[:60]!r}")
        sock.settimeout(None)  # and from here it is never read again
        return sock

    held: list[socket.socket] = []
    hold_error = ""
    try:
        for _path in ("/audio/rx", "/events"):
            held.append(_hold_ws_unresponsive(_path))  # appended as we go, so a raise leaks nothing
        time.sleep(1.0)  # let the server start streaming into a socket nobody is draining
        t0 = time.monotonic()
        subprocess.run(["systemctl", "--user", "stop", UNIT], check=False)
        elapsed = time.monotonic() - t0
    except Exception as exc:
        elapsed = -1.0
        hold_error = f"{type(exc).__name__}: {exc}"
    finally:
        for sock in held:
            with contextlib.suppress(Exception):
                sock.close()
    # The hold IS the check, so failing to establish it has to be a failure. With the old
    # `websockets` client a raise here was routine — the sockets die with the server — so the timing
    # assertion was quietly skipped and the stage still passed. With raw sockets nothing should
    # raise, and this was observed silently skipping once (a handshake racing a just-restarted
    # server). A stage that answers its own easy case is the thing this check exists to catch.
    st.check(
        "stop under WS load: hold established",
        not hold_error,
        hold_error or "2 sockets",
        "2 sockets",
    )
    result = subprocess.run(
        ["systemctl", "--user", "show", UNIT, "-p", "Result", "--value"],
        capture_output=True, text=True,
    ).stdout.strip()
    st.check("stop under WS load: result", result == "success", result, "success")
    if elapsed >= 0:
        st.check("stop under WS load: seconds", elapsed < 15.0, f"{elapsed:.2f}s", "< 15s")
    subprocess.run(["systemctl", "--user", "start", UNIT], check=False)
    st.check("restarted healthy", wait_healthy(RADIO_BASE), "up", "HTTP 200 on /healthz")
    return st


def stage_web() -> Stage:
    """DoD 2 — the page serves over TLS on the LAN and the read endpoints are healthy."""
    st = Stage("web")
    lan = os.environ.get("RADIO_LAN_BASE", "")
    for label, base in (("radio", RADIO_BASE), ("kv4p", KV4P_BASE)):
        code, body = api(base, "GET", "/status")
        st.check(f"{label} GET /status", code == 200, code, "200")
        code, caps = api(base, "GET", "/capabilities")
        st.check(f"{label} GET /capabilities", code == 200 and isinstance(caps, list), code, "200")
        if isinstance(caps, list):
            st.num(f"{label} capabilities", ",".join(caps))
    for path in ("/services", "/presets", "/auth/totp"):
        code, _ = api(RADIO_BASE, "GET", path)
        st.check(f"radio GET {path}", code == 200, code, "200")
    # The SPA itself, over TLS, on the LAN address a phone would use — not just loopback.
    for label, base in (("loopback", RADIO_BASE), ("LAN", lan)):
        if not base:
            continue
        code, body = api(base, "GET", "/")
        served = code == 200 and isinstance(body, (bytes, bytearray)) and b"<" in body
        st.check(f"web root over TLS ({label})", served, f"{code} / {len(body)}B", "200, HTML")
    # The liveness verdict, on both stations (ADR 0166). A 200 here means the serial reader is
    # actually running; `/status` answering 200 does not, and used to be the only thing checked.
    for label, base in (("radio", RADIO_BASE), ("kv4p", KV4P_BASE)):
        code, body = api(base, "GET", "/healthz")
        st.check(f"{label} GET /healthz", code == 200, code, "200")
        if isinstance(body, dict) and body.get("transport") is not None:
            st.num(f"{label} serial reader", "alive" if body["transport"]["alive"] else "DEAD")
    # An unauthenticated call must still be refused (auth is the gate, ADR guardrail 4).
    host, port = _host_port(RADIO_BASE)
    conn = http.client.HTTPSConnection(host, port, context=_SSL, timeout=10)
    conn.request("GET", "/status")
    st.check("unauthenticated /status", conn.getresponse().status == 401, "401", "401")
    conn.close()
    return st


def stage_presets() -> Stage:
    """DoD 7 — apply a preset over the API and the radio's state follows."""
    st = Stage("presets")
    code, body = api(RADIO_BASE, "GET", "/presets")
    presets = body.get("presets", []) if isinstance(body, dict) else []
    st.check("presets configured", len(presets) >= 2, len(presets), ">= 2")
    if not presets:
        return st
    before = api(RADIO_BASE, "GET", "/status")[1].get("frequency")
    target = next((p for p in presets if p["frequency"] != before), presets[0])
    code, applied = api(RADIO_BASE, "POST", "/presets/apply", body={"name": target["name"]})
    st.check(f"apply {target['name']!r}", code == 200, code, "200")
    now = api(RADIO_BASE, "GET", "/status")[1].get("frequency")
    st.check("radio state followed", now == target["frequency"], now, str(target["frequency"]))
    # Put the bench back on the shared 445.800 working frequency for the RF stages — SIMPLEX. The
    # `not tx_frequency` guard is load-bearing now that the split fixtures exist: `Bench Split` also
    # sits on frequency 445800000, so an unguarded match can restore "home" with a split armed to
    # 446.400. Checking only `frequency` would pass green, and `stage_tx` would then key on the
    # armed leg while the witness listened on 445.800 — zero carrier, zero RMS, a red X that looks
    # exactly like a dead transmitter (ADR 0134).
    home = next(
        (p for p in presets if p["frequency"] == 445_800_000 and not p.get("tx_frequency")), None
    )
    if home:
        api(RADIO_BASE, "POST", "/presets/apply", body={"name": home["name"]})
    state = api(RADIO_BASE, "GET", "/status")[1]
    back = state.get("frequency")
    st.check("restored to 445.800", back == 445_800_000, back, "445800000")
    st.check("restored simplex, no split armed", state.get("tx_frequency") is None,
             state.get("tx_frequency"), "null")
    return st


def stage_rx() -> Stage:
    """DoD 3 — kv4p transmits, the K6 receives, and /audio/rx delivers smooth frames."""
    st = Stage("rx")
    if not rf_witness(st):
        return st
    # Let the receiver settle before the window opens. The preceding stage retunes the radio, and
    # `set_frequency` cycles reg 0x30 — it takes the receiver down and back up — so a capture
    # hiccup right after it is the retune, not a reader fault. Measuring RX health across a retune
    # transient conflates two different things; this stage is about the steady-state receiver.
    # (Isolated, this stage measures 100.3% duty / 40 ms / 0 xruns three times out of three.)
    time.sleep(3.0)
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
    # The listening window must outlast the whole transmission, or the tail is clipped and the
    # active span under-reports. Measured on this bench: a 5.0 s tone occupies the kv4p for ~6.2 s
    # (0.5 s TX lead-in + encode/serial overhead), and the K6's first frame lands ~0.7 s after the
    # POST — so 11 s of listening comfortably brackets it.
    tone_seconds = 5.0
    seconds = 11.0
    tone = synth_tone(1000.0, tone_seconds * 1000.0, amplitude=0.6).samples

    async def run():
        started = asyncio.Event()
        collector = asyncio.create_task(_collect_rx(RADIO_BASE, seconds, started))
        await started.wait()
        await asyncio.sleep(0.5)
        await asyncio.to_thread(transmit, KV4P_BASE, tone, st, "rx-tone")
        return await collector

    cap = asyncio.run(run())
    st.num("audio received", f"{len(cap.pcm)} B over a {cap.active:.2f}s active span "
                             f"(in a {cap.window:.2f}s window)")
    st.check("frame duty (active span)", cap.duty >= 97.0, f"{cap.duty:.1f}%", ">= 97%")
    st.check("largest inter-frame gap", cap.max_gap < 0.25, f"{cap.max_gap * 1000:.0f} ms", "< 250 ms")
    # The whole transmission must arrive, not just a smooth fragment of it — a receiver that
    # delivers 3 s of a 5 s over is "smooth" and still wrong.
    st.check("whole over received", cap.active >= tone_seconds, f"{cap.active:.2f}s",
             f">= {tone_seconds:.1f}s")
    st.check("received audio RMS", rms(cap.pcm) > 300, f"{rms(cap.pcm):.0f}", "> 300")
    st.check("1000 Hz tone recovered", tone_power(cap.pcm, 1000.0) > 0.30,
             f"{tone_power(cap.pcm, 1000.0):.3f}", "> 0.30")
    # Scoped to this stage's own capture: an overrun *while receiving* means dropped audio, which
    # is the fault under test. Overruns elsewhere in the run (the restart in `systemd`, the end of
    # each keyed announcement) are structural — nobody is reading the ring then, by design.
    # Corroborated by the duty figure above: at >=97% of the active span, nothing was dropped.
    st.check("reader stalls while receiving", journal_xruns(UNIT, since) == 0,
             journal_xruns(UNIT, since), "0")
    return st


def stage_dtmf() -> Stage:
    """DoD 4 — kv4p keys DTMF, the deployed decoder reads it, the operating log records it."""
    st = Stage("dtmf")
    if not rf_witness(st):
        return st
    off = log_offset()
    if not transmit(KV4P_BASE, dtmf_pcm("1234#"), st, "dtmf"):
        return st
    time.sleep(8.0)  # dtmf.timeout is 3 s; give the entry time to complete and be logged
    records, _ = log_tail_since(off)
    kinds = [r.get("event") or r.get("type") or "" for r in records]
    st.num("new operating-log records", f"{len(records)}: {','.join(k for k in kinds if k)[:100]}")
    hit = any(k in ("auth_rejected", "auth_accepted", "command", "session_open") for k in kinds)
    st.check("decoder saw the entry", hit, kinds[:6] or "none", "an auth/command record")
    return st


def stage_auth() -> Stage:
    """DoD 5a — a real TOTP login over RF opens a session on the deployed decoder."""
    st = Stage("auth")
    if not rf_witness(st):
        return st
    import tomllib

    import pyotp

    secrets_path = Path(os.environ.get("RADIO_SECRETS", _ROOT / "radio-secrets.toml"))
    if not secrets_path.exists():
        st.fail(f"no secrets file at {secrets_path} — cannot derive a TOTP code")
        return st
    secret = tomllib.loads(secrets_path.read_text()).get("totp_secret", "")
    st.check("totp_secret present on this box", bool(secret), bool(secret), "True")
    if not secret:
        return st
    # Start from logged-out, or a session left open by an earlier run turns the TOTP entry into a
    # plain command and there is no fresh accept to assert. This is what makes the stage repeatable.
    if not _set_session(False, st, "pre-logout"):
        return st

    code = pyotp.TOTP(secret).now()
    off = log_offset()
    if not transmit(KV4P_BASE, dtmf_pcm(code + "#"), st, "totp"):
        return st
    # The session flips before the (long) login announcement finishes playing, so watch the state
    # first and only then wait for the durable record.
    opened = _poll_session(True, 60.0)
    st.check("session opened over RF", opened, opened, "True")
    kinds: list[str] = []
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        records, _ = log_tail_since(off)
        kinds = [r.get("event") or r.get("type") or "" for r in records]
        if "session_open" in kinds:
            break
        time.sleep(2.0)
    st.num("operating-log records", ",".join(k for k in kinds if k)[:120] or "none")
    st.check("logged in the operating log", "session_open" in kinds, kinds[:8] or "none",
             "session_open")
    # Leave the station logged out, so the next run starts from the same place this one did.
    st.check("logged back out", _set_session(False, st, "post-logout"), "closed", "session closed")
    return st


def _session_open() -> bool:
    try:
        return api(RADIO_BASE, "GET", "/status")[1].get("controller", {}).get("session_open") is True
    except Exception:
        return False


def _poll_session(want: bool, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _session_open() is want:
            return want
        time.sleep(1.0)
    return not want


def _set_session(want: bool, st: Stage, label: str) -> bool:
    """Drive the session to ``want`` over the API, waiting out the spoken announcement.

    Logging out is `99` — a real built-in that keys the radio and speaks, so this is not
    instantaneous; the wait has to cover the whole over.
    """
    if _session_open() is want:
        return True
    digit = "99" if not want else "01"
    # `99` speaks, and an announcement is still a transmission — so it goes through the same guard
    # as every other keying path here. This was the one hole left in it: `rf_witness` compares the
    # two radios' RECEIVE frequencies and would happily pass a station whose split is armed to a
    # real repeater's input, which is precisely what `bench_frequency_only` exists to refuse.
    # Guarded for BOTH digits: dispatching any service is a request that may key, and the guard
    # costs one /status read. Cheaper than reasoning about which built-ins currently speak.
    if not bench_frequency_only(RADIO_BASE, st, label):
        return False
    code, body = api(RADIO_BASE, "POST", f"/services/{digit}", timeout=120)
    if code != 200:
        st.fail(f"{label}: POST /services/{digit} -> {code} {str(body)[:80]}")
        return False
    got = _poll_session(want, 90.0)
    if got is not want:
        st.fail(f"{label}: session_open stayed {got}, wanted {want}")
        return False
    return True


def stage_tx() -> Stage:
    """DoD 6 — the K6 transmits and the kv4p measures real RF, not just a keyed register."""
    st = Stage("tx")
    if not rf_witness(st):
        return st
    seconds = 5.0
    tone = synth_tone(1000.0, (seconds - 1.0) * 1000.0, amplitude=0.6).samples

    carrier_polls: list[bool] = []

    async def watch_and_key():
        started = asyncio.Event()
        collector = asyncio.create_task(_collect_rx(KV4P_BASE, seconds + 1.0, started))

        async def poll():
            t0 = time.monotonic()
            while time.monotonic() - t0 < seconds + 1.0:
                try:
                    s = await asyncio.to_thread(api, KV4P_BASE, "GET", "/status", None, None, 4.0)
                    carrier_polls.append(bool(s[1].get("busy")))
                except Exception:
                    pass
                await asyncio.sleep(0.25)

        await started.wait()
        poller = asyncio.create_task(poll())
        await asyncio.sleep(0.3)
        await asyncio.to_thread(transmit, RADIO_BASE, tone, st, "tx-tone")
        cap = await collector
        poller.cancel()
        return cap

    cap = asyncio.run(watch_and_key())
    with_rf = sum(1 for c in carrier_polls if c)
    st.num("kv4p carrier polls", f"{with_rf} with RF / {len(carrier_polls)} total")
    st.check("kv4p saw carrier", with_rf > 0, with_rf, "> 0")
    st.check("kv4p received RMS", rms(cap.pcm) > 300, f"{rms(cap.pcm):.0f}", "> 300")
    st.check("kv4p recovered 1000 Hz", tone_power(cap.pcm, 1000.0) > 0.30,
             f"{tone_power(cap.pcm, 1000.0):.3f}", "> 0.30")
    st.num("kv4p audio", f"{len(cap.pcm)} B over a {cap.active:.2f}s active span")
    return st


def stage_services() -> Stage:
    """DoD 5b — a voice service is heard as received audio on the kv4p, not just logged."""
    st = Stage("services")
    if not rf_witness(st):
        return st
    code, services = api(RADIO_BASE, "GET", "/services")
    st.check("service catalog", code == 200 and bool(services), code, "200 + entries")
    digit = os.environ.get("ACCEPT_SERVICE_DIGIT", "02")  # 02 = time
    # This stage keys through the controller (`POST /services/{digit}`), not the `transmit` helper,
    # so it needs the bench-frequency guard explicitly — an announcement is still a transmission.
    if not bench_frequency_only(RADIO_BASE, st, f"services/{digit}"):
        return st

    async def run():
        started = asyncio.Event()
        collector = asyncio.create_task(_collect_rx(KV4P_BASE, 22.0, started))
        await started.wait()
        await asyncio.sleep(0.3)
        res = await asyncio.to_thread(api, RADIO_BASE, "POST", f"/services/{digit}", None, None, 60.0)
        return (await collector), res

    cap, (code, body) = asyncio.run(run())
    st.check(f"POST /services/{digit}", code == 200, f"{code} {str(body)[:90]}", "200")
    st.num("announcement audio", f"{len(cap.pcm)} B over a {cap.active:.1f}s active span")
    st.check("heard on the kv4p (RMS)", rms(cap.pcm) > 300, f"{rms(cap.pcm):.0f}", "> 300")
    st.check("speech-band energy", speech_band_ratio(cap.pcm) > 0.5,
             f"{speech_band_ratio(cap.pcm):.2f}", "> 0.50")
    st.check("announcement long enough", cap.active > 1.5, f"{cap.active:.1f}s", "> 1.5s")
    return st


#: The synthetic split channels these stages need in `radio.toml`. Bench frequencies only — a real
#: repeater's uplink is the operator's to key, never the runner's.
#:
#: **Both signs, because the field failure of ADR 0134 was invisible with only one.** The original
#: stage proved a +600 kHz split and nothing else, while 34 of the operator's 37 repeaters are
#: negative — so a duplex-sign fault anywhere below the preset layer would have passed the bench and
#: failed every repeater on the air. The minus channel is the exact mirror: the same two bench
#: frequencies with the legs swapped, so it exercises the negative branch while keying nothing new.
SPLIT_PRESET = "Bench Split"
SPLIT_RX_HZ = 445_800_000
SPLIT_TX_HZ = 446_400_000
SPLIT_MINUS_PRESET = "Bench Split Minus"
SPLIT_TONE_HZ = 100.0

#: Narrow enough to exclude DC and the voice band (`tone_power`'s 60 Hz default around 100 Hz spans
#: -40..160 Hz and would call a tone-less carrier "toned").
CTCSS_WIDTH_HZ = 5.0
#: Measured by `scripts/bench/ctcss_probe.py` on this bench: 0.000026 with the tone off, 0.109 with
#: it on — a 4250x ratio. This threshold sits ~10x above the floor and ~10x below the signal.
CTCSS_FLOOR = 0.01
#: How much louder the transmit leg must be than the receive leg for "the carrier moved" to mean
#: anything. A ratio, not silence: the two radios are inches apart, so 600 kHz of near-field bleed
#: is physics rather than a defect. Set from measurement, not guessed — see the ADR 0133 numbers.
SPLIT_LEG_RATIO = 3.0


def _watch_kv4p_while_keying(pcm: bytes, seconds: float, st: Stage, label: str):
    """Key the radio under test and return (kv4p capture, carrier polls) — the `stage_tx` shape."""
    polls: list[bool] = []

    async def run():
        started = asyncio.Event()
        collector = asyncio.create_task(_collect_rx(KV4P_BASE, seconds + 1.0, started))

        async def poll():
            t0 = time.monotonic()
            while time.monotonic() - t0 < seconds + 1.0:
                try:
                    s = await asyncio.to_thread(api, KV4P_BASE, "GET", "/status", None, None, 4.0)
                    polls.append(bool(s[1].get("busy")))
                except Exception:
                    pass
                await asyncio.sleep(0.25)

        await started.wait()
        poller = asyncio.create_task(poll())
        await asyncio.sleep(0.3)
        await asyncio.to_thread(transmit, RADIO_BASE, pcm, st, label)
        capture = await collector
        poller.cancel()
        return capture

    return asyncio.run(run()), polls


def stage_split() -> Stage:
    """ADR 0133 — a **positive**-offset repeater split (rx 445.800 / tx 446.400)."""
    return _split_stage("split", SPLIT_PRESET, SPLIT_RX_HZ, SPLIT_TX_HZ)


def stage_split_minus() -> Stage:
    """ADR 0134 — the same proof with a **negative** offset (rx 446.400 / tx 445.800).

    The shape 34 of the operator's 37 repeaters actually use, and the one the bench had never keyed.
    Nothing in the backend's split math is sign-sensitive on inspection — the offset is compared with
    `abs()`, both legs are validated as absolute frequencies, and the synthesiser word is unsigned —
    but "sign-agnostic on inspection" is not "measured", and a repeater that never opens is exactly
    the failure that does not announce itself.
    """
    return _split_stage("split-minus", SPLIT_MINUS_PRESET, SPLIT_TX_HZ, SPLIT_RX_HZ)


def _split_stage(name: str, preset_name: str, rx_hz: int, tx_hz: int) -> Stage:
    """ADR 0133 — a repeater split really moves the carrier, and carries the CTCSS up with it.

    Three claims, and the second is the one a one-legged test cannot make:

    1. Keyed on a split, the kv4p tuned to the **transmit** leg hears a clean carrier.
    2. Moved to the **receive** leg, it hears dramatically less. That is what proves the transmitter
       actually moved rather than the radio simply working as it always did.
    3. The CTCSS the repeater needs is on that carrier, not merely in a register.

    Parameterised over the two legs so the positive and negative stages are the *same* proof rather
    than a copy that could drift out of step with its twin (ADR 0134).
    """
    st = Stage(name)
    # `/capabilities` returns a bare JSON list of strings, not an object.
    advertised = api(RADIO_BASE, "GET", "/capabilities")[1]
    if not isinstance(advertised, list) or "set_split" not in advertised:
        st.skip(f"the radio under test does not advertise set_split (has: {advertised})")
        return st
    presets = api(RADIO_BASE, "GET", "/presets")[1].get("presets", [])
    split = next((p for p in presets if p["name"] == preset_name), None)
    if split is None:
        st.skip(
            f"no {preset_name!r} preset in radio.toml — add one (frequency {rx_hz}, "
            f"tx_frequency {tx_hz}, tx_tone {SPLIT_TONE_HZ}) so the split has something to "
            f"prove itself on that is not a real repeater"
        )
        return st
    # The preset is the runner's own fixture; if it has drifted, every measurement below is about a
    # channel this stage was not written for. Say so rather than quietly proving the wrong thing.
    # The TONE is part of the fixture too: without it, a drifted `tx_tone` surfaces as a failed CTCSS
    # check, which looks exactly like the backend having stopped putting CTCSS on the carrier — one
    # of the two things this cycle exists to make observable (ADR 0134).
    have = (split["frequency"], split.get("tx_frequency"), split.get("tx_tone"))
    if have != (rx_hz, tx_hz, SPLIT_TONE_HZ):
        st.fail(
            f"{preset_name!r} is rx {have[0]} / tx {have[1]} / tone {have[2]}, but this stage "
            f"proves rx {rx_hz} / tx {tx_hz} / tone {SPLIT_TONE_HZ}"
        )
        return st
    st.num("offset under test", f"{(tx_hz - rx_hz) / 1e6:+.3f} MHz")

    code, applied = api(RADIO_BASE, "POST", "/presets/apply", body={"name": preset_name})
    st.check(f"apply {preset_name!r}", code == 200, code, "200")
    # From here the station is off its home channel and may have a split armed, so EVERY exit has to
    # go through the restore. The plus stage was accidentally immune — its RX leg *is* home, 445.800
    # — but the minus stage listens on 446.400, where a bail would strand the station off the only
    # frequency the kv4p can hear and make every later stage skip or fail for the wrong reason.
    try:
        state = api(RADIO_BASE, "GET", "/status")[1]
        st.check("listening on the RX leg", state.get("frequency") == split["frequency"],
                 state.get("frequency"), str(split["frequency"]))
        st.check("armed on the TX leg", state.get("tx_frequency") == split["tx_frequency"],
                 state.get("tx_frequency"), str(split["tx_frequency"]))
        if state.get("tx_frequency") != split["tx_frequency"]:
            return st  # nothing below means anything without the split actually armed

        seconds = 5.0
        tone = synth_tone(1000.0, (seconds - 1.0) * 1000.0, amplitude=0.6).samples
        measured: dict[str, tuple[float, int]] = {}
        try:
            for leg, hz in (("tx", split["tx_frequency"]), ("rx", split["frequency"])):
                code, _ = api(KV4P_BASE, "POST", "/frequency", body={"hz": hz})
                if code != 200:
                    st.fail(f"could not tune the witness to the {leg} leg ({hz} Hz): HTTP {code}")
                    return st
                time.sleep(3.0)  # a kv4p retune cycles its receiver; let it settle (stage_presets)
                cap, polls = _watch_kv4p_while_keying(tone, seconds, st, f"{name}-{leg}")
                measured[leg] = (rms(cap.pcm), sum(polls))
                if leg == "tx":
                    st.num("kv4p carrier polls (TX leg)", f"{sum(polls)} with RF / {len(polls)} total")
                    st.check("kv4p saw carrier on the TX leg", sum(polls) > 0, sum(polls), "> 0")
                    st.check("kv4p received RMS", rms(cap.pcm) > 300, f"{rms(cap.pcm):.0f}", "> 300")
                    st.check("kv4p recovered 1000 Hz", tone_power(cap.pcm, 1000.0) > 0.30,
                             f"{tone_power(cap.pcm, 1000.0):.3f}", "> 0.30")
                    ctcss = tone_power(cap.pcm, SPLIT_TONE_HZ, width=CTCSS_WIDTH_HZ)
                    st.check(f"{SPLIT_TONE_HZ:.0f} Hz CTCSS on the carrier", ctcss > CTCSS_FLOOR,
                             f"{ctcss:.6f}", f"> {CTCSS_FLOOR}")
        finally:
            api(KV4P_BASE, "POST", "/frequency", body={"hz": SPLIT_RX_HZ})

        tx_rms, _ = measured.get("tx", (0.0, 0))
        rx_rms, _ = measured.get("rx", (0.0, 0))
        ratio = tx_rms / rx_rms if rx_rms else float("inf")
        st.num("TX leg vs RX leg", f"RMS {tx_rms:.0f} on {tx_hz}, {rx_rms:.0f} on {rx_hz}")
        # THE check. Everything above is also true of a radio that ignored the split entirely and
        # transmitted on the frequency it was listening on.
        st.check("the carrier moved to the TX leg", ratio > SPLIT_LEG_RATIO,
                 f"{ratio:.1f}x", f"> {SPLIT_LEG_RATIO}x")
    finally:
        _restore_bench_home(presets, st)
    return st


def _restore_bench_home(presets: list[dict], st: Stage) -> None:
    """Put the radio back on 445.800 simplex, split disarmed, and check that it landed.

    Home is always 445.800 — the shared bench channel and the only frequency the kv4p can hear — not
    whichever leg the calling stage was listening on. Every stage after a split stage expects to
    find the pair there, and an armed split left behind would key the *next* stage somewhere it
    never intended (the reason `bench_frequency_only` reads `tx_frequency` first).
    """
    home = next(
        (p for p in presets if p["frequency"] == SPLIT_RX_HZ and not p.get("tx_frequency")), None
    )
    if home:
        api(RADIO_BASE, "POST", "/presets/apply", body={"name": home["name"]})
    else:
        api(RADIO_BASE, "POST", "/split", body={"tx_hz": None})
        api(RADIO_BASE, "POST", "/frequency", body={"hz": SPLIT_RX_HZ})
    after = api(RADIO_BASE, "GET", "/status")[1]
    st.check("split disarmed afterwards", after.get("tx_frequency") is None,
             after.get("tx_frequency"), "null")
    st.check("back on 445.800", after.get("frequency") == SPLIT_RX_HZ,
             after.get("frequency"), str(SPLIT_RX_HZ))


STAGES = {
    "systemd": stage_systemd,
    "web": stage_web,
    "presets": stage_presets,
    "rx": stage_rx,
    "dtmf": stage_dtmf,
    "auth": stage_auth,
    "tx": stage_tx,
    "split": stage_split,
    "split-minus": stage_split_minus,
    "services": stage_services,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", nargs="+", metavar="STAGE", help="run only these stages")
    parser.add_argument("--skip", nargs="+", metavar="STAGE", default=[], help="skip these stages")
    parser.add_argument("--list", action="store_true", help="list stage names and exit")
    args = parser.parse_args(argv)
    if args.list:
        print(" ".join(STAGES))
        return 0
    if not TOKEN:
        print("RADIO_API_TOKEN is not set", file=sys.stderr)
        return 2
    names = [n for n in (args.only or STAGES) if n not in args.skip]
    unknown = [n for n in names if n not in STAGES]
    if unknown:
        print(f"unknown stage(s): {unknown}; known: {list(STAGES)}", file=sys.stderr)
        return 2

    print(f"radio-server bench acceptance — {time.strftime('%Y-%m-%dT%H:%M:%S%z')}")
    print(f"  radio under test : {RADIO_BASE}")
    print(f"  measuring radio  : {KV4P_BASE}")
    print()
    results: list[Stage] = []
    for name in names:
        print(f"[{name}]", flush=True)
        try:
            st = STAGES[name]()
        except Exception as exc:
            st = Stage(name, ok=False, notes=[f"  XX raised {type(exc).__name__}: {exc}"])
        results.append(st)
        for line in st.notes:
            print(line)
        print(f"  -> {stage_verdict(st)}\n", flush=True)

    width = max(len(s.name) for s in results)
    print("summary")
    # Informational: includes the restart this runner performs and the end of every keyed over.
    print(f"  {'(reader stalls in run)':<{width}}  {journal_xruns(UNIT, RUN_START)}  (not a verdict)")
    for s in results:
        print(f"  {s.name:<{width}}  {stage_verdict(s)}")
    skipped = [s.name for s in results if s.skipped]
    if skipped:
        # A skipped stage is not a pass. Exit 0 would read as "acceptance is green" to anyone
        # scripting this, when in fact the RF stages never ran.
        print(f"\n  {len(skipped)} stage(s) could not be attempted: {' '.join(skipped)}")
    label, code = overall_verdict(results)
    print(f"\nRESULT: {label}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
