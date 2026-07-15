"""The inbound-link transmit bridge: a network peer keys the local transmitter (ADR 0048).

Direction three — ``link.receive()`` → ``radio.transmit()``, the highest-risk path in the project. The
:class:`LinkTxBridge` mirrors ``TxSession``: a clock-injected **synchronous keying core**
(``on_start``/``on_frame``/``on_end``/``tick``/``hard_unkey``, driven here with a :class:`FakeClock`, no
asyncio, no sleeps) wrapped in an async **poll loop**. Two planes of proof, matching ``test_tx_audio.py``:

- **Unit** (FakeClock, no asyncio): the keying lifecycle bracketed by PTT; the two backstops
  (``tx.idle_timeout`` for an unpaired ``START``, the ``TxLimiter`` for continuous audio); the cooloff
  refusal; and the contention drop (the local operator owns the shared ``TxSlot``). ``MockRadio.ptt`` only
  flips a flag, so a ``_PttSpyRadio`` records the key sequence — ``[True, False]`` shows PTT asserted for
  the stream's duration and dropped at its end.
- **Loop** (``asyncio.run``): a scripted ``START``/frames/``END`` round-trips to ``radio.tx_log`` bracketed
  by PTT and tees to the browser ``link_hub``; a disabled link never keys; ``stop()`` hard-unkeys.
"""

from __future__ import annotations

import asyncio

from radio_server.audio import AudioFrame
from radio_server.backends import MockRadio
from radio_server.link import MockLink, StreamEdge
from radio_server.linktx import LinkTxBridge
from radio_server.rx import AudioHub
from radio_server.tx import TxSlot
from radio_server.txlimit import TxLimiter

from .conftest import FakeClock

IDLE = 2.0
MAX_TX = 180.0
COOLOFF = 10.0


class _PttSpyRadio(MockRadio):
    """A MockRadio that records its ``ptt()`` calls, so a test can assert the keying sequence.

    MockRadio has no PTT history (the state is one private bool that ``transmit()`` also touches), so the
    bridge's key-up/key-down edges are invisible without this spy — the ``test_tx_audio.py`` idiom.
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.ptt_log: list[bool] = []

    def ptt(self, on: bool) -> None:
        self.ptt_log.append(on)
        super().ptt(on)


def _make_bridge(
    *,
    link: MockLink | None = None,
    clock: FakeClock | None = None,
    max_tx: float = MAX_TX,
    cooloff: float = COOLOFF,
    tx_slot: TxSlot | None = None,
):
    """Build a bridge over a ``_PttSpyRadio`` with a fresh slot + limiter; return (bridge, radio, ev)."""
    radio = _PttSpyRadio()
    hub = AudioHub()
    limiter = TxLimiter(max_tx, cooloff)
    events: list[tuple[str, dict]] = []
    bridge = LinkTxBridge(
        link or MockLink(),
        radio,
        hub,
        tx_slot=tx_slot or TxSlot(),
        limiter=limiter,
        idle_timeout=IDLE,
        clock=clock or FakeClock(),
        on_event=lambda phase, **f: events.append((phase, f)),
    )
    return bridge, radio, events


# --- Unit: the keying lifecycle bracketed by PTT (FakeClock, no asyncio) --------------------------


def test_start_frames_end_brackets_transmit_with_ptt():
    # The acceptance shape: START keys, each frame transmits, END unkeys — the frames land in tx_log
    # bracketed by exactly one key-up / key-down.
    bridge, radio, _ = _make_bridge()
    a, b = AudioFrame(b"\x01\x02"), AudioFrame(b"\x03\x04")
    bridge.on_start(0.0)
    bridge.on_frame(a.samples, 0.0)
    bridge.on_frame(b.samples, 0.0)
    bridge.on_end(0.0)
    assert radio.ptt_log == [True, False]  # keyed for the stream's duration, dropped at END
    assert [f.samples for f in radio.tx_log] == [a.samples, b.samples]
    assert bridge.keyed is False


def test_frames_before_start_are_not_transmitted():
    # A dropped/unbracketed frame never keys the radio (mirrors TxSession: a bad frame never keys).
    bridge, radio, _ = _make_bridge()
    bridge.on_frame(b"\x01\x02", 0.0)
    assert radio.ptt_log == []
    assert radio.tx_log == []


def test_unpaired_start_drops_ptt_after_idle_timeout():
    # A peer vanished mid-stream — START and frames, no END (ADR 0047). tx.idle_timeout is the backstop:
    # once the inbound stream has been silent for the window, `tick` drops PTT. No cooloff (not a limit).
    clock = FakeClock()
    bridge, radio, events = _make_bridge(clock=clock)
    bridge.on_start(clock.now)
    bridge.on_frame(b"\x01\x02", clock.now)
    assert bridge.idle_elapsed(clock.now) is False  # just stamped active
    clock.advance(IDLE)  # the stream has gone silent for the full window
    bridge.tick(clock.now)
    assert radio.ptt_log == [True, False]
    assert bridge.keyed is False
    assert events == []  # an idle-out is not a forced unkey — no limiter record


def test_continuous_frames_force_unkey_at_max_tx():
    # The runaway the whole limiter exists for: CONTINUOUS audio never goes silent, so idle_timeout never
    # fires. The limiter force-unkeys at max_tx mid-stream, WITHOUT waiting for END. `run`'s loop calls
    # `tick` before each item, so the frame arriving at/after the limit is not transmitted.
    clock = FakeClock()
    bridge, radio, events = _make_bridge(clock=clock, max_tx=MAX_TX)
    bridge.on_start(clock.now)
    bridge.on_frame(b"\xaa\xaa", clock.now)  # one frame in-window
    clock.advance(MAX_TX)  # held keyed for the full max
    bridge.tick(clock.now)  # the backstop fires here — force unkey
    assert radio.ptt_log == [True, False]
    assert bridge.keyed is False
    assert [phase for phase, _ in events] == ["forced_unkey"]
    assert events[0][1]["duration"] == MAX_TX  # keyed-since-key-down, operator-visible
    # A frame after the cut is not transmitted (not keyed) — only the in-window frame landed.
    bridge.on_frame(b"\xbb\xbb", clock.now)
    assert [f.samples for f in radio.tx_log] == [b"\xaa\xaa"]


def test_cooloff_refuses_next_start_then_permits():
    # After a forced unkey the limiter refuses to re-key for cooloff_seconds — a START refused by cooloff
    # is DROPPED, not queued. Once the window elapses, a fresh START keys again.
    clock = FakeClock()
    bridge, radio, events = _make_bridge(clock=clock, max_tx=MAX_TX, cooloff=COOLOFF)
    bridge.on_start(clock.now)
    clock.advance(MAX_TX)
    bridge.tick(clock.now)  # forced unkey → cooloff begins
    bridge.on_end(clock.now)  # the peer's stream ends normally afterward (clears nothing to clear)
    # A new stream tries to key DURING cooloff → refused, not keyed.
    clock.advance(COOLOFF / 2)
    bridge.on_start(clock.now)
    assert bridge.keyed is False
    assert ("refused_cooloff", {}) in events
    # After the cooloff window elapses, the next START keys.
    clock.advance(COOLOFF)  # now well past cooloff_until
    bridge.on_start(clock.now)
    assert bridge.keyed is True
    assert radio.ptt_log == [True, False, True]  # keyed, forced-off, keyed again


def test_link_start_dropped_while_local_holds_the_slot():
    # THE LOCAL OPERATOR OWNS THE STATION: a browser Talk / voice service / ID holds the shared slot, so a
    # link START is DROPPED — not queued, not preempting. No PTT, no frames on the air, refusal by name.
    slot = TxSlot()
    assert slot.try_acquire() is True  # the local operator is holding the transmitter
    bridge, radio, events = _make_bridge(tx_slot=slot)
    bridge.on_start(0.0)
    bridge.on_frame(b"\x01\x02", 0.0)
    assert bridge.keyed is False
    assert radio.ptt_log == []  # never keyed
    assert radio.tx_log == []  # nothing reached the antenna
    assert ("dropped", {}) in events
    assert slot.occupied is True  # the local operator's slot is untouched


def test_hard_unkey_drops_ptt_mid_stream_and_releases_the_slot():
    # POST /link/disable is a hard unkey: drop PTT NOW, mid-frame, and release the slot. Idempotent.
    slot = TxSlot()
    bridge, radio, _ = _make_bridge(tx_slot=slot)
    bridge.on_start(0.0)
    bridge.on_frame(b"\x01\x02", 0.0)
    assert bridge.keyed is True and slot.occupied is True
    bridge.hard_unkey(0.0)
    assert bridge.keyed is False
    assert radio.ptt_log == [True, False]
    assert slot.occupied is False  # slot freed for the local operator
    bridge.hard_unkey(0.0)  # idempotent — no spurious second key-down
    assert radio.ptt_log == [True, False]


# --- Loop: the async poll loop (asyncio.run, self-terminating) ------------------------------------


class _ScriptedLink(MockLink):
    """A MockLink that signals when its scripted RX sequence is exhausted (the ``test_link_audio`` idiom),
    so a bridge loop over it terminates deterministically. Born disabled like any Link."""

    def __init__(self, frames: list[AudioFrame | StreamEdge | None]) -> None:
        super().__init__(rx_frames=frames)
        self._remaining = len(frames)
        self.drained = asyncio.Event()

    def receive(self) -> AudioFrame | StreamEdge | None:
        item = super().receive()
        if self._remaining > 0:
            self._remaining -= 1
            if self._remaining == 0:
                self.drained.set()
        return item


async def _run_scripted(link: _ScriptedLink, radio: _PttSpyRadio, hub: AudioHub) -> None:
    """Run an ENABLED bridge over the scripted link until it drains, then stop it."""
    link.enable(True)
    bridge = LinkTxBridge(
        link, radio, hub, tx_slot=TxSlot(), limiter=TxLimiter(MAX_TX, COOLOFF), idle_timeout=IDLE, poll=0
    )
    bridge.start()
    await link.drained.wait()
    await asyncio.sleep(0)  # let the loop process the final item before we stop it
    await bridge.stop()


def test_loop_round_trips_start_frames_end_to_tx_log_and_tees_to_hub():
    a, b = AudioFrame(b"\x01\x02"), AudioFrame(b"\x03\x04")
    link = _ScriptedLink([StreamEdge.START, a, b, StreamEdge.END])
    radio = _PttSpyRadio()
    hub = AudioHub()
    queue = hub.subscribe()
    asyncio.run(_run_scripted(link, radio, hub))
    # Bracketed by PTT on the air:
    assert radio.ptt_log == [True, False]
    assert [f.samples for f in radio.tx_log] == [a.samples, b.samples]
    # And teed to the browser monitor:
    teed = []
    while not queue.empty():
        teed.append(queue.get_nowait())
    assert teed == [a.samples, b.samples]


def test_loop_disabled_link_never_keys():
    # The enable gate (ADR 0041/0042): a disabled link is never read, so it never keys. The scripted
    # frames survive; the radio stays silent.
    async def scenario() -> tuple[list[bool], list]:
        link = MockLink(rx_frames=[StreamEdge.START, AudioFrame(b"\x01\x02"), StreamEdge.END])  # disabled
        radio = _PttSpyRadio()
        bridge = LinkTxBridge(
            link, radio, AudioHub(), tx_slot=TxSlot(), limiter=TxLimiter(MAX_TX, COOLOFF),
            idle_timeout=IDLE, poll=0,
        )
        bridge.start()
        for _ in range(5):
            await asyncio.sleep(0)  # several loop turns, all gated off
        await bridge.stop()
        return radio.ptt_log, radio.tx_log

    ptt_log, tx_log = asyncio.run(scenario())
    assert ptt_log == []
    assert tx_log == []


def test_loop_tees_dropped_stream_to_hub_but_not_to_radio():
    # A link stream dropped on contention still tees to the browser monitor (audible), but nothing reaches
    # the antenna. Pre-hold the shared slot so the START is dropped.
    a = AudioFrame(b"\x05\x06")
    link = _ScriptedLink([StreamEdge.START, a, StreamEdge.END])
    radio = _PttSpyRadio()
    hub = AudioHub()
    queue = hub.subscribe()

    async def scenario() -> None:
        link.enable(True)
        slot = TxSlot()
        slot.try_acquire()  # the local operator holds the transmitter
        bridge = LinkTxBridge(
            link, radio, hub, tx_slot=slot, limiter=TxLimiter(MAX_TX, COOLOFF), idle_timeout=IDLE, poll=0
        )
        bridge.start()
        await link.drained.wait()
        await asyncio.sleep(0)
        await bridge.stop()

    asyncio.run(scenario())
    assert radio.ptt_log == []  # never keyed — local owns the slot
    assert radio.tx_log == []  # nothing on the air
    teed = []
    while not queue.empty():
        teed.append(queue.get_nowait())
    assert teed == [a.samples]  # but the frame was still audible in the browser monitor
