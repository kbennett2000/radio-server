"""DTMF command dispatch and voice services.

Backend-agnostic, like the auth layer: services operate on the sound-card audio surface
(`transmit`/`receive`), so every service works identically in both radio modes. A
`Dispatcher` plugs into `AuthGate`'s command hook; registered services produce audio the
dispatcher transmits.
"""

from .dispatch import (
    DispatchResult,
    Dispatcher,
    Service,
    ServiceContext,
    ServiceRegistry,
)
from .cw import (
    DEFAULT_CW_TONE_HZ,
    DEFAULT_CW_WPM,
    RADIO_CW_TONE_HZ_ENV_VAR,
    RADIO_CW_WPM_ENV_VAR,
    CwId,
    cw_timeline,
    load_cw_tone_hz,
    load_cw_wpm,
    unit_ms,
)
from .station_id import (
    DEFAULT_ID_INTERVAL,
    MAX_ID_INTERVAL,
    RADIO_CALLSIGN_ENV_VAR,
    RADIO_ID_INTERVAL_ENV_VAR,
    IdEncoder,
    StationId,
    StreamingId,
    StubId,
    load_callsign,
    load_id_interval,
)
from .fetch import (
    DEFAULT_FETCH_TIMEOUT,
    FetchError,
    Fetcher,
    StubFetcher,
    UrllibFetcher,
)
from .time_service import (
    RADIO_TZ_ENV_VAR,
    TIME_DIGIT,
    TIME_NAME,
    format_spoken_time,
    load_timezone,
    time_service,
)
from .local import (
    DEFAULT_LOCAL_SERVICES_DIR,
    discover_local_plugins,
)
from .tts import (
    RADIO_TTS_VOICE_ENV_VAR,
    PiperTts,
    StubTts,
    TtsEngine,
    load_tts_voice,
)
from .plugin import (
    BUILTIN_IDS,
    DEFAULT_BINDINGS,
    ID_BUILTIN,
    LOGOUT_BUILTIN,
    PLUGINS,
    PluginBuildContext,
    ServicePlugin,
    build_registry,
    builtin_digits,
    resolve_bindings,
)
from .voice_id import (
    DEFAULT_ID_MODE,
    ID_MODES,
    PHONETIC,
    RADIO_ID_MODE_ENV_VAR,
    VoiceId,
    build_id_encoder,
    load_id_mode,
    spell_callsign,
)

__all__ = [
    "DispatchResult",
    "Dispatcher",
    "DEFAULT_CW_TONE_HZ",
    "DEFAULT_CW_WPM",
    "DEFAULT_FETCH_TIMEOUT",
    "DEFAULT_LOCAL_SERVICES_DIR",
    "FetchError",
    "Fetcher",
    "StubFetcher",
    "UrllibFetcher",
    "discover_local_plugins",
    "DEFAULT_ID_INTERVAL",
    "DEFAULT_ID_MODE",
    "ID_MODES",
    "MAX_ID_INTERVAL",
    "PHONETIC",
    "RADIO_CALLSIGN_ENV_VAR",
    "RADIO_CW_TONE_HZ_ENV_VAR",
    "RADIO_CW_WPM_ENV_VAR",
    "RADIO_ID_INTERVAL_ENV_VAR",
    "RADIO_ID_MODE_ENV_VAR",
    "RADIO_TTS_VOICE_ENV_VAR",
    "RADIO_TZ_ENV_VAR",
    "CwId",
    "IdEncoder",
    "PiperTts",
    "Service",
    "ServiceContext",
    "ServiceRegistry",
    "StationId",
    "StreamingId",
    "StubId",
    "StubTts",
    "TIME_DIGIT",
    "TIME_NAME",
    "TtsEngine",
    "VoiceId",
    "build_id_encoder",
    "cw_timeline",
    "format_spoken_time",
    "load_callsign",
    "load_cw_tone_hz",
    "load_cw_wpm",
    "load_id_interval",
    "load_id_mode",
    "load_timezone",
    "load_tts_voice",
    "spell_callsign",
    "time_service",
    "unit_ms",
    "PLUGINS",
    "DEFAULT_BINDINGS",
    "BUILTIN_IDS",
    "ID_BUILTIN",
    "LOGOUT_BUILTIN",
    "ServicePlugin",
    "PluginBuildContext",
    "build_registry",
    "builtin_digits",
    "resolve_bindings",
]
