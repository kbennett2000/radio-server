"""The durable write seam for the station ledger (ADR 0018): a one-method ``LogSink`` and its
default append-only JSONL implementation.

``JsonlSink`` writes one JSON object per line — greppable, self-hosted-friendly, and the same
file-backed shape the rest of the project favours over a database. A SQLite sink is the notable
future swap behind the :class:`LogSink` protocol; it is deliberately **not** built here.

The output path is configuration (:data:`RADIO_LOG_PATH_ENV_VAR`) with a marked default, mirroring
``services.time_service.load_timezone``. Unlike the TOTP secret (which must fail loud on *absence*),
a log path has a sensible default; but a *set-but-unwritable* path fails loud at construction — an
operating log that silently isn't being written is worse than none.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..config import Settings

#: Legacy env var name, retained as metadata (the config schema owns resolution now, ADR 0025).
RADIO_LOG_PATH_ENV_VAR = "RADIO_LOG_PATH"

#: Marked default. A relative JSONL file in the working directory — self-hosted-friendly and always
#: a sensible target. Referenced by the config schema.
DEFAULT_LOG_PATH = "radio-server.jsonl"


def load_log_path(settings: Settings) -> str:
    """Return the ledger path (`logging.path`)."""
    return settings.get("logging.path")


@runtime_checkable
class LogSink(Protocol):
    """A durable destination for ledger records — one flat JSON-ready dict at a time.

    The seam that lets the ledger's record taxonomy stay independent of storage: the default is
    :class:`JsonlSink`; a SQLite (or remote) sink is a future swap that need only satisfy this
    protocol.
    """

    def write(self, record: dict[str, Any]) -> None:
        """Append one record durably."""
        ...

    def close(self) -> None:
        """Flush and release the underlying resource."""
        ...


class JsonlSink:
    """Append-only JSONL file sink: one JSON object per line, flushed per write.

    Opens the file in append mode **at construction**, so an unwritable path (missing parent
    directory, no permission) raises ``OSError`` immediately rather than swallowing every record at
    runtime. Each :meth:`write` serializes one record compactly and flushes it, so the log is
    durable line-by-line and safe to ``tail -f`` / ``grep`` while the server runs.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        # Fail loud here: a set-but-unwritable path is a misconfiguration, not something to
        # discover one dropped record at a time.
        self._fh = open(path, "a", encoding="utf-8")

    def write(self, record: dict[str, Any]) -> None:
        # Compact separators keep lines tight; one object + newline per record. flush() makes the
        # append durable immediately so a reader (or a crash) sees complete lines.
        self._fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()
