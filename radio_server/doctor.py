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
prints what the board reported (HELLO banner, DeviceState, decoded flags). **Run it first on bench
day**: it settles a pile of "verify against hardware" unknowns in one shot — the windowSize default
(2048), whether pyserial's open resets the board (did a HELLO arrive unbidden?), the real RF module
band, and whether the host flags survive the reconcile (TX_ALLOWED / RADIO_CONFIG_VALID coming back
set). ``--key-test`` on kv4p is a KEYING test (there is no serial line to bisect): it reconciles PTT
on, asserts the device reports TX_ACTIVE, holds, and drops — exercising the TX_ALLOWED gate (0063).
Running ``--dtmf`` on kv4p is the bench measurement that settles the arc's oldest open question — DTMF
through the lossy 16 kHz ADPCM path against the native Goertzel decoder (open since cycle 1); it is a
measurement, not a code change.

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

_AIOC_NAME_HINTS = ("AllInOneCable", "All-In-One-Cable", "AIOC")
_KEY_TEST_SECONDS = 2.0  # hard cap on how long --key-test holds the line
_TX_TONE_MAX_SECONDS = 5.0  # hard cap on how long --tx-tone keys the radio
_INT16_FULL_SCALE = 32768.0
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
    avg_rms = math.sqrt(sum_sq / total_samples) if total_samples else 0.0
    return RxLevels(frames, total_samples, peak_sample, peak_block_rms, avg_rms)


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
        from .config import load_settings

        s = load_settings()
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
        DEFAULT_SERIAL_PORT as KV4P_DEFAULT_SERIAL_PORT,
        DEFAULT_SQUELCH,
        DEFAULT_TX_ALLOWED,
        DEFAULT_TX_LEAD_SECONDS,
    )

    cfg = {
        "backend": "kv4p",
        "serial_port": KV4P_DEFAULT_SERIAL_PORT,
        "squelch": DEFAULT_SQUELCH,
        "tx_lead_seconds": DEFAULT_TX_LEAD_SECONDS,
        "high_power": DEFAULT_HIGH_POWER,
        "tx_allowed": DEFAULT_TX_ALLOWED,
        "frequency": None,
    }
    try:
        from .config import load_settings

        s = load_settings()
        keys = ("serial_port", "squelch", "tx_lead_seconds", "high_power", "tx_allowed", "frequency")
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
        from .config import DEFAULT_CONFIG_PATH, load_mumble_servers, load_secrets, load_settings

        # The same nick the server presents (entries.link_username): the callsign when set.
        try:
            settings = load_settings()
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


def _kv4p_connect_probe(report: _Report, cfg: dict, *, transport=None) -> None:
    """Open the kv4p, run the transport handshake, and print what the board reported — read-only.

    Never keys: ``connect()`` sends only the neutral desired state + ENABLE_STATUS_REPORTS (no
    PTT_REQUESTED). Uses :class:`Kv4pTransport` directly, **not** :class:`Kv4pHt`, whose constructor
    would eagerly reconcile and configure the module — a probe must observe, not mutate. Degrades to a
    clear FAIL line when the ``hardware`` extra or the device is absent, so it still runs in CI.

    ``transport`` is an injection seam for tests (an already-built transport); when ``None`` this owns
    the transport it builds and closes.
    """
    print("Connect probe (kv4p handshake — read-only, never keys):")
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
                module = RfModuleType(v.rf_module_type).name
            except ValueError:
                module = f"unknown({v.rf_module_type})"
            feats = " | ".join(f.name for f in FeatureFlag if v.features & f.value) or "(none)"
            report.pas(
                "HELLO received",
                f"fw v{v.ver}, module {module}, {v.min_radio_freq:.4f}–{v.max_radio_freq:.4f} MHz, "
                f"windowSize {v.window_size}, features {feats}",
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

        # The two host flags an operator most needs to see survive the reconcile before bench TX.
        if flags & DeviceStateFlag.TX_ALLOWED:
            report.pas("TX_ALLOWED set", "the firmware TX gate is open")
        else:
            report.warn(
                "TX_ALLOWED not set", "TX is gated off — set kv4p.tx_allowed = true before keying"
            )
        if flags & DeviceStateFlag.RADIO_CONFIG_VALID:
            report.pas("RADIO_CONFIG_VALID set", "module config applied")
        else:
            report.warn(
                "RADIO_CONFIG_VALID not set",
                "the module config has not been applied yet (expected before the first tune)",
            )
    finally:
        if owns:
            try:
                transport.close()
            except Exception:
                pass  # best-effort cleanup — a close failure must not mask the probe result


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
        report.pas("TX_ACTIVE confirmed", "the device reports it is transmitting")
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
            squelch=cfg["squelch"],
            tx_lead_seconds=cfg["tx_lead_seconds"],
            high_power=cfg["high_power"],
            tx_allowed=cfg["tx_allowed"],
            frequency=cfg["frequency"],
        )
    raise ValueError(f"doctor: unsupported backend {backend!r} (expected 'baofeng' or 'kv4p')")


def _vad_thresholds() -> tuple[float, float]:
    """The configured squelch open/close thresholds (audio.vad_on_rms / vad_off_rms), with defaults."""
    on, off = 500.0, 300.0
    try:
        from .config import load_settings

        s = load_settings()
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
    try:
        try:
            levels = measure_rx_levels(radio, seconds=seconds)
        except Exception as exc:
            # The AIOC capture is single-open; a running radio-server (or another app) holding the
            # card makes it drop out of PortAudio's device list, so the name no longer resolves.
            print(f"[FAIL] could not open the AIOC capture device: {exc}", file=sys.stderr)
            print("       The sound card is single-open — stop the running radio-server (or any")
            print("       other app using the AIOC) and retry. (Run plain `doctor` to check the")
            print("       device name if the server is not running.)")
            return 1
    finally:
        radio.close()

    if levels.frames == 0:
        print("[FAIL] no audio frames captured — is the AIOC capture device correct? run the")
        print("       plain `doctor` to check device resolution.")
        return 1

    on, off = _vad_thresholds()
    print(f"  frames captured : {levels.frames}")
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

    try:
        radio = _build_backend(cfg)
    except Exception as exc:
        where = "kv4p backend" if cfg.get("backend") == "kv4p" else "AIOC backend"
        print(f"could not open the {where}: {exc}", file=sys.stderr)
        return 1
    try:
        print(f"Keying + playing {freq:.0f} Hz for ~{seconds:.0f}s — listen on another radio...")
        radio.transmit(synth_tone(freq, seconds * 1000.0))  # one-shot transmit() self-keys + drains
    finally:
        radio.close()
    print("Done — line dropped.")
    heard = input("Did another radio hear the tone? [y/n]: ").strip().lower()
    if heard.startswith("y"):
        print("TX audio path confirmed. If it was faint, raise the AIOC playback level in alsamixer.")
        return 0
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
        load_dtmf_timeout,
        load_multimon_bin,
        resolve_decode_mode,
    )

    multimon_bin, timeout, decode_mode = "multimon-ng", 3.0, DECODE_MODE_AUTO
    try:
        from .config import load_settings

        s = load_settings()
        multimon_bin = load_multimon_bin(s)
        timeout = load_dtmf_timeout(s)
        decode_mode = load_dtmf_decode_mode(s)
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
        dtmf = StreamingDtmfInput(GoertzelStream(), framer, on_digit=_on_digit)
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
        from .config import load_settings

        if load_settings().get("server.backend") == "kv4p":
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

    if args.key_test:
        return _kv4p_key_test(cfg)
    if args.rx_level:
        return _rx_level(cfg, args.seconds or 5.0)
    if args.tx_tone:
        return _tx_tone(cfg, args.seconds or 5.0, args.freq)
    if args.dtmf:
        return _dtmf(cfg, args.seconds or 30.0)

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
    parser.add_argument("--host", help="Override the Murmur server host for --link.")
    parser.add_argument(
        "--port", type=int, help="Override the Murmur server port for --link (default 64738)."
    )
    parser.add_argument("--ptt-line", choices=[m.value for m in PttLine], help="Override the PTT line.")
    parser.add_argument(
        "--seconds",
        type=float,
        default=None,
        help="Duration for --rx-level / --tx-tone / --dtmf / --link (defaults: 5 / 5 / 30 / 10).",
    )
    parser.add_argument(
        "--freq", type=float, default=1000.0, help="Tone frequency in Hz for --tx-tone (default 1000)."
    )
    args = parser.parse_args(argv)
    backend = _resolve_doctor_backend(args)

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
