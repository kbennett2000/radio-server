"""The scan runner: drive :meth:`ScanEngine.tick` in a background async task (ADR 0028).

The mirror of :class:`radio_server.rx.pump.RxPump` for scan. ``POST /scan`` used to run one
synchronous :meth:`ScanEngine.sweep`, which blocked and could not be stopped; this runner instead
steps the clock-driven :meth:`ScanEngine.tick` on the scan-poll cadence in an owned asyncio task,
so a scan runs in the background and can be stopped at a tick boundary.

Layering: like the engine, the runner imports nothing above ``scan`` and emits progress through an
injected ``on_event`` callback (the API adapts :class:`ScanEvent` to an ``Event(type="scan", ...)``).
The engine itself is built by an injected ``engine_factory`` so the runner never needs the plan, the
radio, or the API's publish seam at construction — only when a scan is started.

Two load-bearing properties, both inherited from the engine being what it is:

- **Clean stop at the tick boundary.** :meth:`ScanEngine.tick` is fully synchronous (no ``await``),
  so a :meth:`asyncio.Task.cancel` can only deliver ``CancelledError`` at this loop's
  ``await asyncio.sleep`` — never mid-``tick``. The in-progress tick always completes; a stop never
  interrupts a tune.
- **No wedge while TX-suspended.** While ``arbiter.transmitting`` the engine's ``tick`` early-returns
  (ADR 0017), so this loop keeps *polling* (spinning cheaply), it does not block waiting for TX to
  drop. A stop therefore cancels cleanly even while a scan is paused for TX.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

from .engine import DEFAULT_SCAN_POLL, ScanEvent

if TYPE_CHECKING:
    from .engine import ScanEngine, ScanPlan

#: Builds the :class:`ScanEngine` for one scan from its plan and the progress callback. Injected so
#: the runner stays below the API: the API's factory closes over the radio, settings, and arbiter.
EngineFactory = Callable[["ScanPlan", "Callable[[ScanEvent], None] | None"], "ScanEngine"]


class ScanRunner:
    """Step a :class:`ScanEngine` in a background task; start/stop one scan at a time.

    Owns a single asyncio task, mirroring :class:`~radio_server.rx.pump.RxPump`. :meth:`start` is a
    single-scan guard (a start while already running is refused, not stacked); :meth:`stop` clears
    its task reference **before** awaiting the cancel and is idempotent (a stop when idle is a clean
    no-op). A fresh engine is built per scan via ``engine_factory``.
    """

    def __init__(
        self,
        engine_factory: EngineFactory,
        *,
        on_event: Callable[[ScanEvent], None] | None = None,
        poll: float = DEFAULT_SCAN_POLL,
    ) -> None:
        self._engine_factory = engine_factory
        # The runner also emits the `stopped` lifecycle event through this same callback, so a
        # client watching `/events` sees the scan drop back to idle. The engine uses it for the
        # scanning/active/dwelling/resumed phases.
        self._on_event = on_event
        self._poll = poll
        self._engine: ScanEngine | None = None
        self._running = False
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._running

    @property
    def current_frequency(self) -> int | None:
        """The channel the scan is currently on (Hz), or ``None`` when not scanning."""
        return self._engine.current_frequency if self._engine is not None else None

    def start(self, plan: ScanPlan) -> bool:
        """Start a background scan of ``plan``; return ``False`` if one is already running.

        Sets ``running`` synchronously (before the task first executes) so a caller — e.g. the
        ``POST /scan`` route deciding whether to report started or 409 — sees the state immediately.
        """
        if self._task is not None:
            return False
        self._engine = self._engine_factory(plan, self._on_event)
        self._running = True
        self._task = asyncio.create_task(self.run())
        return True

    async def run(self) -> None:
        """Step the engine on the poll cadence until :meth:`stop` cancels the task.

        The engine owns its own clock, so ``tick()`` needs no argument here; every timing decision
        (settle, dwell) is made against that clock. This loop only paces how often a tick happens.
        """
        assert self._engine is not None  # start() sets it before creating this task
        self._running = True
        try:
            while self._running:
                self._engine.tick()
                await asyncio.sleep(self._poll)
        finally:
            self._running = False

    async def stop(self) -> bool:
        """Stop a running scan and join its task; return whether a scan was actually stopped.

        Idempotent: a stop when nothing is scanning is a clean no-op that returns ``False`` and emits
        nothing. When a scan was running, the task is cancelled (cleanly, at a tick boundary — see the
        module docstring), then a ``stopped`` event is emitted so the scan visibly drops to idle.
        """
        task = self._task
        if task is None:
            return False
        # Clear state before awaiting the cancel so a concurrent start observes an idle runner.
        self._task = None
        self._running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._engine = None
        if self._on_event is not None:
            self._on_event(ScanEvent(phase="stopped"))
        return True
