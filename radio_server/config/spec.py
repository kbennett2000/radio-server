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

from dataclasses import dataclass, field, replace
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
    DECODE_MODES,
    DEFAULT_DTMF_BUFFER_SECONDS,
    DEFAULT_DTMF_DECODE_MODE,
    DEFAULT_DTMF_TIMEOUT,
    DEFAULT_MULTIMON_BIN,
    NATIVE_REVERSE_TWIST_DB,
)
from ..backends.aioc_baofeng import (
    DEFAULT_BLOCKSIZE as DEFAULT_BAOFENG_BLOCKSIZE,
    DEFAULT_INPUT_DEVICE as DEFAULT_BAOFENG_INPUT_DEVICE,
    DEFAULT_OUTPUT_DEVICE as DEFAULT_BAOFENG_OUTPUT_DEVICE,
    DEFAULT_PTT_LINE as DEFAULT_BAOFENG_PTT_LINE,
    DEFAULT_SERIAL_PORT as DEFAULT_BAOFENG_SERIAL_PORT,
    DEFAULT_TX_LEAD_SECONDS as DEFAULT_BAOFENG_TX_LEAD,
    PttLine,
)
from ..backends.kv4p.radio import (
    DEFAULT_HIGH_POWER as DEFAULT_KV4P_HIGH_POWER,
    DEFAULT_MODULE_TYPE as DEFAULT_KV4P_MODULE_TYPE,
    DEFAULT_SAMPLE_RATE_CORRECTION as DEFAULT_KV4P_SAMPLE_RATE_CORRECTION,
    DEFAULT_SERIAL_PORT as DEFAULT_KV4P_SERIAL_PORT,
    DEFAULT_SQUELCH as DEFAULT_KV4P_SQUELCH,
    DEFAULT_TX_ALLOWED as DEFAULT_KV4P_TX_ALLOWED,
    DEFAULT_TX_GAIN as DEFAULT_KV4P_TX_GAIN,
    DEFAULT_TX_LEAD_SECONDS as DEFAULT_KV4P_TX_LEAD,
    Kv4pBand,
)
from ..backends.uvk5.radio import (
    DEFAULT_BLOCKSIZE as DEFAULT_UVK5_BLOCKSIZE,
    DEFAULT_INPUT_DEVICE as DEFAULT_UVK5_INPUT_DEVICE,
    DEFAULT_MODE as DEFAULT_UVK5_MODE,
    DEFAULT_OUTPUT_DEVICE as DEFAULT_UVK5_OUTPUT_DEVICE,
    DEFAULT_SQUELCH_THRESHOLD as DEFAULT_UVK5_SQUELCH_THRESHOLD,
    DEFAULT_TOT as DEFAULT_UVK5_TOT,
    DEFAULT_TX_ALLOWED as DEFAULT_UVK5_TX_ALLOWED,
    DEFAULT_TX_LEAD_SECONDS as DEFAULT_UVK5_TX_LEAD,
)
from ..link.client import DEFAULT_MUMBLE_RX_GUARD_SECONDS, DEFAULT_MUMBLE_TX_HANG
from ..link.entries import DEFAULT_MUMBLE_DISCONNECT_DTMF, LINK_DTMF_ALPHABET
from ..link.mute import DEFAULT_DTMF_MUTE, DEFAULT_DTMF_MUTE_HOLD
from ..dstar.bridge import DEFAULT_DSTAR_DEAD_AIR, DEFAULT_DSTAR_MAX_OVER, DEFAULT_DSTAR_TX_HANG
from ..dstar.client import (
    DEFAULT_GATEWAY_HOST,
    DEFAULT_GATEWAY_PORT,
    DEFAULT_LOCAL_PORT,
    DEFAULT_MODULE,
)
from ..dstar.remote_client import DEFAULT_REMOTE_HOST as DEFAULT_DVAP_HOST
from ..dstar.remote_codec import DEFAULT_REMOTE_PORT as DEFAULT_DVAP_PORT
from ..controller.engine import (
    DEFAULT_CONTROLLER_POLL,
    DEFAULT_LINK_ANNOUNCEMENT,
    DEFAULT_LINK_OFF_ANNOUNCEMENT,
    DEFAULT_LOGIN_ANNOUNCEMENT,
    DEFAULT_LOGOUT_ANNOUNCEMENT,
    DEFAULT_FIXED_CODE_ENABLED,
    DEFAULT_SESSION_TIMEOUT,
    DEFAULT_TIMEOUT_ANNOUNCEMENT,
    DEFAULT_TOTP_ENABLED,
)
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
from ..tx.session import DEFAULT_TX_IDLE_TIMEOUT, DEFAULT_TX_TOT

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
#: TLS cert/key paths for serving the UI over HTTPS (ADR 0039). Empty = plain HTTP (the default);
#: set BOTH to a PEM cert and key to serve HTTPS, which a phone needs for a secure context (mic +
#: AudioWorklet). Per-deployment (guardrail 1): generate with scripts/gen-selfsigned-cert.sh.
DEFAULT_TLS_CERT = ""
DEFAULT_TLS_KEY = ""
#: Web UI: whether the browser auto-starts Listen once authenticated (ADR 0037). A convenience only —
#: browser autoplay means it takes effect on the login gesture, not a cold page load.
DEFAULT_WEB_AUTO_LISTEN = True
#: Command the settings UI's "Restart server" button runs (ADR 0047). The marked default matches
#: the checked-in systemd-user deployment (restart-radio-server.sh); ``--no-block`` queues the
#: restart with systemd so the HTTP reply gets out before the stop signal lands. Per-deployment
#: (guardrail 1): a bare `uv run` bench has no unit, so set empty to disable (hides the button).
DEFAULT_RESTART_COMMAND = "systemctl --user --no-block restart radio-server"
#: Whether a configured controller loop starts automatically on boot (ADR 0037), replacing the manual
#: Start/Stop button removed from the web UI. No-op when no controller is wired (no TOTP secret).
DEFAULT_CONTROLLER_AUTOSTART = True

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


def coerce_uvk5_tot(raw: object, key: str) -> object:
    """The UV-K5's MANDATORY transmitter time-out (seconds); blank → default (ADR 0117).

    The docked UV-K6 has no device-side stuck-key backstop (unlike kv4p's firmware `RUNAWAY_TX_SEC`
    or the UV-5R's TOT menu), so the server cap is the only protection and MUST stay armed: config
    may only *shorten* it. A value of 0 (or negative) — the "disable" the global `tx.tot` allows — is
    rejected, not clamped; so is any value above the `DEFAULT_UVK5_TOT` (180 s) default, so it can
    never be weakened past the mandatory ceiling. Rejects, mirroring `coerce_id_interval`."""
    if _blank(raw):
        return USE_DEFAULT
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{key}={raw!r} is not a number") from exc
    if value <= 0:
        raise RuntimeError(
            f"{key}={raw!r} must be positive: the UV-K5 has no device-side stuck-key time-out, so "
            f"its server TOT is mandatory and cannot be disabled (0). Shorten it, don't disable it."
        )
    if value > DEFAULT_UVK5_TOT:
        raise RuntimeError(
            f"{key}={raw!r} exceeds the mandatory {DEFAULT_UVK5_TOT:.0f} s UV-K5 ceiling — it may "
            f"only be shortened from the default, never lengthened past it (ADR 0117)."
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


def coerce_link_dtmf(raw: object, key: str) -> object:
    """A Mumble-link DTMF combo (ADR 0042): 1+ chars of 0-9/A-D; blank → default. Narrower than the
    ``[services]`` alphabet because ``#`` submits and ``*`` clears in the framer — neither can ever
    appear inside a matchable combo. Collision checks against entries/[services] happen where both
    sides are known (`link.entries.validate_link_digits`)."""
    if _blank(raw):
        return USE_DEFAULT
    value = str(raw)
    if set(value) - LINK_DTMF_ALPHABET:
        raise RuntimeError(
            f"{key}={raw!r} must be one or more of 0-9/A-D ('#' submits and '*' clears)"
        )
    return value


def coerce_optional_str(raw: object, key: str) -> object:
    """A string where an **absent** value falls to the default but an explicit **empty** value is
    kept as ``""`` — so a config can blank the setting out (used by the announcement phrases, where
    empty means "say nothing"). ``None`` (unset) → default; any string, including ``""`` → itself."""
    if raw is None:
        return USE_DEFAULT
    return str(raw)


def coerce_optional_int(raw: object, key: str) -> object:
    """An integer whose default is ``None`` (unset). Absent → the ``None`` default; a present value
    must parse as an int (a blank/non-int fails loud, so an unset key never resolves to a stray
    ``None`` that is also marked *set*). Used by ``kv4p.frequency`` (omit = keep the device's
    NVS frequency)."""
    if raw is None:
        return USE_DEFAULT
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{key}={raw!r} is not an integer") from exc


def coerce_optional_float(raw: object, key: str) -> object:
    """A float whose default is ``None`` (unset). Absent → the ``None`` default; a present value must
    parse as a float (a blank/non-number fails loud). Used by ``uvk5.tone`` (omit = CTCSS off)."""
    if raw is None:
        return USE_DEFAULT
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{key}={raw!r} is not a number") from exc


def coerce_link_announcement(raw: object, key: str) -> object:
    """`coerce_optional_str` plus template validation: the link-connect confirmation may embed
    ``{name}`` (the entry name, underscores spoken as spaces), so a typo'd placeholder like
    ``{nmae}`` must fail loud at load — not blow up rendering announcements at controller build."""
    value = coerce_optional_str(raw, key)
    if value is USE_DEFAULT or value == "":
        return value
    try:
        str(value).format(name="test")
    except (KeyError, IndexError, ValueError) as exc:
        raise RuntimeError(
            f"{key}={raw!r} is not a valid template; the only placeholder is {{name}}"
        ) from exc
    return value


def coerce_required_str(raw: object, key: str) -> object:
    """A required non-empty string. A present-but-blank value fails loud AT LOAD (matching e.g.
    ``load_callsign({RADIO_CALLSIGN: ""})``); an ABSENT value is handled by resolution as
    `UNSET_REQUIRED` and fails loud lazily on first read."""
    if _blank(raw):
        raise RuntimeError(f"{key} is set but empty; provide a real value")
    return str(raw)


def coerce_required_int(raw: object, key: str) -> object:
    """A required integer (no invented default). A present-but-blank value fails loud AT LOAD; an
    ABSENT value is handled by resolution as `UNSET_REQUIRED` and fails loud lazily on first read.
    Used by ``uvk5.frequency`` — in full-control (XVFO) mode the host owns tuning and there is no
    radio-side value to preserve, so an unset frequency is an error rather than a made-up default."""
    if _blank(raw):
        raise RuntimeError(f"{key} is set but empty; provide a real value")
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{key}={raw!r} is not an integer") from exc


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


def coerce_dtmf_decode_mode(raw: object, key: str) -> object:
    """The DTMF decode mode: ``streaming``, ``buffered``, ``native``, or ``auto`` (`DECODE_MODES`),
    matched after ``.strip().lower()``; blank → default."""
    if _blank(raw):
        return USE_DEFAULT
    mode = str(raw).strip().lower()
    if mode not in DECODE_MODES:
        raise RuntimeError(f"{key}={raw!r}: choose one of {', '.join(DECODE_MODES)}")
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
    #: Whether this is an "advanced" setting — tuning/plumbing an everyday operator rarely touches
    #: (ADR 0037). The settings UI puts these behind a collapsed "Advanced" section. Pure UI metadata;
    #: resolution/persistence ignore it.
    advanced: bool = False

    @property
    def required(self) -> bool:
        return self.default is REQUIRED

    @property
    def leaf(self) -> str:
        """The key without its group prefix — the name under the ``[group]`` TOML table."""
        return self.key.split(".", 1)[1]


def _s(key, env, group, default, coerce, description, *, advanced=False) -> SettingSpec:
    return SettingSpec(
        key=key,
        env=env,
        group=group,
        default=default,
        coerce=coerce,
        description=description,
        advanced=advanced,
    )


_BASE_SETTINGS: tuple[SettingSpec, ...] = (
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
    # --- Auth (over-RF TOTP/DTMF plane) ------------------------------------------------------
    _s(
        "auth.totp_enabled", "RADIO_TOTP_ENABLED", "auth", DEFAULT_TOTP_ENABLED, coerce_strict_bool,
        "Whether over-the-air callers must key a TOTP login code before issuing DTMF commands (on by "
        "default). Turn it OFF to let any caller issue DTMF commands directly, with no login — a "
        "deliberate opt-in to UN-GATED access. Everything is already in the clear over RF, so this "
        "removes access control entirely: anyone in range can trigger any service and key your "
        "transmitter as your station. Automatic station identification still runs regardless (Part "
        "97), and the web UI shows an unlocked indicator while auth is off. Leave on unless you have "
        "a specific reason. Changing this takes effect after a server restart.",
    ),
    _s(
        "auth.fixed_code", "RADIO_FIXED_CODE_ENABLED", "auth", DEFAULT_FIXED_CODE_ENABLED,
        coerce_strict_bool,
        "Use a FIXED login code you choose, instead of a rotating TOTP code (off by default). SECURITY "
        "WARNING: a fixed code never changes, so anyone who overhears it over the air can reuse it "
        "indefinitely — unlike a rotating code, it gets NO single-use protection. It is a convenience "
        "for operators who don't want an authenticator app, not a secure option. Only takes effect "
        "when a login is required (auth.totp_enabled on) AND a fixed code has been set from the "
        "Settings screen; leave OFF to keep the secure rotating login code. Restart to apply.",
    ),
    # --- Audio / squelch (RX gate) -----------------------------------------------------------
    _s(
        "audio.squelch", "RADIO_SQUELCH", "audio", SquelchMode(DEFAULT_SQUELCH_MODE),
        coerce_enum(SquelchMode, strip=False),
        "RX activity gate: 'off' relays all received audio, 'audio' uses the software VAD "
        "(vad_* thresholds below), 'cat' uses the radio's hardware busy line (TM-V71A and kv4p — "
        "radios with a real carrier-detect line; rejected for baofeng). "
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
    _s(
        "audio.dtmf_reverse_twist_db", "RADIO_DTMF_REVERSE_TWIST_DB", "audio",
        NATIVE_REVERSE_TWIST_DB, coerce_positive_float,
        "How much louder the native DTMF decoder tolerates the low tone group being than the high "
        "before rejecting the pair as unbalanced (reverse twist), in dB. Some inexpensive radios (the "
        "UV-5R Mini is the known one) transmit DTMF with the low tones much hotter than the high, and "
        "the tight default rejects them as noise. Raise to ~10 if one radio's DTMF isn't recognized "
        "while another's is; the default keeps compliant radios well protected against talk-off (ADR 0075).",
    ),
    # --- DTMF decode -------------------------------------------------------------------------
    _s(
        "dtmf.decode_mode", "RADIO_DTMF_DECODE_MODE", "dtmf", DEFAULT_DTMF_DECODE_MODE,
        coerce_dtmf_decode_mode,
        "How DTMF is decoded from received audio: 'auto' (default) resolves to 'native', the in-process "
        "Goertzel decoder that needs no external binary — bench-verified to decode better than multimon "
        "on real RF (ADR 0060), and it runs everywhere including native Windows. 'streaming' pipes the "
        "continuous RX stream through one persistent multimon-ng process; 'buffered' is the older "
        "fixed-window multimon path; both are explicit escape hatches that require the multimon-ng "
        "binary (dtmf.multimon_bin) and fail loudly if it is missing. Setting any mode explicitly "
        "overrides auto.",
    ),
    _s(
        "dtmf.multimon_bin", "RADIO_MULTIMON_BIN", "dtmf", DEFAULT_MULTIMON_BIN, coerce_str,
        "Path or name of the multimon-ng binary. Only used by the explicit 'streaming'/'buffered' decode "
        "modes — the default 'auto' (native) needs no binary. Leave as the default if multimon-ng is on "
        "PATH; set an absolute path otherwise.",
    ),
    _s(
        "dtmf.timeout", "RADIO_DTMF_TIMEOUT", "dtmf", DEFAULT_DTMF_TIMEOUT, coerce_positive_float,
        "Seconds of inter-digit silence after which a DTMF entry is considered complete. Raise if "
        "callers key digits slowly; lower for snappier command turnaround.",
    ),
    _s(
        "dtmf.buffer_seconds", "RADIO_DTMF_BUFFER_SECONDS", "dtmf", DEFAULT_DTMF_BUFFER_SECONDS,
        coerce_positive_float,
        "Seconds of received audio to accumulate before each DTMF decode, for the 'buffered' decode "
        "mode only. A single ~20 ms capture block is too short for multimon-ng to lock onto a tone, so "
        "buffered mode accumulates this long first. (The default 'native' decoder doesn't buffer.) "
        "Verify against hardware: raise if keyed digits don't decode, lower for less latency.",
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
    _s(
        "tx.tot", "RADIO_TX_TOT", "tx", DEFAULT_TX_TOT, coerce_nonneg_float,
        "Transmitter time-out timer: the hard cap in seconds on how long ANY keying path may hold PTT "
        "continuously before it is force-dropped (ADR 0090). Unlike tx.idle_timeout this does not reset "
        "per frame — it bounds a continuous transmission (a held mic, a wedged crossband over, a stuck "
        "decode). The classic repeater value is ~180s; set 0 to disable. Verify against hardware.",
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
    _s(
        "controller.login_announcement", "RADIO_LOGIN_ANNOUNCEMENT", "controller",
        DEFAULT_LOGIN_ANNOUNCEMENT, coerce_optional_str,
        "Spoken over the air on a successful DTMF login (prepended with the station ID). Leave blank "
        "to stay silent on login.",
    ),
    _s(
        "controller.timeout_announcement", "RADIO_TIMEOUT_ANNOUNCEMENT", "controller",
        DEFAULT_TIMEOUT_ANNOUNCEMENT, coerce_optional_str,
        "Spoken when a session expires from inactivity, before the closing station ID. Leave blank to "
        "sign off with the ID only.",
    ),
    _s(
        "controller.logout_announcement", "RADIO_LOGOUT_ANNOUNCEMENT", "controller",
        DEFAULT_LOGOUT_ANNOUNCEMENT, coerce_optional_str,
        "Spoken to confirm a deliberate 99# force-logout, before the closing station ID. Leave blank "
        "to sign off with the ID only.",
    ),
    _s(
        "controller.autostart", "RADIO_CONTROLLER_AUTOSTART", "controller",
        DEFAULT_CONTROLLER_AUTOSTART, coerce_strict_bool,
        "Whether the controller loop starts automatically when the server boots (on by default). The "
        "controller is what runs the over-the-air DTMF voice services, TOTP login sessions, and "
        "automatic station identification. This only has an effect when a controller is actually "
        "configured (a TOTP secret and callsign are set); otherwise it is a no-op. Turn it off to keep "
        "the controller idle until started via the API.",
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
        "Which radio backend to drive: 'mock' (software-only, the default), 'v71' (TM-V71A), "
        "'baofeng' (UV-5R via the AIOC cable — see the [baofeng] section), 'kv4p' (a kv4p HT "
        "board over USB — full CAT tuning plus a real busy line; see the [kv4p] section), or 'uvk5' "
        "(a UV-K5/K6 on Quansheng Dock firmware via the AIOC — full-control register tuning plus a "
        "real RSSI busy line; see the [uvk5] section and docs/uvk5-setup.md). 'v71' is not yet "
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
        "server.tls_cert", "RADIO_TLS_CERT", "server", DEFAULT_TLS_CERT, coerce_str,
        "Path to a PEM TLS certificate. Empty (default) serves plain HTTP. Set this AND server.tls_key "
        "to serve HTTPS — required for a phone on the LAN, where browsers gate the microphone and "
        "AudioWorklet (Listen/Talk) behind a secure context that plain http://<lan-ip> is not (ADR "
        "0039). Per-deployment (guardrail 1): generate a self-signed cert/key with "
        "scripts/gen-selfsigned-cert.sh. Setting only one of the two fails loud at startup.",
    ),
    _s(
        "server.tls_key", "RADIO_TLS_KEY", "server", DEFAULT_TLS_KEY, coerce_str,
        "Path to the PEM private key matching server.tls_cert. Empty (default) serves plain HTTP; set "
        "both to serve HTTPS (ADR 0039). Setting only one of the two fails loud at startup.",
    ),
    _s(
        "server.mock_cat", "RADIO_MOCK_CAT", "server", DEFAULT_MOCK_CAT, coerce_permissive_off_bool,
        "Developer toggle (mock backend only): whether the mock advertises CAT tuning. On by default "
        "(a full-CAT mock); set off/0/false/no/n for an audio-only mock so the UI greys out tuning "
        "controls, demonstrating the Baofeng-mode capability split without hardware.",
    ),
    # --- Web UI ------------------------------------------------------------------------------
    _s(
        "web.auto_listen", "RADIO_WEB_AUTO_LISTEN", "web", DEFAULT_WEB_AUTO_LISTEN, coerce_strict_bool,
        "Whether the web UI starts playing received audio automatically, so you don't have to click "
        "Listen every time (on by default). Because browsers block audio until you interact with the "
        "page, this takes effect the moment you log in rather than on a cold page load. Turn it off to "
        "start muted until you press Listen.",
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
    _s(
        "baofeng.tx_lead_seconds", "RADIO_BAOFENG_TX_LEAD", "baofeng", DEFAULT_BAOFENG_TX_LEAD,
        coerce_nonneg_float,
        "Seconds of silence transmitted right after PTT keys up, before real audio, so the UV-5R "
        "transmitter and the receiving radio's squelch are fully up before speech starts — prevents "
        "the first fraction of a second being clipped over the air. 0 disables. Per-hardware "
        "(guardrail 1); bench-tune (raise if speech is still clipped, lower if the pause drags).",
    ),
    # --- kv4p HT hardware backend (ADR 0061/0063; only used when server.backend='kv4p') ---------
    _s(
        "kv4p.serial_port", "RADIO_KV4P_SERIAL_PORT", "kv4p", DEFAULT_KV4P_SERIAL_PORT, coerce_str,
        "Serial device the kv4p board's USB-UART bridge exposes. The board uses a CP210x or CH340, "
        "which enumerate as /dev/ttyUSB0 (NOT the AIOC's /dev/ttyACM0); for a stable, reorder-proof "
        "path prefer the by-id symlink (e.g. /dev/serial/by-id/usb-...CP2102...). Your user must be "
        "in the 'dialout' group.",
    ),
    _s(
        "kv4p.module_type", "RADIO_KV4P_MODULE_TYPE", "kv4p", DEFAULT_KV4P_MODULE_TYPE,
        coerce_enum(Kv4pBand, strip=False),
        "The fitted RF module's band: 'vhf' (SA818-VHF, ~134-174 MHz) or 'uhf' (SA818-UHF, "
        "~400-480 MHz). This decides the band that kv4p.frequency is range-checked against when the "
        "device does NOT send a HELLO — which is the normal case: the HELLO fires only at the board's "
        "power-on (ADR 0062), so a server that restarts against a still-running board never sees it. "
        "Set this to match your board or a UHF radio will reject every UHF frequency as out-of-band. "
        "A HELLO, when one does arrive, overrides this with the module's reported range. Verify "
        "against the hardware in hand (guardrail 1).",
    ),
    _s(
        "kv4p.squelch", "RADIO_KV4P_SQUELCH", "kv4p", DEFAULT_KV4P_SQUELCH, coerce_int,
        "SA818 squelch LEVEL, 0..8 — the RF module's own carrier gate, DISTINCT from audio.squelch "
        "(which selects the server's RX activity gate). This one sets how strong a signal opens the "
        "hardware busy line that audio.squelch='cat' reads. At level 0 the busy line never asserts, "
        "so 'busy' reads True forever and a CAT-squelch scan dwells on every channel — pair "
        "audio.squelch='cat' only with a non-zero level here. Verify against hardware (guardrail 1): "
        "raise if noise keeps the squelch open, lower if it clips weak signals.",
    ),
    _s(
        "kv4p.tx_lead_seconds", "RADIO_KV4P_TX_LEAD", "kv4p", DEFAULT_KV4P_TX_LEAD,
        coerce_nonneg_float,
        "Seconds of silence transmitted right after PTT keys up, before real audio, so the "
        "transmitter and the receiving radio's squelch are fully up before speech starts. 0 "
        "disables. Bench-measured at 0.5 s (ADR 0069): a tone at 0.2 s clipped its onset on a "
        "monitoring receiver; 0.5 s started clean. Re-check if your far-end squelch is slow.",
    ),
    _s(
        "kv4p.high_power", "RADIO_KV4P_HIGH_POWER", "kv4p", DEFAULT_KV4P_HIGH_POWER, coerce_strict_bool,
        "Whether the module transmits at high power (the firmware HIGH_POWER flag). On by default — "
        "a gateway node wants range. Turn off for low power (e.g. a bench test or a dense site). The "
        "exact power levels are per-hardware (guardrail 1).",
    ),
    _s(
        "kv4p.tx_allowed", "RADIO_KV4P_TX_ALLOWED", "kv4p", DEFAULT_KV4P_TX_ALLOWED, coerce_strict_bool,
        "Whether the backend may transmit at all (the firmware TX_ALLOWED NVS gate). On by default "
        "(radio-server exists to transmit). Set false for a genuinely receive-only node: a real "
        "firmware gate, not a pretend one — a keying attempt then fails loud rather than going out "
        "as dead air.",
    ),
    _s(
        "kv4p.frequency", "RADIO_KV4P_FREQUENCY", "kv4p", None, coerce_optional_int,
        "Optional start frequency in Hz (e.g. 146520000 for 146.520 MHz). The board is a bare module "
        "with no tuning knob, but the firmware persists the last frequency to NVS, so when this is "
        "unset the radio comes up on whatever it was last set to and status() reports that. Set it to "
        "tune at startup; an out-of-band value fails loud. No default is invented — an unset value is "
        "left to the device rather than putting a made-up frequency on the air.",
    ),
    _s(
        "kv4p.sample_rate_correction", "RADIO_KV4P_SAMPLE_RATE_CORRECTION", "kv4p",
        DEFAULT_KV4P_SAMPLE_RATE_CORRECTION, coerce_positive_float,
        "The firmware's RX ADC sample-rate multiplier — the shipped board clocks its receive ADC ~2% "
        "fast (rxAudio.h: AUDIO_SAMPLE_RATE * 1.02) but labels the audio 48 kHz, so received audio "
        "arrives ~2% off-frequency: DTMF won't decode and the recorder/Mumble link drift ~1.2 s per "
        "minute. The backend resamples the true device rate back to a real 48 kHz to undo it. Marked "
        "default 1.02 — the ESP32 clock divider quantizes the request, so run `doctor --backend kv4p "
        "--rx-level --seconds 30` and trim this to the measured rate / 48000 (guardrail 1). 1.0 "
        "disables the correction.",
    ),
    _s(
        "kv4p.tx_gain", "RADIO_KV4P_TX_GAIN", "kv4p",
        DEFAULT_KV4P_TX_GAIN, coerce_positive_float,
        "TX audio-level multiplier applied to every transmitted sample before the Opus encoder — the "
        "kv4p's software stand-in for a sound-card playback slider. The kv4p has no sound card, so "
        "(unlike the AIOC, which rides alsamixer's playback level) there is no analog stage to bring "
        "an overmodulated TX level down; this is that stage. Default 1.0 is a no-op. If kv4p "
        "announcements/voice sound overmodulated or distorted, lower this until clean — a good "
        "starting point is ~0.5 (guardrail 1: the right level is a per-radio deviation fact, verify "
        "on bench). Values above 1.0 are allowed but clamp to full scale rather than clipping.",
    ),
    # --- UV-K5 Quansheng Dock hardware backend (ADR 0110-0114; only used when server.backend='uvk5') -
    _s(
        "uvk5.serial_port", "RADIO_UVK5_SERIAL_PORT", "uvk5", REQUIRED, coerce_required_str,
        "Serial device the UV-K5's AIOC cable exposes for dock control/keying (the AIOC also presents "
        "a separate USB sound card for audio). REQUIRED — there is no safe default: the AIOC "
        "enumerates as a /dev/ttyACM* CDC device, and a bench with more than one ACM adapter (a second "
        "AIOC, etc.) makes a bare /dev/ttyACM0 ambiguous, so give a stable, reorder-proof by-id path "
        "(e.g. /dev/serial/by-id/usb-...All-In-One-Cable...). Your user must be in the 'dialout' group.",
    ),
    _s(
        "uvk5.frequency", "RADIO_UVK5_FREQUENCY", "uvk5", REQUIRED, coerce_required_int,
        "Operating frequency in Hz (e.g. 146520000 for 146.520 MHz). REQUIRED — in full-control "
        "(XVFO) mode the host is the radio's brain and owns tuning; unlike the kv4p (whose firmware "
        "persists the last frequency to NVS), there is no radio-side value worth preserving, so an "
        "unset frequency fails loud rather than putting a made-up or stale frequency on the air. An "
        "out-of-band value also fails loud. Retune any time via the API (SET_FREQUENCY).",
    ),
    _s(
        "uvk5.tone", "RADIO_UVK5_TONE", "uvk5", None, coerce_optional_float,
        "Optional TX CTCSS tone in Hz (e.g. 100.0), or unset for no tone. Applied at startup; an "
        "out-of-range value fails loud. Set per your repeater's access tone.",
    ),
    _s(
        "uvk5.mode", "RADIO_UVK5_MODE", "uvk5", DEFAULT_UVK5_MODE, coerce_str,
        "Initial channel bandwidth: 'FM' (wide) or 'NFM' (narrow) — sets the BK4819 reg-0x43 "
        "bandwidth. Aliases 'wide'/'narrow' are accepted. Change any time via the API (SET_MODE).",
    ),
    _s(
        "uvk5.tx_allowed", "RADIO_UVK5_TX_ALLOWED", "uvk5", DEFAULT_UVK5_TX_ALLOWED, coerce_strict_bool,
        "Whether the backend may transmit at all. On by default (radio-server exists to transmit). "
        "Set false for a genuinely receive-only node: unlike the kv4p's firmware NVS gate, this is a "
        "software refuse-to-key (full-control keying is a direct host register write) — a keying "
        "attempt then fails loud rather than going out as dead air.",
    ),
    _s(
        "uvk5.input_device", "RADIO_UVK5_INPUT_DEVICE", "uvk5", DEFAULT_UVK5_INPUT_DEVICE, coerce_str,
        "RX sound card: a case-insensitive substring of the PortAudio device name (default "
        "'All-In-One-Cable: USB' — the AIOC) or an integer index. A raw ALSA 'hw:CARD=...' string "
        "does NOT work. The AIOC's K1 jack carries the radio's audio; plugging it in mutes the "
        "handheld's own speaker/mic (expected). Verify the exact name with `doctor --backend uvk5`.",
    ),
    _s(
        "uvk5.output_device", "RADIO_UVK5_OUTPUT_DEVICE", "uvk5", DEFAULT_UVK5_OUTPUT_DEVICE, coerce_str,
        "TX sound card: same rules as uvk5.input_device (default the AIOC 'All-In-One-Cable: USB'). "
        "The played-out audio is what the register-keyed transmitter sends — whether the AIOC-injected "
        "K1 audio actually modulates TX is the bench acceptance gate (ADR 0112/0113).",
    ),
    _s(
        "uvk5.blocksize", "RADIO_UVK5_BLOCKSIZE", "uvk5", DEFAULT_UVK5_BLOCKSIZE, coerce_int,
        "Sound-card block size in frames (default 960 = 20 ms at 48 kHz). The audio callback quantum "
        "for both capture and playout; larger is more xrun-tolerant but adds latency. Verify against "
        "the AIOC's real codec on the bench (guardrail 1).",
    ),
    _s(
        "uvk5.tx_lead_seconds", "RADIO_UVK5_TX_LEAD", "uvk5", DEFAULT_UVK5_TX_LEAD, coerce_nonneg_float,
        "Seconds of silence transmitted right after keying, before real audio, so the transmitter and "
        "the far-end squelch are fully up before speech. 0 disables. Default 0.5 s is inherited from "
        "the AIOC/UV-5R bench (ADR 0069/0113) — this radio earns its OWN bench number; re-measure "
        "with a monitoring receiver and trim (guardrail 1).",
    ),
    _s(
        "uvk5.squelch_threshold", "RADIO_UVK5_SQUELCH_THRESHOLD", "uvk5", DEFAULT_UVK5_SQUELCH_THRESHOLD,
        coerce_int,
        "RSSI threshold (reg-0x67 value & 0x1FF) at or above which status().busy reads True — the "
        "carrier gate that audio.squelch='cat' reads (ADR 0112). At 0 the gate is always busy, so a "
        "CAT-squelch scan dwells everywhere: audio.squelch='cat' is rejected with a 0 threshold. A "
        "crude RSSI COS; verify/tune on the bench (guardrail 1).",
    ),
    _s(
        "uvk5.tot", "RADIO_UVK5_TOT", "uvk5", DEFAULT_UVK5_TOT, coerce_uvk5_tot,
        "The UV-K5's MANDATORY transmitter time-out in seconds — a hard stuck-key cap the server "
        "enforces (ADR 0117). The docked UV-K6 in full-control (XVFO) mode has NO device-side backstop "
        "(unlike the kv4p's firmware ~200 s cutoff or the UV-5R's own TOT menu), so this is the only "
        "protection and is force-armed on every key-up: config may SHORTEN it but never disable it (0 "
        "is rejected, not accepted like the global tx.tot's disable) nor lengthen it past the 180 s "
        "default. On expiry the server force-unkeys (full RX-register restore) and emits an 'alarm' "
        "event to the log. In-process only — it cannot cover host SIGKILL/power-loss; see uvk5-setup.md.",
        advanced=True,
    ),
    # --- Mumble/Murmur link (ADR 0041/0042; destinations live in [[mumble.servers]]) -----------
    _s(
        "mumble.disconnect_dtmf", "RADIO_MUMBLE_DISCONNECT_DTMF", "mumble",
        DEFAULT_MUMBLE_DISCONNECT_DTMF, coerce_link_dtmf,
        "DTMF combo (digits before '#', keyed inside an authenticated session) that disconnects "
        "whatever Mumble entry is linked. '98' pairs with the shipped two-digit keypad (99# logs "
        "out). Must not collide with any entry's dtmf combo or any [services] keypad digit; only "
        "0-9/A-D are matchable ('#' submits, '*' clears).",
    ),
    _s(
        "mumble.link_announcement", "RADIO_MUMBLE_LINK_ANNOUNCEMENT", "mumble",
        DEFAULT_LINK_ANNOUNCEMENT, coerce_link_announcement,
        "Spoken over the air when a DTMF combo connects a Mumble entry. {name} becomes the "
        "entry's name (underscores spoken as spaces). Leave blank to connect silently.",
    ),
    _s(
        "mumble.link_off_announcement", "RADIO_MUMBLE_LINK_OFF_ANNOUNCEMENT", "mumble",
        DEFAULT_LINK_OFF_ANNOUNCEMENT, coerce_optional_str,
        "Spoken over the air when the disconnect combo drops the Mumble link. Leave blank to "
        "disconnect silently.",
    ),
    _s(
        "mumble.tx_hang", "RADIO_MUMBLE_TX_HANG", "mumble", DEFAULT_MUMBLE_TX_HANG, coerce_positive_float,
        "Seconds of Mumble silence after which the bridge drops PTT and frees the transmitter. Mumble "
        "sends voice only while a peer talks, so this debounces inter-word gaps. A keyed radio can't "
        "hear your DTMF, so a shorter hang reopens the receiver in conversational gaps (ADR 0049); "
        "verify against on-air feel (too short chops PTT between words / clips the next word onto RF, "
        "too long holds the channel and blinds you to your own commands).",
    ),
    _s(
        "mumble.rx_guard_seconds", "RADIO_MUMBLE_RX_GUARD_SECONDS", "mumble",
        DEFAULT_MUMBLE_RX_GUARD_SECONDS, coerce_nonneg_float,
        "Seconds the RF→Mumble relay is muted after any local transmit ends (ADR 0085). On the AIOC the "
        "UV-5R receiver emits a brief burst of hash at the TX→RX turnaround (its FM front-end recovering "
        "before squelch settles) that squelch=off would relay to Mumble as a buzz right after you stop "
        "talking; this guard swallows it. AIOC-only symptom; harmless on the kv4p (hardware squelch keeps "
        "it off the wire). Turnaround duration is a per-radio bench fact — verify on-air (too long clips "
        "the start of a fast reply). Set 0 to disable. Try your radio's own squelch first.",
    ),
    _s(
        "mumble.dtmf_mute", "RADIO_MUMBLE_DTMF_MUTE", "mumble", DEFAULT_DTMF_MUTE, coerce_strict_bool,
        "Whether DTMF control tones are kept out of the audio sent to Mumble (on by default). The "
        "bridge detects DTMF tone energy in each RF frame in real time and drops it, and — the same "
        "signal — withholds Mumble→RF keying so an inbound over doesn't transmit over your command "
        "(ADR 0049). Browser listeners and recordings still carry the tones.",
    ),
    _s(
        "mumble.dtmf_mute_hold", "RADIO_MUMBLE_DTMF_MUTE_HOLD", "mumble", DEFAULT_DTMF_MUTE_HOLD,
        coerce_positive_float,
        "Seconds the bridge stays in 'DTMF active' after each detected tone / decoded digit — each "
        "re-arms it, so a whole hand-dialed command stays muted to Mumble and keeps the radio from "
        "keying over you (ADR 0049). Long enough to span a full command; raise if slow dialing lets "
        "tones or a Mumble over slip through between digits.",
    ),
    # --- D-STAR link (ADR 0087; off unless dstar.callsign is set) --------------------------------
    _s(
        "dstar.callsign", "RADIO_DSTAR_CALLSIGN", "dstar", "", coerce_optional_str,
        "Your callsign for the D-STAR link, e.g. 'AE9S'. This is MYCALL on transmit and the base of the "
        "repeater module + gateway callsigns. LEAVE BLANK to keep the D-STAR link OFF (the default); "
        "set it to bring radio-server up as a homebrew-repeater endpoint on the gateway named below. "
        "You must be registered in the D-STAR gateway system for reflector routing (DPlus/REF) to work.",
    ),
    _s(
        "dstar.module", "RADIO_DSTAR_MODULE", "dstar", DEFAULT_MODULE, coerce_str,
        "The single-letter repeater module radio-server registers as (A/B/C). Pick one NOT used by any "
        "other endpoint on the same gateway (e.g. a DVAP on B → use A here). By convention A=23cm, "
        "B=70cm, C=2m, but for a gateway-only endpoint it is just an identifier the gateway routes by.",
    ),
    _s(
        "dstar.gateway_host", "RADIO_DSTAR_GATEWAY_HOST", "dstar", DEFAULT_GATEWAY_HOST, coerce_str,
        "Host of the ircDDBGateway radio-server links to. Defaults to loopback (the gateway runs on the "
        "same box). The gateway must have a repeater band configured for this callsign+module pointing "
        "back at dstar.local_port.",
    ),
    _s(
        "dstar.gateway_port", "RADIO_DSTAR_GATEWAY_PORT", "dstar", DEFAULT_GATEWAY_PORT, coerce_int,
        "UDP port the ircDDBGateway listens on for its repeaters (g4klx default 20010).",
    ),
    _s(
        "dstar.local_port", "RADIO_DSTAR_LOCAL_PORT", "dstar", DEFAULT_LOCAL_PORT, coerce_int,
        "Local UDP port radio-server binds and the gateway sends back to. Must be distinct from any "
        "other repeater on the gateway (a DVAP typically uses 20011) and match this endpoint's "
        "repeaterPort in the gateway config.",
    ),
    _s(
        "dstar.reflector", "RADIO_DSTAR_REFLECTOR", "dstar", "", coerce_optional_str,
        "Optional reflector to link on startup (e.g. 'REF001 C' — name then module letter). The link is "
        "sent through the bridge as a standard D-STAR URCALL command; you can relink/unlink any time "
        "from the web UI reflector picker (ADR 0088). Leave blank to start unlinked.",
    ),
    _s(
        "dstar.vocoder_port", "RADIO_DSTAR_VOCODER_PORT", "dstar", "", coerce_optional_str,
        "Serial port of the DV Dongle AMBE2000 vocoder (ADR 0086) the link encodes/decodes through. "
        "Prefer a stable /dev/serial/by-id/usb-Internet_Labs_DV_Dongle_* path. Blank uses the vocoder "
        "module's built-in default; verify against your hardware (guardrail 1).",
    ),
    _s(
        "dstar.tx_hang", "RADIO_DSTAR_TX_HANG", "dstar", DEFAULT_DSTAR_TX_HANG, coerce_positive_float,
        "Seconds of RF silence after which an outbound over to the reflector is closed (its end frame "
        "sent, PTT dropped). Also the inbound hang that ends a reflector over if its end frame is lost. "
        "Verify on-air (too short chops a slow talker; too long holds the reflector).",
    ),
    _s(
        "dstar.max_over_seconds", "RADIO_DSTAR_MAX_OVER", "dstar", DEFAULT_DSTAR_MAX_OVER,
        coerce_nonneg_float,
        "Hard ceiling (seconds) on a single reflector→RF crossband over — the content-independent "
        "backstop against a stuck key when an inbound stream never ends cleanly (a lost end frame, or a "
        "decode that produces continuous garbage). Unlike dstar.tx_hang it is NOT reset per frame, so it "
        "bounds a CONTINUOUS keyed over regardless of audio content; it sits below tx.tot (ADR 0090) so a "
        "runaway over closes here first. 0 disables. Verify on the bench: long enough not to clip a "
        "legitimate long over, short enough that junk can't sit on the air (ADR 0097, guardrail 1).",
    ),
    _s(
        "dstar.dead_air_seconds", "RADIO_DSTAR_DEAD_AIR", "dstar", DEFAULT_DSTAR_DEAD_AIR,
        coerce_nonneg_float,
        "Seconds a keyed reflector→RF over may carry decode that fails the level gate before it is cut "
        "(ADR 0106). Over liveness follows frame ARRIVAL — a talker's pause keeps the carrier up like a "
        "real repeater — so this is the content-silence reaper for a stream that keeps sending frames "
        "but decodes to nothing (a lost end-bit trickle, garbage silence). Keep it well above any real "
        "speech pause and below dstar.max_over_seconds. 0 disables. Verify on the bench (guardrail 1).",
    ),
    # --- DVAP control (ADR 0095; off unless [[dvap.modules]] is populated) -----------------------
    _s(
        "dvap.host", "RADIO_DVAP_HOST", "dvap", DEFAULT_DVAP_HOST, coerce_str,
        "Host of the ircDDBGateway whose remote-control interface radio-server links the DVAP modules "
        "through. Defaults to loopback (the gateway runs on the same box). The gateway must have "
        "remote-control ENABLED (remoteEnabled=1 + remotePassword) for the DVAP tab to work.",
    ),
    _s(
        "dvap.port", "RADIO_DVAP_PORT", "dvap", DEFAULT_DVAP_PORT, coerce_int,
        "UDP port of the ircDDBGateway remote-control interface (g4klx default 10022). The DVAP modules "
        "themselves are listed under [[dvap.modules]]; the remote-control PASSWORD is a secret set in "
        "radio-secrets.toml (dvap_remote_password), never here.",
    ),
    # --- Server restart (ADR 0047) --------------------------------------------------------------
    _s(
        "server.restart_command", "RADIO_SERVER_RESTART_COMMAND", "server", DEFAULT_RESTART_COMMAND,
        coerce_str,
        "Command the 'Restart server' button in the settings UI runs (split shell-style, no shell). "
        "The default matches the checked-in restart-radio-server.sh systemd-user deployment; "
        "--no-block queues the restart with systemd so the reply reaches the browser before the "
        "process stops. Per-deployment (guardrail 1). Empty disables (the UI hides the button).",
    ),
)

#: Settings that are tuning/plumbing rather than everyday operation — the settings UI files these
#: under a collapsed "Advanced" section (ADR 0037). Everything NOT listed is "basic": callsign, ID,
#: timezone, squelch mode, TTS voice, and the two convenience toggles.
_ADVANCED_KEYS: frozenset[str] = frozenset({
    "station.cw_wpm", "station.cw_tone_hz",
    "audio.vad_on_rms", "audio.vad_off_rms", "audio.vad_hang", "audio.dtmf_reverse_twist_db",
    "dtmf.decode_mode", "dtmf.multimon_bin", "dtmf.timeout", "dtmf.buffer_seconds",
    "recording.enabled", "recording.path", "recording.mode", "recording.max_seconds", "recording.tx",
    "tx.idle_timeout", "tx.tot",
    "scan.settle", "scan.poll", "scan.dwell", "scan.mode",
    "controller.poll", "controller.session_timeout",
    "controller.login_announcement", "controller.timeout_announcement", "controller.logout_announcement",
    "logging.path",
    "server.backend", "server.host", "server.port", "server.web_dir", "server.mock_cat",
    "server.tls_cert", "server.tls_key",
    "baofeng.serial_port", "baofeng.ptt_line", "baofeng.input_device", "baofeng.output_device",
    "baofeng.blocksize", "baofeng.tx_lead_seconds",
    "kv4p.serial_port", "kv4p.module_type", "kv4p.squelch", "kv4p.tx_lead_seconds",
    "kv4p.high_power", "kv4p.tx_allowed", "kv4p.frequency", "kv4p.sample_rate_correction",
    "kv4p.tx_gain",
    "uvk5.serial_port", "uvk5.frequency", "uvk5.tone", "uvk5.mode", "uvk5.tx_allowed",
    "uvk5.input_device", "uvk5.output_device", "uvk5.blocksize", "uvk5.tx_lead_seconds",
    "uvk5.squelch_threshold", "uvk5.tot",
    "mumble.tx_hang", "mumble.rx_guard_seconds", "mumble.dtmf_mute_hold",
    "dstar.module", "dstar.gateway_host", "dstar.gateway_port", "dstar.local_port",
    "dstar.reflector", "dstar.vocoder_port", "dstar.tx_hang", "dstar.max_over_seconds",
    "dstar.dead_air_seconds",
    "dvap.host", "dvap.port",
    "server.restart_command",
})

#: The registry, with the advanced flag applied as a single overlay so the tier lives in one obvious
#: place rather than being repeated across every spec call.
SETTINGS: tuple[SettingSpec, ...] = tuple(
    replace(spec, advanced=True) if spec.key in _ADVANCED_KEYS else spec for spec in _BASE_SETTINGS
)

BY_KEY: dict[str, SettingSpec] = {s.key: s for s in SETTINGS}
BY_ENV: dict[str, SettingSpec] = {s.env: s for s in SETTINGS}
