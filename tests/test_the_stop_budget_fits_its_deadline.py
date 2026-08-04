"""ADR 0185: the stop budget is a sum, so something has to add it up.

`TimeoutStopSec` was prose in six modules and a fenced block in `docs/deployment.md`, and the parts
it is supposed to bound were seven bare literals typed into call sites. Nothing summed them, so ADR
0184's table could be — and was — an undercount: it missed the D-STAR gateway join, the pymumble
library join, the uvk5 transport reader join, the TX pacer join (paid inside *both* `ptt(False)` and
`close()`), and `PolledGate.stop()`'s join inside the pump's cancellation path.

Two rules this file encodes, both of which the code got wrong before it existed:

1. **A bound inside another bounded call is absorbed; a synchronous thread join is not.** The pacer
   and transport joins sit inside `radio.close()`, which now has a deadline, so they are not additive
   — that is what bounding the outer call buys. But `PolledGate.stop()` runs in `RxPump.run()`'s
   ``finally``, during cancellation delivery, and `asyncio.wait`'s timer cannot fire while the loop
   thread sits in `thread.join()`. A bound expressed in async time does not cover synchronous work
   done inside it.
2. **A bound that expires has to release the process.** Measured (see `radio_server/shutdown.py`): an
   abandoned executor worker is joined at interpreter exit — 300 s for the default executor, forever
   for a pool — so summing such a bound asserts something false. Only a daemon thread walks away.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from pathlib import Path

import pytest

from radio_server.__main__ import GRACEFUL_SHUTDOWN_SECONDS
from radio_server.api.events import EventHub
from radio_server.api.holder import (
    PTT_OFF_TIMEOUT_S,
    RADIO_CLOSE_TIMEOUT_S,
    REBUILD_CLOSE_TIMEOUT_S,
    RadioHolder,
)
from radio_server.arbiter import RadioArbiter
from radio_server.backends import MockRadio, RadioUnavailable
from radio_server.backends.soundcard import DEFAULT_TX_LEAD_SECONDS, TX_PACER_JOIN_TIMEOUT_S
from radio_server.config import resolve_settings
from radio_server.rx import AudioHub
from radio_server.shutdown import (
    STOP_BUDGET_MARGIN_S,
    TIMEOUT_STOP_SEC,
    call_bounded,
    stop_budget_seconds,
    teardown_budget_seconds,
)

UNIT_FILE = Path(__file__).resolve().parent.parent / "scripts" / "radio-server.service"

#: Long enough that a lost bound is unmistakable, short enough that the suite stays quick. Every
#: wedge SELF-RELEASES after this: on master `ptt`/`close` are synchronous ON the event loop, so an
#: infinite wedge would hang the suite rather than fail it — the trap ADR 0184 hit ("killed at
#: 100 s"). A self-releasing wedge fails red instead.
WEDGE_S = 3.0


# --- the budget adds up ----------------------------------------------------------------------------


def test_the_stop_budget_fits_the_shipped_deadline() -> None:
    """The identity test. If a bound grows, this names it rather than waiting for a SIGKILL."""
    required = stop_budget_seconds() + STOP_BUDGET_MARGIN_S
    assert required <= TIMEOUT_STOP_SEC, (
        f"the stop budget needs {required:.2f}s but the shipped unit allows "
        f"{TIMEOUT_STOP_SEC:.0f}s — teardown {teardown_budget_seconds():.2f}s + graceful "
        f"{GRACEFUL_SHUTDOWN_SECONDS:.2f}s + margin {STOP_BUDGET_MARGIN_S:.2f}s. Either derive the "
        f"bound that grew back down, or raise TimeoutStopSec in scripts/radio-server.service AND "
        f"TIMEOUT_STOP_SEC together — never one of the two."
    )


def test_the_shipped_unit_says_what_the_code_says() -> None:
    """The unit is the only copy the repo can check. `docs/` cannot: the contract test blanks fences."""
    text = UNIT_FILE.read_text()
    found = re.search(r"^TimeoutStopSec=(\d+)$", text, re.MULTILINE)
    assert found, "scripts/radio-server.service has no TimeoutStopSec line"
    assert float(found.group(1)) == TIMEOUT_STOP_SEC, (
        f"the shipped unit says TimeoutStopSec={found.group(1)} but the code says "
        f"{TIMEOUT_STOP_SEC:.0f} — the number the budget is sized against has drifted from the "
        f"number the box would install"
    )


def test_the_teardown_budget_is_a_sum_of_named_constants_not_literals() -> None:
    """Every term must be importable, or the sum silently stops tracking the call sites.

    This is the property that makes the identity test load-bearing rather than decorative: before
    ADR 0185 seven of the terms were bare floats typed into call sites, so no test could have added
    them up even in principle.
    """
    from radio_server.activity.broadcast_fm_poll import CADENCE_JOIN_TIMEOUT_S
    from radio_server.activity.gate import GATE_JOIN_TIMEOUT_S
    from radio_server.audio.dtmf import CONTROLLER_CLOSE_BUDGET_S
    from radio_server.dstar.bridge import DSTAR_JOIN_MARGIN_S
    from radio_server.dstar.client import GATEWAY_JOIN_TIMEOUT_S
    from radio_server.link.bridge import MUMBLE_TASK_JOIN_TIMEOUT_S
    from radio_server.rx.pump import PUMP_JOIN_TIMEOUT_S

    named = (
        CADENCE_JOIN_TIMEOUT_S, GATE_JOIN_TIMEOUT_S, CONTROLLER_CLOSE_BUDGET_S,
        DSTAR_JOIN_MARGIN_S, GATEWAY_JOIN_TIMEOUT_S, MUMBLE_TASK_JOIN_TIMEOUT_S,
        PUMP_JOIN_TIMEOUT_S, PTT_OFF_TIMEOUT_S, RADIO_CLOSE_TIMEOUT_S, TX_PACER_JOIN_TIMEOUT_S,
    )
    assert all(isinstance(v, float) and v > 0 for v in named)
    assert teardown_budget_seconds() > 0


def test_the_pacer_join_is_derived_from_the_lead_in_slug() -> None:
    """DERIVED, not chosen. `stop()` clears the deque, so at most one chunk can be in flight.

    The largest chunk any producer enqueues is the lead-in slug — one `DEFAULT_TX_LEAD_SECONDS` of
    audio written as a single call — and a blocking write returns as the device clocks it out. Two of
    them, the same 2x margin `SCAN_JOIN_TIMEOUT_S` takes over `DEFAULT_SCAN_POLL`. It replaced a bare
    5.0 that was ~10x the need, and that mattered because this join sits inside BOTH `ptt(False)` and
    `close()`.
    """
    assert TX_PACER_JOIN_TIMEOUT_S == DEFAULT_TX_LEAD_SECONDS * 2
    assert TX_PACER_JOIN_TIMEOUT_S < RADIO_CLOSE_TIMEOUT_S, (
        "the pacer join must fit inside the close that contains it, or the close bound is the only "
        "one that can ever fire"
    )


def test_the_rebuild_close_bound_is_larger_than_the_shutdown_one() -> None:
    """Two paths, two derivations — and this is not a preference.

    At shutdown the process is about to exit, so the kernel reclaims the fd and `HUPCL` still drops
    the carrier (ADR 0183): waiting out every internal bound buys nothing. On a swap nothing is
    exiting, no cleanup is coming, and the device genuinely has to let go before the target can open
    it — so that bound is sized to `close()`'s own declared internals instead.
    """
    assert REBUILD_CLOSE_TIMEOUT_S > RADIO_CLOSE_TIMEOUT_S


# --- the bounds hold when everything wedges at once -------------------------------------------------


class _WedgedRadio(MockRadio):
    """A radio whose unkey and close both block — the case the two new bounds exist for."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.gate = threading.Event()
        self.closed = 0

    def ptt(self, on: bool) -> None:
        if on:
            super().ptt(on)
            return
        self.gate.wait(WEDGE_S)

    def close(self) -> None:
        self.closed += 1
        self.gate.wait(WEDGE_S)


def _stubborn(release: asyncio.Event) -> asyncio.Task:
    """A task that survives cancellation (`test_shutdown_budget.py`'s model)."""

    async def run() -> None:
        while not release.is_set():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                continue

    return asyncio.create_task(run())


def _make_holder(radio: MockRadio, arbiter: RadioArbiter) -> RadioHolder:
    return RadioHolder(
        radio,
        hub=EventHub(),
        audio_hub=AudioHub(),
        arbiter=arbiter,
        scan_settings=resolve_settings({}),
        scan_poll=0.0,
        controller=None,
    )


def test_a_teardown_with_every_step_wedged_returns_inside_its_bounds(monkeypatch) -> None:
    """FAIL-FIRST (ADR 0185). Red on master: `ptt(False)` and `close()` have no bound at all.

    Every bounded step is driven to its worst case at once — a stubborn scan task, a stubborn pump
    task, a never-resolving in-flight capture read, and a radio whose unkey and close both block.
    The scaled bounds sum to well under one wedge, so if any single one is lost the elapsed time
    jumps to at least `WEDGE_S` and this fails.
    """
    monkeypatch.setattr("radio_server.api.holder.PTT_OFF_TIMEOUT_S", 0.05)
    monkeypatch.setattr("radio_server.api.holder.RADIO_CLOSE_TIMEOUT_S", 0.05)
    monkeypatch.setattr("radio_server.scan.runner.SCAN_JOIN_TIMEOUT_S", 0.05)
    monkeypatch.setattr("radio_server.rx.pump.PUMP_JOIN_TIMEOUT_S", 0.05)
    monkeypatch.setattr("radio_server.rx.pump.READER_JOIN_TIMEOUT_S", 0.05)

    async def scenario() -> tuple[bool, float, _WedgedRadio]:
        import concurrent.futures

        radio = _WedgedRadio()
        arbiter = RadioArbiter()
        holder = _make_holder(radio, arbiter)
        holder.start()
        assert holder.scan_runner is not None and holder.rx_pump is not None

        # The arbiter must genuinely hold TX, or the ptt step is skipped and this silently stops
        # testing it — ADR 0184's trap in a new dress.
        arbiter.acquire_tx()
        assert arbiter.transmitting

        release = asyncio.Event()
        scan_task, pump_task = _stubborn(release), _stubborn(release)
        # Park them before rigging: a task cancelled before its first step ends CANCELLED without
        # entering its own `except`, so an unbounded join would look bounded (ADR 0184).
        await asyncio.sleep(0.05)
        holder.scan_runner._task = scan_task
        holder.scan_runner._running = True
        holder.rx_pump._task = pump_task
        holder.rx_pump._running = True
        holder.rx_pump._inflight = concurrent.futures.Future()  # never resolves

        stopping = asyncio.create_task(holder.stop())
        started = time.monotonic()
        done, _ = await asyncio.wait({stopping}, timeout=WEDGE_S * 2)
        elapsed = time.monotonic() - started

        release.set()
        radio.gate.set()
        for task in (scan_task, pump_task):
            task.cancel()
        await asyncio.gather(stopping, scan_task, pump_task, return_exceptions=True)
        return bool(done), elapsed, radio

    finished, elapsed, radio = asyncio.run(scenario())
    assert finished, f"holder.stop() had not returned after {elapsed:.2f}s with every step wedged"
    assert elapsed < WEDGE_S, (
        f"the teardown took {elapsed:.2f}s against wedges of {WEDGE_S}s — at least one step is "
        f"waiting out its wedge instead of its bound"
    )
    assert radio.closed == 1, "the teardown never reached radio.close()"


def test_a_wedged_unkey_and_close_do_not_block_the_event_loop(monkeypatch) -> None:
    """They ran synchronously ON the loop until ADR 0185 — the ADR 0181/0182/0183 property, untested.

    A device that has stopped answering must stall only itself. On the loop it stalls uvicorn's own
    shutdown machinery, every other request, and the delivery of the next signal.
    """
    monkeypatch.setattr("radio_server.api.holder.PTT_OFF_TIMEOUT_S", 0.1)
    monkeypatch.setattr("radio_server.api.holder.RADIO_CLOSE_TIMEOUT_S", 0.1)

    async def scenario() -> float:
        radio = _WedgedRadio()
        arbiter = RadioArbiter()
        holder = _make_holder(radio, arbiter)
        holder.start()
        arbiter.acquire_tx()
        gaps: list[float] = []

        async def probe() -> None:
            last = time.monotonic()
            while True:
                await asyncio.sleep(0.005)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        probe_task = asyncio.create_task(probe())
        # Let the probe actually start ticking. `stop()`'s first step is the unkey, and on master it
        # is synchronous — so a probe created but never scheduled records no gap and the test passes
        # against the very thing it exists to catch. Verified: without this it is green on the
        # unbounded code. The ADR 0184 unstarted-task trap, one layer over.
        await asyncio.sleep(0.02)
        await holder.stop()
        probe_task.cancel()
        await asyncio.gather(probe_task, return_exceptions=True)
        radio.gate.set()
        return max(gaps) if gaps else 0.0

    worst = asyncio.run(scenario())
    assert worst < 0.5, (
        f"the event loop was unavailable for {worst * 1000:.0f} ms while the teardown waited on a "
        f"wedged device — ptt(False)/close() are back on the loop"
    )


def test_a_swap_whose_close_never_returns_is_refused_rather_than_half_done(monkeypatch) -> None:
    """A rebuild that proceeds against a port this process still holds cannot roll back (ADR 0185).

    The target's open would hit EBUSY/TIOCEXCL, and the rollback reopens the SAME port and fails
    identically — leaving the holder radio-less, the one outcome the rollback exists to prevent. So
    the swap is refused, which is also retryable for free: `close()` early-returns once `_closed`
    is set, so a repeat finds the close already done.
    """
    # Below the wedge, so the close genuinely does not return in time. At the shipped
    # REBUILD_CLOSE_TIMEOUT_S the wedge would self-release first and the swap would proceed —
    # correctly, which is why the bound and not the wedge is what this test moves.
    monkeypatch.setattr("radio_server.api.holder.REBUILD_CLOSE_TIMEOUT_S", 0.05)

    async def scenario() -> None:
        radio = _WedgedRadio()
        holder = _make_holder(radio, RadioArbiter())
        holder.start()
        try:
            with pytest.raises(RadioUnavailable, match="not released its device"):
                await asyncio.wait_for(
                    holder.rebuild(resolve_settings({})), timeout=WEDGE_S * 2
                )
        finally:
            radio.gate.set()

    asyncio.run(scenario())


# --- the primitive itself ---------------------------------------------------------------------------


def test_call_bounded_reports_completion_and_expiry_distinctly() -> None:
    """`None` means "we do not know whether the device was released" — `rebuild` acts on exactly that."""

    async def scenario() -> tuple[float | None, float | None]:
        gate = threading.Event()
        quick = await call_bounded(lambda: None, 1.0, label="quick")
        slow = await call_bounded(lambda: gate.wait(WEDGE_S), 0.05, label="slow")
        gate.set()
        return quick, slow

    quick, slow = asyncio.run(scenario())
    assert quick is not None and quick >= 0.0
    assert slow is None


def test_call_bounded_never_raises_what_the_call_raised() -> None:
    """A teardown is not where a device fault gets to surface; the steps behind it still have to run."""

    async def scenario() -> float | None:
        def boom() -> None:
            raise OSError("the device went away")

        return await call_bounded(boom, 1.0, label="boom")

    assert asyncio.run(scenario()) is not None  # it "returned" — the raise was logged, not propagated


def test_a_refused_swap_mutates_as_little_as_possible(monkeypatch) -> None:
    """A refusal must leave something a retry can recover, not a stripped holder.

    The refusal check sits *before* `rebuild` nulls `rx_pump`/`scan_runner`/`_controller`. Nulling
    first would leave the station with no pipeline and no radio swap either — worse than the
    half-swap the refusal exists to prevent, and invisible until someone pressed something.
    """
    monkeypatch.setattr("radio_server.api.holder.REBUILD_CLOSE_TIMEOUT_S", 0.05)

    async def scenario() -> RadioHolder:
        radio = _WedgedRadio()
        holder = _make_holder(radio, RadioArbiter())
        holder.start()
        try:
            with pytest.raises(RadioUnavailable):
                await asyncio.wait_for(holder.rebuild(resolve_settings({})), timeout=WEDGE_S * 2)
        finally:
            radio.gate.set()
        return holder

    holder = asyncio.run(scenario())
    assert holder.radio is not None, "a refused swap left the holder without a radio"
    assert holder.rx_pump is not None and holder.scan_runner is not None, (
        "a refused swap tore the pipeline down anyway — the refusal is supposed to be the "
        "conservative outcome, not a second failure mode"
    )
