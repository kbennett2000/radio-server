"""The activity-summary REST surface — the Tier-0 "is this repeater dead?" rollup over HTTP (ADR 0039).

One route, `GET /activity/summary`, exposes the composition the previous cycles built internally:
stream the on-disk ledger (`read_records`, ADR 0038) into the pure summarizer (`summarize_activity`,
ADR 0036) and return the resulting `ChannelActivity` as JSON. The window and squelch-crackle cutoff
come from settings (`activity.window` / `activity.min_duration`); `tz` from `time.tz`; `now` from the
wall clock at the composition edge.

**Why the work runs off the event loop.** `read_records` does synchronous file I/O and
`summarize_activity` walks the *whole* ledger — `O(all history)` per call (named in ADR 0038). This
process's main job is real-time audio: doing that walk inline in an `async` handler would block the
event loop and stall the `RxPump` and every `/events` / `/audio/rx` subscriber. So the entire
blocking chain is handed to :func:`asyncio.to_thread`. Same instinct as ADR 0028's async scan runner
(keep synchronous work off the loop); different mechanism, because a single unbounded ledger walk
can't be chunked across `tick()`s the way the scan engine's short steps are.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, FastAPI

from ..eventlog import ChannelActivity, load_log_path, read_records, summarize_activity
from ..services import load_timezone


def _run_summary(
    path: str, now: float, tz: ZoneInfo, window: timedelta, min_duration: float
) -> ChannelActivity:
    """The blocking chain, run whole inside a worker thread: read the ledger and summarize it.

    Both the file I/O *and* the full-ledger walk happen here — ``read_records`` is a generator, so
    it is consumed lazily *by* ``summarize_activity`` within this call, keeping every synchronous
    step off the event loop rather than just the ``open``.
    """
    return summarize_activity(
        read_records(path), now=now, tz=tz, window=window, min_duration=min_duration
    )


def register_activity_routes(api: APIRouter, app: FastAPI) -> None:
    """Attach the activity-summary route to the token-gated ``api`` router."""

    @api.get("/activity/summary")
    async def activity_summary() -> dict[str, Any]:
        settings = app.state.settings
        path = load_log_path(settings)
        tz = load_timezone(settings)
        window = timedelta(seconds=settings.get("activity.window"))
        min_duration = settings.get("activity.min_duration")
        now = time.time()  # wall clock resolved at the edge, mirroring the summarizer's convention
        summary = await asyncio.to_thread(_run_summary, path, now, tz, window, min_duration)
        # A missing or empty ledger yields a zeroed ChannelActivity (no history yet is a valid
        # answer, not an error) — returned as a normal 200, never a 404 or 500.
        return asdict(summary)
