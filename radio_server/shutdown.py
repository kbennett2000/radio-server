"""Bounded task joins for teardown — ADR 0104's contract, with a primitive that actually keeps it.

ADR 0104 gave every shutdown join a deadline so a wedged task could never hold `systemctl stop` past
the unit's `TimeoutStopSec` and earn the SIGKILL that severs the DV Dongle mid-operation. Four sites
implemented it as ``await asyncio.wait_for(task, timeout=...)``, each with a comment naming the case
it was defending against — *"a task parked in a non-cancellable blocking call"*, *"abandon a
still-parked task instead"*.

`wait_for` cannot do that. On expiry it **cancels the awaited task and then waits for the cancellation
to be delivered**, so against a task that cannot take a cancel — the only case worth bounding — the
guard blocks for exactly as long as the thing it was guarding. Measured (ADR 0184):

    wait_for(bound=0.5s): returned=False after 3.00s     <- the 3.0 s is the observer giving up
    wait(bound=0.5s):     returned=True  after 0.50s

A coroutine inside a synchronous call is the reachable form: `ScanEngine.tick()` is documented as
fully synchronous and tunes over serial, and the bridges do blocking sends. The cancel is not
delivered until that call returns and the coroutine reaches its next await, so `wait_for` waits out
the blocking call in full — unbounded, which is precisely what ADR 0104 set out to remove.

`asyncio.wait` is the primitive that matches the intent: it reports the deadline and touches nothing.
The caller has already called `cancel()`; this only decides how long to wait before walking away.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable


async def join_bounded(tasks: Iterable[asyncio.Task | None], timeout: float) -> bool:
    """Wait up to ``timeout`` for already-cancelled ``tasks``; return whether they all finished.

    Concurrent (one deadline for the whole set, not one each — ADR 0104's compounding fix), never
    cancels anything itself, and never raises: a task that ended in an exception is left for the
    caller to inspect, and an abandoned task is daemon-side cleanup at interpreter exit, which ADR
    0104 accepted explicitly. ``None`` entries and already-finished tasks are ignored, so callers
    need no guard of their own.
    """
    pending = {task for task in tasks if task is not None and not task.done()}
    if not pending:
        return True
    _, still_pending = await asyncio.wait(pending, timeout=timeout)
    return not still_pending
