"""ADR 0183: teardown must not free the capture stream under a live reader, and the line must fall.

Two defects from one incident, kept apart.

**The crash.** Six `status=139` exits on the deployed station, and the kernel names the same thread
every time — `rx-read_0`, the pump's dedicated capture reader (ADR 0130) — faulting in
`libasound`, `libportaudio` *and* `libc` at varying addresses. Three libraries, one thread: a
use-after-free, not one bad pointer.

The mechanism is that `RxPump.stop()` never waits for that thread. Cancelling a task parked in
`run_in_executor` cancels only the asyncio wrapper — the underlying `concurrent.futures.Future` is
already RUNNING, so its `cancel()` fails and the worker runs on. Measured on master, `stop()`
returned in **0.06 ms** with the reader still inside `receive()`. So abandoning a live reader was
not the exceptional path ADR 0104 contemplated; it was the *only* path. `holder.stop()` then reached
`radio.close()`, which calls `Pa_CloseStream` → `snd_pcm_close`, freeing the stream the reader was
still inside.

The asymmetry is the finding: `SoundCardTxPacer.stop()` already joins its writer thread before the
stream is closed, and its docstring names this exact hazard. TX understood it; RX never did.

**The consequence.** A process that dies abruptly never runs its unkey, and `TotRadio`'s watchdog is
a daemon `threading.Timer` that dies with it. What actually lowers the line is the kernel: `HUPCL`
on the tty makes last-close drop DTR/RTS. Measured on the station — `stty -a` reports `hupcl` set,
and pyserial 3.5 contains **zero** references to it, so it is inherited rather than chosen. One
`stty -hupcl` and the carrier outlives the process with nothing to notice. These tests pin it.
"""

from __future__ import annotations

import asyncio
import threading
import time

from radio_server.backends import AudioFrame
from radio_server.backends.mock import MockRadio
from radio_server.rx.hub import AudioHub
from radio_server.rx.pump import READER_JOIN_TIMEOUT_S, RxPump


class _ParkedRadio(MockRadio):
    """A radio whose ``receive()`` blocks until released — a capture read parked in the driver.

    Stands in for `Pa_ReadStream`, which is where `rx-read_0` was in all six crashes.
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.in_read = threading.Event()
        self.release = threading.Event()
        self.closed_at: float | None = None
        self.reads_finished = 0

    def receive(self) -> AudioFrame:
        self.in_read.set()
        self.release.wait(10)
        self.reads_finished += 1
        return AudioFrame(b"")

    def close(self) -> None:
        # `Pa_CloseStream` -> `snd_pcm_close` frees the stream here. Stamped so a test can prove it
        # never lands while a read is in flight.
        self.closed_at = time.monotonic()


async def _started(radio: _ParkedRadio) -> RxPump:
    pump = RxPump(radio, AudioHub(), poll=0.001)
    pump.start()
    for _ in range(500):
        if radio.in_read.is_set():
            return pump
        await asyncio.sleep(0.01)
    raise AssertionError("the reader never entered receive()")


def _live_readers() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name.startswith("rx-read")]


# --- the crash ------------------------------------------------------------------------------------


def test_stop_does_not_return_while_a_capture_read_is_in_flight() -> None:
    """FAIL-FIRST (ADR 0183). Red on master: ``stop()`` returns in ~0.06 ms with the reader parked.

    Whatever runs next — `radio.close()`, three lines later in `holder.stop()` — is then tearing the
    PortAudio stream down underneath a live thread.
    """

    async def scenario() -> tuple[float, bool]:
        radio = _ParkedRadio()
        pump = await _started(radio)
        # The read returns just after stop() is entered: the realistic case, where a bounded wait
        # succeeds. A wedged read is the separate test below.
        threading.Timer(0.10, radio.release.set).start()
        t0 = time.monotonic()
        await pump.stop()
        elapsed = time.monotonic() - t0
        parked = not radio.release.is_set()
        radio.release.set()
        return elapsed, parked

    elapsed, still_parked = asyncio.run(scenario())
    assert not still_parked, "the reader was still inside receive() when stop() returned"
    assert elapsed >= 0.05, (
        f"stop() returned after {elapsed * 1000:.2f} ms without waiting for a read that took "
        f"100 ms — the reader is abandoned, and close() will free the stream underneath it"
    )


def test_the_reader_is_finished_before_the_radio_is_closed() -> None:
    """The invariant that actually prevents the SIGSEGV, asserted on ordering rather than timing."""

    async def scenario() -> _ParkedRadio:
        radio = _ParkedRadio()
        pump = await _started(radio)
        threading.Timer(0.10, radio.release.set).start()
        await pump.stop()
        radio.close()  # what holder.stop() does next
        return radio

    radio = asyncio.run(scenario())
    assert radio.closed_at is not None, "close() never ran"
    assert radio.reads_finished >= 1, (
        "close() freed the capture stream while a read was still in flight — this is the "
        "use-after-free the kernel logged as rx-read_0 faulting in libasound/libportaudio/libc"
    )


def test_a_wedged_read_is_still_abandoned_so_the_stop_budget_holds() -> None:
    """ADR 0104's escape is kept: the wait is BOUNDED, not a join.

    A capture read that never returns (dying hardware, the ADR 0029 known limitation) must not hold
    shutdown open. The point of this cycle is to stop abandoning a reader that was about to finish,
    not to start waiting forever for one that never will.
    """

    async def scenario() -> float:
        radio = _ParkedRadio()
        pump = await _started(radio)
        t0 = time.monotonic()
        await pump.stop()  # never released
        elapsed = time.monotonic() - t0
        radio.release.set()
        return elapsed

    elapsed = asyncio.run(scenario())
    assert elapsed < READER_JOIN_TIMEOUT_S + 1.5, (
        f"stop() took {elapsed:.2f}s against a {READER_JOIN_TIMEOUT_S}s reader bound — a wedged "
        f"read is holding the stop budget open, which ADR 0104 refused"
    )


def test_the_reader_bound_is_many_capture_block_periods() -> None:
    """DERIVED, not chosen: the bound must comfortably exceed one capture block.

    `DEFAULT_BLOCKSIZE = 960` frames at 48 kHz is a 20 ms block, so a healthy read always returns
    well inside this. Small enough to be invisible against uvicorn's 5 s graceful window and the
    unit's `TimeoutStopSec=20`; large enough that the normal case never abandons.
    """
    from radio_server.backends.soundcard import DEFAULT_BLOCKSIZE
    from radio_server.audio import CANONICAL_RATE

    block_s = DEFAULT_BLOCKSIZE / CANONICAL_RATE
    assert READER_JOIN_TIMEOUT_S >= block_s * 5, (
        f"the reader bound is {READER_JOIN_TIMEOUT_S}s against a {block_s * 1000:.0f} ms capture "
        f"block — too tight, so healthy reads would still be abandoned"
    )


def test_stop_is_still_idempotent_and_safe_with_no_reader() -> None:
    """A pump that never started, and a second stop, must both be no-ops."""

    async def scenario() -> None:
        pump = RxPump(MockRadio(), AudioHub(), poll=0.001)
        await pump.stop()  # never started
        pump.start()
        await pump.stop()
        await pump.stop()  # twice

    asyncio.run(scenario())
    assert not _live_readers(), f"reader threads leaked: {_live_readers()}"


# --- the consequence ------------------------------------------------------------------------------


def test_hangup_on_close_is_asserted_not_inherited() -> None:
    """The line must fall when the process dies, and that must be a decision rather than a default.

    `HUPCL` is what makes the kernel lower DTR/RTS at last-close, which is the only thing that
    unkeys after a SIGSEGV or a SIGKILL — `TotRadio`'s timer is a daemon thread and dies with the
    process. It was measured set on the station, but pyserial never touches it, so it is inherited:
    one `stty -hupcl` away from a carrier that outlives the process, with nothing to notice.
    """
    import termios

    from radio_server.backends.uvk5.transport import ensure_hangup_on_close

    class _FakeTty:
        def __init__(self, cflag: int) -> None:
            self.attrs = [0, 0, cflag, 0, 0, 0, []]
            self.written: list[list] = []

        def fileno(self) -> int:
            return 4242

    for name, start, expect_already in (
        ("inherited set", termios.HUPCL, True),
        ("cleared by someone", 0, False),
    ):
        tty = _FakeTty(start)
        seen = {}

        def _get(_fd, _tty=tty):
            return list(_tty.attrs)

        def _set(_fd, _when, attrs, _tty=tty):
            _tty.attrs = attrs
            seen["set"] = attrs

        already = ensure_hangup_on_close(tty, _tcgetattr=_get, _tcsetattr=_set)
        assert already is expect_already, f"{name}: reported already={already!r}"
        assert tty.attrs[2] & termios.HUPCL, (
            f"{name}: HUPCL is not set after ensure_hangup_on_close — the kernel will not drop "
            f"DTR when the process dies, and nothing else will either"
        )


def test_ensure_hangup_on_close_never_raises_on_a_handle_without_a_real_fd() -> None:
    """Every fake in the suite, and any non-tty handle. A port that cannot be hardened is still usable."""
    from radio_server.backends.uvk5.transport import ensure_hangup_on_close

    class _NoFd:
        pass

    assert ensure_hangup_on_close(_NoFd()) is None
