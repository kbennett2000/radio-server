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

Two M17 reflector modes are the bring-up instrument for the M17 link (ADR 0053): ``--link-listen``
opens a read-only ``LSTN`` session to the configured reflector and reports, live, the handshake and
its timing, the observed ``PING`` cadence, and — when someone transmits — the talker callsign, the
raw LSF bytes (hex, to eyeball ``TYPE``/``DST`` against the spec), the frame count/duration, and the
*measured* inter-frame interval; ``--link-decode`` adds a Codec2 decode of the payload to a WAV so
the audio can be judged for intelligibility. Both are read-only — they send only ``LSTN`` and the
``PONG`` keepalive, never ``CONN``, a stream frame, or PTT, so nothing reaches the air. They fail
loud by name on missing reflector config, an unresolvable host, a ``NACK``, or (decode) a missing
``libcodec2``. This is a self-contained raw observer, not a wrapper around the runtime ``M17Client``,
which deliberately hides the raw bytes and control-packet timing a bring-up needs to see.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import math
import os
import socket
import sys
from dataclasses import dataclass, field

from .activity.gate import frame_rms
from .link.m17.packet import build_disc, build_lstn, build_pong, parse_control, parse_stream
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

    start = clock()
    while True:
        now = clock()
        if now - start >= seconds:
            break
        _collect(dtmf.pump(radio.receive(), now))
    _collect(dtmf.flush(clock()))  # decode whatever is left in the tail buffer
    return "".join(raw), entries


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
    from .audio import DtmfFramer, MultimonDtmfDecoder, load_dtmf_timeout, load_multimon_bin

    multimon_bin, timeout = "multimon-ng", 3.0
    try:
        from .config import load_settings

        s = load_settings()
        multimon_bin = load_multimon_bin(s)
        timeout = load_dtmf_timeout(s)
    except Exception:
        pass  # defaults are fine for a diagnostic

    print(f"Listening for DTMF for ~{seconds:.0f}s (no transmit).")
    print("Key digits on the radio into the UV-5R: '#' submits an entry, '*' clears.\n")
    try:
        radio = _build_backend(cfg)
    except Exception as exc:
        print(f"[FAIL] could not open the AIOC backend: {exc}", file=sys.stderr)
        return 1

    decoder = MultimonDtmfDecoder(multimon_bin)
    framer = DtmfFramer(timeout=timeout)

    def _on_event(kind: str, value: str) -> None:
        print(f"  heard: {value}" if kind == "digit" else f"  ENTRY: {value}")

    try:
        try:
            raw, entries = collect_dtmf(radio, decoder, framer, seconds=seconds, on_event=_on_event)
        except RuntimeError as exc:  # multimon-ng missing — decode() raises with an install hint
            print(f"[FAIL] {exc}", file=sys.stderr)
            print("       install it: sudo apt install multimon-ng")
            return 1
        except Exception as exc:
            # The AIOC capture is single-open; a running server holding it fails here (see --rx-level).
            print(f"[FAIL] could not open the AIOC capture device: {exc}", file=sys.stderr)
            print("       Stop the running radio-server (single-open sound card) and retry.")
            return 1
    finally:
        radio.close()

    print()
    if raw:
        print(f"Decoded digits: {raw!r}; completed entries: {entries}")
        return 0
    print("No DTMF decoded. Check a strong RX signal first (`--rx-level`), and that you keyed digits")
    print("on the radio while this was listening (hold each tone ~100 ms+).")
    return 1


# --- M17 reflector listen / decode (ADR 0053) ----------------------------------------------------
#
# A read-only bring-up instrument for the M17 link. The runtime ``M17Client`` (ADR 0051) is the
# wrong shape here: it swallows control packets in ``_handle_control`` and discards the raw datagram
# in ``parse_stream``, so PING cadence and the raw LSF bytes — the whole point of a bench listen — are
# invisible through it, and the cycle mandate forbids touching it. So this opens its own ``LSTN``
# socket and reads the raw wire, reusing only the pure ``packet.py`` codec. It sends ``LSTN`` and the
# ``PONG`` keepalive and nothing else (never ``CONN``, a stream frame, or PTT): nothing reaches RF.

_LINK_DEFAULT_SECONDS = 60.0
_LINK_CONNECT_TIMEOUT = 5.0  # mirror M17Client's DEFAULT_CONNECT_TIMEOUT: ACKN/NACK wait
#: The raw LSF lives at bytes [6:34] of a stream frame — DST(6)+SRC(6)+TYPE(2)+META(14) = 28 bytes
#: (packet.py offsets _OFF_DST.._OFF_FN). Kept as the received bytes, never a re-encode, so TYPE/DST
#: can be eyeballed against the spec.
_LSF_START, _LSF_END = 6, 34


class _LinkConfigError(Exception):
    """No usable reflector configuration (missing host or callsign) — fail loud, by name."""


class _LinkHandshakeError(Exception):
    """The reflector refused the LSTN (NACK) or never answered (timeout)."""


@dataclass
class _StreamObs:
    """Live accounting for one inbound M17 stream (one StreamID), as its frames arrive."""

    stream_id: int
    talker: str | None
    lsf_hex: str  # raw hex of the first frame's 28 LSF bytes — the actual wire bytes
    started: float
    last_ts: float
    frames: int = 0
    intervals_ms: list = field(default_factory=list)  # deltas between consecutive frame arrivals
    ended: bool = False


@dataclass(frozen=True)
class _LinkReport:
    """What a listen window observed: handshake timing, PING cadence, streams, and dropped datagrams."""

    handshake_ms: float
    ping_count: int
    ping_intervals_ms: list
    streams: list
    dropped_source: int


class LinkObserver:
    """Pure, socket-free accounting for a read-only reflector listen (ADR 0053).

    Fed one datagram at a time as ``ingest(data, addr, now)`` and returns an optional reply to send
    back — a ``PONG`` for an inbound ``PING`` (the keepalive that keeps the ``LSTN`` session alive
    long enough to observe cadence). It keeps the **raw** LSF bytes of each stream's first frame (so
    ``TYPE``/``DST`` can be eyeballed against the spec — never a re-encode) and timestamps every
    arrival, so the inter-frame interval and the PING cadence are *measured* from the injected clock
    rather than assumed. No I/O and no asyncio: the socket driver (:func:`_observe_link`) owns those,
    which is what makes the interval/cadence math unit-testable with a synthetic ``now``.
    """

    def __init__(
        self, reflector_addr, callsign, *, codec=None, wav=None, on_event=None, on_handshake=None
    ) -> None:
        self._reflector_addr = reflector_addr
        self._callsign = callsign
        self._codec = codec
        self._wav = wav
        self._on_event = on_event
        self._on_handshake = on_handshake
        self.dropped_source = 0
        self._ping_ts: list[float] = []
        self._streams: dict[int, _StreamObs] = {}
        self._order: list[int] = []

    def ingest(self, data: bytes, addr, now: float) -> bytes | None:
        # Source validation first — the same outer gate M17Client applies (ADR 0051): a datagram
        # from anyone but the connected reflector is counted and dropped before any parse.
        if not self._addr_matches(addr):
            self.dropped_source += 1
            return None
        control = parse_control(data)
        if control is not None:
            return self._on_control(control, now)
        frame = parse_stream(data)
        if frame is not None:
            self._on_stream(data, frame, now)
        return None  # well-sourced but unparseable — ignore (untrusted-peer rule)

    def _addr_matches(self, addr) -> bool:
        ref = self._reflector_addr
        return ref is not None and addr[0] == ref[0] and addr[1] == ref[1]

    def _on_control(self, control, now: float) -> bytes | None:
        kind = control.kind
        if kind == "PING":
            self._ping_ts.append(now)
            if self._on_event is not None:
                self._on_event("ping", now)
            return build_pong(self._callsign)  # keepalive — UDP, not RF
        if kind in ("ACKN", "NACK") and self._on_handshake is not None:
            self._on_handshake(kind == "ACKN")
        return None

    def _on_stream(self, data: bytes, frame, now: float) -> None:
        st = self._streams.get(frame.stream_id)
        if st is None:
            st = _StreamObs(
                stream_id=frame.stream_id,
                talker=frame.src,
                lsf_hex=data[_LSF_START:_LSF_END].hex(),
                started=now,
                last_ts=now,
            )
            self._streams[frame.stream_id] = st
            self._order.append(frame.stream_id)
            if self._on_event is not None:
                self._on_event("stream_start", st)
        else:
            st.intervals_ms.append((now - st.last_ts) * 1000.0)
            st.last_ts = now
        st.frames += 1
        if self._codec is not None:
            decoded = self._codec.decode(frame.payload)  # 16 B → canonical 48 kHz AudioFrame
            if self._wav is not None:
                self._wav.writeframes(decoded.samples)
        if frame.last and not st.ended:
            st.ended = True
            if self._on_event is not None:
                self._on_event("stream_end", st)

    def streams(self) -> list:
        return [self._streams[i] for i in self._order]

    def ping_count(self) -> int:
        return len(self._ping_ts)

    def ping_intervals_ms(self) -> list:
        ts = self._ping_ts
        return [(b - a) * 1000.0 for a, b in zip(ts, ts[1:])]


async def _resolve_reflector(loop, host: str, port: int) -> tuple:
    """Resolve host/port to a concrete address, preferring IPv4 (mirrors M17Client._resolve)."""
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
    for family in (socket.AF_INET, socket.AF_INET6):
        for info in infos:
            if info[0] == family:
                return info[4]
    return infos[0][4]


async def _observe_link(cfg: dict, seconds: float, *, codec=None, wav=None, on_event=None) -> _LinkReport:
    """Open a read-only LSTN session, observe for ``seconds``, and return what was heard.

    Mirrors ``M17Client.connect`` (ADR 0051 ``client.py``): prefer-IPv4 resolve (``socket.gaierror``
    propagates), an *unconnected* datagram endpoint bound to ``bind_host``/``bind_port``, ``LSTN``,
    then await ``ACKN``/``NACK`` against ``_LINK_CONNECT_TIMEOUT``. It sends only ``LSTN``, ``PONG``
    (via the observer), and a best-effort ``DISC`` on teardown — never ``CONN``, a stream frame, or
    PTT. Raises :class:`_LinkHandshakeError` on ``NACK`` or timeout.
    """
    loop = asyncio.get_running_loop()
    reflector_addr = await _resolve_reflector(loop, cfg["reflector_host"], cfg["reflector_port"])
    handshake = loop.create_future()
    observer = LinkObserver(
        reflector_addr,
        cfg["callsign"],
        codec=codec,
        wav=wav,
        on_event=on_event,
        on_handshake=lambda ok: handshake.done() or handshake.set_result(ok),
    )

    class _ObserverProto(asyncio.DatagramProtocol):
        def connection_made(self, transport) -> None:
            self._transport = transport

        def datagram_received(self, data: bytes, addr) -> None:
            reply = observer.ingest(data, addr, loop.time())
            if reply is not None:
                self._transport.sendto(reply, reflector_addr)

        def error_received(self, exc) -> None:  # ICMP port-unreachable etc. — non-fatal for a listen
            pass

    transport, _ = await loop.create_datagram_endpoint(
        _ObserverProto, local_addr=(cfg["bind_host"], cfg["bind_port"])
    )
    try:
        t0 = loop.time()
        transport.sendto(build_lstn(cfg["callsign"], cfg["reflector_module"]), reflector_addr)
        try:
            acknowledged = await asyncio.wait_for(handshake, _LINK_CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            raise _LinkHandshakeError(
                f"no ACKN/NACK from {cfg['reflector_host']}:{cfg['reflector_port']} "
                f"(timed out after {_LINK_CONNECT_TIMEOUT:.0f}s)"
            )
        handshake_ms = (loop.time() - t0) * 1000.0
        if not acknowledged:
            raise _LinkHandshakeError(
                f"reflector NACKed the LSTN to module {cfg['reflector_module']} "
                "(module full/blocked, or callsign not permitted?)"
            )
        await asyncio.sleep(seconds)  # observe
    finally:
        try:
            transport.sendto(build_disc(cfg["callsign"]), reflector_addr)  # best-effort unlink
        except Exception:
            pass
        transport.close()

    return _LinkReport(
        handshake_ms=handshake_ms,
        ping_count=observer.ping_count(),
        ping_intervals_ms=observer.ping_intervals_ms(),
        streams=observer.streams(),
        dropped_source=observer.dropped_source,
    )


def _resolve_link_cfg(settings) -> dict:
    """Read the M17 link connection config from ``settings``; raise :class:`_LinkConfigError` by name.

    Unlike ``_baofeng_config`` (which defaults silently), an unconfigured reflector is fatal — a
    listen with no reflector has nothing to point at. ``station.callsign`` is the M17 source (reused,
    no second callsign); it is a required setting, so an unset value raises here too.
    """
    host = settings.get("link.reflector_host")
    if not host:
        raise _LinkConfigError(
            "no reflector configured — set link.reflector_host and link.reflector_module in "
            "radio.toml (see docs/deployment.md §6)"
        )
    try:
        callsign = settings.get("station.callsign")
    except Exception as exc:
        raise _LinkConfigError("station.callsign is not set — the M17 source callsign is required") from exc
    if not callsign:
        raise _LinkConfigError("station.callsign is not set — the M17 source callsign is required")
    return {
        "reflector_host": host,
        "reflector_port": settings.get("link.reflector_port"),
        "reflector_module": settings.get("link.reflector_module"),
        "bind_host": settings.get("link.bind_host"),
        "bind_port": settings.get("link.bind_port"),
        "callsign": callsign,
    }


def _load_link_cfg_or_fail() -> dict | None:
    """Resolve the link config, printing a ``[FAIL]`` and returning ``None`` if it is missing."""
    try:
        from .config import load_settings

        settings = load_settings()
    except Exception as exc:
        print(f"[FAIL] could not load configuration: {exc}", file=sys.stderr)
        return None
    try:
        return _resolve_link_cfg(settings)
    except _LinkConfigError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return None


def _mean(xs) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _link_on_event(kind: str, obj) -> None:
    """Live per-event lines during a listen (PING is summarized at the end, not printed per-packet)."""
    if kind == "stream_start":
        print(f"  ◆ stream {obj.stream_id} from {obj.talker or '?'} — LSF {obj.lsf_hex}")
    elif kind == "stream_end":
        iv = obj.intervals_ms
        interval = f", ~{_mean(iv):.0f} ms/frame" if iv else ""
        print(f"    └ {obj.frames} frame(s), {obj.last_ts - obj.started:.2f}s{interval}")


def _print_link_report(report: _LinkReport, seconds: float) -> None:
    print("\n── link listen report ──")
    print(f"  handshake      : ACKN in {report.handshake_ms:.0f} ms")
    if report.ping_count >= 2:
        pi = report.ping_intervals_ms
        print(
            f"  PING cadence   : {report.ping_count} PINGs, mean {_mean(pi):.0f} ms "
            f"(min {min(pi):.0f}, max {max(pi):.0f})"
        )
    else:
        print(
            f"  PING cadence   : {report.ping_count} PING(s) in {seconds:.0f}s "
            "(need ≥2 to measure a cadence)"
        )
    if report.streams:
        for st in report.streams:
            iv = st.intervals_ms
            interval = (
                f"~{_mean(iv):.0f} ms/frame (min {min(iv):.0f}, max {max(iv):.0f})"
                if iv
                else "n/a (single frame)"
            )
            flag = "" if st.ended else "  [no EOT — stream cut off]"
            print(
                f"  stream {st.stream_id} : talker {st.talker or '?'}, {st.frames} frame(s), "
                f"{st.last_ts - st.started:.2f}s, {interval}{flag}"
            )
            print(f"             raw LSF: {st.lsf_hex}")
    else:
        print("  streams        : none heard (nobody transmitted during the window)")
    if report.dropped_source:
        print(
            f"  dropped (src)  : {report.dropped_source}  "
            "[!] non-zero on a quiet reflector is suspicious — check the bind/route"
        )
    else:
        print("  dropped (src)  : 0")


def _link_listen(seconds: float) -> int:
    """LSTN a real reflector read-only and report handshake, PING cadence, and any inbound streams."""
    cfg = _load_link_cfg_or_fail()
    if cfg is None:
        return 1
    print(
        f"Listening (LSTN) to {cfg['reflector_host']}:{cfg['reflector_port']} module "
        f"{cfg['reflector_module']} as {cfg['callsign']} for ~{seconds:.0f}s — no keying, no radio.\n"
    )
    try:
        report = asyncio.run(_observe_link(cfg, seconds, on_event=_link_on_event))
    except socket.gaierror as exc:
        print(f"[FAIL] could not resolve reflector host {cfg['reflector_host']!r}: {exc}", file=sys.stderr)
        return 1
    except _LinkHandshakeError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    _print_link_report(report, seconds)
    return 0


def _link_decode(seconds: float, out: str) -> int:
    """As --link-listen, plus Codec2-decode the payload to a WAV so the audio can be judged by ear."""
    try:
        from .audio.codec2 import Codec2

        codec = Codec2()  # fails loud naming libcodec2 + the codec2 extra (ADR 0049), before any socket
    except RuntimeError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    cfg = _load_link_cfg_or_fail()
    if cfg is None:
        codec.close()
        return 1

    import wave

    try:
        wav = wave.open(out, "wb")
        wav.setnchannels(CANONICAL_FORMAT.channels)
        wav.setsampwidth(CANONICAL_FORMAT.width)
        wav.setframerate(CANONICAL_FORMAT.rate)
    except Exception as exc:
        print(f"[FAIL] could not open WAV output {out!r}: {exc}", file=sys.stderr)
        codec.close()
        return 1

    print(
        f"Listening (LSTN) + decoding to {out} for ~{seconds:.0f}s — no keying, no radio.\n"
    )
    try:
        report = asyncio.run(
            _observe_link(cfg, seconds, codec=codec, wav=wav, on_event=_link_on_event)
        )
    except socket.gaierror as exc:
        print(f"[FAIL] could not resolve reflector host {cfg['reflector_host']!r}: {exc}", file=sys.stderr)
        return 1
    except _LinkHandshakeError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    finally:
        wav.close()
        codec.close()

    _print_link_report(report, seconds)
    total = sum(st.frames for st in report.streams)
    print(
        f"\nWrote {total} decoded frame(s) to {out} (48 kHz mono s16le). "
        "Play it back to judge intelligibility."
    )
    return 0


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
        "--link-listen",
        action="store_true",
        help="LSTN the configured M17 reflector and report handshake, PING cadence, talkers, and "
        "raw LSF hex (read-only — no keying).",
    )
    mode.add_argument(
        "--link-decode",
        action="store_true",
        help="As --link-listen, plus Codec2-decode inbound audio to a WAV (--out); needs libcodec2.",
    )
    parser.add_argument("--serial-port", help="Override the PTT serial device path.")
    parser.add_argument("--ptt-line", choices=[m.value for m in PttLine], help="Override the PTT line.")
    parser.add_argument(
        "--seconds",
        type=float,
        default=None,
        help="Duration for --rx-level / --tx-tone / --dtmf / --link-listen / --link-decode "
        "(defaults: 5 / 5 / 30 / 60 / 60).",
    )
    parser.add_argument(
        "--freq", type=float, default=1000.0, help="Tone frequency in Hz for --tx-tone (default 1000)."
    )
    parser.add_argument(
        "--out",
        default="m17-decode.wav",
        help="WAV output path for --link-decode (default: m17-decode.wav in the current directory).",
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
    if args.link_listen:
        return _link_listen(args.seconds or _LINK_DEFAULT_SECONDS)
    if args.link_decode:
        return _link_decode(args.seconds or _LINK_DEFAULT_SECONDS, args.out)

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
        print("  • `--link-listen` — LSTN an M17 reflector; report handshake, PING cadence, raw LSF")
        print("  • `--link-decode` — as above, plus decode inbound audio to a WAV (needs libcodec2)")
        print("Then run the server with server.backend=baofeng (see docs/hardware-bringup.md).")
        return 0
    print("Some checks failed — see [FAIL] lines above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
