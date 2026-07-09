"""The half-duplex duplex arbiter: TX-priority exclusion of RX + scan (ADR 0017).

A half-duplex radio cannot receive and transmit at once — keying the transmitter blinds the
receiver. The shared :class:`RadioArbiter` is the single seam that enforces it: TX claims the radio
on key-up, and the RX pump and scan engine consult it and stand down while it holds, resuming when
TX drops. Software-first, no hardware — the arbiter models the *logical* exclusion, never PTT-tail
timing (guardrail 1).

Three instruments, matching the rest of the suite:

- **Arbiter unit tests** drive :class:`RadioArbiter` directly — pure state, no asyncio.
- **RX-pump tests** run the real :class:`RxPump` over a self-terminating scripted radio with
  ``asyncio.run`` and ``poll=0`` (the ``test_rx_audio.py`` idiom), toggling the shared arbiter to
  prove the pump suspends while transmitting and resumes after — with the listener staying
  subscribed throughout.
- **Scan tests** drive :class:`ScanEngine.tick` against a ``FakeClock`` (the ``test_scan.py``
  idiom), proving a scan pauses in place on a TX key-up and resumes exactly where it left off.

The load-bearing proofs: TX priority over RX in the derived mode; the coherence guard refuses a
double-key; the RX pump never pulls ``receive()`` while keyed and its listener is never dropped;
and a scan neither tunes nor advances while transmitting, then continues from its held position.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from radio_server.api import create_app
from radio_server.arbiter import ArbiterStateError, RadioArbiter, RadioMode
from radio_server.audio import AudioFrame
from radio_server.backends import MockRadio
from radio_server.rx import AudioHub, RxPump
from radio_server.scan import ResumeMode, ScanEngine, ScanPlan

from .conftest import FakeClock

TOKEN = "test-lan-secret"

# Three 15 kHz-spaced 2 m channels (the `test_scan.py` fixture); all clear unless scripted busy.
FREQS = [146_500_000, 146_520_000, 146_540_000]
SETTLE = 0.05


# --- the arbiter: two latches, TX-priority derived mode, coherence guard -------------------

def test_fresh_arbiter_is_idle():
    arb = RadioArbiter()
    assert arb.mode is RadioMode.IDLE
    assert arb.transmitting is False


def test_begin_receive_reports_receiving():
    arb = RadioArbiter()
    arb.begin_receive()
    assert arb.mode is RadioMode.RECEIVING
    assert arb.transmitting is False  # receiving does not key TX


def test_acquire_tx_reports_transmitting():
    arb = RadioArbiter()
    arb.acquire_tx()
    assert arb.mode is RadioMode.TRANSMITTING
    assert arb.transmitting is True


def test_tx_takes_priority_over_rx_and_rx_latch_survives():
    # RX wanting the radio and TX holding it are independent latches; the derived mode is TX-first.
    arb = RadioArbiter()
    arb.begin_receive()
    arb.acquire_tx()
    assert arb.mode is RadioMode.TRANSMITTING  # TX wins while both latches are set
    arb.release_tx()
    assert arb.mode is RadioMode.RECEIVING  # the RX latch survived — no restore bookkeeping
    arb.end_receive()
    assert arb.mode is RadioMode.IDLE


def test_double_key_is_rejected():
    # The coherence guard: one transmitter, one talker — you cannot key it twice.
    arb = RadioArbiter()
    arb.acquire_tx()
    with pytest.raises(ArbiterStateError):
        arb.acquire_tx()


def test_release_tx_is_idempotent():
    # Mirrors `TxSession.close()`, which may run on a stream that never keyed: releasing when not
    # transmitting is a no-op, never a raise.
    arb = RadioArbiter()
    arb.release_tx()  # no raise
    assert arb.mode is RadioMode.IDLE
    arb.acquire_tx()
    arb.release_tx()
    arb.release_tx()  # still no raise
    assert arb.mode is RadioMode.IDLE


# --- on_change fires the ledger a mode string on every real transition (ADR 0019) ----------

def test_on_change_reports_each_real_mode_transition():
    seen: list[RadioMode] = []
    arb = RadioArbiter(on_change=seen.append)
    arb.begin_receive()   # idle -> receiving
    arb.acquire_tx()      # receiving -> transmitting (TX priority)
    arb.release_tx()      # transmitting -> receiving (RX latch survives)
    arb.end_receive()     # receiving -> idle
    assert seen == [
        RadioMode.RECEIVING,
        RadioMode.TRANSMITTING,
        RadioMode.RECEIVING,
        RadioMode.IDLE,
    ]


def test_on_change_dedupes_latch_flips_that_do_not_change_derived_mode():
    seen: list[RadioMode] = []
    arb = RadioArbiter(on_change=seen.append)
    arb.acquire_tx()      # idle -> transmitting  (fires)
    arb.begin_receive()   # RX latch set, but TX priority keeps mode transmitting (no fire)
    arb.end_receive()     # RX latch cleared, still transmitting (no fire)
    assert seen == [RadioMode.TRANSMITTING]
    # Dropping TX now surfaces idle exactly once (the RX latch is already clear).
    arb.release_tx()
    assert seen == [RadioMode.TRANSMITTING, RadioMode.IDLE]


def test_on_change_mode_serializes_to_its_string_value():
    # The app wires `on_change` to publish `{"mode": str(mode)}`; a StrEnum stringifies to its value.
    seen: list[str] = []
    arb = RadioArbiter(on_change=lambda m: seen.append(str(m)))
    arb.acquire_tx()
    assert seen == ["transmitting"]


# --- the RX pump respects the arbiter (asyncio.run, self-terminating scripted radio) --------

class _CountingRadio(MockRadio):
    """A MockRadio that counts ``receive()`` calls and signals when its scripted RX drains, so a
    pump loop over it both terminates deterministically and reveals whether it was polled at all
    (the ``_ScriptedRadio`` idiom, plus a receive counter)."""

    def __init__(self, frames: list[AudioFrame]) -> None:
        super().__init__(rx_frames=frames)
        self._remaining = len(frames)
        self.receive_calls = 0
        self.drained = asyncio.Event()

    def receive(self) -> AudioFrame:
        self.receive_calls += 1
        frame = super().receive()
        if self._remaining > 0:
            self._remaining -= 1
            if self._remaining == 0:
                self.drained.set()
        return frame


def test_pump_mode_tracks_receiving_transmitting_idle():
    async def scenario() -> dict[str, RadioMode]:
        arb = RadioArbiter()
        pump = RxPump(MockRadio(), AudioHub(), poll=0, arbiter=arb)  # empty radio: no traffic
        seen: dict[str, RadioMode] = {"before": arb.mode}
        pump.start()
        await asyncio.sleep(0)  # let run() assert begin_receive
        seen["running"] = arb.mode
        arb.acquire_tx()
        seen["tx"] = arb.mode
        arb.release_tx()
        seen["after_tx"] = arb.mode  # RX latch survived the transmission
        await pump.stop()
        seen["stopped"] = arb.mode
        return seen

    seen = asyncio.run(scenario())
    assert seen == {
        "before": RadioMode.IDLE,
        "running": RadioMode.RECEIVING,
        "tx": RadioMode.TRANSMITTING,
        "after_tx": RadioMode.RECEIVING,
        "stopped": RadioMode.IDLE,
    }


def test_pump_suspends_while_transmitting_then_resumes():
    frames = [AudioFrame(b"\x01\x02"), AudioFrame(b"\x03\x04")]

    async def scenario():
        radio = _CountingRadio(frames)
        hub = AudioHub()
        queue = hub.subscribe()
        arb = RadioArbiter()
        pump = RxPump(radio, hub, poll=0, arbiter=arb)

        arb.acquire_tx()  # TX owns the radio before the pump even starts
        pump.start()
        for _ in range(5):
            await asyncio.sleep(0)  # give the loop several turns; it must not pull while keyed
        suspended = (radio.receive_calls, queue.empty(), hub.subscriber_count)

        arb.release_tx()  # TX ends -> RX resumes
        await radio.drained.wait()
        await asyncio.sleep(0)  # let the pump publish the final frame before we stop it
        await pump.stop()

        out: list[bytes] = []
        while not queue.empty():
            out.append(queue.get_nowait())
        return suspended, out

    (receive_calls, was_empty, subs), out = asyncio.run(scenario())
    # While transmitting: the receiver is blinded — never polled, nothing delivered...
    assert receive_calls == 0
    assert was_empty is True
    # ...and the listener stayed subscribed across the whole suspend (socket never dropped).
    assert subs == 1
    # After TX drops: frames flow again to the *same* queue, in order (resume, not reconnect).
    assert out == [f.samples for f in frames]


# --- a scan pauses on TX key-up and resumes after (FakeClock, no real sleeps) --------------

class _TuneSpyRadio(MockRadio):
    """A CAT MockRadio that logs every ``set_frequency`` so a test can prove a paused scan does
    not tune while transmitting (the ``_PttSpyRadio`` spy idiom, for the CAT tune path)."""

    def __init__(self, **kwargs) -> None:
        super().__init__(supports_cat=True, **kwargs)
        self.tune_log: list[int] = []

    def set_frequency(self, hz: int) -> None:
        self.tune_log.append(hz)
        super().set_frequency(hz)


def test_scan_pauses_on_tx_and_resumes_in_place():
    clock = FakeClock()
    radio = _TuneSpyRadio()  # no busy_frequencies -> every channel reads clear
    arb = RadioArbiter()
    engine = ScanEngine(
        radio,
        ScanPlan.from_frequencies(FREQS),
        mode=ResumeMode.CARRIER,
        settle=SETTLE,
        clock=clock,
        arbiter=arb,
    )

    engine.tick()  # IDLE -> LISTENING on FREQS[0] (tune #1)
    clock.advance(SETTLE)
    engine.tick()  # FREQS[0] clear -> advance -> LISTENING on FREQS[1] (tune #2)
    assert engine.current_frequency == FREQS[1]

    tunes_before = list(radio.tune_log)
    state_before = engine.state

    arb.acquire_tx()  # TX takes the radio -> scan must pause in place
    clock.advance(SETTLE)
    engine.tick()
    clock.advance(SETTLE)
    engine.tick()
    assert radio.tune_log == tunes_before  # no tuning while transmitting
    assert engine.current_frequency == FREQS[1]  # frozen on the held channel
    assert engine.state == state_before

    arb.release_tx()  # TX ends -> scan resumes from exactly where it paused
    clock.advance(SETTLE)
    engine.tick()  # FREQS[1] clear -> advance -> LISTENING on FREQS[2] (tune #3)
    assert len(radio.tune_log) == len(tunes_before) + 1
    assert engine.current_frequency == FREQS[2]


# --- end-to-end: the shared arbiter is wired through create_app, RX unregressed -------------

def test_create_app_exposes_shared_arbiter_and_rx_still_streams():
    frames = [AudioFrame(b"\x01\x02"), AudioFrame(b"\x03\x04"), AudioFrame(b"\x05\x06")]
    radio = MockRadio(rx_frames=frames)
    app = create_app(radio, api_token=TOKEN)
    assert isinstance(app.state.arbiter, RadioArbiter)  # one shared arbiter on app.state
    with TestClient(app) as client:
        with client.websocket_connect(f"/audio/rx?token={TOKEN}") as ws:
            got = [ws.receive_bytes() for _ in range(len(frames))]
    # Injecting the arbiter did not regress the cycle-13 RX path (idle arbiter -> relay everything).
    assert got == [f.samples for f in frames]
    assert app.state.arbiter.mode is RadioMode.IDLE  # last listener gone -> back to idle
