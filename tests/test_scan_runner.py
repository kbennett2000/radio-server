"""The async scan runner: background start/stop of ScanEngine.tick (ADR 0028).

Unit tests drive :class:`ScanRunner` directly with ``asyncio.run(...)`` — there is no pytest-asyncio,
the same convention as ``test_rx_audio.py``/``test_controller.py``. The engine is built with the
``FakeClock`` from conftest and ``poll=0``, so nothing sleeps for real: the runner's loop yields with
``await asyncio.sleep(0)`` and the engine's timing decisions run against the fake clock.

The load-bearing proofs: a started scan runs in the background and emits ``scanning``; a second start
while running is refused (one scan at a time); a stop cancels cleanly, drops to idle, and emits
``stopped`` with no leaked task; a stop when idle is a clean no-op that emits nothing; and a stop while
the scan is TX-suspended (ADR 0017 — ticks early-return) cancels without wedging.
"""

from __future__ import annotations

import asyncio

from radio_server.arbiter import RadioArbiter
from radio_server.backends import MockRadio
from radio_server.scan import ResumeMode, ScanEngine, ScanEvent, ScanPlan, ScanRunner

from .conftest import FakeClock

FREQS = [146_500_000, 146_520_000, 146_540_000]
BUSY = FREQS[1]
SETTLE = 0.05


def _make_runner(
    radio: MockRadio,
    *,
    clock: FakeClock,
    arbiter: RadioArbiter | None = None,
    events: list[ScanEvent] | None = None,
    poll: float = 0.0,
) -> ScanRunner:
    def factory(plan: ScanPlan, on_event):
        return ScanEngine(
            radio,
            plan,
            on_event=on_event,
            mode=ResumeMode.CARRIER,
            settle=SETTLE,
            clock=clock,
            arbiter=arbiter,
        )

    return ScanRunner(
        factory,
        on_event=(events.append if events is not None else None),
        poll=poll,
    )


def test_runner_starts_in_background_and_emits_scanning():
    events: list[ScanEvent] = []
    radio = MockRadio(supports_cat=True, busy_frequencies={BUSY})
    clock = FakeClock()

    async def scenario() -> ScanRunner:
        runner = _make_runner(radio, clock=clock, events=events)
        assert runner.start(ScanPlan.from_frequencies(FREQS)) is True
        assert runner.running is True  # set synchronously, before the task first runs
        await asyncio.sleep(0)  # let run() do its first tick
        await asyncio.sleep(0)
        await runner.stop()
        return runner

    runner = asyncio.run(scenario())
    assert "scanning" in [e.phase for e in events]  # the background task actually stepped the engine
    assert runner.running is False


def test_runner_start_while_running_is_rejected():
    radio = MockRadio(supports_cat=True)
    clock = FakeClock()

    async def scenario() -> ScanRunner:
        runner = _make_runner(radio, clock=clock)
        assert runner.start(ScanPlan.from_frequencies(FREQS)) is True
        assert runner.start(ScanPlan.from_frequencies(FREQS)) is False  # single-scan guard
        await runner.stop()
        return runner

    runner = asyncio.run(scenario())
    assert runner.running is False


def test_runner_stop_is_clean_and_emits_stopped():
    events: list[ScanEvent] = []
    radio = MockRadio(supports_cat=True, busy_frequencies={BUSY})
    clock = FakeClock()

    async def scenario() -> ScanRunner:
        runner = _make_runner(radio, clock=clock, events=events)
        runner.start(ScanPlan.from_frequencies(FREQS))
        await asyncio.sleep(0)
        assert await runner.stop() is True
        return runner

    runner = asyncio.run(scenario())
    assert events[-1].phase == "stopped"  # the runner's lifecycle event lands last
    assert runner.running is False
    assert runner._task is None  # no leaked task (mirrors the RxPump proof)
    assert runner.current_frequency is None


def test_runner_stop_when_idle_is_clean_noop():
    events: list[ScanEvent] = []
    radio = MockRadio(supports_cat=True)
    clock = FakeClock()

    async def scenario() -> bool:
        runner = _make_runner(radio, clock=clock, events=events)
        return await runner.stop()

    stopped = asyncio.run(scenario())
    assert stopped is False
    assert events == []  # a no-op stop emits nothing


def test_runner_stop_while_tx_suspended_does_not_wedge():
    radio = MockRadio(supports_cat=True, busy_frequencies={BUSY})
    clock = FakeClock()
    arbiter = RadioArbiter()
    arbiter.acquire_tx()  # TX holds the radio; every tick early-returns (ADR 0017)

    async def scenario() -> ScanRunner:
        runner = _make_runner(radio, clock=clock, arbiter=arbiter)
        runner.start(ScanPlan.from_frequencies(FREQS))
        for _ in range(5):  # loop spins on early-returning ticks — it must not block
            await asyncio.sleep(0)
        assert await runner.stop() is True  # cancels cleanly despite the suspension
        return runner

    runner = asyncio.run(scenario())
    assert runner.running is False
    assert runner._task is None
