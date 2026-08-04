"""Bounded task joins for teardown — ADR 0104's contract, with a primitive that actually keeps it.

ADR 0104 gave the shutdown joins a deadline so a wedged task could never hold `systemctl stop` past
the unit's `TimeoutStopSec` and earn the SIGKILL that severs the DV Dongle mid-operation. Three of
them — the RX pump's task, and the Mumble and D-STAR bridges' task sets — were written as
``await asyncio.wait_for(task, timeout=...)``, each with a comment naming the case it was defending
against: *"a task parked in a non-cancellable blocking call"*, *"abandon a still-parked task
instead"*. (A fourth, `ScanRunner`, had no deadline at all.)

`wait_for` cannot do that for a **Task**. On expiry it **cancels the awaited task and then waits for
the cancellation to be delivered**, so against a task that cannot take a cancel — the only case worth
bounding — the guard blocks for exactly as long as the thing it was guarding. Measured (ADR 0184):

    wait_for(bound=0.5s): returned=False after 3.00s     <- the 3.0 s is the observer giving up
    wait(bound=0.5s):     returned=True  after 0.50s

A coroutine inside a synchronous call is the reachable form: `ScanEngine.tick()` is documented as
fully synchronous and tunes over serial, and the bridges do blocking sends. The cancel is not
delivered until that call returns and the coroutine reaches its next await, so `wait_for` waits out
the blocking call in full — unbounded, which is precisely what ADR 0104 set out to remove.

`asyncio.wait` is the primitive that matches the intent: it reports the deadline and touches nothing.
The caller has already called `cancel()`; this only decides how long to wait before walking away.

**This does not apply to `run_in_executor`**, and the D-STAR bridge's bounded `vocoder.close()` wait
is deliberately left as `wait_for`. Cancelling the asyncio wrapper around an executor future succeeds
immediately whether or not the worker thread notices, so that deadline really does hold — measured at
0.50 s against a wedged 30 s worker. The thread is abandoned, which is the documented intent there.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable


async def join_bounded(
    tasks: Iterable[asyncio.Task | None], timeout: float
) -> list[BaseException]:
    """Wait up to ``timeout`` for already-cancelled ``tasks``; return what any of them died of.

    Concurrent — one deadline for the whole set, not one each, which is ADR 0104's compounding fix —
    and it never cancels anything itself: the caller has already called ``cancel()``, so this only
    decides how long to wait before walking away. A task still pending at the deadline is abandoned,
    which ADR 0104 accepted explicitly (daemon-side cleanup at interpreter exit).

    **Never raises.** A teardown is not where a background task's fault gets to surface — the steps
    behind this one still have to run (ADR 0184). But the exception is *retrieved* rather than
    ignored, both so asyncio does not log it as never-retrieved (which the previous
    ``gather(..., return_exceptions=True)`` shape also avoided) and so a caller that wants to log it
    can. Callers with nothing useful to say about it may discard the return value.

    ``None`` entries and already-finished tasks are handled, so callers need no guard of their own.
    """
    known = {task for task in tasks if task is not None}
    pending = {task for task in known if not task.done()}
    if pending:
        await asyncio.wait(pending, timeout=timeout)
    errors: list[BaseException] = []
    for task in known:
        if not task.done() or task.cancelled():
            continue
        error = task.exception()
        if error is not None:
            errors.append(error)
    return errors
