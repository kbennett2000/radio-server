"""``python -m radio_server.doctor`` — AIOC/Baofeng hardware diagnostic (ADR 0029).

Read-only checks that answer "is the AIOC ready?": the USB sound card enumerates with 48 kHz
capture + playback, the PTT serial port exists and opens, and the current user can reach it
(``dialout``). Everything here is safe to run anywhere — it never keys the transmitter and degrades
gracefully (a clear FAIL line) when the ``hardware`` extra or the device is absent, so it also runs
harmlessly in CI.

Two audio-level modes help tune the levels once the plumbing works (ADR 0029 bring-up):
``--rx-level`` reads the AIOC capture for a few seconds and reports the received RMS/peak against the
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
            chosen = next((e for e in entries if e.name == entry_name), None)
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
            cfg["password"] = load_secrets().get(mumble_password_secret(chosen.name)) or ""
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


def _build_backend(cfg: dict):
    """Construct the real AiocBaofeng from resolved config (opens serial with lines held low)."""
    return create_radio(
        "baofeng",
        serial_port=cfg["serial_port"],
        ptt_line=cfg["ptt_line"],
        input_device=cfg["input_device"],
        output_device=cfg["output_device"],
        blocksize=cfg["blocksize"],
    )


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
        print(f"[FAIL] could not open the AIOC backend: {exc}", file=sys.stderr)
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
    if category == "silent":
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
    line = cfg["ptt_line"]
    print("=" * 72)
    print("  RF TX-TONE TEST — this WILL key the transmitter and play a tone.")
    print("  Connect a DUMMY LOAD (or be certain it is safe to transmit) before continuing.")
    print(f"  It will key (PTT line: {line}) and play a {freq:.0f} Hz tone for ~{seconds:.0f}s.")
    print("=" * 72)
    if input("Type CONFIRM (all caps) to proceed: ").strip() != "CONFIRM":
        print("Aborted — nothing was keyed.")
        return 1

    from .audio.tone import synth_tone

    try:
        radio = _build_backend(cfg)
    except Exception as exc:
        print(f"could not open the AIOC backend: {exc}", file=sys.stderr)
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
        DECODE_MODE_BUFFERED,
        BufferedDtmfInput,
        DtmfFramer,
        MultimonDtmfDecoder,
        MultimonStream,
        StreamingDtmfInput,
        load_dtmf_decode_mode,
        load_dtmf_timeout,
        load_multimon_bin,
    )

    multimon_bin, timeout, decode_mode = "multimon-ng", 3.0, "streaming"
    try:
        from .config import load_settings

        s = load_settings()
        multimon_bin = load_multimon_bin(s)
        timeout = load_dtmf_timeout(s)
        decode_mode = load_dtmf_decode_mode(s)
    except Exception:
        pass  # defaults are fine for a diagnostic

    print(f"Listening for DTMF for ~{seconds:.0f}s (no transmit, {decode_mode} decode).")
    print("Key digits on the radio into the UV-5R: '#' submits an entry, '*' clears.\n")
    try:
        radio = _build_backend(cfg)
    except Exception as exc:
        print(f"[FAIL] could not open the AIOC backend: {exc}", file=sys.stderr)
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

    # Drive the same input the live controller uses (ADR 0038): streaming by default, buffered when
    # `dtmf.decode_mode=buffered`, so this diagnostic validates the exact decode path the server runs.
    if decode_mode == DECODE_MODE_BUFFERED:
        dtmf = BufferedDtmfInput(MultimonDtmfDecoder(multimon_bin), framer, on_digit=_on_digit)
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
    # The import split mirrors _check_audio: ImportError = the extra isn't installed, OSError =
    # opuslib found no system libopus.
    try:
        import pymumble_py3  # noqa: F401
    except ImportError:
        report.fail(
            "pymumble not installed",
            "install the mumble extra: pip install 'radio-server[mumble]'",
        )
        return 1
    except OSError:
        report.fail("libopus not found", "install the system library: sudo apt install libopus0")
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m radio_server.doctor",
        description="AIOC/Baofeng hardware diagnostic (ADR 0029).",
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
    parser.add_argument("--serial-port", help="Override the PTT serial device path.")
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
    if args.link is not None:
        link_cfg = _mumble_config(args.link)
        if args.host:
            link_cfg["host"] = args.host
            link_cfg["error"] = None  # an explicit host overrides entry selection entirely
        if args.port:
            link_cfg["port"] = args.port
        return _link(link_cfg, args.seconds or 10.0)

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
