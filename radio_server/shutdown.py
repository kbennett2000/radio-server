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

**A deadline that expires is not the same as a worker that goes away** — and the paragraph that used
to sit here got that wrong. It said `wait_for` over a `run_in_executor` future "really does hold —
measured at 0.50 s against a wedged 30 s worker", which is true of the **await** and false of the
**process**. Measured (ADR 0185), wedging a worker for 30 s:

    default executor:  await returned 0.50s   asyncio.run() returned 30.00s   process exited 30.9s
    daemon thread:     await returned 0.50s   asyncio.run() returned  0.50s   process exited  1.2s

`asyncio.Runner.close()` calls `loop.shutdown_default_executor(THREAD_JOIN_TIMEOUT)`, and
`THREAD_JOIN_TIMEOUT` is **300**. A dedicated pool is no better — `concurrent.futures.thread`
registers an atexit hook that joins every worker of every pool with no timeout at all:

    pool.shutdown(wait=False) returned in 0.01 ms   ...   process exited after 20.83s

So an executor worker is never abandoned; the hang just moves from the lifespan, where it is logged,
to interpreter shutdown, where it is not — and still ends in the SIGKILL at `TimeoutStopSec` that the
bound existed to prevent. **Only a daemon thread actually walks away**, which is why
:func:`call_bounded` uses one. `SoundCardTxPacer` already established that idiom in-repo.

This matters beyond tidiness: a budget is a sum of bounds, and summing a bound that does not release
the process is asserting something false. See :data:`STOP_BUDGET_S`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from typing import Any

log = logging.getLogger(__name__)


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


@contextlib.contextmanager
def timed(label: str) -> Iterator[None]:
    """Log how long a teardown step took. The instrument the derivations are made from.

    Nothing in this package measured a teardown step before ADR 0185, which is why every bound in the
    budget below was either a round number or a guess. ``try/finally`` with **no** ``except``: the
    per-step guards in ``holder.stop()`` (ADR 0184) stay the only thing that decides whether a fault
    ends a step, and wall time across an ``await`` is exactly what a budget wants to know.
    """
    started = time.monotonic()
    try:
        yield
    finally:
        log.info("teardown: %s took %.3f s", label, time.monotonic() - started)


async def call_bounded(
    fn: Callable[[], Any], timeout: float, *, label: str
) -> float | None:
    """Run a **blocking** call under a deadline that the process actually honours.

    :func:`join_bounded`'s sibling for synchronous work. It cannot be reused here: it bounds
    ``asyncio.Task`` objects, and ``radio.ptt(False)`` / ``radio.close()`` are plain calls that block
    the thread they run on — today, the event loop's.

    Two properties, both load-bearing and both measured (see the module docstring):

    - **Off the loop.** The call runs on its own thread, so a device that has stopped answering
      stalls only itself. On the loop it stalls uvicorn's own shutdown machinery too — the fault
      ADRs 0181 and 0182 spent two cycles removing from the keying paths.
    - **A daemon thread, not an executor.** An abandoned executor worker holds interpreter exit
      (300 s for the default executor, forever for a pool), so an expiring bound would still end in
      the SIGKILL it exists to prevent. A daemon thread is the only shape that truly walks away.

    Returns the elapsed seconds when the call finished, or ``None`` when the deadline expired and the
    worker was abandoned — so a caller that needs to know whether the device was actually released
    (``holder.rebuild``) can ask. Never raises: an exception from ``fn`` is logged and counts as
    "it returned", because the steps behind this one still have to run.
    """
    loop = asyncio.get_running_loop()
    done: asyncio.Future[None] = loop.create_future()
    started = time.monotonic()

    def _settle() -> None:
        if not done.done():
            done.set_result(None)

    def _run() -> None:
        try:
            fn()
        except Exception:
            log.exception("teardown: %s raised; treating it as returned", label)
        # The loop may already be closed if we are the abandoned worker finishing late.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(_settle)

    threading.Thread(target=_run, name=f"teardown-{label}", daemon=True).start()
    try:
        await asyncio.wait_for(done, timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        log.warning(
            "teardown: %s did not return within %.2f s — abandoning the worker thread", label, timeout
        )
        return None
    elapsed = time.monotonic() - started
    log.info("teardown: %s took %.3f s", label, elapsed)
    return elapsed
