"""ADR 0181: a hung disk must not stall the transmitter.

``EventLog.handle`` is called from the ``_drain_log`` task **on the event loop**, and until this
ADR it called ``JsonlSink.write`` — a synchronous ``write`` + ``flush()`` — inline. ADR 0018's
docstring promised the ledger "never breaks the flow or a transmission", which was true of a disk
that RAISES (caught, record dropped) and false of one that BLOCKS.

The keying paths that pay for it run on that same loop: ``/audio/tx`` calls ``session.feed(data)``
inline, so ``TxSession._key_up`` → ``radio.ptt(True)`` and — worse — the ``finally: session.close()``
that DROPS PTT both need the loop to be running. A blocked loop is a keyed transmitter the server
cannot unkey.

Timing style follows ``test_shutdown_budget.py``: real wall-clock bounds with generous margins,
distinguishing "absorbed" (milliseconds) from "stalled" (a multiple of the sink's stall), and every
assert reports the number it measured rather than asserting the property qualitatively.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

from fastapi.testclient import TestClient

from radio_server.__main__ import GRACEFUL_SHUTDOWN_SECONDS
from radio_server.api import create_app
from radio_server.api.events import Event
from radio_server.backends.mock import MockRadio
from radio_server.eventlog import DEFAULT_LEDGER_QUEUE_MAXSIZE, EventLog, ThreadedSink

TOKEN = "secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
#: The canonical format-declaration header, as `test_tx_audio.py` writes it.
CANONICAL_HEADER = {"rate": 48000, "width": 2, "channels": 1}

#: How long the staged sink blocks per write. Far longer than the ~2 us a real ``write`` + ``flush``
#: measured on the deployed station's storage (n=2000: median 1.9 us, p99 10.2 us, max 41.4 us), so
#: the test is measuring the SHAPE of the hazard, not a plausible disk. A hung filesystem blocks
#: without bound; 0.5 s is simply long enough to be unmistakable in a suite that must stay fast.
STALL = 0.5


class StallingSink:
    """A ``LogSink`` whose every write BLOCKS — the failure mode ``ExplodingSink`` does not cover.

    ``tests/test_event_log.py`` already proves a *raising* sink is isolated. This is the other half:
    a sink that neither raises nor returns. Records are appended after the stall so ordering can be
    asserted on the far side of it.
    """

    def __init__(self, stall: float = STALL) -> None:
        self._stall = stall
        self.records: list[dict] = []
        self.closed = False
        #: Set the first time a write begins, so a test can wait for the stall to be underway.
        self.writing = threading.Event()

    def write(self, record: dict) -> None:
        self.writing.set()
        time.sleep(self._stall)
        self.records.append(record)

    def close(self) -> None:
        self.closed = True


def _shipped(sink: object) -> EventLog:
    """The composition ``build_app`` ships — pinned by ``test_build_app_wraps_the_ledger_sink``.

    The tests below stall a sink and assert the loop kept running, so they must exercise the real
    arrangement rather than a hand-rolled one; the pin is what stops this helper drifting away from
    what the station actually runs.
    """
    return EventLog(ThreadedSink(sink))  # type: ignore[arg-type]


class _StampingRadio(MockRadio):
    """A ``MockRadio`` that timestamps every ``ptt()`` call — the ``_PttSpyRadio`` idiom, with a clock.

    Proving a key-up was not *delayed* needs when it happened, not just that it happened.
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.ptt_log: list[bool] = []
        self.ptt_at: list[tuple[bool, float]] = []

    def ptt(self, on: bool) -> None:
        super().ptt(on)
        self.ptt_log.append(on)
        self.ptt_at.append((on, time.monotonic()))


# --- the hazard, through the wired app ------------------------------------------------------------


def test_a_stalled_ledger_write_does_not_delay_a_key_up() -> None:
    """FAIL-FIRST (ADR 0181). Red on master: the key-up waits out the ledger's blocking write.

    Every ``POST /ptt`` publishes a ``ptt`` event, which the drain task turns into a ledger record
    on the loop. With a blocking sink the NEXT request cannot even be dispatched — uvicorn/starlette
    must read it on the loop the ledger is sitting on — so the key-up inherits the full stall.
    """
    sink = StallingSink()
    radio = _StampingRadio()
    app = create_app(radio, api_token=TOKEN, event_log=_shipped(sink))

    worst = 0.0
    with TestClient(app) as client:
        for _ in range(4):
            # A key-DOWN first: it generates a record, so the drain is blocking when the key-up
            # below arrives. This is what makes the test deterministic rather than a race.
            client.post("/ptt", json={"on": False}, headers=AUTH)
            started = time.monotonic()
            client.post("/ptt", json={"on": True}, headers=AUTH)  # the key-up under test
            worst = max(worst, time.monotonic() - started)
    assert radio.ptt_log, "the test never keyed at all"
    assert worst < STALL / 2, (
        f"a key-up waited {worst * 1000:.0f} ms behind a ledger write that blocks "
        f"{STALL * 1000:.0f} ms — the ledger is writing on the event loop"
    )


def test_a_stalled_ledger_write_does_not_delay_the_un_key() -> None:
    """The stuck-key shape: ``/audio/tx``'s ``finally: session.close()`` drops PTT ON THE LOOP.

    The key-up one loop-step earlier queued a ``ptt`` record. If writing it blocks the loop, the
    disconnect is not noticed and PTT stays asserted — a keyed transmitter with dead air that the
    server cannot unkey. ``TotRadio``'s independent timer thread is the only thing left, and it
    force-drops at ``tx.tot`` (180 s deployed) and latches streaming TX off.
    """
    sink = StallingSink()
    radio = _StampingRadio()
    app = create_app(radio, api_token=TOKEN, event_log=_shipped(sink))

    with TestClient(app) as client:
        with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws:
            ws.send_json(CANONICAL_HEADER)
            assert ws.receive_json()["status"] == "ready"
            ws.send_bytes(b"\x01\x02")  # keys: ptt(True) + a queued tx_key_up record
            released = time.monotonic()  # the operator lets go of Talk here
    # Asserted after the socket and its server task have fully torn down (test_tx_audio's idiom).
    assert radio.ptt_log == [True, False], f"keying sequence was {radio.ptt_log}"
    unkeyed_at = next(at for on, at in reversed(radio.ptt_at) if on is False)
    delay = unkeyed_at - released
    assert delay < STALL / 2, (
        f"PTT stayed asserted {delay * 1000:.0f} ms after the talker let go, behind a ledger "
        f"write that blocks {STALL * 1000:.0f} ms — that is a stuck key, not a slow log"
    )


def test_the_event_loop_keeps_running_while_the_ledger_writes() -> None:
    """The mechanism, measured directly: model ``_drain_log`` verbatim and watch the loop.

    ``app.py``'s drain is ``while True: log.handle(await queue.get())``. A probe coroutine ticking
    every 5 ms measures how long the loop was unavailable — which is the quantity every other
    symptom in this file is downstream of.
    """

    async def scenario() -> float:
        sink = StallingSink()
        log = _shipped(sink)
        queue: asyncio.Queue = asyncio.Queue()

        async def drain() -> None:
            while True:
                log.handle(await queue.get())

        gaps: list[float] = []

        async def probe() -> None:
            last = time.monotonic()
            while True:
                await asyncio.sleep(0.005)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        drain_task = asyncio.create_task(drain())
        probe_task = asyncio.create_task(probe())
        try:
            for _ in range(3):
                queue.put_nowait(Event(type="ptt", data={"on": True}))
                await asyncio.sleep(0.25)
        finally:
            for task in (drain_task, probe_task):
                task.cancel()
            await asyncio.gather(drain_task, probe_task, return_exceptions=True)
            log.close()
        return max(gaps) if gaps else 0.0

    worst = asyncio.run(scenario())
    assert worst < STALL / 2, (
        f"the event loop was unavailable for {worst * 1000:.0f} ms while the ledger wrote "
        f"(probe ticks every 5 ms); nothing else on the loop ran — including PTT"
    )


# --- the ordering invariant the ledger needs ------------------------------------------------------


def test_records_land_in_the_order_they_were_generated() -> None:
    """Decoupling must not reorder. A ledger whose records are out of order is not a ledger.

    This is the invariant that rules out fire-and-forget ``asyncio.to_thread``: its default
    executor has several workers, so concurrent writes interleave.
    """
    sink = StallingSink(stall=0.02)
    radio = MockRadio()
    app = create_app(radio, api_token=TOKEN, event_log=_shipped(sink))

    with TestClient(app) as client:
        for _ in range(12):
            client.post("/ptt", json={"on": True}, headers=AUTH)
            client.post("/ptt", json={"on": False}, headers=AUTH)

    kinds = [r["type"] for r in sink.records]
    assert kinds, "no records reached the sink at all"
    expected = ["tx_key_up", "tx_key_down"] * (len(kinds) // 2)
    assert kinds[: len(expected)] == expected, f"records arrived out of order: {kinds[:8]}"
    stamps = [r["ts"] for r in sink.records]
    assert stamps == sorted(stamps), "record timestamps are not monotonic — the writer reordered"


def test_a_graceful_shutdown_loses_no_records(tmp_path) -> None:
    """SIGTERM path: the lifespan teardown must flush everything still in flight.

    ``TestClient``'s context exit runs the same lifespan shutdown ``systemctl stop`` does. Records
    published right before it must still reach the file — decoupling may not turn a graceful stop
    into a lossy one.
    """
    from radio_server.eventlog import JsonlSink

    path = tmp_path / "ledger.jsonl"
    app = create_app(MockRadio(), api_token=TOKEN, event_log=EventLog(JsonlSink(path)))
    with TestClient(app) as client:
        for _ in range(25):
            client.post("/ptt", json={"on": True}, headers=AUTH)
            client.post("/ptt", json={"on": False}, headers=AUTH)

    kinds = [json.loads(line)["type"] for line in path.read_text().splitlines()]
    assert kinds.count("tx_key_up") == 25, f"lost key-ups: {kinds.count('tx_key_up')} of 25"
    assert kinds.count("tx_key_down") == 25, f"lost key-downs: {kinds.count('tx_key_down')} of 25"


# --- ThreadedSink itself --------------------------------------------------------------------------


def _drain_until(sink: ThreadedSink, done, timeout: float = 5.0) -> None:
    """Bounded wait for the writer thread — the house 'poll to a deadline' idiom."""
    deadline = time.monotonic() + timeout
    while not done() and time.monotonic() < deadline:
        time.sleep(0.005)


def test_write_returns_immediately_even_though_the_inner_sink_blocks() -> None:
    """The core claim, at the unit level: enqueue is O(1), the stall lives on the writer thread."""
    inner = StallingSink()
    sink = ThreadedSink(inner, maxsize=64)
    try:
        started = time.monotonic()
        for i in range(50):
            sink.write({"ts": float(i), "type": "tx_key_up"})
        elapsed = time.monotonic() - started
        # 50 enqueues, not 50 blocked flushes (which would be 25 s).
        assert elapsed < 1.0, f"50 writes took {elapsed * 1000:.0f} ms — write() is still blocking"
        assert sink.stats()["queue_maxsize"] == 64
    finally:
        sink.close()


def test_a_full_queue_drops_the_newest_and_counts_it() -> None:
    """Overflow inverts the tree's usual drop-oldest: the prefix that survives must be contiguous."""
    inner = StallingSink(stall=0.05)
    sink = ThreadedSink(inner, maxsize=4)
    try:
        for i in range(40):
            sink.write({"ts": float(i), "type": "tx_key_up", "seq": i})
        stats = sink.stats()
        assert stats["dropped_records"] > 0, "a 4-deep queue swallowed 40 records without dropping"
        assert stats["deepest_queue"] <= 4, f"queue grew to {stats['deepest_queue']}, past its bound"
    finally:
        sink.close()
    seqs = [r["seq"] for r in inner.records]
    assert seqs == sorted(seqs), f"records arrived out of order: {seqs}"
    # Drop-NEWEST: what landed is a prefix of what was generated, with no hole punched in it.
    assert seqs == list(range(len(seqs))), f"the surviving records are not contiguous: {seqs}"


def test_a_raising_inner_sink_is_counted_and_does_not_kill_the_writer() -> None:
    """ADR 0018 isolated these and counted none of them; a full disk was silent forever."""

    class _FlakySink:
        def __init__(self) -> None:
            self.records: list[dict] = []
            self.fail = True

        def write(self, record: dict) -> None:
            if self.fail:
                raise OSError("disk full")
            self.records.append(record)

        def close(self) -> None:
            pass

    inner = _FlakySink()
    sink = ThreadedSink(inner, maxsize=64)
    try:
        for i in range(5):
            sink.write({"ts": float(i), "type": "tx_key_up"})
        _drain_until(sink, lambda: sink.stats()["write_errors"] >= 5)
        assert sink.stats()["write_errors"] == 5, sink.stats()
        assert sink.stats()["written"] == 0
        # The writer survived every one of them: later records still land.
        inner.fail = False
        sink.write({"ts": 99.0, "type": "tx_key_down"})
        _drain_until(sink, lambda: sink.stats()["written"] >= 1)
        assert sink.stats()["written"] == 1
        assert [r["ts"] for r in inner.records] == [99.0]
    finally:
        sink.close()


def test_close_is_bounded_when_the_sink_is_hung() -> None:
    """A disk that never returns must not spend the unit's TimeoutStopSec and earn a SIGKILL."""
    sink = ThreadedSink(StallingSink(stall=30.0), maxsize=64, close_timeout=0.5)
    sink.write({"ts": 1.0, "type": "tx_key_up"})
    _drain_until(sink, lambda: sink.stats()["queued"] == 0)
    started = time.monotonic()
    sink.close()
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, f"close() waited {elapsed:.1f}s on a hung sink — the join is not bounded"


def test_close_is_idempotent_and_flushes_what_is_queued() -> None:
    inner = StallingSink(stall=0.0)
    sink = ThreadedSink(inner, maxsize=64)
    for i in range(20):
        sink.write({"ts": float(i), "type": "tx_key_up"})
    sink.close()
    sink.close()  # must not raise, must not re-close the inner sink
    assert len(inner.records) == 20, f"flushed {len(inner.records)} of 20 on close"
    assert inner.closed


def test_stats_reports_the_documented_shape() -> None:
    sink = ThreadedSink(StallingSink(stall=0.0), maxsize=8)
    try:
        assert set(sink.stats()) == {
            "written",
            "queued",
            "deepest_queue",
            "queue_maxsize",
            "dropped_records",
            "write_errors",
        }
    finally:
        sink.close()


def test_the_derived_bound_is_the_measured_peak_times_the_graceful_window() -> None:
    """The constant is a product of two measured numbers, not a round one (ADR 0181)."""
    assert DEFAULT_LEDGER_QUEUE_MAXSIZE == 241 * int(GRACEFUL_SHUTDOWN_SECONDS)


# --- the pin: the station must actually run the composition the tests above exercise ---------------


def test_build_app_wraps_the_ledger_sink_so_the_station_never_writes_on_the_loop(tmp_path) -> None:
    """Everything above proves ``ThreadedSink`` works. This proves the deployed station uses it.

    Without this pin, ``build_app`` could go back to ``EventLog(JsonlSink(...))`` and every stall
    test here would still pass, because they compose the sink themselves.
    """
    from radio_server.api.app import build_app

    from .conftest import make_secrets, make_settings

    app = build_app(
        make_settings({"logging.path": str(tmp_path / "ledger.jsonl")}),
        make_secrets(api_token="ledger-pin-token"),
        config_path=str(tmp_path / "absent.toml"),
    )
    stats = app.state.event_log.sink_stats()
    assert stats is not None, "build_app's ledger sink reports nothing — it is not a ThreadedSink"
    assert stats["queue_maxsize"] == DEFAULT_LEDGER_QUEUE_MAXSIZE
    # And the wrap is real: a write returns without touching the file on this thread.
    app.state.event_log.handle(Event(type="ptt", data={"on": True}))
    assert app.state.event_log.sink_stats()["written"] + app.state.event_log.sink_stats()[
        "queued"
    ] == 1
    app.state.event_log.close()


def test_status_carries_the_ledgers_own_counters(tmp_path) -> None:
    """`GET /status` surfaces the block, and reports `null` rather than a confident zero."""
    app = create_app(MockRadio(), api_token=TOKEN, event_log=_shipped(StallingSink(stall=0.0)))
    with TestClient(app) as client:
        block = client.get("/status", headers=AUTH).json()["ledger"]
    assert set(block) == {
        "written",
        "queued",
        "deepest_queue",
        "queue_maxsize",
        "dropped_records",
        "write_errors",
    }

    # No ledger wired at all → `null`, the `wire: null` posture, never a zero that looks measured.
    bare = create_app(MockRadio(), api_token=TOKEN)
    with TestClient(bare) as client:
        assert client.get("/status", headers=AUTH).json()["ledger"] is None
