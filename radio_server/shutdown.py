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
the process is asserting something false. See :func:`stop_budget_seconds`.
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

#: systemd `Stopping` -> uvicorn `Shutting down`: how long the event loop takes to *notice* SIGTERM.
#: MEASURED on the deployed station, n=201 stops: median 84.4 ms, p99 99.9 ms, **max 194.7 ms**
#: (969 ms historically, before ADRs 0181/0182 took the blocking work off the loop). Nobody had
#: counted this — it is spent before the graceful window even starts, and a blocked loop makes it
#: arbitrarily large, which is the connection between "the loop is busy" and "the stop is late".
SIGNAL_DELIVERY_MAX_S = 0.2

#: `Application shutdown complete` -> the process is actually gone. MEASURED by subtraction on the
#: station: a whole no-client stop maxed at 0.48 s against a 0.195 s signal delivery and a 0.241 s
#: teardown, so exit itself is <= 0.05 s. This is ~5x that.
#:
#: It stays small only because :func:`call_bounded` uses a daemon thread. An abandoned *executor*
#: worker is joined at interpreter exit — 300 s for the default executor, forever for a pool — which
#: would make this term the largest in the budget while looking like zero.
EXIT_RESERVE_S = 0.25

#: Explicit head-room on top of the proven budget. A term, not a multiplier: a multiplier hides the
#: arithmetic, and the arithmetic is the whole point of this module.
STOP_BUDGET_MARGIN_S = 2.0

#: ``TimeoutStopSec`` as shipped in ``scripts/radio-server.service``. Restated here because this is
#: the side that can be *tested*: :func:`stop_budget_seconds` computes what the deadline has to
#: cover, and the identity test asserts both that this covers it and that the unit file says this.
#:
#: **Raised from 20 (ADR 0185), and the distinction from the raise this station's history refuted
#: matters.** 10 -> 20 was made against an **unbounded** stall — `timeout_graceful_shutdown` was
#: unset, i.e. wait forever — so the deadline moved and nothing else did, and 31 more SIGKILLs
#: followed at the new one (ADR 0184). This is sized *to* a finite sum of named constants that a test
#: adds up. Different operation, and the test is what keeps it that way.
#:
#: What a longer deadline costs is only ever a delay to SIGKILL, which fires when the teardown has
#: already failed — and the measured teardown is 241 ms at its worst over n=201.
TIMEOUT_STOP_SEC = 35.0


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


def teardown_budget_seconds() -> float:
    """Sum every bound the in-process teardown can spend, in lifespan order.

    Computed rather than declared, and computed **here** rather than in a test, so the server can be
    asked what its own deadline needs to be. Imports live inside the function: `shutdown` is imported
    *by* four of the modules it has to read (`rx.pump`, `link.bridge`, `dstar.bridge`, `scan.runner`),
    so a module-level import would be a cycle.

    Only **outer** bounds are charged. A bound that sits inside another bounded call is absorbed, not
    additive — the uvk5 transport's reader join and the TX pacer's join are both inside
    ``radio.close()``, which now has a deadline of its own, and that is exactly what bounding the
    outer call buys. What *is* additive is a synchronous thread join reached from inside an async
    bound: `PolledGate.stop()` runs in `RxPump.run()`'s ``finally``, i.e. during cancellation
    delivery, and `asyncio.wait`'s timer cannot fire while the loop thread sits in `thread.join()`.
    **A bound expressed in async time does not cover synchronous work done inside it.**

    The D-STAR terms assume the shipped ``tx_hang`` default; a station that raises it raises its own
    requirement, which is why this is a function and not a constant.
    """
    from .activity.broadcast_fm_poll import CADENCE_JOIN_TIMEOUT_S
    from .activity.gate import GATE_JOIN_TIMEOUT_S
    from .api.holder import PTT_OFF_TIMEOUT_S, RADIO_CLOSE_TIMEOUT_S
    from .audio.dtmf import CONTROLLER_CLOSE_BUDGET_S
    from .dstar.bridge import DEFAULT_DSTAR_TX_HANG, DSTAR_JOIN_MARGIN_S
    from .dstar.client import GATEWAY_JOIN_TIMEOUT_S
    from .eventlog.sink import LEDGER_CLOSE_TIMEOUT_S
    from .link.bridge import MUMBLE_TASK_JOIN_TIMEOUT_S
    from .link.pymumble_client import DEFAULT_JOIN_TIMEOUT
    from .rx.pump import PUMP_JOIN_TIMEOUT_S, READER_JOIN_TIMEOUT_S
    from .scan.runner import SCAN_JOIN_TIMEOUT_S

    dstar_join = DEFAULT_DSTAR_TX_HANG + DSTAR_JOIN_MARGIN_S
    return sum(teardown_budget_parts(
        dstar_vocoder_close=dstar_join,
        dstar_task_join=dstar_join,
        dstar_gateway_join=GATEWAY_JOIN_TIMEOUT_S,
        broadcast_fm_cadence_join=CADENCE_JOIN_TIMEOUT_S,
        mumble_task_join=MUMBLE_TASK_JOIN_TIMEOUT_S,
        pymumble_library_join=DEFAULT_JOIN_TIMEOUT,
        holder_ptt_off=PTT_OFF_TIMEOUT_S,
        holder_scan_join=SCAN_JOIN_TIMEOUT_S,
        holder_pump_task_join=PUMP_JOIN_TIMEOUT_S,
        holder_capture_reader_join=READER_JOIN_TIMEOUT_S,
        holder_squelch_gate_join=GATE_JOIN_TIMEOUT_S,
        holder_decoder_reap=CONTROLLER_CLOSE_BUDGET_S,
        holder_radio_close=RADIO_CLOSE_TIMEOUT_S,
        ledger_close=LEDGER_CLOSE_TIMEOUT_S,
    ).values())


def teardown_budget_parts(**parts: float) -> dict[str, float]:
    """Identity helper: the budget's terms, so a failing test can name which one moved."""
    return dict(parts)


def stop_budget_seconds() -> float:
    """What ``TimeoutStopSec`` has to cover: notice SIGTERM, drain, tear down, exit."""
    from .__main__ import GRACEFUL_SHUTDOWN_SECONDS

    return (
        SIGNAL_DELIVERY_MAX_S
        + GRACEFUL_SHUTDOWN_SECONDS
        + teardown_budget_seconds()
        + EXIT_RESERVE_S
    )
