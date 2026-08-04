"""Station event log / QSO ledger package (ADR 0018).

A durable, timestamped record of what the station did. :class:`EventLog` is a **passive
subscriber** of the event flow (``EventHub``, ADR 0011) — it adds no new emissions — that
translates each event into a flat ledger record and hands it to a :class:`LogSink`. The default
:class:`JsonlSink` writes append-only JSONL; a SQLite sink is a documented future swap behind the
protocol. The output path is configured by ``RADIO_LOG_PATH`` with a marked default.

:class:`ThreadedSink` is the wrapper that keeps all of it off the event loop (ADR 0181) — the
composition ``EventLog(ThreadedSink(JsonlSink(path)))`` is what ``build_app`` ships, because the
loop ``EventLog.handle`` runs on is the one that drops PTT.
"""

from .log import Clock, EventLog
from .sink import (
    DEFAULT_LEDGER_QUEUE_MAXSIZE,
    DEFAULT_LOG_PATH,
    LEDGER_CLOSE_TIMEOUT_S,
    RADIO_LOG_PATH_ENV_VAR,
    JsonlSink,
    LogSink,
    ThreadedSink,
    load_log_path,
)

__all__ = [
    "EventLog",
    "Clock",
    "LogSink",
    "JsonlSink",
    "ThreadedSink",
    "load_log_path",
    "RADIO_LOG_PATH_ENV_VAR",
    "DEFAULT_LOG_PATH",
    "DEFAULT_LEDGER_QUEUE_MAXSIZE",
    "LEDGER_CLOSE_TIMEOUT_S",
]
