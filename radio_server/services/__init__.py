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
from .station_id import (
    DEFAULT_ID_INTERVAL,
    MAX_ID_INTERVAL,
    RADIO_CALLSIGN_ENV_VAR,
    RADIO_ID_INTERVAL_ENV_VAR,
    IdEncoder,
    StationId,
    StubId,
    load_callsign,
    load_id_interval,
)
from .time_service import (
    RADIO_TZ_ENV_VAR,
    TIME_DIGIT,
    TIME_NAME,
    format_spoken_time,
    load_timezone,
    register,
    time_service,
)
from .tts import StubTts, TtsEngine

__all__ = [
    "DispatchResult",
    "Dispatcher",
    "DEFAULT_ID_INTERVAL",
    "MAX_ID_INTERVAL",
    "RADIO_CALLSIGN_ENV_VAR",
    "RADIO_ID_INTERVAL_ENV_VAR",
    "RADIO_TZ_ENV_VAR",
    "IdEncoder",
    "Service",
    "ServiceContext",
    "ServiceRegistry",
    "StationId",
    "StubId",
    "StubTts",
    "TIME_DIGIT",
    "TIME_NAME",
    "TtsEngine",
    "format_spoken_time",
    "load_callsign",
    "load_id_interval",
    "load_timezone",
    "register",
    "time_service",
]
