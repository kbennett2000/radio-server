"""The durable write seam for the station ledger (ADR 0018): a one-method ``LogSink``, its default
append-only JSONL implementation, and the thread that keeps it off the event loop (ADR 0181).

``JsonlSink`` writes one JSON object per line — greppable, self-hosted-friendly, and the same
file-backed shape the rest of the project favours over a database. A SQLite sink is the notable
future swap behind the :class:`LogSink` protocol; it is deliberately **not** built here.

The output path is configuration (:data:`RADIO_LOG_PATH_ENV_VAR`) with a marked default, mirroring
``services.time_service.load_timezone``. Unlike the TOTP secret (which must fail loud on *absence*),
a log path has a sensible default; but a *set-but-unwritable* path fails loud at construction — an
operating log that silently isn't being written is worse than none.

**A sink is called on the event loop.** ``EventLog.handle`` runs in ``app.py``'s ``_drain_log``
task, and the loop it runs on is where ``/audio/tx`` keys and — crucially — unkeys the transmitter.
``JsonlSink.write`` blocks its caller, so the shipped composition wraps it in :class:`ThreadedSink`
(ADR 0181). Any sink used directly on the loop must return without blocking.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import threading
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..config import Settings

#: Legacy env var name, retained as metadata (the config schema owns resolution now, ADR 0025).
RADIO_LOG_PATH_ENV_VAR = "RADIO_LOG_PATH"

#: Marked default. A relative JSONL file in the working directory — self-hosted-friendly and always
#: a sensible target. Referenced by the config schema.
DEFAULT_LOG_PATH = "radio-server.jsonl"

log = logging.getLogger(__name__)

#: Depth (records) of :class:`ThreadedSink`'s hand-off queue. **DERIVED, not chosen** (ADR 0181):
#: ``241 × 5 = 1205``.
#:
#: ``241`` is MEASURED — the busiest one-second bucket in the deployed station's own operating log,
#: across 71,455 records spanning 455.9 h. (Organic traffic is 1-2 records/s; the peak is a bench
#: driver key-cycling ``POST /ptt``, which is exactly the load a bound has to survive.)
#:
#: ``5`` is ``GRACEFUL_SHUTDOWN_SECONDS`` (``radio_server.__main__``) — the window uvicorn gives
#: in-flight work on SIGTERM before it cancels and runs the lifespan teardown that flushes this
#: sink. It is the process's guaranteed remaining lifetime once shutdown has begun.
#:
#: The product is what makes the bound mean something rather than being a round number: **a backlog
#: deeper than this could not be written even if the disk recovered the instant shutdown started**,
#: so queueing it would be a promise the process cannot keep. At the organic rate the same 1205
#: slots absorb ten minutes of hung disk without losing a record.
DEFAULT_LEDGER_QUEUE_MAXSIZE = 1205

#: Seconds :meth:`ThreadedSink.close` waits for the writer to drain before giving up and reporting
#: what was left. Bounded because the case that matters is a disk that never returns: an unbounded
#: join would spend the unit's whole ``TimeoutStopSec`` (20 s, ``docs/deployment.md``) and end in a
#: SIGKILL, which is strictly worse — SIGKILL skips the rest of the teardown, including the unkey.
LEDGER_CLOSE_TIMEOUT_S = 2.0

#: Queue sentinel: "no more records, exit". A record is always a dict, so ``None`` is unambiguous.
_STOP = None


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
        """Append one record durably.

        **Must not block the caller** (ADR 0181): this is called on the event loop, which is where
        PTT is dropped. An implementation that talks to a disk, a socket or a database blocks by
        nature and belongs behind :class:`ThreadedSink`. Raising is fine — ``EventLog.handle``
        isolates that (ADR 0018); not returning is not.
        """
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

    **This sink blocks, so it must not be driven from the event loop directly** (ADR 0181). On the
    deployed station a healthy ``write`` + ``flush()`` measured ~2 us (n=2000: median 1.9, p99 10.2,
    max 41.4), which is why the hazard went unnoticed for 163 ADRs — but a full, stalled or
    network-backed filesystem blocks without bound, and the loop it blocks is the one that unkeys
    the transmitter. ``build_app`` wraps this in :class:`ThreadedSink`.
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


class ThreadedSink:
    """Hand records to a daemon writer thread so a blocking sink can never stall the event loop.

    ADR 0181. :meth:`write` is a bounded, non-blocking enqueue; one writer thread drains the queue
    into ``inner`` in FIFO order. This is ADR 0040's shape, applied to the last blocking write left
    on the loop: ``MultimonStream`` already hands PCM to a bounded ``queue.Queue`` drained by a
    daemon thread "so a slow/stuck pipe can never block the event-loop caller".

    **One queue, one thread — deliberately, not for simplicity.** ``asyncio.to_thread`` per write
    was the obvious alternative and is wrong twice over: its default executor has several workers,
    so fire-and-forget writes interleave and the ledger loses its ordering; and awaiting each write
    to restore that ordering makes ``_drain_log`` *suspend*, which destroys the property ADR 0180
    proved and wrote into ``app.py`` — that the ledger's drain can never fall behind, because
    ``asyncio.Queue.get()`` does not suspend on a non-empty queue and ``handle`` is synchronous.
    A suspending drain is a slow consumer of a 160-deep ``DROP_OLDEST`` queue, i.e. silent Part 97
    record loss from the one subscriber that must never lose anything. The hand-off here is also
    one-way and therefore far cheaper: a ``put_nowait`` costs well under a microsecond against the
    86 us an ``asyncio.to_thread`` round-trip measured on the deployed station.

    **Overflow drops the NEWEST record, and this inverts every other queue in the tree.** ``AudioHub``,
    ``MultimonStream`` and ``EventHub``'s default all drop the *oldest*, because their payload is a
    sample the next one supersedes. A ledger record is history: nothing supersedes it, there is no
    peer to drop (ADR 0180's answer — this sink is the terminal, not a subscriber) and no snapshot
    to resync from. Dropping the oldest would leave a log missing records from the middle, with
    jumping timestamps and no way to tell which are gone. Dropping the newest leaves a **contiguous
    prefix**, so the last written record's ``ts`` plus :attr:`dropped_records` pins the gap exactly.

    The gap is reported, never invented: no "records lost" entry is written, because the ledger's
    record taxonomy is not this ADR's to change. It surfaces as a counter on ``GET /status`` and one
    ``logger.warning`` per gap.
    """

    def __init__(
        self,
        inner: LogSink,
        *,
        maxsize: int = DEFAULT_LEDGER_QUEUE_MAXSIZE,
        close_timeout: float = LEDGER_CLOSE_TIMEOUT_S,
    ) -> None:
        self._inner = inner
        self._maxsize = maxsize
        self._close_timeout = close_timeout
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=maxsize)
        self._lock = threading.Lock()
        self._closed = False
        self._written = 0
        self._deepest = 0
        self._dropped = 0
        self._errors = 0
        #: False while a gap is open, so the warning fires once per gap rather than once per record.
        self._gap_reported = False
        self._thread = threading.Thread(target=self._pump, name="ledger-writer", daemon=True)
        self._thread.start()
        # Belt and braces for an exit that never reaches the lifespan teardown (ADR 0040's idiom):
        # a daemon thread is abandoned at interpreter exit, so flush here too. close() is idempotent.
        atexit.register(self.close)

    # -- the loop side ---------------------------------------------------------------------------

    def write(self, record: dict[str, Any]) -> None:
        """Enqueue one record. Never blocks, never raises — the whole point of this class."""
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            # Drop the NEWEST: the queued records are an in-order prefix of history and evicting one
            # to make room would put a hole in the middle of it. See the class docstring.
            with self._lock:
                self._dropped += 1
                report = not self._gap_reported
                self._gap_reported = True
            if report:
                log.warning(
                    "station ledger is not keeping up: its %d-record queue is full, so records are "
                    "being DROPPED and the Part 97 log now has a gap. The disk is stalled or gone; "
                    "the log is contiguous up to the last record written.",
                    self._maxsize,
                )
            return
        with self._lock:
            self._deepest = max(self._deepest, self._queue.qsize())

    def close(self) -> None:
        """Stop the writer, drain what is queued, and close ``inner``. Idempotent.

        Bounded: a disk that never returns must not spend the unit's whole ``TimeoutStopSec`` and
        end in a SIGKILL, which would skip the rest of the lifespan teardown — including the unkey.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:
            # A full queue at shutdown means the disk is already wedged; the join below will time
            # out and say so. Nothing to gain by blocking here as well.
            pass
        self._thread.join(timeout=self._close_timeout)
        if self._thread.is_alive():
            left = self._queue.qsize()
            log.warning(
                "station ledger writer did not finish within %.1fs; %d record(s) were still queued "
                "and are lost. The sink is blocked, not slow.",
                self._close_timeout,
                left,
            )
            with self._lock:
                self._dropped += left
            return  # the thread still owns `inner`; closing it underneath would be worse
        try:
            self._inner.close()
        except Exception:
            log.exception("station ledger sink failed to close")

    # -- the writer thread -----------------------------------------------------------------------

    def _pump(self) -> None:
        while True:
            record = self._queue.get()
            if record is _STOP:
                return
            try:
                self._inner.write(record)
            except Exception:
                # ADR 0018's failure isolation, moved to where the failure now happens — and COUNTED
                # for the first time. `EventLog.handle` swallowed these with nothing recording that
                # a record had been lost, so a full disk dropped the operating log silently forever.
                with self._lock:
                    self._errors += 1
                continue
            with self._lock:
                self._written += 1
                self._gap_reported = False  # a write landed, so the next gap is a new one

    # -- the instrument --------------------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        """Counters for ``GET /status``; see ``docs/api.md`` for each field's nonzero meaning."""
        with self._lock:
            return {
                "written": self._written,
                "queued": self._queue.qsize(),
                "deepest_queue": self._deepest,
                "queue_maxsize": self._maxsize,
                "dropped_records": self._dropped,
                "write_errors": self._errors,
            }
