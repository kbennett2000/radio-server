"""Station event log / QSO ledger package (ADR 0018).

A durable, timestamped record of what the station did. :class:`EventLog` is a **passive
subscriber** of the event flow (``EventHub``, ADR 0011) — it adds no new emissions — that
translates each event into a flat ledger record and hands it to a :class:`LogSink`. The default
:class:`JsonlSink` writes append-only JSONL; a SQLite sink is a documented future swap behind the
protocol. The output path is configured by ``RADIO_LOG_PATH`` with a marked default.
"""

from .log import Clock, EventLog
from .sink import (
    DEFAULT_LOG_PATH,
    RADIO_LOG_PATH_ENV_VAR,
    JsonlSink,
    LogSink,
    load_log_path,
)

__all__ = [
    "EventLog",
    "Clock",
    "LogSink",
    "JsonlSink",
    "load_log_path",
    "RADIO_LOG_PATH_ENV_VAR",
    "DEFAULT_LOG_PATH",
]
