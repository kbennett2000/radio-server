"""``python -m radio_server.doctor`` — AIOC/Baofeng or kv4p HT hardware diagnostic (ADR 0029/0061).

Which backend it diagnoses is resolved from ``server.backend`` (or the ``--backend`` override):
``kv4p`` gets the kv4p checks, anything else falls back to the AIOC/Baofeng checks (the default, since
``server.backend`` starts as ``mock`` and the AIOC bring-up runs doctor before flipping it). Everything
here is safe to run anywhere — it never keys the transmitter and degrades gracefully (a clear FAIL line)
when the ``hardware`` extra or the device is absent, so it also runs harmlessly in CI.

**AIOC/Baofeng.** Read-only checks that answer "is the AIOC ready?": the USB sound card enumerates with
48 kHz capture + playback, the PTT serial port exists and opens, and the current user can reach it
(``dialout``).

**kv4p HT.** There is no sound card — everything (RX/TX audio, tuning, PTT) rides one UART — so the
default check is a **connect probe** instead: it opens the port, runs the transport handshake, and
prints what the board reported (HELLO banner, DeviceState, decoded flags). It **does not key**, but it
is **not read-only**: shipped firmware overwrites and persists its whole desired state on any host frame
(ADR 0066), so the probe performs a config-preserving handshake — it restores the board's tuned
frequency/CTCSS and re-enables status reports, leaving TX-allow/filter flags at safe defaults (TX stays
off). A board already streaming reports is read with zero writes. **Run it first on bench day**: it
settles a pile of "verify against hardware" unknowns in one shot — the windowSize default (2048),
whether pyserial's open resets the board (did a HELLO arrive unbidden?), and the real RF module band.
``--key-test`` on kv4p is a KEYING test (there is no serial line to bisect): it reconciles PTT
on, asserts the device reports TX_ACTIVE, holds, and drops — exercising the TX_ALLOWED gate (0063).
Running ``--dtmf`` on kv4p is the bench measurement that settles the arc's oldest open question — DTMF
through the lossy Opus codec (ADR 0064/0065) against the native Goertzel decoder (open since cycle 1);
it is a measurement, not a code change.

Two audio-level modes help tune the levels once the plumbing works (ADR 0029 bring-up):
``--rx-level`` reads the capture for a few seconds and reports the received RMS/peak against the
squelch (VAD) threshold — read-only, no keying. ``--tx-tone`` plays a test tone out the radio so a
second receiver can confirm TX audio; it is RF and carries the same dummy-load guard as ``--key-test``.

The RF paths (``--key-test``, ``--tx-tone``) are opt-in, refuse to run non-interactively (guardrail:
never key the radio unattended), demand a typed ``CONFIRM``, and key for a hard-capped duration.
"""

from __future__ import annotations

import argparse
import glob
import math
import os
import sys
import time
from dataclasses import dataclass

from .activity.gate import frame_rms
from .audio import (
    DEFAULT_DTMF_CHUNK_BYTES,
    CANONICAL_FORMAT,
    AudioFrame,
    BufferedDtmfInput,
)
from .backends import create_radio
from .backends.aioc_baofeng import (
    DEFAULT_BLOCKSIZE,
    DEFAULT_INPUT_DEVICE,
    DEFAULT_OUTPUT_DEVICE,
    DEFAULT_PTT_LINE,
    DEFAULT_SERIAL_PORT,
    PttLine,
)
from .backends.kv4p.radio import Kv4pBand

_AIOC_NAME_HINTS = ("AllInOneCable", "All-In-One-Cable", "AIOC")
_KEY_TEST_SECONDS = 2.0  # hard cap on how long --key-test holds the line
_TX_TONE_MAX_SECONDS = 5.0  # hard cap on how long --tx-tone keys the radio
_INT16_FULL_SCALE = 32768.0
#: How close doctor's measured RX correction must be to the configured one before it stops nagging
#: (ADR 0071). 0.2% — a DTMF bin is only ~39 Hz wide, so a larger residual is worth correcting.
_RATE_MATCH_TOL = 0.002
#: Below this block RMS the capture is effectively silent — no signal is arriving (a volume / ALSA
#: mixer problem), as opposed to a signal that is arriving but sitting under the squelch threshold.
_RX_SILENCE_RMS = 50.0


def _dbfs(rms: float) -> str:
    """Format an int16 RMS/peak as dBFS (full scale 32768); '-inf' for silence."""
    if rms <= 0:
        return "-inf dBFS"
    return f"{20 * math.log10(rms / _INT16_FULL_SCALE):.1f} dBFS"


@dataclass(frozen=True)
class RxLevels:
    """Summary of a short capture: frame count, loudest block RMS, peak sample, overall RMS."""

    frames: int
    total_samples: int
    peak_sample: int  # max |sample|, int16 units (0..32767)
    peak_block_rms: float  # RMS of the loudest ~20 ms block
    avg_rms: float  # RMS across the whole capture
    elapsed: float = 0.0  # wall-clock the capture actually ran, for the frame-rate estimate


def measure_rx_levels(radio, *, seconds: float, clock=None) -> RxLevels:
    """Read ``radio.receive()`` for ``seconds`` and summarize the received level (RMS/peak).

    Pure and hardware-agnostic: any object with ``receive() -> AudioFrame`` works, so a test drives it
    with a ``MockRadio`` scripted with known frames and an injected ``clock`` (no real sleeps). Reuses
    :func:`radio_server.activity.gate.frame_rms` — the same energy primitive the squelch gate uses — so
    the number reported here is directly comparable to ``audio.vad_on_rms``.
    """
    import numpy as np

    if clock is None:
        import time

        clock = time.monotonic
    start = clock()
    frames = 0
    total_samples = 0
    sum_sq = 0.0
    peak_sample = 0
    peak_block_rms = 0.0
    while clock() - start < seconds:
        frame = radio.receive()
        if not frame.samples:
            continue
        n = len(frame.samples) // 2  # s16le → 2 bytes/sample
        if n == 0:
            continue
        block_rms = frame_rms(frame)
        frames += 1
        total_samples += n
        sum_sq += block_rms * block_rms * n  # block_rms² · n == that block's Σ(sample²)
        peak_block_rms = max(peak_block_rms, block_rms)
        peak_sample = max(peak_sample, int(np.abs(np.frombuffer(frame.samples, dtype="<i2")).max()))
    elapsed = clock() - start
    avg_rms = math.sqrt(sum_sq / total_samples) if total_samples else 0.0
    return RxLevels(frames, total_samples, peak_sample, peak_block_rms, avg_rms, elapsed)


def collect_dtmf(
    radio,
    decoder,
    framer,
    *,
    seconds: float,
    chunk_bytes: int = DEFAULT_DTMF_CHUNK_BYTES,
    clock=None,
    on_event=None,
    dedup: bool = True,
) -> tuple[str, list[str]]:
    """Listen for ``seconds``, decode DTMF from accumulated audio, and frame digits into entries.

    Accumulation is the whole point: a single ~20 ms ``receive()`` block is too short for multimon to
    detect a tone, so frames are buffered until ``chunk_bytes`` (~0.5 s) and decoded as one
    :class:`AudioFrame`. Each decoded key is fed to ``framer`` (``#`` submits an entry, ``*`` clears).

    ``dedup`` (default on) collapses a **held tone** to a single keypress: multimon re-emits the same
    digit for as long as a key is held (and a tone can straddle chunk boundaries), so consecutive
    identical detections are suppressed until a **silent chunk** (no tone = a gap) resets the run — so
    a genuinely repeated key (e.g. "55" in a code) still registers twice as long as there is a pause
    between the presses. Without a pause the two cannot be told apart from one held key (a fundamental
    limit of per-chunk decoding; smaller chunks resolve shorter gaps).

    ``on_event(kind, value)`` — ``kind`` in ``{"digit", "entry"}`` — is called live for the caller to
    print. Returns ``(raw_digits, entries)``. Pure/hardware-agnostic: a test drives it with a
    ``MockRadio`` + a fake decoder + an injected clock (the same shape as :func:`measure_rx_levels`).

    Thin driver over :class:`~radio_server.audio.BufferedDtmfInput` — the accumulate-and-dedup core the
    live controller also runs (ADR 0030), so this diagnostic exercises the exact decode path the server
    uses. This function just runs it for a fixed duration and adapts its output to ``on_event``.
    """
    if clock is None:
        import time

        clock = time.monotonic
    raw: list[str] = []
    entries: list[str] = []

    def _on_digit(digit: str) -> None:
        raw.append(digit)
        if on_event is not None:
            on_event("digit", digit)

    dtmf = BufferedDtmfInput(
        decoder, framer, window_bytes=chunk_bytes, dedup=dedup, on_digit=_on_digit
    )

    def _collect(new_entries: list[str]) -> None:
        for entry in new_entries:
            entries.append(entry)
            if on_event is not None:
                on_event("entry", entry)

    _drive_dtmf(radio, dtmf, seconds=seconds, clock=clock, collect=_collect)
    return "".join(raw), entries


def _drive_dtmf(radio, dtmf, *, seconds: float, clock, collect) -> None:
    """Run the decode loop: pump each `receive()` for ``seconds``, then flush the tail.

    Shared by `collect_dtmf` (buffered) and the streaming `--dtmf` path so both diagnostics drive the
    same accumulate/decode loop as the live controller. ``collect(new_entries)`` receives each pump's
    completed entries; the per-digit hook is wired into ``dtmf`` by the caller.
    """
    start = clock()
    while True:
        now = clock()
        if now - start >= seconds:
            break
        collect(dtmf.pump(radio.receive(), now))
    collect(dtmf.flush(clock()))  # decode whatever is left in the tail buffer


class _Report:
    """Accumulates PASS/FAIL/WARN lines and prints a compact table; tracks overall success."""

    def __init__(self) -> None:
        self.ok = True

    def pas(self, label: str, detail: str = "") -> None:
        print(f"  [PASS] {label}{f' — {detail}' if detail else ''}")

    def warn(self, label: str, detail: str = "") -> None:
        print(f"  [WARN] {label}{f' — {detail}' if detail else ''}")

    def fail(self, label: str, detail: str = "") -> None:
        self.ok = False
        print(f"  [FAIL] {label}{f' — {detail}' if detail else ''}")


def _doctor_settings():
    """Load the operator's ``radio.toml`` — the same file the server runs from.

    ``load_settings()`` with *no path* resolves to pure defaults; it never touches the file. Every
    doctor config helper below called it that way, so doctor was silently ignoring the operator's real
    settings and pointing a bench check — including ``--key-test`` — at the **default** serial port,
    band, and frequency rather than the configured ones (ADR 0069; on this bench ``/dev/ttyUSB0`` is a
    different device entirely). Doctor must read :data:`DEFAULT_CONFIG_PATH`, like ``radio_server.__main__``.
    """
    from .config import DEFAULT_CONFIG_PATH, load_settings

    return load_settings(DEFAULT_CONFIG_PATH)


def _validate_doctor_backend_config(backend: str) -> str | None:
    """Validate the selected backend's config block against the real ``radio.toml`` (ADR 0074).

    Returns a FAIL message if the ``[<backend>]`` block is broken (e.g. `audio.squelch=cat` with no
    busy line, or an out-of-band `kv4p.frequency`), else ``None``. Runs the pure config-layer checks
    up front — with ``include_construction_checks=True``, since we are about to build this backend —
    so a config error surfaces here instead of silently falling back to defaults (the ADR 0069 failure
    mode) or only erroring deep in device construction (which, with no hardware, never reaches the
    frequency check). A genuinely unreadable file is left for the build path to report.
    """
    from .api.backend_config import validate_backend_config

    try:
        settings = _doctor_settings()
    except Exception:
        return None
    try:
        validate_backend_config(settings, backend, include_construction_checks=True)
    except RuntimeError as exc:
        return str(exc)
    return None


def _baofeng_config() -> dict:
    """Resolve the baofeng settings (from radio.toml if present), falling back to module defaults."""
    cfg = {
        "serial_port": DEFAULT_SERIAL_PORT,
        "ptt_line": str(DEFAULT_PTT_LINE),
        "input_device": DEFAULT_INPUT_DEVICE,
        "output_device": DEFAULT_OUTPUT_DEVICE,
        "blocksize": DEFAULT_BLOCKSIZE,
    }
    try:
        s = _doctor_settings()
        for key in cfg:
            cfg[key] = s.get(f"baofeng.{key}")
    except Exception:
        pass  # no config / unreadable — defaults are a fine diagnostic baseline
    cfg["backend"] = "baofeng"  # tag so _build_backend can dispatch (added last: not a baofeng.* key)
    return cfg


def _kv4p_config() -> dict:
    """Resolve the kv4p settings (from radio.toml if present), falling back to module defaults.

    Mirrors :func:`_baofeng_config`. ``frequency`` is optional (``None`` = leave the device on its
    NVS frequency); the rest carry the backend's marked verify-on-bench defaults.
    """
    from .backends.kv4p.radio import (
        DEFAULT_HIGH_POWER,
        DEFAULT_MODULE_TYPE,
        DEFAULT_SAMPLE_RATE_CORRECTION,
        DEFAULT_SERIAL_PORT as KV4P_DEFAULT_SERIAL_PORT,
        DEFAULT_SQUELCH,
        DEFAULT_TX_ALLOWED,
        DEFAULT_TX_LEAD_SECONDS,
    )

    cfg = {
        "backend": "kv4p",
        "serial_port": KV4P_DEFAULT_SERIAL_PORT,
        "module_type": DEFAULT_MODULE_TYPE,
        "squelch": DEFAULT_SQUELCH,
        "tx_lead_seconds": DEFAULT_TX_LEAD_SECONDS,
        "high_power": DEFAULT_HIGH_POWER,
        "tx_allowed": DEFAULT_TX_ALLOWED,
        "frequency": None,
        "sample_rate_correction": DEFAULT_SAMPLE_RATE_CORRECTION,
    }
    try:
        s = _doctor_settings()
        keys = ("serial_port", "module_type", "squelch", "tx_lead_seconds", "high_power",
                "tx_allowed", "frequency", "sample_rate_correction")
        for key in keys:
            cfg[key] = s.get(f"kv4p.{key}")
    except Exception:
        pass  # no config / unreadable — defaults are a fine diagnostic baseline
    return cfg


def _mumble_config(entry_name: str | None = None) -> dict:
    """Resolve one ``[[mumble.servers]]`` entry + its password secret (ADR 0042).

    ``entry_name`` selects an entry by name; empty/None picks the sole entry, else the
    ``autoconnect`` one. When the choice stays ambiguous the caller gets the configured names
    (``names``) to report; an unknown name lands in ``error``.
    """
    from .link import (
        DEFAULT_MUMBLE_CHANNEL,
        DEFAULT_MUMBLE_PORT,
        link_username,
        mumble_password_secret,
        resolve_mumble_entries,
    )

    cfg: dict = {
        "host": "",
        "port": DEFAULT_MUMBLE_PORT,
        "username": link_username(None),
        "channel": DEFAULT_MUMBLE_CHANNEL,
        "password": "",
        "name": None,
        "names": [],
        "error": None,
    }
    try:
        from .config import DEFAULT_CONFIG_PATH, load_mumble_servers, load_secrets

        # The same nick the server presents (entries.link_username): the callsign when set.
        try:
            settings = _doctor_settings()
            if settings.is_set("station.callsign"):
                cfg["username"] = link_username(settings.get("station.callsign"))
        except Exception:
            pass  # no callsign / unreadable config — the bare default nick still diagnoses fine

        entries = resolve_mumble_entries(load_mumble_servers(DEFAULT_CONFIG_PATH))
        cfg["names"] = [entry.name for entry in entries]
        chosen = None
        if entry_name:
            # Match by display name or derived slug (ADR 0052) — either spelling diagnoses.
            chosen = next(
                (e for e in entries if entry_name in (e.name, e.slug)), None
            )
            if chosen is None:
                cfg["error"] = (
                    f"unknown mumble entry {entry_name!r}; configured: "
                    f"{', '.join(cfg['names']) or '(none)'}"
                )
        elif len(entries) == 1:
            chosen = entries[0]
        else:
            chosen = next((e for e in entries if e.autoconnect), None)
        if chosen is not None:
            cfg.update(
                host=chosen.host,
                port=chosen.port,
                channel=chosen.channel,
                name=chosen.name,
            )
            # Same precedence as the live client factory: secrets override the entry's
            # plaintext password field (ADR 0052).
            cfg["password"] = (
                load_secrets().get(mumble_password_secret(chosen.slug))
                or chosen.password
                or ""
            )
    except Exception:
        pass  # no config / unreadable — flags or defaults are a fine diagnostic baseline
    return cfg


def _check_audio(report: _Report, input_device, output_device) -> None:
    print("Audio (AIOC USB sound card):")
    try:
        import sounddevice as sd
    except ImportError:
        report.fail(
            "sounddevice not installed",
            "install the hardware extra: pip install 'radio-server[hardware]' (+ system libportaudio2)",
        )
        return
    except OSError:  # sounddevice imports but PortAudio (libportaudio2) is missing
        report.fail(
            "PortAudio library not found",
            "install the system lib: sudo apt install libportaudio2",
        )
        return
    try:
        devices = sd.query_devices()
    except Exception as exc:  # PortAudio init failure (no libportaudio2, no audio system, ...)
        report.fail("could not query audio devices", str(exc))
        return

    hits = [
        (i, d)
        for i, d in enumerate(devices)
        if any(h in d.get("name", "") for h in _AIOC_NAME_HINTS)
    ]
    if not hits:
        report.fail("AIOC sound card not found", "is the cable plugged in and enumerated?")
        return
    # Show every AIOC-matching PortAudio device, so any ambiguity (e.g. a PulseAudio-wrapped copy)
    # is visible and the operator can pick an explicit index if the name substring is ambiguous.
    for i, d in hits:
        report.pas(
            "AIOC audio device",
            f"index {i}: {d['name']!r} (in={d.get('max_input_channels', 0)}, "
            f"out={d.get('max_output_channels', 0)})",
        )
    cap_idx = next((i for i, d in hits if d.get("max_input_channels", 0) > 0), None)
    out_idx = next((i for i, d in hits if d.get("max_output_channels", 0) > 0), None)

    for kind, device, fallback_idx, checker in (
        ("capture", input_device, cap_idx, sd.check_input_settings),
        ("playback", output_device, out_idx, sd.check_output_settings),
    ):
        try:
            checker(device=device, samplerate=48000, channels=1, dtype="int16")
            report.pas(f"48 kHz {kind} accepted", f"configured device={device!r}")
            continue
        except Exception as exc:
            # The configured value did not resolve — try the discovered index and, if that works,
            # tell the operator exactly what to put in config.
            if fallback_idx is not None:
                try:
                    checker(device=fallback_idx, samplerate=48000, channels=1, dtype="int16")
                    key = "input_device" if kind == "capture" else "output_device"
                    report.fail(
                        f"configured {kind} device={device!r} did not resolve",
                        f"the card works at index {fallback_idx} — set baofeng.{key} = "
                        f"{fallback_idx} (or a unique name substring)",
                    )
                    continue
                except Exception:
                    pass
            report.fail(
                f"48 kHz {kind} not accepted",
                f"{exc} (is PulseAudio/PipeWire holding the card? try an explicit index)",
            )


def _check_serial(report: _Report, port: str, ptt_line: str) -> None:
    print("Serial (PTT line):")
    byid = glob.glob("/dev/serial/by-id/*All-In-One-Cable*") or glob.glob(
        "/dev/serial/by-id/*AIOC*"
    )
    if byid:
        report.pas("stable by-id path present", byid[0])
    else:
        report.warn("no /dev/serial/by-id AIOC symlink", "using the raw device path")

    if os.path.exists(port):
        report.pas("serial device exists", port)
    else:
        report.fail("serial device missing", f"{port} (cable plugged in? correct path?)")
        return

    try:
        import serial  # pyserial
    except ImportError:
        report.fail(
            "pyserial not installed",
            "install the hardware extra: pip install 'radio-server[hardware]'",
        )
        return
    try:
        handle = serial.Serial()
        handle.port = port
        handle.dtr = False  # hold both lines low on open — never key on a diagnostic
        handle.rts = False
        handle.open()
        handle.close()
        report.pas("serial port opens (no keying)", f"PTT line configured: {ptt_line}")
    except PermissionError:
        report.fail(
            "permission denied opening the serial port",
            "add yourself to the 'dialout' group: sudo usermod -aG dialout $USER (then re-login)",
        )
    except Exception as exc:
        report.fail("could not open the serial port", str(exc))


def _key_test(port: str, ptt_line: str) -> int:
    """Interactive RF verification of which line keys PTT. Refuses to run unattended."""
    if not sys.stdin.isatty() or os.environ.get("CI"):
        print(
            "REFUSING --key-test: not an interactive terminal (RF safety — this keys the "
            "transmitter and must never run unattended or in CI).",
            file=sys.stderr,
        )
        return 2
    try:
        line = PttLine(str(ptt_line).lower())
    except ValueError:
        print(f"invalid ptt_line {ptt_line!r}; choose 'rts' or 'dtr'.", file=sys.stderr)
        return 2

    print("=" * 72)
    print("  RF KEY TEST — this WILL key the transmitter.")
    print("  Connect a DUMMY LOAD (or be certain it is safe to transmit) before continuing.")
    print(f"  It will assert the {line.value.upper()} line on {port} for ~{_KEY_TEST_SECONDS:.0f}s.")
    print("=" * 72)
    if input("Type CONFIRM (all caps) to proceed: ").strip() != "CONFIRM":
        print("Aborted — nothing was keyed.")
        return 1

    import time

    import serial  # pyserial

    handle = serial.Serial()
    handle.port = port
    handle.dtr = False
    handle.rts = False
    handle.open()
    try:
        setattr(handle, line.value, True)
        print(f"{line.value.upper()} asserted — watch the radio's TX LED / dummy load...")
        time.sleep(_KEY_TEST_SECONDS)
    finally:
        setattr(handle, line.value, False)
        handle.close()
    print("Line dropped.")

    keyed = input(f"Did the radio key up on {line.value.upper()}? [y/n]: ").strip().lower()
    if keyed.startswith("y"):
        print(f"CONFIRMED: {line.value.upper()} keys PTT. Set baofeng.ptt_line = '{line.value}'.")
        return 0
    other = PttLine.DTR if line is PttLine.RTS else PttLine.RTS
    print(
        f"{line.value.upper()} did NOT key. Re-run with --ptt-line {other.value} to test the other "
        f"line, and set baofeng.ptt_line accordingly."
    )
    return 1


def _check_kv4p_serial(report: _Report, port: str) -> None:
    """Port + dialout reachability for the kv4p UART (the AIOC serial check, CP210x/CH340 shaped).

    Mirrors :func:`_check_serial` (by-id symlink, device exists, opens with lines held low so the
    ESP32 does not auto-reset — ADR 0062), minus the PTT-line concept the kv4p does not have.
    """
    print("Serial (kv4p UART — RX/TX audio, tuning and PTT all ride this one port):")
    byid = glob.glob("/dev/serial/by-id/*CP210*") or glob.glob("/dev/serial/by-id/*CH340*")
    if byid:
        report.pas("stable by-id path present", byid[0])
    else:
        report.warn("no /dev/serial/by-id CP210x/CH340 symlink", "using the raw device path")

    if os.path.exists(port):
        report.pas("serial device exists", port)
    else:
        report.fail(
            "serial device missing",
            f"{port} (board plugged in? correct path? kv4p is /dev/ttyUSB*, not the AIOC's ttyACM*)",
        )
        return

    try:
        import serial  # pyserial
    except ImportError:
        report.fail(
            "pyserial not installed",
            "install the hardware extra: pip install 'radio-server[hardware]'",
        )
        return
    try:
        handle = serial.Serial()
        handle.port = port
        handle.dtr = False  # hold both lines low on open — avoid the ESP32 auto-reset (ADR 0062)
        handle.rts = False
        handle.open()
        handle.close()
        report.pas("serial port opens (no ESP32 reset)", "DTR/RTS held low")
    except PermissionError:
        report.fail(
            "permission denied opening the serial port",
            "add yourself to the 'dialout' group: sudo usermod -aG dialout $USER (then re-login)",
        )
    except Exception as exc:
        report.fail("could not open the serial port", str(exc))


# Pre-KISS firmware frames its serial output with this delimiter; the KISS protocol (ADR 0064) replaced
# it. A board on pre-KISS firmware fails the handshake *silently* — no FEND, no KV4P prefix ever appears
# — so on a failed connect we sniff the raw wire for this signature. Bench-confirmed this cycle; a wire
# fact, so it stays a marked constant (guardrail 1).
_PRE_KISS_DELIMITER = b"\xde\xad\xbe\xef"
_PRE_KISS_SNIFF_SECONDS = 1.5


def _sniff_pre_kiss_firmware(port: str, *, seconds: float = _PRE_KISS_SNIFF_SECONDS, _open=None) -> bool:
    """After a failed handshake, sniff the raw UART for the pre-KISS firmware's frame delimiter.

    Opening the port resets the ESP32 (reset-on-open, even with DTR/RTS held low — ADR 0066), so a
    pre-KISS board dumps its old-protocol boot frames right here. We report pre-KISS only on a positive
    tell: the ``de ad be ef`` delimiter present **and** no KISS FEND (``0xC0``) **and** no ``KV4P``
    vendor prefix anywhere in the window. The boot banner is deliberately NOT used as the tell — it
    exists in both firmwares. On any inability to open/read we return ``False`` (stay inconclusive and
    let the caller print the generic handshake-failure line).

    ``_open`` is a test seam (a callable returning an open pyserial-like handle); when ``None`` this
    opens the real port with DTR/RTS held low, reads for ``seconds``, and closes it.
    """
    from .backends.kv4p.frames import KISS_FEND, KV4P_VENDOR_PREFIX

    def _default_open():
        import serial  # pyserial

        handle = serial.Serial()
        handle.port = port
        handle.dtr = False  # hold both lines low on open (ADR 0062) — no deliberate reset
        handle.rts = False
        handle.timeout = seconds
        handle.open()
        return handle

    try:
        handle = (_open or _default_open)()
    except Exception:
        return False  # no pyserial / device gone / port busy — nothing to sniff
    buf = bytearray()
    try:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            chunk = handle.read(256)
            if not chunk:
                break
            buf.extend(chunk)
    finally:
        try:
            handle.close()
        except Exception:
            pass
    return _PRE_KISS_DELIMITER in buf and KISS_FEND not in buf and KV4P_VENDOR_PREFIX not in buf


def _kv4p_connect_probe(report: _Report, cfg: dict, *, transport=None, sniff=None) -> None:
    """Open the kv4p, run the transport handshake, and print what the board reported.

    **Does not key** (``connect()`` never sets PTT_REQUESTED), but **not read-only**: shipped firmware
    persists its whole desired state on any host frame, so ``connect()`` performs a config-preserving
    handshake — it restores the board's tuned frequency/CTCSS and re-enables status reports, leaving
    TX-allow/filter flags at safe defaults (ADR 0066). A board already streaming reports is read with
    zero writes. Uses :class:`Kv4pTransport` directly, **not** :class:`Kv4pHt`, whose constructor would
    eagerly reconcile the operator's tuning to the *server's* configured frequency. Degrades to a clear
    FAIL line when the ``hardware`` extra or the device is absent, so it still runs in CI.

    ``transport`` is an injection seam for tests (an already-built transport); when ``None`` this owns
    the transport it builds and closes. ``sniff`` is the pre-KISS firmware sniffer (test seam); when
    ``None`` it defaults to :func:`_sniff_pre_kiss_firmware`.
    """
    sniff = sniff or _sniff_pre_kiss_firmware
    print("Connect probe (kv4p handshake — does not key; preserves the board's tuned frequency):")
    from .backends.kv4p.frames import (
        DeviceMode,
        DeviceStateError,
        DeviceStateFlag,
        FeatureFlag,
        RfModuleType,
    )

    owns = transport is None
    if owns:
        try:
            from .backends.kv4p.transport import Kv4pTransport

            transport = Kv4pTransport(serial_port=cfg["serial_port"])
        except RuntimeError as exc:  # pyserial / hardware extra missing (_load_serial raises this)
            report.fail("cannot open the kv4p transport", str(exc))
            return
        except Exception as exc:  # device absent / port error
            report.fail("could not open the serial port", str(exc))
            return
    try:
        try:
            state = transport.connect()
        except Exception as exc:  # Kv4pTimeout, serial error, ...
            if owns:
                try:
                    transport.close()  # free the port so the pre-KISS sniff can reopen it
                except Exception:
                    pass
            if sniff(cfg["serial_port"]):
                report.fail(
                    "this board is running pre-KISS firmware — flash v17",
                    "the wire shows the old de-ad-be-ef framing (no KISS FEND, no KV4P prefix); "
                    "see docs/kv4p-setup.md to flash firmware v17",
                )
            else:
                report.fail(
                    "no response to the connect handshake",
                    f"{exc} (is the board powered and running kv4p firmware?)",
                )
            return

        hello = transport.hello
        if hello is None:
            # HELLO only fires at ESP32 boot (ADR 0062) — absent is informational, not a failure.
            report.warn(
                "no HELLO banner (fires only at ESP32 boot — ADR 0062)",
                f"windowSize defaulted to {transport.window_size}",
            )
        else:
            v = hello.version
            try:
                reported = RfModuleType(v.rf_module_type)
                module = reported.name
            except ValueError:
                reported = None
                module = f"unknown({v.rf_module_type})"
            feats = " | ".join(f.name for f in FeatureFlag if v.features & f.value) or "(none)"
            report.pas(
                "HELLO received",
                f"fw v{v.ver}, module {module}, {v.min_radio_freq:.4f}–{v.max_radio_freq:.4f} MHz, "
                f"windowSize {v.window_size}, features {feats}",
            )

            # Wrong/missing hwconfig NVS: the firmware reads RF_MODULE_TYPE from NVS and falls back to a
            # compiled VHF default, so a missing NVS is indistinguishable on the protocol from a real VHF
            # board — EXCEPT that we know what the operator configured. If the HELLO's reported band
            # disagrees with kv4p.module_type, the NVS is probably wrong/missing (e.g. firmware reflashed
            # without re-flashing the board-config; the merged image wipes NVS — see docs/kv4p-setup.md).
            from .backends.kv4p.radio import module_type_from_band

            configured_band = cfg.get("module_type")
            if reported is not None and configured_band is not None:
                configured = module_type_from_band(configured_band)
                if configured is not reported:
                    report.warn(
                        f"band mismatch: board reports {reported.name}, you configured "
                        f"{configured.name}",
                        "the hwconfig NVS is probably missing or wrong — reflash the board-config image "
                        "for your PCB and band (docs/kv4p-setup.md)",
                    )

        if state is None:
            state = transport.device_state
        if state is None:
            report.fail("no DeviceState returned", "the handshake synced no device state")
            return

        try:
            mode = DeviceMode(state.mode).name
        except ValueError:
            mode = f"unknown({state.mode})"
        report.pas(
            "DeviceState synced",
            f"appliedSequence {state.applied_sequence}, rx {state.freq_rx:.4f} MHz, "
            f"tx {state.freq_tx:.4f} MHz, bw {state.bw}, squelch {state.squelch}, "
            f"ctcss tx/rx {state.ctcss_tx}/{state.ctcss_rx}, mode {mode}, rssi {state.latest_rssi}",
        )
        flags = DeviceStateFlag(state.flags)
        report.pas("device flags", " | ".join(f.name for f in flags) or "(none)")

        # A non-NONE lastError must surface loudly, never a silent pass.
        try:
            err = DeviceStateError(state.last_error)
        except ValueError:
            report.fail("device lastError unknown", f"code {state.last_error}")
        else:
            if err is DeviceStateError.NONE:
                report.pas("device lastError", "NONE")
            else:
                report.fail(f"device lastError: {err.name}", "the radio module reported a fault")

        # After the config-preserving handshake (ADR 0066): RADIO_CONFIG_VALID reads SET (the restore
        # re-asserts the board's own tuning) and TX_ALLOWED reads CLEAR BY POLICY (fail-safe — the probe
        # never re-enables TX; on a board already streaming reports both reflect the operator's real
        # state untouched). Report, never warn.
        if flags & DeviceStateFlag.TX_ALLOWED:
            report.pas("TX_ALLOWED set", "the firmware TX gate is open (board was already reporting)")
        else:
            report.pas(
                "TX_ALLOWED clear",
                "expected — the probe leaves TX off (fail-safe); use --key-test to exercise the gate",
            )
        if flags & DeviceStateFlag.RADIO_CONFIG_VALID:
            report.pas("RADIO_CONFIG_VALID set", "the module holds a valid config")
        else:
            report.pas(
                "RADIO_CONFIG_VALID clear",
                "unexpected after a restore — the board reported no valid tuning to preserve",
            )
    finally:
        if owns:
            try:
                transport.close()
            except Exception:
                pass  # best-effort cleanup — a close failure must not mask the probe result


def _print_kv4p_open_hint() -> None:
    """After a kv4p open/connect failure in a keying mode, point at the non-keying diagnosis.

    A first open after the board's been idle can lose the elicit handshake (reset-on-open race, ADR
    0066) — the connect probe distinguishes that (a retry succeeds) from pre-KISS firmware, and is
    non-destructive. The raw keying-mode error alone doesn't tell the operator that (ADR 0069 bench).
    """
    print(
        "  → run the connect probe first (non-keying): python -m radio_server.doctor --backend kv4p\n"
        "    it diagnoses pre-KISS firmware and a first-connect race; if it's the race, just retry.",
        file=sys.stderr,
    )


def _kv4p_keying_core(radio, *, seconds: float, clock=None) -> int:
    """Assert the kv4p keys up: reconcile PTT on, confirm TX_ACTIVE, hold, drop, confirm it cleared.

    Split from the interactive guard so a test drives it with a fake-transport-backed ``Kv4pHt`` (no
    RF, no CONFIRM). Returns 0 on a clean key-up/key-down, 1 on any failure. Always closes ``radio``.
    A device with TX_ALLOWED off makes ``ptt(True)`` raise :class:`Kv4pKeyingError` — surfaced as a
    loud FAIL here rather than reported as success (ADR 0063).
    """
    if clock is None:
        clock = time.monotonic
    from .backends.kv4p.radio import Kv4pKeyingError

    report = _Report()
    try:
        t0 = clock()
        try:
            radio.ptt(True)
        except Kv4pKeyingError as exc:
            report.fail(
                "keying REFUSED by the device",
                f"{exc} (TX_ALLOWED gate off? set kv4p.tx_allowed = true and check the RF module)",
            )
            return 1
        if not radio.status().transmitting:
            report.fail("keyed but the device never reported TX_ACTIVE", "the firmware did not TX")
            radio.ptt(False)
            return 1
        key_ms = (clock() - t0) * 1000.0
        report.pas("TX_ACTIVE confirmed", f"the device reports it is transmitting (keyed in {key_ms:.0f} ms)")
        start = clock()
        while clock() - start < seconds:
            time.sleep(0.05)
        radio.ptt(False)
        if radio.status().transmitting:
            report.fail("TX_ACTIVE did not clear after unkey", "the device is still transmitting")
            return 1
        report.pas("unkeyed cleanly", "TX_ACTIVE cleared")
        return 0
    finally:
        radio.close()


def _kv4p_key_test(cfg: dict, *, radio=None) -> int:
    """Interactive RF keying test for the kv4p (exercises the TX_ALLOWED gate; ADR 0063).

    Same RF guards as the baofeng :func:`_key_test`: refuses to run unattended, demands a typed
    CONFIRM, dummy-load warning, hard-capped hold. The kv4p has no DTR/RTS line to bisect, so the
    "test" is that keying actually reaches TX_ACTIVE. ``radio`` is a test injection seam.
    """
    if not sys.stdin.isatty() or os.environ.get("CI"):
        print(
            "REFUSING --key-test: not an interactive terminal (RF safety — this keys the "
            "transmitter and must never run unattended or in CI).",
            file=sys.stderr,
        )
        return 2

    print("=" * 72)
    print("  RF KEY TEST — this WILL key the transmitter.")
    print("  Connect a DUMMY LOAD (or be certain it is safe to transmit) before continuing.")
    print(f"  It reconciles PTT on the kv4p and holds TX for ~{_KEY_TEST_SECONDS:.0f}s.")
    print("=" * 72)
    if input("Type CONFIRM (all caps) to proceed: ").strip() != "CONFIRM":
        print("Aborted — nothing was keyed.")
        return 1

    if radio is None:
        try:
            radio = _build_backend(cfg)
        except Exception as exc:
            print(f"[FAIL] could not open the kv4p backend: {exc}", file=sys.stderr)
            _print_kv4p_open_hint()
            return 1
    print("Keying — watch the radio's TX LED / dummy load...")
    return _kv4p_keying_core(radio, seconds=_KEY_TEST_SECONDS)


def _build_backend(cfg: dict):
    """Construct the real backend from resolved config (opens the device with TX inert).

    Dispatches on ``cfg["backend"]`` so the receive/transmit diagnostics (``--rx-level`` / ``--tx-tone``
    / ``--dtmf``) — which only drive the backend-agnostic ``Radio`` surface — work for either
    backend with no change beyond which radio is built here.
    """
    backend = cfg.get("backend", "baofeng")
    if backend == "baofeng":
        return create_radio(
            "baofeng",
            serial_port=cfg["serial_port"],
            ptt_line=cfg["ptt_line"],
            input_device=cfg["input_device"],
            output_device=cfg["output_device"],
            blocksize=cfg["blocksize"],
        )
    if backend == "kv4p":
        return create_radio(
            "kv4p",
            serial_port=cfg["serial_port"],
            module_type=cfg["module_type"],
            squelch=cfg["squelch"],
            tx_lead_seconds=cfg["tx_lead_seconds"],
            high_power=cfg["high_power"],
            tx_allowed=cfg["tx_allowed"],
            frequency=cfg["frequency"],
            sample_rate_correction=cfg["sample_rate_correction"],
        )
    raise ValueError(f"doctor: unsupported backend {backend!r} (expected 'baofeng' or 'kv4p')")


def _vad_thresholds() -> tuple[float, float]:
    """The configured squelch open/close thresholds (audio.vad_on_rms / vad_off_rms), with defaults."""
    on, off = 500.0, 300.0
    try:
        s = _doctor_settings()
        on = float(s.get("audio.vad_on_rms"))
        off = float(s.get("audio.vad_off_rms"))
    except Exception:
        pass
    return on, off


def classify_rx_level(peak_block_rms: float, vad_on: float) -> str:
    """Categorize a measured RX level vs the squelch open threshold: 'silent' (nothing arriving —
    a volume/mixer problem), 'gated' (arriving but under the threshold), or 'ok' (would open the
    gate). Pure, so the recommendation logic is unit-testable without hardware."""
    if peak_block_rms < _RX_SILENCE_RMS:
        return "silent"
    if peak_block_rms < vad_on:
        return "gated"
    return "ok"


def _format_kv4p_rx_rate(frames: int, elapsed: float, correction: float) -> list[str]:
    """The kv4p RX true-rate estimate (ADR 0070), as printable lines. Pure → unit-testable.

    The device emits exactly one 1920-sample Opus packet per 1920 ADC samples, so the packet arrival
    rate reveals the true ADC clock regardless of any host-side correction resample: ``fps × 1920`` is
    the real capture rate, and ``rate / 48000`` is the ``kv4p.sample_rate_correction`` that undoes it.
    Needs a long-enough window (``--seconds 30``) for USB jitter to average out; too-short windows are
    flagged rather than trusted. ``correction`` is the value currently in effect, for a set-it-to hint.
    The mismatch threshold is **0.2 %** (``_RATE_MATCH_TOL``): a DTMF bin is ~39 Hz wide, so even a
    0.4 % residual (e.g. a measured 1.0158 against a 1.02 default) shifts 1633 Hz by ~7 Hz and is worth
    correcting — the old 0.5 % gate wrongly called that "dialed in".
    """
    from .backends.kv4p.audio import FRAME_SAMPLES, OPUS_RATE

    if frames < 2 or elapsed <= 0:
        return ["  RX frame rate   : too few frames to estimate the true sample rate"]
    fps = frames / elapsed
    true_rate = fps * FRAME_SAMPLES
    implied = true_rate / OPUS_RATE
    lines = [
        f"  RX frame rate   : {fps:.2f} frames/s over {elapsed:.1f}s "
        f"→ true ADC rate ≈ {true_rate:,.0f} Hz (nominal {OPUS_RATE:,})",
        f"  implied correction: {implied:.4f}  (kv4p.sample_rate_correction, currently {correction:.4f})",
    ]
    gap = abs(implied - correction)
    if elapsed < 20:
        lines.append(
            "  (short window — run `--rx-level --seconds 30` so USB jitter averages out before trusting this)"
        )
    elif gap > _RATE_MATCH_TOL:
        lines.append(
            f"  → off by {gap / correction * 100:.2f}% from the value in effect — "
            f"set kv4p.sample_rate_correction = {implied:.4f} and re-run."
        )
    else:
        lines.append("  → matches the value in effect (within 0.2%); the correction is dialed in.")
    return lines


# --------------------------------------------------------------------------------------
# RX capture + direct WAV DTMF analysis (ADR 0071) — read the tones out of the actual
# received audio, independent of GoertzelStream, to name why kv4p DTMF won't decode.
# --------------------------------------------------------------------------------------

#: DTMF row (low) and column (high) tone groups — the fixed telephony grid (matches audio.dtmf).
_DTMF_LOW = (697.0, 770.0, 852.0, 941.0)
_DTMF_HIGH = (1209.0, 1336.0, 1477.0, 1633.0)
#: How near a measured peak must sit to a grid tone to count as "on-frequency" (Hz). A DTMF bin is
#: ~39 Hz; anything inside ~half a bin is a clean hit, beyond it the tone is drifting off.
_DTMF_ON_FREQ_HZ = 18.0
#: |sample| at/above this fraction of full scale counts as clipped — the firmware's 16x RX gain
#: (rxAudio.h Boost(16.0)) will saturate a strong signal, and a clipped dual-tone breeds the
#: harmonics/intermodulation that knock DTMF off its bins. Surfacing the clip fraction names that.
_CLIP_FRACTION = 0.98


@dataclass(frozen=True)
class DtmfWindow:
    """One ~100 ms analysis window of received audio, read straight off the spectrum (no Goertzel)."""

    t0: float  # window start, seconds into the capture
    rms: float  # RMS level (int16 units)
    peak: int  # max |sample| (int16 units)
    clip_frac: float  # fraction of samples at/above _CLIP_FRACTION of full scale
    low_hz: float  # strongest peak in the DTMF low band (0 if the window is silent)
    low_mag: float
    high_hz: float  # strongest peak in the DTMF high band
    high_mag: float
    digit: str | None  # the DTMF key whose pair matches, if both peaks are on-frequency
    top_peaks: tuple[tuple[float, float], ...]  # (Hz, magnitude) of the loudest spectral peaks


def _parabolic_offset(mag, k: int) -> float:
    """Sub-bin peak offset in [-0.5, 0.5] from a 3-point parabola around bin ``k`` (0 at the edges)."""
    if k <= 0 or k >= len(mag) - 1:
        return 0.0
    a, b, c = float(mag[k - 1]), float(mag[k]), float(mag[k + 1])
    denom = a - 2.0 * b + c
    return 0.0 if denom == 0.0 else 0.5 * (a - c) / denom


def _band_peak(freqs, mag, lo: float, hi: float) -> tuple[float, float]:
    """Strongest spectral peak within ``[lo, hi]`` Hz, frequency parabola-interpolated for sub-bin
    accuracy (so a ~2% residual on 1633 Hz is visible past the 10 Hz FFT bin)."""
    import numpy as np

    band = np.where((freqs >= lo) & (freqs <= hi))[0]
    if band.size == 0:
        return 0.0, 0.0
    k = int(band[np.argmax(mag[band])])
    df = float(freqs[1] - freqs[0])
    return float(freqs[k]) + _parabolic_offset(mag, k) * df, float(mag[k])


def _nearest(grid: tuple[float, ...], hz: float) -> tuple[float, float]:
    """Nearest grid tone to ``hz`` and the absolute residual in Hz."""
    best = min(grid, key=lambda g: abs(g - hz))
    return best, abs(best - hz)


def analyze_dtmf_windows(samples, rate: int = 48000, window_ms: float = 100.0) -> list[DtmfWindow]:
    """FFT each ~100 ms window of ``samples`` (int16 mono) and read the DTMF tones out directly.

    Pure and hardware-free: a test drives it with synthesized audio. Deliberately does NOT use
    :class:`GoertzelStream` — it is the independent second opinion on what the decoder is being fed, so
    it can distinguish "the tones aren't in the audio" (upstream/firmware) from "they're there but the
    decoder still fails" (decode wiring). Per window it reports the strongest low- and high-band peaks
    (sub-bin accurate), whether they land on a DTMF pair, the clip fraction, and the loudest peaks
    overall (so clipping harmonics / intermodulation products show up).
    """
    import numpy as np

    samples = np.asarray(samples, dtype=np.float64)
    n = int(rate * window_ms / 1000.0)
    if n <= 0 or samples.size < n:
        return []
    window = np.hanning(n)
    freqs = np.fft.rfftfreq(n, 1.0 / rate)
    out: list[DtmfWindow] = []
    for start in range(0, samples.size - n + 1, n):
        seg = samples[start : start + n]
        rms = float(np.sqrt(np.mean(seg * seg)))
        peak = int(np.max(np.abs(seg))) if seg.size else 0
        clip_frac = float(np.mean(np.abs(seg) >= _CLIP_FRACTION * _INT16_FULL_SCALE))
        mag = np.abs(np.fft.rfft(seg * window))
        low_hz, low_mag = _band_peak(freqs, mag, 650.0, 1000.0)
        high_hz, high_mag = _band_peak(freqs, mag, 1150.0, 1700.0)
        digit = None
        if low_mag > 0.0 and high_mag > 0.0:
            lo, lo_res = _nearest(_DTMF_LOW, low_hz)
            hi, hi_res = _nearest(_DTMF_HIGH, high_hz)
            if lo_res <= _DTMF_ON_FREQ_HZ and hi_res <= _DTMF_ON_FREQ_HZ:
                digit = _DTMF_DIGIT_BY_PAIR.get((lo, hi))
        # Loudest peaks overall (300–4000 Hz) so harmonics / intermod are visible in the report.
        speech = np.where((freqs >= 300.0) & (freqs <= 4000.0))[0]
        top: tuple[tuple[float, float], ...] = ()
        if speech.size:
            order = speech[np.argsort(mag[speech])[::-1]]
            picked: list[tuple[float, float]] = []
            for k in order:
                f = float(freqs[k])
                if all(abs(f - pf) > 40.0 for pf, _ in picked):  # dedup peaks within ~a bin group
                    picked.append((f, float(mag[k])))
                if len(picked) >= 4:
                    break
            top = tuple(picked)
        out.append(
            DtmfWindow(
                t0=start / rate, rms=rms, peak=peak, clip_frac=clip_frac,
                low_hz=low_hz, low_mag=low_mag, high_hz=high_hz, high_mag=high_mag,
                digit=digit, top_peaks=top,
            )
        )
    return out


#: (low, high) tone pair -> DTMF key, for mapping measured peaks back to a digit.
_DTMF_DIGIT_BY_PAIR = {
    (697.0, 1209.0): "1", (697.0, 1336.0): "2", (697.0, 1477.0): "3", (697.0, 1633.0): "A",
    (770.0, 1209.0): "4", (770.0, 1336.0): "5", (770.0, 1477.0): "6", (770.0, 1633.0): "B",
    (852.0, 1209.0): "7", (852.0, 1336.0): "8", (852.0, 1477.0): "9", (852.0, 1633.0): "C",
    (941.0, 1209.0): "*", (941.0, 1336.0): "0", (941.0, 1477.0): "#", (941.0, 1633.0): "D",
}


def format_dtmf_analysis(windows: list[DtmfWindow], correction: float) -> list[str]:
    """Turn analyzed windows into a report that names the cause (ADR 0071), with real numbers. Pure.

    Prints the active (non-silent) windows with their dominant low/high peaks and mapped digit, then a
    verdict in the task's priority order: (1) tones ABSENT/mangled in the audio → upstream of the
    decoder (firmware RX chain / SA818 / RF) — clipping from the 16x gain is called out when the clip
    fraction is high; (2) tones PRESENT but off-frequency → the sample-rate correction is wrong, with
    the residual and the implied extra factor; (3) tones PRESENT and on-frequency → the bug is in the
    decode-path wiring, since the audio the analyzer sees is clean.
    """
    import numpy as np

    if not windows:
        return ["DTMF analysis: no audio to analyze (empty capture)."]
    peak_rms = max(w.rms for w in windows)
    floor = max(300.0, peak_rms * 0.15)  # "active" = a real tone burst, not room noise
    active = [w for w in windows if w.rms >= floor]
    lines = [
        f"DTMF analysis: {len(windows)} windows (~100 ms), {len(active)} with signal "
        f"(peak RMS {peak_rms:.0f}); DTMF grid low {'/'.join(f'{f:.0f}' for f in _DTMF_LOW)} × "
        f"high {'/'.join(f'{f:.0f}' for f in _DTMF_HIGH)} Hz:",
    ]
    if not active:
        lines.append("  no window carried a signal above the noise floor — was a digit keyed during capture?")
        return lines

    decoded = []
    for w in active:
        lo, lo_res = _nearest(_DTMF_LOW, w.low_hz)
        hi, hi_res = _nearest(_DTMF_HIGH, w.high_hz)
        tag = f"={w.digit}" if w.digit else " (no clean pair)"
        clip = f" CLIP {w.clip_frac * 100:.0f}%" if w.clip_frac > 0.01 else ""
        lines.append(
            f"  t={w.t0:5.2f}s rms={w.rms:5.0f}{clip}  low {w.low_hz:6.1f} (→{lo:.0f}, {lo_res:+.1f}) "
            f"high {w.high_hz:6.1f} (→{hi:.0f}, {hi_res:+.1f}){tag}"
        )
        if w.digit:
            decoded.append(w.digit)

    # Aggregate residuals across active windows (sub-bin) for the on/off-frequency verdict.
    lo_res_all = [_nearest(_DTMF_LOW, w.low_hz)[1] for w in active if w.low_mag > 0]
    hi_res_all = [_nearest(_DTMF_HIGH, w.high_hz)[1] for w in active if w.high_mag > 0]
    med_lo = float(np.median(lo_res_all)) if lo_res_all else 0.0
    med_hi = float(np.median(hi_res_all)) if hi_res_all else 0.0
    on_freq = [w for w in active if w.digit]
    clipped = [w for w in active if w.clip_frac > 0.05]
    lines.append("")
    # Clipping is checked FIRST: a clipped dual-tone still shows its fundamentals in the FFT (so it can
    # look "on-frequency"), but the harmonics/intermod it breeds are exactly what trip GoertzelStream's
    # twist / 2nd-harmonic / group-dominance gates — so clipped-but-present is an UPSTREAM fault, not a
    # clean signal. It must win over the on-frequency verdict.
    if len(clipped) >= max(1, len(active) // 2):
        lines.append(
            f"  VERDICT (1): {len(clipped)}/{len(active)} active windows are CLIPPING (up to "
            f"{max(w.clip_frac for w in active) * 100:.0f}% of samples at full scale) — the DTMF "
            f"fundamentals are present but saturated. The firmware's 16x RX gain (rxAudio.h "
            f"Boost(16.0)) is driving a strong dual-tone into hard clipping; the resulting "
            f"harmonics/intermodulation trip the decoder's twist/harmonic-rejection gates, so nothing "
            f"decodes. The fault is UPSTREAM (firmware RX gain), not GoertzelStream — confirm by "
            f"feeding the far end a much weaker signal (turn the source radio down / detune slightly): "
            f"if it then decodes, the gain is the cause."
        )
    elif len(on_freq) >= max(1, len(active) // 3):
        # (3) the tones are in the audio, clean, on-frequency — the decoder is fed but doesn't decode.
        seq = "".join(dict.fromkeys(decoded)) if decoded else "".join(decoded)
        lines.append(
            f"  VERDICT (3): DTMF tones ARE present, on-frequency (median residual "
            f"low {med_lo:.1f} Hz / high {med_hi:.1f} Hz) and NOT clipping — mapped ~{seq!r}. The audio "
            f"reaching the decoder is clean, so the fault is in the decode-path WIRING, not the tones. "
            f"Compare how the corrected frame reaches GoertzelStream in the server/doctor vs this capture."
        )
    elif med_lo > _DTMF_ON_FREQ_HZ or med_hi > _DTMF_ON_FREQ_HZ:
        # (2) tones present but off-frequency — the sample-rate correction is wrong.
        extra = 1.0 + (med_hi / float(np.mean(_DTMF_HIGH))) if med_hi else 1.0
        lines.append(
            f"  VERDICT (2): DTMF tones present but OFF-FREQUENCY (median residual low {med_lo:.1f} Hz "
            f"/ high {med_hi:.1f} Hz after correction {correction:.4f}). The sample-rate correction is "
            f"still wrong — nudge kv4p.sample_rate_correction by ~×{extra:.4f} (cross-check --rx-level) "
            f"and re-capture."
        )
    else:
        # tones absent and not clipping — a different upstream cause (filtering, level, RF).
        peaks = "; ".join(f"{f:.0f} Hz" for f, _ in (active[len(active) // 2].top_peaks or ()))
        lines.append(
            f"  VERDICT (1): no clean DTMF pair in the received audio and no clipping — the tones are "
            f"absent or mangled UPSTREAM of the decoder (firmware RX filtering, the SA818, or the RF "
            f"path), not GoertzelStream. Loudest peaks in a mid window: {peaks or '(none)'}."
        )
    return lines


def _read_wav_mono16(path: str):
    """Read a mono s16 WAV into an int16 numpy array (+ its rate). Fails loud on a wrong format."""
    import wave

    import numpy as np

    with wave.open(path, "rb") as wav:
        if wav.getsampwidth() != 2 or wav.getnchannels() != 1:
            raise RuntimeError(
                f"{path}: expected mono 16-bit WAV, got {wav.getnchannels()}ch/"
                f"{wav.getsampwidth() * 8}-bit"
            )
        rate = wav.getframerate()
        raw = wav.readframes(wav.getnframes())
    return np.frombuffer(raw, dtype="<i2"), rate


def _write_wav_mono16(path: str, samples: bytes) -> None:
    """Write canonical 48 kHz mono s16 PCM bytes to a WAV (stdlib, deterministic header)."""
    import wave

    with wave.open(path, "wb") as wav:
        wav.setnchannels(CANONICAL_FORMAT.channels)
        wav.setsampwidth(CANONICAL_FORMAT.width)
        wav.setframerate(CANONICAL_FORMAT.rate)
        wav.writeframes(samples)


def _format_tx_stats(stats, window_size: int) -> list[str]:
    """Render one keying's kv4p TX-audio telemetry (a ``TxStats``) as printable report lines.

    Pure, like :func:`classify_rx_level`, so the bench-number formatting is unit-tested without
    keying. It turns the counters into the facts the TX bring-up records (ADR 0069): the encoded
    bytes/frame the Opus codec actually produced, how many such frames fit the flow-control window,
    and whether that window ever became the bottleneck.
    """
    if stats.frames == 0:
        return ["TX telemetry: no audio frames were sent (nothing to measure)."]
    mean_opus = stats.opus_bytes_sum / stats.frames
    mean_wire = stats.wire_bytes_sum / stats.frames
    lines = [
        f"TX telemetry ({stats.frames} Opus frames over ~{stats.frames * 0.04:.1f}s):",
        f"  encoded bytes/frame : min {stats.opus_bytes_min}  mean {mean_opus:.1f}  max {stats.opus_bytes_max}",
        f"  on-wire bytes/frame : mean {mean_wire:.1f}  (escaped + FENDs — what the window spends)",
    ]
    if mean_wire > 0:
        lines.append(f"  frames per {window_size}-byte window : ~{window_size / mean_wire:.1f}")
    if stats.blocked_frames:
        lines.append(
            f"  window blocked on {stats.blocked_frames} frame(s) (min credits {stats.min_credits}) — "
            "expected backpressure: a one-shot clip is pushed faster than the device drains it (~25 "
            "frames/s), so the fixed device window paces you to it. Fine unless a write hits the timeout."
        )
    else:
        lines.append(
            f"  window never blocked (min credits {stats.min_credits}) — the write timeout was never neared."
        )
    return lines


def _rx_level(cfg: dict, seconds: float) -> int:
    """Measure and report the AIOC's received audio level vs the squelch threshold (no keying)."""
    print(f"Measuring received audio level for ~{seconds:.0f}s (no transmit)...")
    print("Have a signal coming in (e.g. transmit into the radio from another handheld).\n")
    try:
        radio = _build_backend(cfg)
    except Exception as exc:
        where = "kv4p backend" if cfg.get("backend") == "kv4p" else "AIOC backend"
        print(f"[FAIL] could not open the {where}: {exc}", file=sys.stderr)
        return 1
    is_kv4p = cfg.get("backend") == "kv4p"
    try:
        try:
            levels = measure_rx_levels(radio, seconds=seconds)
        except Exception as exc:
            if is_kv4p:
                # No sound card here — RX audio rides the UART. A failure means the receive path
                # (transport/decode) errored, not a PortAudio device.
                print(f"[FAIL] the kv4p RX path errored: {exc}", file=sys.stderr)
                print("       Only one process can hold the serial port — stop the running")
                print("       radio-server and retry. Then run `doctor --backend kv4p` (the connect")
                print("       probe) to confirm the board is answering.")
            else:
                # The AIOC capture is single-open; a running radio-server (or another app) holding
                # the card drops it from PortAudio's device list, so the name no longer resolves.
                print(f"[FAIL] could not open the AIOC capture device: {exc}", file=sys.stderr)
                print("       The sound card is single-open — stop the running radio-server (or any")
                print("       other app using the AIOC) and retry. (Run plain `doctor` to check the")
                print("       device name if the server is not running.)")
            return 1
    finally:
        radio.close()

    if levels.frames == 0:
        if is_kv4p:
            print("[FAIL] no RX audio arrived — the device streamed no audio frames. Run")
            print("       `doctor --backend kv4p` (the connect probe): if it cannot prove a round")
            print("       trip the host frame isn't landing; otherwise the RX audio stream never")
            print("       opened (check the firmware protocol matches this build, the squelch, and")
            print("       that a signal is actually being received).")
        else:
            print("[FAIL] no audio frames captured — is the AIOC capture device correct? run the")
            print("       plain `doctor` to check device resolution.")
        return 1

    on, off = _vad_thresholds()
    print(f"  frames captured : {levels.frames}")
    if is_kv4p:
        for line in _format_kv4p_rx_rate(levels.frames, levels.elapsed, cfg["sample_rate_correction"]):
            print(line)
    print(f"  peak sample     : {levels.peak_sample} / 32767 ({_dbfs(levels.peak_sample)})")
    print(f"  loudest block   : {levels.peak_block_rms:.0f} RMS ({_dbfs(levels.peak_block_rms)})")
    print(f"  average level   : {levels.avg_rms:.0f} RMS ({_dbfs(levels.avg_rms)})")
    print(f"  squelch opens at: vad_on_rms={on:.0f}  (closes below vad_off_rms={off:.0f})\n")

    category = classify_rx_level(levels.peak_block_rms, on)
    if category == "silent" and cfg.get("backend") == "kv4p":
        # kv4p has no OS capture level and no radio volume knob: the SA818 audio volume is a
        # firmware constant (upstream kv4p-ht globals.h DEFAULT_VOLUME 8 -> hw.volume; verify against
        # the pinned firmware / on bench) and is NOT in HostDesiredState — the host cannot raise it.
        print("→ Almost no audio is arriving. On the kv4p there is NO OS capture level and NO radio")
        print("  volume knob to raise: the SA818 audio volume is a firmware constant (kv4p-ht")
        print("  globals.h DEFAULT_VOLUME 8 → hw.volume; verify against the pinned firmware / bench)")
        print("  and is not in HostDesiredState, so the host cannot set it over the protocol. The")
        print("  only host-side levers are kv4p.squelch and audio.vad_on_rms — re-run while receiving.")
    elif category == "silent":
        print("→ Almost no audio is arriving. This is a LEVEL problem, not the squelch:")
        print("  • turn UP the UV-5R volume knob (the AIOC taps the radio's speaker line), and")
        print("  • raise the capture level for the AIOC card in `alsamixer` (F6 to pick the card).")
        print("  Then re-run this while a signal is being received.")
    elif category == "gated":
        rec_on, rec_off = round(levels.peak_block_rms * 0.4), round(levels.peak_block_rms * 0.25)
        print("→ Audio IS arriving but sits BELOW your squelch threshold, so it is gated out")
        print("  (Listen stays silent). Either relay everything with audio.squelch=off, or lower")
        print(f"  the threshold to match: audio.vad_on_rms ≈ {rec_on}, audio.vad_off_rms ≈ {rec_off}.")
    else:
        print("→ Received audio comfortably exceeds the squelch threshold — the gate should open.")
        print("  If Listen is still silent, check the browser: click Listen (needed to start audio),")
        print("  and make sure it is not muted.")
    return 0


def _rx_capture(cfg: dict, seconds: float, out_path: str, clock=None) -> int:
    """Record the received audio to a WAV and analyze its DTMF tones directly (ADR 0071, no keying).

    Captures exactly what ``receive()`` returns — the corrected 48 kHz stream GoertzelStream would see —
    so the WAV analysis is the ground truth on what the decoder is being fed. Read-only: it never keys;
    the operator keys ``1234#`` from a separate handheld while this runs. ``clock`` is injectable (as in
    :func:`measure_rx_levels`) so a test drives the capture with a scripted radio and no real sleeps."""
    import numpy as np

    if clock is None:
        clock = time.monotonic
    print(f"Capturing ~{seconds:.0f}s of received audio to {out_path} (no transmit)...")
    print("Key 1234# from a handheld into the radio, a few times, while this runs.\n")
    try:
        radio = _build_backend(cfg)
    except Exception as exc:
        where = "kv4p backend" if cfg.get("backend") == "kv4p" else "AIOC backend"
        print(f"[FAIL] could not open the {where}: {exc}", file=sys.stderr)
        return 1
    chunks: list[bytes] = []
    try:
        start = clock()
        while clock() - start < seconds:
            frame = radio.receive()
            if frame.samples:
                chunks.append(frame.samples)
    finally:
        radio.close()

    pcm = b"".join(chunks)
    if not pcm:
        print("[FAIL] no RX audio arrived to capture — run `--rx-level` first to confirm the receive "
              "path, and that a signal is actually being received.", file=sys.stderr)
        return 1
    try:
        _write_wav_mono16(out_path, pcm)
    except Exception as exc:
        print(f"[FAIL] could not write {out_path}: {exc}", file=sys.stderr)
        return 1
    samples = np.frombuffer(pcm, dtype="<i2")
    print(f"  wrote {samples.size} samples ({samples.size / CANONICAL_FORMAT.rate:.1f}s) to {out_path}\n")
    windows = analyze_dtmf_windows(samples, CANONICAL_FORMAT.rate)
    for line in format_dtmf_analysis(windows, cfg.get("sample_rate_correction", 1.0)):
        print(line)
    return 0


def _analyze_wav(path: str) -> int:
    """Analyze the DTMF tones in an existing mono-16 WAV (ADR 0071) — no radio, no keying."""
    try:
        samples, rate = _read_wav_mono16(path)
    except Exception as exc:
        print(f"[FAIL] could not read {path}: {exc}", file=sys.stderr)
        return 1
    correction = 1.0
    try:
        correction = _doctor_settings().get("kv4p.sample_rate_correction")
    except Exception:
        pass  # no config — the verdict-2 hint just falls back to 1.0
    print(f"Analyzing {path} ({samples.size} samples @ {rate} Hz)\n")
    for line in format_dtmf_analysis(analyze_dtmf_windows(samples, rate), correction):
        print(line)
    return 0


def _tx_tone(cfg: dict, seconds: float, freq: float) -> int:
    """Key the radio and play a test tone (RF — dummy-load guarded, refuses unattended)."""
    if not sys.stdin.isatty() or os.environ.get("CI"):
        print(
            "REFUSING --tx-tone: not an interactive terminal (RF safety — this keys the "
            "transmitter and must never run unattended or in CI).",
            file=sys.stderr,
        )
        return 2
    seconds = min(seconds, _TX_TONE_MAX_SECONDS)
    line = cfg.get("ptt_line")  # kv4p has no PTT line (keying rides the reconciled PTT flag)
    print("=" * 72)
    print("  RF TX-TONE TEST — this WILL key the transmitter and play a tone.")
    print("  Connect a DUMMY LOAD (or be certain it is safe to transmit) before continuing.")
    if line:
        print(f"  It will key (PTT line: {line}) and play a {freq:.0f} Hz tone for ~{seconds:.0f}s.")
    else:
        print(f"  It will key the radio and play a {freq:.0f} Hz tone for ~{seconds:.0f}s.")
    print("=" * 72)
    if input("Type CONFIRM (all caps) to proceed: ").strip() != "CONFIRM":
        print("Aborted — nothing was keyed.")
        return 1

    from .audio.tone import synth_tone

    is_kv4p = cfg.get("backend") == "kv4p"
    try:
        radio = _build_backend(cfg)
    except Exception as exc:
        where = "kv4p backend" if is_kv4p else "AIOC backend"
        print(f"could not open the {where}: {exc}", file=sys.stderr)
        if is_kv4p:
            _print_kv4p_open_hint()
        return 1
    stats = window = None
    try:
        print(f"Keying + playing {freq:.0f} Hz for ~{seconds:.0f}s — listen on another radio...")
        radio.transmit(synth_tone(freq, seconds * 1000.0))  # one-shot transmit() self-keys + drains
        if is_kv4p and hasattr(radio, "tx_stats"):  # kv4p carries per-keying TX telemetry (ADR 0069)
            stats, window = radio.tx_stats, radio.window_size
    finally:
        radio.close()
    print("Done — line dropped.")
    if stats is not None:
        for tx_line in _format_tx_stats(stats, window):
            print(tx_line)
    heard = input("Did another radio hear the tone? [y/n]: ").strip().lower()
    if heard.startswith("y"):
        if is_kv4p:
            print("TX audio path confirmed — the kv4p keyed and audio reached the air.")
        else:
            print("TX audio path confirmed. If it was faint, raise the AIOC playback level in alsamixer.")
        return 0
    if is_kv4p:
        print("No tone heard — check: it keyed at all (`--key-test`), the TX_ALLOWED gate is on")
        print("(kv4p.tx_allowed = true), and the second receiver is on the kv4p's frequency (445.800).")
    else:
        print("No tone heard — check: the PTT line keyed (doctor --key-test), the AIOC playback level in")
        print("alsamixer, and that the other radio is on the UV-5R's frequency.")
    return 1


def _dtmf(cfg: dict, seconds: float) -> int:
    """Listen for DTMF from the radio and print decoded digits/entries (read-only, no keying)."""
    import time

    from .audio import (
        DECODE_MODE_AUTO,
        DECODE_MODE_BUFFERED,
        DECODE_MODE_NATIVE,
        BufferedDtmfInput,
        DtmfFramer,
        GoertzelStream,
        MultimonDtmfDecoder,
        MultimonStream,
        StreamingDtmfInput,
        load_dtmf_decode_mode,
        load_dtmf_reverse_twist_db,
        load_dtmf_timeout,
        load_multimon_bin,
        resolve_decode_mode,
    )

    multimon_bin, timeout, decode_mode = "multimon-ng", 3.0, DECODE_MODE_AUTO
    reverse_twist_db = 4.0  # NATIVE_REVERSE_TWIST_DB default; overridden below if config loads
    try:
        s = _doctor_settings()
        multimon_bin = load_multimon_bin(s)
        timeout = load_dtmf_timeout(s)
        decode_mode = load_dtmf_decode_mode(s)
        reverse_twist_db = load_dtmf_reverse_twist_db(s)
    except Exception:
        pass  # defaults are fine for a diagnostic

    # Resolve `auto` and say which decoder is live (ADR 0055) — printed before the backend opens, so it
    # shows even with no radio attached. An explicit mode reports itself; `auto` reports what it picked.
    resolved, reason = resolve_decode_mode(decode_mode, multimon_bin)
    if decode_mode == DECODE_MODE_AUTO:
        print(f"decode mode: auto -> {resolved} ({reason})")
    else:
        print(f"decode mode: {resolved}")
    print(f"Listening for DTMF for ~{seconds:.0f}s (no transmit, {resolved} decode).")
    print("Key digits on the radio into the UV-5R: '#' submits an entry, '*' clears.\n")
    try:
        radio = _build_backend(cfg)
    except Exception as exc:
        where = "kv4p backend" if cfg.get("backend") == "kv4p" else "AIOC backend"
        print(f"[FAIL] could not open the {where}: {exc}", file=sys.stderr)
        return 1

    framer = DtmfFramer(timeout=timeout)
    raw: list[str] = []
    entries: list[str] = []

    def _on_digit(digit: str) -> None:
        raw.append(digit)
        print(f"  heard: {digit}")

    def _collect(new_entries: list[str]) -> None:
        for entry in new_entries:
            entries.append(entry)
            print(f"  ENTRY: {entry}")

    # Drive the same input the live controller uses, over the `resolved` mode (ADR 0055): buffered
    # when `dtmf.decode_mode=buffered` (ADR 0030), the in-process Goertzel decoder for `native`
    # (ADR 0054), else streaming (ADR 0038) — so this diagnostic validates the exact decode path the
    # server runs.
    if resolved == DECODE_MODE_BUFFERED:
        dtmf = BufferedDtmfInput(MultimonDtmfDecoder(multimon_bin), framer, on_digit=_on_digit)
    elif resolved == DECODE_MODE_NATIVE:
        dtmf = StreamingDtmfInput(
            GoertzelStream(reverse_twist_db=reverse_twist_db), framer, on_digit=_on_digit
        )
    else:
        dtmf = StreamingDtmfInput(MultimonStream(multimon_bin), framer, on_digit=_on_digit)

    try:
        try:
            _drive_dtmf(radio, dtmf, seconds=seconds, clock=time.monotonic, collect=_collect)
        except RuntimeError as exc:  # multimon-ng missing — the decoder raises with an install hint
            print(f"[FAIL] {exc}", file=sys.stderr)
            print("       install it: sudo apt install multimon-ng")
            return 1
        except Exception as exc:
            # The AIOC capture is single-open; a running server holding it fails here (see --rx-level).
            print(f"[FAIL] could not open the AIOC capture device: {exc}", file=sys.stderr)
            print("       Stop the running radio-server (single-open sound card) and retry.")
            return 1
    finally:
        dtmf.close()  # reap the persistent multimon process in streaming mode (ADR 0038)
        radio.close()

    print()
    if raw:
        print(f"Decoded digits: {''.join(raw)!r}; completed entries: {entries}")
        return 0
    print("No DTMF decoded. Check a strong RX signal first (`--rx-level`), and that you keyed digits")
    print("on the radio while this was listening (hold each tone ~100 ms+).")
    return 1


def _link(cfg: dict, seconds: float) -> int:
    """Connect to the configured Murmur server and report the link state (read-only, no RF).

    Polls :meth:`PyMumbleClient.status` up to ``seconds`` (connect is non-blocking by design — the
    same client the server runs), reports PASS/FAIL, and always disconnects. Unlike ``--key-test``
    this never touches the radio, so it needs no CONFIRM/CI guard.
    """
    print("radio-server doctor — Mumble link (ADR 0041/0042)\n")
    report = _Report()
    print("Mumble (Murmur server):")
    if cfg.get("error"):
        report.fail(cfg["error"], "pass a configured entry name: --link <name>")
        return 1
    if not cfg["host"]:
        if cfg.get("names"):
            report.fail(
                "no entry selected",
                f"several [[mumble.servers]] entries are configured — pass one: "
                f"--link {{{', '.join(cfg['names'])}}}",
            )
        else:
            report.fail(
                "no server configured",
                "add a [[mumble.servers]] entry to radio.toml (or pass --host)",
            )
        return 1
    if cfg.get("name"):
        print(f"  entry: {cfg['name']}")
    # Point ctypes at the bundled libopus (the mumble extra's carrier wheel) before opuslib's
    # import-time load, and print which opus path was taken so a failing box is debuggable (ADR 0057).
    from .link._opus import ensure_opus_loadable, opus_install_hint

    print(f"  opus: {ensure_opus_loadable()}")
    # ImportError = the extra isn't installed. Otherwise = the opus load failed: opuslib raises a
    # bare Exception when libopus is missing (not OSError), plus OSError for an unloadable DLL, so the
    # second arm catches Exception to actually reach the per-platform hint (ADR 0056).
    try:
        import pymumble_py3  # noqa: F401
    except ImportError:
        report.fail(
            "pymumble not installed",
            "install the mumble extra: uv sync --extra mumble (name every extra you use — "
            "sync installs exactly what's listed)",
        )
        return 1
    except Exception:  # noqa: BLE001 — see comment above
        report.fail("libopus not found", opus_install_hint())
        return 1
    report.pas("pymumble + libopus importable")

    from .link import PyMumbleClient

    client = PyMumbleClient(
        host=cfg["host"],
        port=cfg["port"],
        username=cfg["username"],
        channel=cfg["channel"],
        password=cfg["password"],
    )
    try:
        client.connect()
        deadline = time.monotonic() + seconds
        status = client.status()
        while not status.connected and time.monotonic() < deadline:
            time.sleep(0.2)
            status = client.status()
        if status.connected:
            peers = "unknown" if status.peers is None else str(status.peers)
            channel = cfg["channel"] or "(root)"
            report.pas(
                f"connected to {cfg['host']}:{cfg['port']}",
                f"channel {channel}, {peers} peer(s)",
            )
        else:
            report.fail(
                f"no connection to {cfg['host']}:{cfg['port']} within {seconds:.0f}s",
                "check the host/port, the entry's password (mumble_password_<name> in "
                "radio-secrets.toml / RADIO_MUMBLE_PASSWORD_<NAME>), and firewall",
            )
    finally:
        client.disconnect()
    return 0 if report.ok else 1


def _resolve_doctor_backend(args) -> str:
    """Pick the backend to diagnose: ``--backend`` override, else ``server.backend`` if it is 'kv4p',
    else 'baofeng'.

    ``server.backend`` defaults to 'mock' and the AIOC bring-up runs doctor *before* flipping it to
    'baofeng', so every non-kv4p value (mock/v71/baofeng/unset) resolves to the AIOC/Baofeng checks —
    today's behaviour, preserved. Only 'baofeng' and 'kv4p' are supported hardware backends here.
    """
    if getattr(args, "backend", None):
        return args.backend
    try:
        if _doctor_settings().get("server.backend") == "kv4p":
            return "kv4p"
    except Exception:
        pass  # no config / unreadable — the AIOC checks are the safe default
    return "baofeng"


def _run_kv4p(args) -> int:
    """Dispatch the kv4p modes: the connect probe (default), the keying test, and the shared
    receive/transmit diagnostics (which are backend-agnostic — they only drive the Radio surface)."""
    cfg = _kv4p_config()
    if args.serial_port:
        cfg["serial_port"] = args.serial_port
    if getattr(args, "module_type", None):
        cfg["module_type"] = args.module_type
    if getattr(args, "tx_lead", None) is not None:  # `is not None` so --tx-lead 0 (disable) sweeps too
        cfg["tx_lead_seconds"] = args.tx_lead

    if args.key_test:
        return _kv4p_key_test(cfg)
    if args.rx_level:
        return _rx_level(cfg, args.seconds or 5.0)
    if args.tx_tone:
        return _tx_tone(cfg, args.seconds or 5.0, args.freq)
    if args.dtmf:
        return _dtmf(cfg, args.seconds or 30.0)
    if args.rx_capture:
        return _rx_capture(cfg, args.seconds or 10.0, args.out)

    print("radio-server doctor — kv4p HT backend\n")
    report = _Report()
    _check_kv4p_serial(report, cfg["serial_port"])
    print()
    _kv4p_connect_probe(report, cfg)
    print()
    if report.ok:
        print("All checks passed. Next steps:")
        print("  • `--key-test` — key up into a dummy load and confirm TX_ACTIVE (RF)")
        print("  • `--rx-level` — measure received audio + tune audio.vad_on_rms (while receiving)")
        print("  • `--dtmf`     — decode DTMF keyed from a radio (measures ADPCM→Goertzel on-air)")
        print("  • `--rx-capture` — record RX to a WAV + read its DTMF tones directly (why --dtmf fails)")
        print("Then run the server with server.backend=kv4p (see docs/hardware-bringup.md).")
        return 0
    print("Some checks failed — see [FAIL] lines above.")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m radio_server.doctor",
        description="AIOC/Baofeng or kv4p HT hardware diagnostic (ADR 0029/0061).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--key-test",
        action="store_true",
        help="Interactively key PTT to verify which serial line works (RF — use a dummy load).",
    )
    mode.add_argument(
        "--rx-level",
        action="store_true",
        help="Measure the AIOC's received audio level vs the squelch threshold (read-only).",
    )
    mode.add_argument(
        "--tx-tone",
        action="store_true",
        help="Key the radio and play a test tone (RF — use a dummy load) to verify TX audio.",
    )
    mode.add_argument(
        "--dtmf",
        action="store_true",
        help="Listen and print DTMF digits decoded from the radio (read-only; needs multimon-ng).",
    )
    mode.add_argument(
        "--rx-capture",
        action="store_true",
        help="Record N seconds of the received audio to a WAV (--out) and analyze the DTMF tones in it "
        "directly (FFT per window) — reads the tones out of the actual audio, independent of the "
        "decoder, to tell an upstream/RF problem from a decode-path one (read-only, no RF).",
    )
    mode.add_argument(
        "--analyze-wav",
        metavar="PATH",
        default=None,
        help="Analyze the DTMF tones in an existing mono-16 WAV (no radio) — the same per-window FFT "
        "report as --rx-capture, for re-reading a capture off disk.",
    )
    mode.add_argument(
        "--link",
        nargs="?",
        const="",
        default=None,
        metavar="ENTRY",
        help="Connect to a configured [[mumble.servers]] entry (by name; the sole/autoconnect "
        "entry when omitted) and report the Mumble link state (read-only, no RF; ADR 0041/0042).",
    )
    parser.add_argument(
        "--backend",
        choices=["baofeng", "kv4p"],
        help="Which hardware backend to diagnose (default: server.backend if 'kv4p', else baofeng).",
    )
    parser.add_argument("--serial-port", help="Override the radio's serial device path.")
    parser.add_argument(
        "--module-type", choices=[m.value for m in Kv4pBand],
        help="Override the kv4p RF module band (vhf/uhf) — the freq-range fallback when no HELLO.",
    )
    parser.add_argument("--host", help="Override the Murmur server host for --link.")
    parser.add_argument(
        "--port", type=int, help="Override the Murmur server port for --link (default 64738)."
    )
    parser.add_argument("--ptt-line", choices=[m.value for m in PttLine], help="Override the PTT line.")
    parser.add_argument(
        "--seconds",
        type=float,
        default=None,
        help="Duration for --rx-level / --tx-tone / --dtmf / --rx-capture / --link "
        "(defaults: 5 / 5 / 30 / 10 / 10).",
    )
    parser.add_argument(
        "--freq", type=float, default=1000.0, help="Tone frequency in Hz for --tx-tone (default 1000)."
    )
    parser.add_argument(
        "--out",
        default="kv4p-rx-capture.wav",
        help="WAV path for --rx-capture (default: kv4p-rx-capture.wav in the current directory).",
    )
    parser.add_argument(
        "--tx-lead",
        type=float,
        default=None,
        help="Override kv4p.tx_lead_seconds for --key-test / --tx-tone — sweep the key-up lead-in to "
        "find the smallest value whose audio start isn't clipped (0 disables it).",
    )
    args = parser.parse_args(argv)
    backend = _resolve_doctor_backend(args)

    # --analyze-wav is backend-independent (reads a file, no radio) — handle it before the backend
    # split so it never opens a device.
    if args.analyze_wav is not None:
        return _analyze_wav(args.analyze_wav)

    # --link is backend-independent (Mumble, no radio) — handle it the same either way, before the
    # backend split so it never builds a hardware config it does not use.
    if args.link is not None:
        link_cfg = _mumble_config(args.link)
        if args.host:
            link_cfg["host"] = args.host
            link_cfg["error"] = None  # an explicit host overrides entry selection entirely
        if args.port:
            link_cfg["port"] = args.port
        return _link(link_cfg, args.seconds or 10.0)

    # Validate the selected backend's config block up front (ADR 0074), reading the real radio.toml:
    # a broken [<backend>] block fails loud here rather than silently defaulting or only surfacing at
    # device construction. Placed after the backend-independent handlers (they build no radio).
    config_problem = _validate_doctor_backend_config(backend)
    if config_problem is not None:
        print(f"[FAIL] {config_problem}", file=sys.stderr)
        return 1

    if backend == "kv4p":
        return _run_kv4p(args)

    # --- AIOC / Baofeng (unchanged) ---
    cfg = _baofeng_config()
    if args.serial_port:
        cfg["serial_port"] = args.serial_port
    if args.ptt_line:
        cfg["ptt_line"] = args.ptt_line

    if args.key_test:
        return _key_test(cfg["serial_port"], cfg["ptt_line"])
    if args.rx_level:
        return _rx_level(cfg, args.seconds or 5.0)
    if args.tx_tone:
        return _tx_tone(cfg, args.seconds or 5.0, args.freq)
    if args.dtmf:
        return _dtmf(cfg, args.seconds or 30.0)
    if args.rx_capture:
        return _rx_capture(cfg, args.seconds or 10.0, args.out)

    print("radio-server doctor — AIOC/Baofeng backend\n")
    report = _Report()
    _check_audio(report, cfg["input_device"], cfg["output_device"])
    print()
    _check_serial(report, cfg["serial_port"], cfg["ptt_line"])
    print()
    if report.ok:
        print("All checks passed. Next steps:")
        print("  • `--key-test` — confirm which serial line keys PTT (into a dummy load)")
        print("  • `--rx-level` — measure received audio + tune audio.vad_on_rms (while receiving)")
        print("  • `--tx-tone`  — confirm TX audio goes out (into a dummy load)")
        print("  • `--dtmf`     — decode DTMF digits keyed from a radio (needs multimon-ng)")
        print("Then run the server with server.backend=baofeng (see docs/hardware-bringup.md).")
        return 0
    print("Some checks failed — see [FAIL] lines above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
