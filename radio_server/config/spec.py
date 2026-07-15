"""The setting schema — one :class:`SettingSpec` per configurable value (ADR 0025).

This registry is the single source of truth the whole config system turns on: it drives
resolution (`radio_server.config.settings`), the round-trip writer (`radio_server.config.save`),
the shipped ``radio.toml.example``, and — in later cycles — the settings REST API (26) and the web
settings screen (27). Every spec references the module-local ``DEFAULT_*`` constant for its
default, so those constants stay the one place a default is written; the schema only points at them.

**All per-field logic lives in the spec's ``coerce`` callable**, because the fields genuinely
diverge (empty-string handling, exception types, two different boolean grammars — see ADR 0025).
A single uniform validator would flatten and break tested behavior, so each field carries the exact
coercion its old ``load_*`` loader had.

Secrets (``RADIO_TOTP_SECRET`` / ``RADIO_API_TOKEN``) are deliberately NOT in this registry — they
live on a separate channel (`radio_server.config.secrets`) and are never rendered or serialized.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from ..activity.gate import (
    DEFAULT_SQUELCH_MODE,
    DEFAULT_VAD_HANG,
    DEFAULT_VAD_OFF_RMS,
    DEFAULT_VAD_ON_RMS,
    SquelchMode,
)
from ..audio.dtmf import (
    DEFAULT_DTMF_BUFFER_SECONDS,
    DEFAULT_DTMF_TIMEOUT,
    DEFAULT_MULTIMON_BIN,
)
from ..backends.aioc_baofeng import (
    DEFAULT_BLOCKSIZE as DEFAULT_BAOFENG_BLOCKSIZE,
    DEFAULT_INPUT_DEVICE as DEFAULT_BAOFENG_INPUT_DEVICE,
    DEFAULT_OUTPUT_DEVICE as DEFAULT_BAOFENG_OUTPUT_DEVICE,
    DEFAULT_PTT_LINE as DEFAULT_BAOFENG_PTT_LINE,
    DEFAULT_SERIAL_PORT as DEFAULT_BAOFENG_SERIAL_PORT,
    PttLine,
)
from ..controller.engine import DEFAULT_CONTROLLER_POLL, DEFAULT_SESSION_TIMEOUT
from ..eventlog.sink import DEFAULT_LOG_PATH
from ..recording.recorder import (
    DEFAULT_RECORD_MAX_SECONDS,
    DEFAULT_RECORD_MODE,
    DEFAULT_RECORD_PATH,
    RecordMode,
)
from ..scan.engine import (
    DEFAULT_SCAN_DWELL,
    DEFAULT_SCAN_MODE,
    DEFAULT_SCAN_POLL,
    DEFAULT_SCAN_SETTLE,
    ResumeMode,
)
from ..services.cw import DEFAULT_CW_TONE_HZ, DEFAULT_CW_WPM
from ..services.station_id import DEFAULT_ID_INTERVAL, MAX_ID_INTERVAL
from ..services.time_service import _DEFAULT_TZ as DEFAULT_TZ
from ..services.voice_id import DEFAULT_ID_MODE, ID_MODES
from ..tx.session import DEFAULT_TX_IDLE_TIMEOUT

#: Bootstrap/server defaults that had no ``load_*`` loader (they were inline ``env.get`` calls in
#: the composition root / entrypoint). Their canonical home is here so the schema owns them without
#: importing from ``api.app`` / ``__main__`` (which import this package — that would be a cycle).
DEFAULT_BACKEND = "mock"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
#: The built web-UI bundle. Computed relative to the package root, identical to the path the API
#: layer used before (``<repo>/web/dist``): ``config/spec.py`` → ``config`` → ``radio_server`` → repo.
DEFAULT_WEB_DIR = str(Path(__file__).resolve().parent.parent.parent / "web" / "dist")
DEFAULT_MOCK_CAT = True

#: Returned by a coercer to mean "no usable value here — fall through to the spec default". Distinct
#: from ``None``, which for some fields (e.g. a token) would be a real value.
USE_DEFAULT = object()
#: Marker stored as a required setting's resolved value when it is absent. `Settings.get` turns a
#: read of this into a fail-loud, preserving the old point-of-use behavior of callsign / tts.voice.
UNSET_REQUIRED = object()
#: Sentinel used as a spec's ``default`` to mark it required (no baked-in default).
REQUIRED = object()


# --- Reusable coercers -----------------------------------------------------------------------
# Each accepts a raw value (a string from a TOML string / test dict, or an already-native TOML
# scalar) plus the dotted key (for error messages), and returns the typed value, ``USE_DEFAULT``,
# or raises the SAME exception type the old loader raised (mostly RuntimeError; ZoneInfo is the
# documented exception — see coerce_zoneinfo).


def _blank(raw: object) -> bool:
    """Whether ``raw`` means 'unset' — ``None`` or an empty/whitespace-only string."""
    return raw is None or (isinstance(raw, str) and raw.strip() == "")


def coerce_positive_float(raw: object, key: str) -> object:
    """A strictly-positive float; blank → default. Folds the four identical ``_load_positive_float``
    copies (cw, activity, scan, controller) into one."""
    if _blank(raw):
        return USE_DEFAULT
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{key}={raw!r} is not a number") from exc
    if value <= 0:
        raise RuntimeError(f"{key}={raw!r} must be positive")
    return value


def coerce_nonneg_float(raw: object, key: str) -> object:
    """A non-negative float (0 allowed); blank → default. Used only by ``audio.vad_hang``."""
    if _blank(raw):
        return USE_DEFAULT
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{key}={raw!r} is not a number") from exc
    if value < 0:
        raise RuntimeError(f"{key}={raw!r} must not be negative")
    return value


def coerce_id_interval(raw: object, key: str) -> object:
    """A positive float capped at the Part-97 ceiling (`MAX_ID_INTERVAL`); blank → default. A value
    above the ceiling is rejected, not clamped."""
    if _blank(raw):
        return USE_DEFAULT
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{key}={raw!r} is not a number") from exc
    if value <= 0:
        raise RuntimeError(f"{key}={raw!r} must be positive")
    if value > MAX_ID_INTERVAL:
        raise RuntimeError(
            f"{key}={raw!r} exceeds the {MAX_ID_INTERVAL} s Part-97 identification ceiling"
        )
    return value


def coerce_int(raw: object, key: str) -> object:
    """A base-10 integer; blank → default."""
    if _blank(raw):
        return USE_DEFAULT
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{key}={raw!r} is not an integer") from exc


def coerce_str(raw: object, key: str) -> object:
    """A marked-default string: blank → default, otherwise the value as-is (matches the old
    ``env.get(...) or DEFAULT`` / ``if not value`` loaders)."""
    if _blank(raw):
        return USE_DEFAULT
    return str(raw)


def coerce_required_str(raw: object, key: str) -> object:
    """A required non-empty string. A present-but-blank value fails loud AT LOAD (matching e.g.
    ``load_callsign({RADIO_CALLSIGN: ""})``); an ABSENT value is handled by resolution as
    `UNSET_REQUIRED` and fails loud lazily on first read."""
    if _blank(raw):
        raise RuntimeError(f"{key} is set but empty; provide a real value")
    return str(raw)


def coerce_zoneinfo(raw: object, key: str) -> object:
    """Validate a tz name by constructing `ZoneInfo` (an unknown zone raises
    ``ZoneInfoNotFoundError`` — the SAME type ``load_timezone`` raised, asserted by
    ``test_time_service``), then store the name string (TOML-native, round-trippable). Blank →
    default. `load_timezone` reconstructs the `ZoneInfo` at use."""
    if _blank(raw):
        return USE_DEFAULT
    name = str(raw)
    ZoneInfo(name)  # validate; let ZoneInfoNotFoundError propagate
    return name


def coerce_id_mode(raw: object, key: str) -> object:
    """The station-ID mode: ``cw`` or ``voice`` (`ID_MODES`), matched after ``.strip().lower()``;
    blank → default."""
    if _blank(raw):
        return USE_DEFAULT
    mode = str(raw).strip().lower()
    if mode not in ID_MODES:
        raise RuntimeError(f"{key}={raw!r}: choose one of {', '.join(ID_MODES)}")
    return mode


def coerce_enum(enum_cls: type[Enum], *, strip: bool) -> Callable[[object, str], object]:
    """Build a coercer for a `StrEnum`, matched after ``.lower()`` (and ``.strip()`` when the old
    loader did — squelch/scan_mode/record_mode do not strip, only id_mode strips). Blank → default;
    an already-typed member passes through."""

    def _coerce(raw: object, key: str) -> object:
        if _blank(raw):
            return USE_DEFAULT
        if isinstance(raw, enum_cls):
            return raw
        text = str(raw)
        text = text.strip().lower() if strip else text.lower()
        try:
            return enum_cls(text)
        except ValueError as exc:
            choices = ", ".join(m.value for m in enum_cls)  # type: ignore[attr-defined]
            raise RuntimeError(f"{key}={raw!r} is not one of: {choices}") from exc

    return _coerce


#: Strict boolean grammar (recording toggles): recognized on/off spellings only, anything else
#: fails loud. Mirrors `recording.recorder`'s ``_TRUTHY`` / ``_FALSEY``.
_STRICT_TRUE = frozenset({"1", "true", "on", "yes"})
_STRICT_FALSE = frozenset({"", "0", "false", "off", "no"})


def coerce_strict_bool(raw: object, key: str) -> object:
    """A strict boolean: an already-native bool passes through; a string must be a recognized
    on/off spelling, else fail loud. A blank string is False (it is in the falsey set)."""
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return USE_DEFAULT
    text = str(raw).strip().lower()
    if text in _STRICT_TRUE:
        return True
    if text in _STRICT_FALSE:
        return False
    raise RuntimeError(f"{key}={raw!r} is not a boolean (on/off/true/false/1/0/yes/no)")


#: Permissive off-set for ``server.mock_cat``: anything NOT here (case-insensitive) is truthy, so an
#: audio-only mock is a hard-to-misread explicit opt-out. Mirrors `api.app`'s ``_MOCK_CAT_OFF``.
_PERMISSIVE_OFF = frozenset({"0", "off", "false", "no", "n"})


def coerce_permissive_off_bool(raw: object, key: str) -> object:
    """A permissive boolean: an already-native bool passes through; a string is False only when it
    is in the off-set, otherwise True. Never fails loud (matches the old ``_load_mock_cat``)."""
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return USE_DEFAULT
    return str(raw).strip().lower() not in _PERMISSIVE_OFF


# --- The spec + registry ---------------------------------------------------------------------


@dataclass(frozen=True)
class SettingSpec:
    """One configurable setting: its dotted key, group, default, coercion, and human description."""

    key: str
    #: The legacy ``RADIO_*`` env var name. Now metadata (used by docs / the example generator); the
    #: only settings still read from env are the secrets, which are not in this registry.
    env: str
    group: str
    #: A concrete default value (already the coerced type), or the `REQUIRED` sentinel.
    default: object
    coerce: Callable[[object, str], object]
    description: str = ""

    @property
    def required(self) -> bool:
        return self.default is REQUIRED

    @property
    def leaf(self) -> str:
        """The key without its group prefix — the name under the ``[group]`` TOML table."""
        return self.key.split(".", 1)[1]


def _s(key, env, group, default, coerce, description) -> SettingSpec:
    return SettingSpec(key=key, env=env, group=group, default=default, coerce=coerce, description=description)


SETTINGS: tuple[SettingSpec, ...] = (
    # --- Station / identity ------------------------------------------------------------------
    _s(
        "station.callsign", "RADIO_CALLSIGN", "station", REQUIRED, coerce_required_str,
        "Your FCC callsign. Every transmission the server makes is legally your station, so a "
        "callsign is required before the controller or any voice service will key the radio — "
        "there is no default and no un-ID'd operation. Set it to the callsign licensed to this "
        "station.",
    ),
    _s(
        "station.id_interval", "RADIO_ID_INTERVAL", "station", DEFAULT_ID_INTERVAL, coerce_id_interval,
        "Maximum seconds between automatic station identifications. Part 97 requires an ID at least "
        f"every 10 minutes, so this is capped at {MAX_ID_INTERVAL:.0f} s (a larger value is rejected, "
        "not clamped). Lower it if you want to ID more often; it cannot legally go higher.",
    ),
    _s(
        "station.id_mode", "RADIO_ID_MODE", "station", DEFAULT_ID_MODE, coerce_id_mode,
        "How the station ID is sent: 'cw' (Morse sidetone) or 'voice' (spoken via the TTS voice). "
        "'voice' requires a configured tts.voice and does not silently fall back to CW.",
    ),
    _s(
        "station.cw_wpm", "RADIO_CW_WPM", "station", DEFAULT_CW_WPM, coerce_positive_float,
        "CW identification speed in words per minute (PARIS timing). Raise for faster IDs, lower for "
        "easier copy. Applies only when station.id_mode is 'cw'.",
    ),
    _s(
        "station.cw_tone_hz", "RADIO_CW_TONE_HZ", "station", DEFAULT_CW_TONE_HZ, coerce_positive_float,
        "CW sidetone frequency in Hz for the Morse ID. A matter of preference/audibility on the "
        "receiving end; typical values are 500-800 Hz.",
    ),
    # --- Audio / squelch (RX gate) -----------------------------------------------------------
    _s(
        "audio.squelch", "RADIO_SQUELCH", "audio", SquelchMode(DEFAULT_SQUELCH_MODE),
        coerce_enum(SquelchMode, strip=False),
        "RX activity gate: 'off' relays all received audio, 'audio' uses the software VAD "
        "(vad_* thresholds below), 'cat' uses the radio's hardware busy line (TM-V71A only). "
        "Gating is what lets recording segment one file per received transmission.",
    ),
    _s(
        "audio.vad_on_rms", "RADIO_VAD_ON_RMS", "audio", DEFAULT_VAD_ON_RMS, coerce_positive_float,
        "Software-VAD open threshold as int16 RMS: the gate opens when received audio rises above "
        "this. Verify against your hardware's noise floor. Must sit above vad_off_rms (hysteresis).",
    ),
    _s(
        "audio.vad_off_rms", "RADIO_VAD_OFF_RMS", "audio", DEFAULT_VAD_OFF_RMS, coerce_positive_float,
        "Software-VAD close threshold as int16 RMS: the gate closes when audio falls below this. "
        "Must be lower than vad_on_rms — the two form the hysteresis band that prevents chatter.",
    ),
    _s(
        "audio.vad_hang", "RADIO_VAD_HANG", "audio", DEFAULT_VAD_HANG, coerce_nonneg_float,
        "Seconds to hold the RX gate open after audio drops below vad_off_rms, so brief pauses in "
        "speech don't chop a transmission into fragments. 0 closes the instant the level drops.",
    ),
    # --- DTMF decode -------------------------------------------------------------------------
    _s(
        "dtmf.multimon_bin", "RADIO_MULTIMON_BIN", "dtmf", DEFAULT_MULTIMON_BIN, coerce_str,
        "Path or name of the multimon-ng binary used to decode DTMF from received audio. Leave as "
        "the default if multimon-ng is on PATH; set an absolute path otherwise.",
    ),
    _s(
        "dtmf.timeout", "RADIO_DTMF_TIMEOUT", "dtmf", DEFAULT_DTMF_TIMEOUT, coerce_positive_float,
        "Seconds of inter-digit silence after which a DTMF entry is considered complete. Raise if "
        "callers key digits slowly; lower for snappier command turnaround.",
    ),
    _s(
        "dtmf.buffer_seconds", "RADIO_DTMF_BUFFER_SECONDS", "dtmf", DEFAULT_DTMF_BUFFER_SECONDS,
        coerce_positive_float,
        "Seconds of received audio to accumulate before each DTMF decode. A single ~20 ms capture "
        "block is too short for multimon-ng to lock onto a tone, so the controller buffers this long "
        "first. Verify against hardware: raise if keyed digits don't decode, lower for less latency.",
    ),
    # --- Recording ---------------------------------------------------------------------------
    _s(
        "recording.enabled", "RADIO_RECORD", "recording", False, coerce_strict_bool,
        "Whether received (RX) audio is recorded to disk as WAV segments. Off by default. When on, "
        "audio is written under recording.path; pair with a squelch mode for one file per "
        "transmission (otherwise segments roll purely on recording.max_seconds).",
    ),
    _s(
        "recording.path", "RADIO_RECORD_PATH", "recording", DEFAULT_RECORD_PATH, coerce_str,
        "Directory for recorded WAV segments (RX and, if enabled, TX). Created/opened fail-loud at "
        "startup, so a set-but-unwritable path stops the server rather than silently dropping audio.",
    ),
    _s(
        "recording.mode", "RADIO_RECORD_MODE", "recording", DEFAULT_RECORD_MODE,
        coerce_enum(RecordMode, strip=False),
        "What RX audio is captured: 'gated' records only what the squelch/VAD passes as live. "
        "'full' (pre-gate capture) is reserved and not yet implemented.",
    ),
    _s(
        "recording.max_seconds", "RADIO_RECORD_MAX_SECONDS", "recording", DEFAULT_RECORD_MAX_SECONDS,
        coerce_positive_float,
        "Per-segment duration cap in seconds — the always-on safety rail bounding a single WAV's "
        "size (default one hour). There is no disable sentinel; a segment always rolls at this cap.",
    ),
    _s(
        "recording.tx", "RADIO_RECORD_TX", "recording", False, coerce_strict_bool,
        "Whether transmitted (TX) audio is recorded, independent of recording.enabled (which gates "
        "RX). TX segments are written to recording.path with a 'tx-' filename prefix.",
    ),
    # --- TTS ---------------------------------------------------------------------------------
    _s(
        "tts.voice", "RADIO_TTS_VOICE", "tts", REQUIRED, coerce_required_str,
        "Filesystem path to a Piper voice model (.onnx), with its .onnx.json sidecar beside it. "
        "Required for voice services and voice-mode station ID; there is no baked-in voice, so this "
        "fails loud when a voice feature is used without it. Not needed for a CW-only mock setup.",
    ),
    # --- Time --------------------------------------------------------------------------------
    _s(
        "time.tz", "RADIO_TZ", "time", DEFAULT_TZ, coerce_zoneinfo,
        "IANA timezone name (e.g. 'America/New_York') the time-announce service speaks in. An "
        "unknown zone fails loud. Defaults to UTC.",
    ),
    # --- TX ----------------------------------------------------------------------------------
    _s(
        "tx.idle_timeout", "RADIO_TX_IDLE_TIMEOUT", "tx", DEFAULT_TX_IDLE_TIMEOUT, coerce_positive_float,
        "Seconds of silence on an inbound /audio/tx stream before PTT is dropped automatically, so a "
        "stalled client cannot hold the transmitter keyed. Verify against hardware keying latency.",
    ),
    # --- Scan --------------------------------------------------------------------------------
    _s(
        "scan.settle", "RADIO_SCAN_SETTLE", "scan", DEFAULT_SCAN_SETTLE, coerce_positive_float,
        "Seconds to wait after retuning before sampling a channel for activity, letting the radio's "
        "PLL/AGC settle so a freshly-tuned channel isn't misjudged as busy or clear.",
    ),
    _s(
        "scan.poll", "RADIO_SCAN_POLL", "scan", DEFAULT_SCAN_POLL, coerce_positive_float,
        "Seconds between activity checks while the scanner dwells on a channel. Lower reacts faster "
        "to a signal appearing; higher is gentler on the CAT link.",
    ),
    _s(
        "scan.dwell", "RADIO_SCAN_DWELL", "scan", DEFAULT_SCAN_DWELL, coerce_positive_float,
        "Seconds the scanner holds on an active channel before resuming, per the resume mode below.",
    ),
    _s(
        "scan.mode", "RADIO_SCAN_MODE", "scan", ResumeMode(DEFAULT_SCAN_MODE),
        coerce_enum(ResumeMode, strip=False),
        "How scanning resumes after a hit: 'carrier' resumes when the signal drops, 'timed' resumes "
        "after scan.dwell seconds regardless, 'hold' stops on the active channel.",
    ),
    # --- Controller --------------------------------------------------------------------------
    _s(
        "controller.poll", "RADIO_CONTROLLER_POLL", "controller", DEFAULT_CONTROLLER_POLL,
        coerce_positive_float,
        "Seconds between iterations of the live controller loop (how often it services received "
        "audio / DTMF). Lower is more responsive; higher lowers idle CPU.",
    ),
    _s(
        "controller.session_timeout", "RADIO_SESSION_TIMEOUT", "controller", DEFAULT_SESSION_TIMEOUT,
        coerce_positive_float,
        "Seconds of inactivity after which an authenticated over-RF session expires and the operator "
        "must re-authenticate. Keep short — access is gated, not secure; everything is in the clear.",
    ),
    # --- Logging -----------------------------------------------------------------------------
    _s(
        "logging.path", "RADIO_LOG_PATH", "logging", DEFAULT_LOG_PATH, coerce_str,
        "Path to the JSONL station/operating log (every transmission, session, and command event is "
        "appended here). Opened fail-loud at startup if unwritable.",
    ),
    # --- Server / web ------------------------------------------------------------------------
    _s(
        "server.backend", "RADIO_BACKEND", "server", DEFAULT_BACKEND, coerce_str,
        "Which radio backend to drive: 'mock' (software-only, the default), 'v71' (TM-V71A), or "
        "'baofeng' (UV-5R via the AIOC cable — see the [baofeng] section). 'v71' is not yet "
        "implemented and raises if selected.",
    ),
    _s(
        "server.host", "RADIO_HOST", "server", DEFAULT_HOST, coerce_str,
        "Bind address for the HTTP/WebSocket server. Defaults to loopback (127.0.0.1) — safe by "
        "default. Set to 0.0.0.0 to serve the LAN the gateway is meant for.",
    ),
    _s(
        "server.port", "RADIO_PORT", "server", DEFAULT_PORT, coerce_int,
        "TCP port the server binds. Defaults to 8000.",
    ),
    _s(
        "server.web_dir", "RADIO_WEB_DIR", "server", DEFAULT_WEB_DIR, coerce_str,
        "Directory of the built web UI served at '/'. Defaults to the repo's web/dist. An unbuilt "
        "directory serves a 'run the build' placeholder rather than crashing.",
    ),
    _s(
        "server.mock_cat", "RADIO_MOCK_CAT", "server", DEFAULT_MOCK_CAT, coerce_permissive_off_bool,
        "Developer toggle (mock backend only): whether the mock advertises CAT tuning. On by default "
        "(a full-CAT mock); set off/0/false/no/n for an audio-only mock so the UI greys out tuning "
        "controls, demonstrating the Baofeng-mode capability split without hardware.",
    ),
    # --- Baofeng / AIOC hardware backend (ADR 0029; only used when server.backend='baofeng') --
    _s(
        "baofeng.serial_port", "RADIO_BAOFENG_SERIAL_PORT", "baofeng", DEFAULT_BAOFENG_SERIAL_PORT,
        coerce_str,
        "Serial device the AIOC exposes for PTT keying. Defaults to /dev/ttyACM0; for a stable, "
        "reorder-proof path prefer the by-id symlink (e.g. "
        "/dev/serial/by-id/usb-...All-In-One-Cable...). Your user must be in the 'dialout' group.",
    ),
    _s(
        "baofeng.ptt_line", "RADIO_BAOFENG_PTT_LINE", "baofeng", DEFAULT_BAOFENG_PTT_LINE,
        coerce_enum(PttLine, strip=False),
        "Which serial control line keys PTT on the AIOC: 'dtr' (default, confirmed on the bench — "
        "cycle 29) or 'rts'. This is a per-hardware fact (guardrail 1); if a different AIOC/radio "
        "keys on RTS instead, confirm with `python -m radio_server.doctor --key-test` into a dummy "
        "load and set this accordingly.",
    ),
    _s(
        "baofeng.input_device", "RADIO_BAOFENG_INPUT_DEVICE", "baofeng", DEFAULT_BAOFENG_INPUT_DEVICE,
        coerce_str,
        "Capture device (sounddevice/PortAudio) for received audio — the AIOC USB sound card. "
        "sounddevice matches a device by a case-insensitive substring of its PortAudio name (default "
        "'All-In-One-Cable: USB', which targets the raw ALSA device unambiguously even when "
        "PulseAudio also exposes the card) or an integer index; a raw ALSA 'hw:CARD=...' string does "
        "NOT work. The card is 48 kHz-native. If the default doesn't resolve, "
        "`python -m radio_server.doctor` prints the exact index/name to use.",
    ),
    _s(
        "baofeng.output_device", "RADIO_BAOFENG_OUTPUT_DEVICE", "baofeng", DEFAULT_BAOFENG_OUTPUT_DEVICE,
        coerce_str,
        "Playback device (sounddevice/PortAudio) for transmitted audio — the AIOC USB sound card. "
        "Same matching rules and default as baofeng.input_device (name substring or index, not a raw "
        "ALSA 'hw:' string).",
    ),
    _s(
        "baofeng.blocksize", "RADIO_BAOFENG_BLOCKSIZE", "baofeng", DEFAULT_BAOFENG_BLOCKSIZE, coerce_int,
        "Frames per audio capture/playback block. 960 = 20 ms at 48 kHz. Verify against hardware "
        "(guardrail 1): lower trims latency, higher is more robust against xruns on the real codec.",
    ),
)

BY_KEY: dict[str, SettingSpec] = {s.key: s for s in SETTINGS}
BY_ENV: dict[str, SettingSpec] = {s.env: s for s in SETTINGS}
