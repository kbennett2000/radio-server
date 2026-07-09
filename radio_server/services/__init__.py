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
    "RADIO_TZ_ENV_VAR",
    "Service",
    "ServiceContext",
    "ServiceRegistry",
    "StubTts",
    "TIME_DIGIT",
    "TIME_NAME",
    "TtsEngine",
    "format_spoken_time",
    "load_timezone",
    "register",
    "time_service",
]
