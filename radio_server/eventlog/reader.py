"""Streaming reader for the station ledger (ADR 0038): the seam from ``radio-server.jsonl`` to the
pure summarizer.

:class:`~radio_server.eventlog.sink.JsonlSink` (ADR 0018) writes the ledger as one compact JSON
object per line; :func:`~radio_server.eventlog.summary.summarize_activity` (ADR 0036) consumes an
already-parsed iterable of those dicts. :func:`read_records` is the missing middle: it **streams** the
file line by line and yields one record dict at a time.

It **parses only** — it never filters by record ``type`` or timestamp; the summarizer owns that. It
is a stdlib-only sibling of ``sink.py`` and imports no other ``radio_server`` layer. Path resolution
is not re-implemented here: ``load_log_path(settings)`` in ``sink.py`` is the existing resolver at
the composition edge, and callers pass the resolved path in.

Failure stance — the crux of ADR 0038, and the deliberate inverse of the sink:

- **Missing file → empty**, never raises. A fresh install has no history yet; that is expected, not
  an error. (The sink, by contrast, fails loud on an unwritable path: writing to nowhere is a bug.)
- **Torn / garbage / non-dict line → skipped**, never raises. The writer may be mid-append or have
  crashed, leaving a truncated tail; the ledger is history, not input to validate, so one bad line
  must not sink the whole read. (Mirrors the summarizer's own skip-don't-raise stance.)
- **Concurrent writer → open, stream, close.** No lock, no tail, no retry — a summary is a
  point-in-time snapshot; records appended after the read starts are seen on the next call.

**Known limit (documented in ADR 0038, not solved here):** every summary re-reads the *whole* ledger
— ``O(all history)`` per call — so records outside the window are parsed and discarded each time.
Fine at today's size; reverse-seek, an index, and rotation are all deferred.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any


def read_records(path: str | os.PathLike[str]) -> Iterator[dict[str, Any]]:
    """Stream ledger records from a JSONL file, one parsed dict at a time.

    A generator over the file's lines (never ``readlines()``/``read()`` — the append-only ledger is
    unbounded). Each line is parsed with :func:`json.loads`; a line that fails to parse (a torn final
    line, or garbage) or that parses to a non-dict is **skipped**, not raised. A missing file yields
    nothing (no history yet is not an error). Records are yielded in file order; ``type``/time
    filtering is left to :func:`~radio_server.eventlog.summary.summarize_activity`.
    """
    try:
        fh = open(path, "r", encoding="utf-8")  # utf-8, matching JsonlSink
    except FileNotFoundError:
        # A not-yet-existing history is the normal first-run state, not an error. Deliberate
        # asymmetry with the sink, which fails loud when its path is unwritable (ADR 0018).
        return

    with fh:  # closes on exhaustion or early GeneratorExit
        for line in fh:  # line-by-line: memory stays flat regardless of ledger size
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                # Torn tail or garbage line: skip it. History tolerates a bad line; it never
                # invalidates the whole read.
                continue
            if isinstance(record, dict):
                yield record
            # A parsed non-dict (bare number/list/string) is structurally not a record — skip it.
            # An unknown record *type* is still a dict and passes through untouched by design.
