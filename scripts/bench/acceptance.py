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

Usage::

    RADIO_API_TOKEN=... .venv/bin/python scripts/bench/acceptance.py            # every stage
    RADIO_API_TOKEN=... .venv/bin/python scripts/bench/acceptance.py --only rx tx
    RADIO_API_TOKEN=... .venv/bin/python scripts/bench/acceptance.py --list

Exit code is 0 only when every selected stage passes.
"""

from __future__ import annotations

import argparse
import asyncio
import http.client
import json
import os
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


def journal_xruns(unit: str, since: str) -> int:
    out = subprocess.run(
        ["journalctl", "--user", "-u", unit, "--since", since, "--no-pager"],
        capture_output=True, text=True,
    ).stdout
    return sum(1 for line in out.splitlines() if "xrun" in line)


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
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if api(base, "GET", "/status", timeout=4)[0] == 200:
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


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


def transmit(base: str, pcm: bytes, stage: Stage, label: str) -> bool:
    """One-shot keyed transmission of raw PCM (``POST /transmit`` keys, plays, unkeys)."""
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

    # A stop must stay clean with clients attached — an idle browser tab holding /audio/rx open
    # is exactly what used to wedge the stop for 20 s and earn a SIGKILL.
    async def hold_and_stop():
        async with websockets.connect(ws_url(RADIO_BASE, "/audio/rx"), ssl=_SSL, max_size=None) as a:
            async with websockets.connect(ws_url(RADIO_BASE, "/events"), ssl=_SSL) as b:
                await asyncio.wait_for(a.recv(), timeout=10)
                await asyncio.wait_for(b.recv(), timeout=10)
                t0 = time.monotonic()
                await asyncio.to_thread(
                    subprocess.run, ["systemctl", "--user", "stop", UNIT], check=False
                )
                return time.monotonic() - t0

    try:
        elapsed = asyncio.run(hold_and_stop())
    except Exception as exc:  # the sockets die with the server; that is the expected ending
        elapsed = -1.0
        st.notes.append(f"    (hold sockets ended: {type(exc).__name__})")
    result = subprocess.run(
        ["systemctl", "--user", "show", UNIT, "-p", "Result", "--value"],
        capture_output=True, text=True,
    ).stdout.strip()
    st.check("stop under WS load: result", result == "success", result, "success")
    if elapsed >= 0:
        st.check("stop under WS load: seconds", elapsed < 15.0, f"{elapsed:.2f}s", "< 15s")
    subprocess.run(["systemctl", "--user", "start", UNIT], check=False)
    st.check("restarted healthy", wait_healthy(RADIO_BASE), "up", "HTTP 200 on /status")
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
    # Put the bench back on the shared 445.800 working frequency for the RF stages.
    home = next((p for p in presets if p["frequency"] == 445_800_000), None)
    if home:
        api(RADIO_BASE, "POST", "/presets/apply", body={"name": home["name"]})
    back = api(RADIO_BASE, "GET", "/status")[1].get("frequency")
    st.check("restored to 445.800", back == 445_800_000, back, "445800000")
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
    st.check("ALSA xruns while receiving", journal_xruns(UNIT, since) == 0,
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


STAGES = {
    "systemd": stage_systemd,
    "web": stage_web,
    "presets": stage_presets,
    "rx": stage_rx,
    "dtmf": stage_dtmf,
    "auth": stage_auth,
    "tx": stage_tx,
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
        verdict = "SKIP" if st.skipped else ("PASS" if st.ok else "FAIL")
        print(f"  -> {verdict}\n", flush=True)

    width = max(len(s.name) for s in results)
    print("summary")
    # Informational: includes the restart this runner performs and the end of every keyed over.
    print(f"  {'(xruns anywhere in run)':<{width}}  {journal_xruns(UNIT, RUN_START)}  (not a verdict)")
    for s in results:
        print(f"  {s.name:<{width}}  {'SKIP' if s.skipped else ('PASS' if s.ok else 'FAIL')}")
    ok = all(s.ok for s in results)
    skipped = [s.name for s in results if s.skipped]
    if skipped:
        # A skipped stage is not a pass. Exit 0 would read as "acceptance is green" to anyone
        # scripting this, when in fact the RF stages never ran.
        ok = False
        print(f"\n  {len(skipped)} stage(s) could not be attempted: {' '.join(skipped)}")
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
