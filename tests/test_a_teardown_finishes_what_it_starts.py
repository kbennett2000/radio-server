"""ADR 0184: a teardown that raises partway through must still reach the end of the teardown.

`holder.stop()` is the pipeline teardown — drop PTT, stop the scan, halt the pump, reap the DTMF
decoder, close the radio device — and its docstring claimed *"each step independently guarded"*. Two
of the five steps were not:

    if self.scan_runner is not None:
        await self.scan_runner.stop()      # unguarded
    if self.rx_pump is not None:
        await self.rx_pump.stop()          # unguarded
    ...
    close()                                # never reached if either of those raises

And `ScanRunner.stop()` caught only ``CancelledError``, so anything else the task died of came back
out at the join. `ScanEngine.tick()` tunes the radio (``_tune`` → ``set_frequency``) and has no
try/except, so a serial fault mid-scan ends the task with that exception stored on it. Nothing clears
``_task``, so the *next* teardown cancels an already-finished task (a no-op), re-raises the stored
exception at the join, and skips the pump stop, the decoder reap and ``radio.close()``.

Why that matters is the `rebuild` path, not shutdown. On process exit the kernel cleans up regardless
and `HUPCL` still drops the carrier (ADR 0183). But ``POST /radio/select`` runs the same
``stop()`` with no process exit: a skipped ``close()`` leaves the serial device ``TIOCEXCL``-claimed
by this very process, so the swap that follows fails against a port nothing else can take.

The join was also the one ADR 0104 never bounded — `test_shutdown_budget.py` rigs a stubborn task on
the D-STAR bridge, the Mumble bridge and the RX pump, and `ScanRunner` is absent from all three.
"""

from __future__ import annotations

import asyncio
import time

from radio_server.api.events import EventHub
from radio_server.api.holder import RadioHolder
from radio_server.arbiter import RadioArbiter
from radio_server.backends import MockRadio
from radio_server.config import resolve_settings
from radio_server.rx import AudioHub
from radio_server.scan import ScanPlan
from radio_server.scan.engine import DEFAULT_SCAN_POLL
from radio_server.scan.runner import SCAN_JOIN_TIMEOUT_S, ScanRunner
from radio_server.shutdown import join_bounded

FREQS = [146_500_000, 146_520_000, 146_540_000]


class _TeardownSpyRadio(MockRadio):
    """Records `close()` — the last step of the teardown, and the one that gets skipped.

    Also optionally fails `set_frequency`, which is how a real scan task dies: `ScanEngine._tune`
    calls it every channel step, and a UV-K5 that has been switched off raises there.
    """

    def __init__(self, *, tune_fails: bool = False, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.tune_fails = tune_fails
        self.closed = 0

    def set_frequency(self, hz: int) -> None:
        if self.tune_fails:
            raise OSError("serial write failed: device reports not ready")
        super().set_frequency(hz)

    def close(self) -> None:
        self.closed += 1


class _ReapSpyController:
    """Stands in for the DTMF controller, so the test can see whether the decoder was reaped."""

    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


def _make_holder(radio: MockRadio, *, controller: object | None = None) -> RadioHolder:
    """The `test_radio_holder.py` fixture, trimmed to what these tests drive."""
    return RadioHolder(
        radio,
        hub=EventHub(),
        audio_hub=AudioHub(),
        arbiter=RadioArbiter(),
        scan_settings=resolve_settings({}),
        scan_poll=0.0,
        controller=controller,  # type: ignore[arg-type]
    )


async def _scan_until_dead(holder: RadioHolder) -> None:
    """Start a scan whose first tune raises, and wait for the task to die of it.

    Deliberately does NOT touch `_task`: the point is that a task which died on its own is left
    in place, which is exactly the state the next `stop()` walks into.
    """
    assert holder.scan_runner is not None
    holder.scan_runner.start(ScanPlan.from_frequencies(FREQS))
    for _ in range(500):
        task = holder.scan_runner._task
        if task is not None and task.done():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("the scan task never died — set_frequency was not reached")


def _stubborn(release: asyncio.Event) -> asyncio.Task:
    """A task that survives cancellation — the `test_shutdown_budget.py` model of an unreachable park."""

    async def run() -> None:
        while not release.is_set():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                continue

    return asyncio.create_task(run())


# --- the teardown reaches its end ------------------------------------------------------------------


def test_a_scan_task_that_died_does_not_skip_the_radio_close() -> None:
    """FAIL-FIRST (ADR 0184). Red on master: the stored exception comes back out of the join.

    On master this raises `OSError` out of `holder.stop()` and `close()` is never called — so on the
    `rebuild` path the port stays claimed by a process that thinks it let go.
    """

    async def scenario() -> _TeardownSpyRadio:
        radio = _TeardownSpyRadio(tune_fails=True)
        holder = _make_holder(radio)
        holder.start()
        await _scan_until_dead(holder)
        await holder.stop()
        return radio

    radio = asyncio.run(scenario())
    assert radio.closed == 1, (
        "holder.stop() never reached radio.close() — a scan task that died of a tune error took the "
        "rest of the teardown with it, and on POST /radio/select that leaves the serial port "
        "TIOCEXCL-claimed by this same process"
    )


def test_a_scan_task_that_died_does_not_skip_the_decoder_reap() -> None:
    """The step between the two: the persistent multimon-ng process (ADR 0038) must still be reaped."""

    async def scenario() -> _ReapSpyController:
        controller = _ReapSpyController()
        holder = _make_holder(_TeardownSpyRadio(tune_fails=True), controller=controller)
        holder.start()
        await _scan_until_dead(holder)
        await holder.stop()
        return controller

    controller = asyncio.run(scenario())
    assert controller.closed == 1, (
        "the DTMF decoder was never reaped — a dead scan task orphans the multimon-ng subprocess"
    )


def test_an_rx_pump_stop_that_raises_does_not_skip_the_radio_close() -> None:
    """The second unguarded await. Same contract, so neither step can be the one that ends a teardown."""

    async def scenario() -> _TeardownSpyRadio:
        radio = _TeardownSpyRadio()
        holder = _make_holder(radio)
        holder.start()
        assert holder.rx_pump is not None

        async def _boom() -> None:
            raise RuntimeError("the pump's executor is gone")

        holder.rx_pump.stop = _boom  # type: ignore[method-assign]
        await holder.stop()
        return radio

    radio = asyncio.run(scenario())
    assert radio.closed == 1, "an rx_pump.stop() failure skipped radio.close()"


def test_the_teardown_still_reports_nothing_when_every_step_is_clean() -> None:
    """The guards must not turn a healthy teardown into a silent one — close() runs exactly once."""

    async def scenario() -> _TeardownSpyRadio:
        radio = _TeardownSpyRadio()
        holder = _make_holder(radio)
        holder.start()
        holder.scan_runner.start(ScanPlan.from_frequencies(FREQS))  # type: ignore[union-attr]
        await holder.stop()
        await holder.stop()  # idempotent
        return radio

    radio = asyncio.run(scenario())
    assert radio.closed == 2, f"close() ran {radio.closed}x across two stops, expected one each"


# --- the join is bounded ---------------------------------------------------------------------------


def test_scan_runner_stop_is_bounded_against_a_stubborn_task() -> None:
    """FAIL-FIRST (ADR 0184). Red on master: `await task` with no bound — this hangs forever.

    The gap ADR 0104 left. Its three siblings (D-STAR bridge, Mumble bridge, RX pump) are each pinned
    by `test_shutdown_budget.py`; `ScanRunner` was pinned by an argument in a docstring instead.
    """

    async def scenario() -> tuple[bool, float]:
        runner = ScanRunner(lambda plan, on_event: None, poll=0.0)  # type: ignore[arg-type]
        release = asyncio.Event()
        task = _stubborn(release)
        # Let it actually reach the sleep. A task cancelled before its first step ends as CANCELLED
        # without ever entering its own `except`, so an unbounded join would look bounded — the trap
        # `test_shutdown_budget.py`'s three siblings sat in until this ADR.
        await asyncio.sleep(0.05)
        runner._task = task
        runner._running = True
        # OBSERVED, not cancelled. `wait_for` would cancel `stop()`, whose cancellation cannot be
        # delivered while it is parked on a task that ignores cancels — so the guard itself hangs
        # forever. `asyncio.wait` reports the deadline without touching the coroutine.
        stopping = asyncio.create_task(runner.stop())
        started = time.monotonic()
        done, _ = await asyncio.wait({stopping}, timeout=SCAN_JOIN_TIMEOUT_S + 1.0)
        elapsed = time.monotonic() - started
        release.set()  # let the stubborn task end so the pending stop can unwind
        task.cancel()
        await asyncio.gather(stopping, task, return_exceptions=True)
        return bool(done), elapsed

    finished, elapsed = asyncio.run(scenario())
    assert finished, (
        f"ScanRunner.stop() had not returned after {elapsed:.2f}s against a {SCAN_JOIN_TIMEOUT_S}s "
        f"bound — the join is unbounded, and it sits on holder.stop()'s critical path with "
        f"radio.close() behind it"
    )
    assert elapsed < SCAN_JOIN_TIMEOUT_S + 0.5, (
        f"ScanRunner.stop() took {elapsed:.2f}s against a {SCAN_JOIN_TIMEOUT_S}s bound"
    )


def test_a_dead_scan_task_does_not_come_back_out_of_the_join() -> None:
    """`ScanRunner.stop()` alone: a teardown is not where an engine fault gets to surface."""

    async def scenario() -> bool:
        radio = _TeardownSpyRadio(tune_fails=True)
        settings = resolve_settings({})
        from radio_server.scan import build_scan_engine

        runner = ScanRunner(
            lambda plan, on_event: build_scan_engine(
                settings, radio=radio, plan=plan, on_event=on_event
            ),
            poll=0.0,
        )
        runner.start(ScanPlan.from_frequencies(FREQS))
        for _ in range(500):
            if runner._task is not None and runner._task.done():
                break
            await asyncio.sleep(0.001)
        return await runner.stop()

    stopped = asyncio.run(scenario())
    assert stopped is True, "stop() must still report that it stopped a scan"


def test_the_scan_join_bound_is_derived_from_the_poll_cadence() -> None:
    """DERIVED, not chosen: a cancel can only land at the loop's ``await asyncio.sleep(self._poll)``.

    So the bound has to clear one poll period comfortably — otherwise a perfectly healthy runner
    mid-sleep would be abandoned every stop, which is the ADR 0183 mistake in a different module.
    Small enough to stay invisible against uvicorn's 5 s graceful window and the unit's
    ``TimeoutStopSec=20``.
    """
    assert SCAN_JOIN_TIMEOUT_S >= DEFAULT_SCAN_POLL * 2, (
        f"the scan join bound is {SCAN_JOIN_TIMEOUT_S}s against a {DEFAULT_SCAN_POLL}s poll — too "
        f"tight, so a healthy runner parked in its own sleep would be abandoned"
    )
    assert SCAN_JOIN_TIMEOUT_S <= 2.0, (
        f"the scan join bound is {SCAN_JOIN_TIMEOUT_S}s — large enough to matter against the "
        f"20 s stop budget, which is already over-subscribed"
    )


def test_join_bounded_retrieves_what_a_task_died_of_instead_of_raising_it() -> None:
    """The shared primitive's contract: never raise, but never silently drop it either.

    The shape it replaces was ``wait_for`` (which re-raises, ending the teardown) at one site and
    ``gather(..., return_exceptions=True)`` (which consumes) at two others. Returning the exceptions
    keeps the consuming behaviour — so asyncio does not log a never-retrieved task — while giving the
    caller something to log.
    """

    async def scenario() -> tuple[list[BaseException], list[BaseException]]:
        async def boom() -> None:
            raise OSError("the device went away")

        dead = asyncio.create_task(boom())
        await asyncio.sleep(0.01)
        from_dead = await join_bounded([dead, None], 0.5)

        release = asyncio.Event()
        parked = _stubborn(release)
        await asyncio.sleep(0.05)
        parked.cancel()
        from_parked = await join_bounded([parked], 0.1)  # abandoned, not an error
        release.set()
        parked.cancel()
        await asyncio.gather(parked, return_exceptions=True)
        return from_dead, from_parked

    from_dead, from_parked = asyncio.run(scenario())
    assert len(from_dead) == 1 and isinstance(from_dead[0], OSError)
    assert from_parked == [], "an abandoned task is not an error — it is ADR 0104's escape"


def test_join_bounded_is_a_clean_no_op_for_an_empty_or_finished_set() -> None:
    """`asyncio.wait` rejects an empty set, so the guard lives in the helper, not at four call sites."""

    async def scenario() -> tuple[list, list]:
        done = asyncio.create_task(asyncio.sleep(0))
        await asyncio.sleep(0.01)
        return await join_bounded([], 0.1), await join_bounded([done, None], 0.1)

    assert asyncio.run(scenario()) == ([], [])


def test_stop_is_still_a_clean_no_op_when_nothing_is_scanning() -> None:
    """Unchanged contract: a stop with nothing running reports False and is repeatable."""

    async def scenario() -> tuple[bool, bool]:
        runner = ScanRunner(lambda plan, on_event: None, poll=0.0)  # type: ignore[arg-type]
        return await runner.stop(), await runner.stop()

    assert asyncio.run(scenario()) == (False, False)
