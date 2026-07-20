"""ADR 0104: every shutdown join is bounded — a stubborn task can never hang (or compound) a stop.

The worst case each join site faces is a task parked in a non-cancellable blocking call (a wedged
executor decode, a blocking send/receive): modelled here by a coroutine that CATCHES CancelledError
and keeps sleeping. Pre-ADR 0104 the Mumble bridge and RX pump awaited such a task with NO bound
(shutdown hung until systemd's SIGKILL — which severs the DV Dongle mid-operation and wedges it),
and the D-STAR bridge joined its four tasks sequentially, compounding ~12 s of bounded waits. Each
``stop()`` must now return within its documented budget regardless of task cooperation.

Timing style: real (small) wall-clock bounds, generous margins — the asserts distinguish "bounded"
(a few seconds) from "hung/sequential" (multiples of the bound / forever), not exact durations.
"""

from __future__ import annotations

import asyncio
import time

from radio_server.arbiter import RadioArbiter
from radio_server.backends import MockRadio
from radio_server.dstar import DStarBridge, MockGatewayClient
from radio_server.link import MockMumbleClient, MumbleBridge
from radio_server.rx import AudioHub, RxPump
from radio_server.tx import TxSlot


def _stubborn(release: asyncio.Event) -> asyncio.Task:
    """A task that survives cancellation — the model of a park no cancel can reach."""

    async def run():
        while not release.is_set():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                continue  # ignore the cancel; only the release event ends us (test cleanup)

    return asyncio.create_task(run())


async def _reap(release: asyncio.Event, tasks: list[asyncio.Task]) -> None:
    """Test cleanup: end the stubborn tasks so the loop closes without pending-task noise."""
    release.set()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def test_dstar_bridge_joins_tasks_concurrently_not_sequentially():
    # Four stubborn tasks x a (tx_hang + 2.0) bound: sequential joins would burn ~4x the bound
    # (~8.2 s at tx_hang=0.05); the ADR 0104 concurrent join burns it ONCE. The assert threshold
    # sits between the two, so a regression back to sequential joins fails this test.
    async def scenario():
        bridge = DStarBridge(
            MockGatewayClient(),
            MockRadio(),
            lambda: None,  # never started, so the factory is never called
            arbiter=RadioArbiter(),
            tx_slot=TxSlot(),
            audio_hub=AudioHub(),
            callsign="AE9S",
            module="A",
            tx_hang=0.05,
        )
        release = asyncio.Event()
        tasks = [_stubborn(release) for _ in range(4)]
        bridge._running = True
        bridge._tasks = list(tasks)
        started = time.monotonic()
        await asyncio.wait_for(bridge.stop(), timeout=10.0)  # outer guard: never hang the suite
        elapsed = time.monotonic() - started
        await _reap(release, tasks)
        assert elapsed < 4.5, f"stop took {elapsed:.1f}s — joins look sequential again"

    asyncio.run(scenario())


def test_mumble_bridge_stop_is_bounded_against_a_stubborn_task():
    # Pre-ADR 0104 this hung FOREVER: link/bridge.py awaited each cancelled task with no wait_for.
    async def scenario():
        bridge = MumbleBridge(
            MockMumbleClient(),
            MockRadio(),
            arbiter=RadioArbiter(),
            tx_slot=TxSlot(),
            audio_hub=AudioHub(),
        )
        release = asyncio.Event()
        tasks = [_stubborn(release)]
        bridge._running = True
        bridge._tasks = list(tasks)
        started = time.monotonic()
        await asyncio.wait_for(bridge.stop(), timeout=10.0)
        elapsed = time.monotonic() - started
        await _reap(release, tasks)
        assert elapsed < 4.0, f"stop took {elapsed:.1f}s — the join bound is gone"

    asyncio.run(scenario())


def test_rx_pump_stop_is_bounded_against_a_stubborn_task():
    # Pre-ADR 0104 this hung FOREVER: rx/pump.py awaited its cancelled task with no wait_for — and
    # the pump's task really can park in a blocking backend receive() (the ADR 0029 limitation).
    async def scenario():
        pump = RxPump(MockRadio(), AudioHub(), poll=0)
        release = asyncio.Event()
        task = _stubborn(release)
        pump._task = task
        pump._running = True
        started = time.monotonic()
        await asyncio.wait_for(pump.stop(), timeout=10.0)
        elapsed = time.monotonic() - started
        await _reap(release, [task])
        assert elapsed < 4.0, f"stop took {elapsed:.1f}s — the join bound is gone"

    asyncio.run(scenario())
