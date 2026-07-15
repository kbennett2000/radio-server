"""Channel-activity summary (ADR 0036): the pure transform from RX ledger edges to a busy-ness answer.

Tier 0 — "is this repeater actually dead?" — needs the raw ``rx_open``/``rx_close`` records the
ledger persists (ADR 0035) rolled up into: how often the channel is busy, *when* during the day and
week, and when it was *last heard*. :func:`summarize_activity` is that rollup and nothing more.

It is a **leaf-pure** sibling of :mod:`radio_server.eventlog.log`: stdlib only, no import of any
other ``radio_server`` layer, no :class:`~radio_server.config.Settings`, no disk, no wall clock.
``now`` and ``tz`` are **injected** exactly like ``format_spoken_time(now, tz)`` in
``services/time_service.py`` — a caller at the composition edge resolves them (``time.time`` /
``load_timezone(settings)``) and passes them in, so the whole function is exercisable from literals
with a fake ``now``.

This cycle reads **no file**: callers pass an already-parsed iterable of ledger dicts. The JSONL
reader is a later cycle.

**Known limit — per-radio, not per-frequency.** ``rx_open``/``rx_close`` carry no frequency (the
Baofeng has no CAT), so this summarizes activity on "whatever channel the radio is parked on," not a
specific frequency. Per-frequency attribution waits on the TM-V71A backend (ADR 0036).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

#: Marked default. Records older than this (by their open timestamp) are excluded — the summary
#: answers "is it dead *lately*," not "what did this append-only file ever see." A plain parameter,
#: overridable per call.
DEFAULT_WINDOW = timedelta(days=7)

#: Marked default, **verify on hardware** (guardrail 1). A busy event shorter than this many seconds
#: is treated as a squelch crackle, not a transmission, and excluded. The real crackle-vs-QSO cutoff
#: is a bench fact tuned once audio is flowing; this is a sensible placeholder, never a confirmed
#: value.
MIN_DURATION_DEFAULT = 1.0


@dataclass(frozen=True)
class ChannelActivity:
    """A rollup of RX busy activity over a window, bucketed by local time (ADR 0036).

    ``by_hour`` is 24 buckets indexed by local hour-of-day (0–23); ``by_weekday`` is 7 buckets
    indexed by local weekday (Monday = 0 … Sunday = 6, per :meth:`datetime.date.weekday`). Each
    bucket holds an **event count** — the buckets answer *when* activity happens; ``total_airtime``
    is the separate how-much signal. ``last_heard`` is the open timestamp of the most recent
    surviving event (unix epoch float), or ``None`` when there was no activity.
    """

    busy_count: int
    total_airtime: float
    last_heard: float | None
    by_hour: tuple[int, ...]
    by_weekday: tuple[int, ...]


def _open_timestamps(records: Iterable[dict[str, Any]]) -> list[tuple[float, float]]:
    """Pair ``rx_open``/``rx_close`` records into ``(open_ts, close_ts)`` busy events.

    Mirrors :class:`~radio_server.eventlog.log.EventLog`'s own paired-edge state machine: a single
    pending open. Walks records in iteration order (the ledger is append-only chronological) and
    **skips** — never raises on — anything that is not a dict, lacks a numeric ``ts``, or carries a
    ``type`` other than ``rx_open``/``rx_close`` (older-schema and unrelated records alike).

    An open that is overwritten by a later open before any close (crash / still-busy) is skipped; an
    unpaired close (no open seen) is skipped; a pending open left at the end is skipped.
    """
    events: list[tuple[float, float]] = []
    pending_open_ts: float | None = None

    for record in records:
        if not isinstance(record, dict):
            continue
        rec_type = record.get("type")
        if rec_type not in ("rx_open", "rx_close"):
            continue
        ts = record.get("ts")
        # bool is an int subclass; a stray True/False `ts` is not a real timestamp.
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            continue

        if rec_type == "rx_open":
            # A prior pending open is dropped (unpaired) — we skip it, never guess its close.
            pending_open_ts = float(ts)
        else:  # rx_close
            if pending_open_ts is not None:
                events.append((pending_open_ts, float(ts)))
                pending_open_ts = None
            # else: unpaired close — skip.

    return events


def summarize_activity(
    records: Iterable[dict[str, Any]],
    *,
    now: float,
    tz: ZoneInfo,
    window: timedelta = DEFAULT_WINDOW,
    min_duration: float = MIN_DURATION_DEFAULT,
) -> ChannelActivity:
    """Roll ledger ``rx_open``/``rx_close`` records up into a :class:`ChannelActivity`.

    Pure and deterministic: ``now`` (unix epoch float) and ``tz`` are injected; ``records`` is an
    already-parsed iterable of ledger dicts (no file is read this cycle). Records are paired into
    busy events (see :func:`_open_timestamps`), events older than ``window`` or shorter than
    ``min_duration`` are excluded, and the survivors are aggregated. Malformed/unknown records are
    skipped, and empty (or entirely-skipped) input yields a zeroed summary — never an error.

    Bucketing is by **local** time via ``tz`` (:meth:`datetime.fromtimestamp` with a
    :class:`~zoneinfo.ZoneInfo` is DST-correct), keyed off each event's **open** timestamp.
    """
    cutoff = now - window.total_seconds()

    hours = [0] * 24
    weekdays = [0] * 7
    busy_count = 0
    total_airtime = 0.0
    last_heard: float | None = None

    for open_ts, close_ts in _open_timestamps(records):
        duration = close_ts - open_ts
        if open_ts < cutoff:
            continue
        if duration < min_duration:
            continue

        busy_count += 1
        total_airtime += duration
        if last_heard is None or open_ts > last_heard:
            last_heard = open_ts

        local = datetime.fromtimestamp(open_ts, tz)
        hours[local.hour] += 1
        weekdays[local.weekday()] += 1

    return ChannelActivity(
        busy_count=busy_count,
        total_airtime=total_airtime,
        last_heard=last_heard,
        by_hour=tuple(hours),
        by_weekday=tuple(weekdays),
    )
