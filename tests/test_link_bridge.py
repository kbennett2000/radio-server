"""MumbleBridge — the RF <-> Mumble peer state machine (ADR 0041).

Driven against `MockMumbleClient` + `MockRadio` with no network and no `pymumble`. Async scenarios
use `asyncio.run(...)` (no pytest-asyncio, the `test_rx_audio.py` convention). The Mumble->RF hang
is a real `asyncio.wait_for` timeout, so those scenarios use a small real `tx_hang` and short real
sleeps; the keyed state is asserted on the arbiter latch / talker slot (a `MockRadio.transmit`
self-resets `transmitting`, so it is not a keyed-state signal — the arbiter is).
"""

from __future__ import annotations

import asyncio

from radio_server.arbiter import RadioArbiter
from radio_server.backends import MockRadio
from radio_server.link import DtmfMuteGate, MockMumbleClient, MumbleBridge
from radio_server.rx import AudioHub
from radio_server.services import StreamingId
from radio_server.services.station_id import StubId
from radio_server.tx import TxSlot

FRAME = b"\x01\x00" * 960  # a whole-sample canonical 20 ms frame
VOICE = b"\x02\x00" * 960
ID = b"<id:AE9S>"


def _bridge(
    radio,
    mumble,
    *,
    tx_to_rf=True,
    station_id=None,
    rx_active=None,
    tx_hang=0.05,
    dtmf_mute=None,
    mute_delay=0.02,
):
    demand = {"n": 0}

    async def acquire():
        demand["n"] += 1

    async def release():
        demand["n"] -= 1

    bridge = MumbleBridge(
        mumble,
        radio,
        arbiter=RadioArbiter(),
        tx_slot=TxSlot(),
        audio_hub=AudioHub(),
        acquire_rx=acquire,
        release_rx=release,
        station_id=station_id,
        tx_to_rf=tx_to_rf,
        rx_active=rx_active,
        tx_hang=tx_hang,
        dtmf_mute=dtmf_mute,
        mute_delay=mute_delay,
    )
    return bridge, demand


def test_start_connects_and_holds_an_rx_demand():
    async def scenario():
        radio, mumble = MockRadio(), MockMumbleClient()
        bridge, demand = _bridge(radio, mumble)
        await bridge.start()
        try:
            assert bridge.running and mumble.status().connected
            assert demand["n"] == 1  # pump demand held so RF audio flows with no browser attached
        finally:
            await bridge.stop()
        assert not bridge.running and not mumble.status().connected
        assert demand["n"] == 0  # demand released on stop

    asyncio.run(scenario())


def test_rf_to_mumble_forwards_hub_frames():
    async def scenario():
        radio, mumble = MockRadio(), MockMumbleClient()
        bridge, _ = _bridge(radio, mumble)
        await bridge.start()
        try:
            bridge._audio_hub.publish(FRAME)  # a gate-open RF frame reaches the bridge subscriber
            await asyncio.sleep(0.02)
            assert mumble.sent_audio == [FRAME]
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_mumble_to_rf_keys_and_identifies():
    async def scenario():
        radio, mumble = MockRadio(), MockMumbleClient()
        sid = StreamingId(StubId(), "AE9S", interval=600.0)
        bridge, _ = _bridge(radio, mumble, station_id=sid)
        await bridge.start()
        try:
            mumble.inject(VOICE)
            await asyncio.sleep(0.02)
            # Keyed (arbiter TX latch held, slot occupied) and the over carries the station ID.
            assert bridge._arbiter.transmitting and bridge._tx_slot.occupied
            assert [f.samples for f in radio.tx_log] == [ID, VOICE]
            # Mumble goes quiet: after the hang the bridge unkeys and frees the slot.
            await asyncio.sleep(0.1)
            assert not bridge._arbiter.transmitting and not bridge._tx_slot.occupied
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_receive_only_never_transmits():
    async def scenario():
        radio, mumble = MockRadio(), MockMumbleClient()
        bridge, _ = _bridge(radio, mumble, tx_to_rf=False)
        await bridge.start()
        try:
            mumble.inject(VOICE)
            await asyncio.sleep(0.05)
            assert radio.tx_log == []  # tx_to_rf=False: monitor only, never keys
            assert not bridge._arbiter.transmitting
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_drops_inbound_when_the_talker_slot_is_busy():
    async def scenario():
        radio, mumble = MockRadio(), MockMumbleClient()
        bridge, _ = _bridge(radio, mumble)
        await bridge.start()
        try:
            # A browser talker already holds the single transmitter slot.
            assert bridge._tx_slot.try_acquire()
            mumble.inject(VOICE)
            await asyncio.sleep(0.05)
            assert radio.tx_log == []  # the bridge refuses (drops) rather than double-keying
        finally:
            bridge._tx_slot.release()
            await bridge.stop()

    asyncio.run(scenario())


def test_defers_to_a_live_rf_signal():
    async def scenario():
        radio, mumble = MockRadio(), MockMumbleClient()
        rx_live = {"on": True}
        bridge, _ = _bridge(radio, mumble, rx_active=lambda: rx_live["on"])
        await bridge.start()
        try:
            mumble.inject(VOICE)
            await asyncio.sleep(0.02)
            assert radio.tx_log == []  # held off while a real signal is being received
            rx_live["on"] = False
            mumble.inject(VOICE)
            await asyncio.sleep(0.02)
            assert [f.samples for f in radio.tx_log] == [VOICE]  # keys once the channel is clear
        finally:
            await bridge.stop()

    asyncio.run(scenario())


# --- DTMF mute (ADR 0045): the delayed RF->Mumble feed + retroactive digit squelch -----------


def test_dtmf_mute_gate_arms_refreshes_and_expires():
    t = {"now": 0.0}
    gate = DtmfMuteGate(hold=1.0, clock=lambda: t["now"])
    assert not gate.muted()
    gate.note_digit()
    assert gate.muted()
    t["now"] = 0.9
    gate.note_digit()  # each digit re-arms the hold
    t["now"] = 1.5
    assert gate.muted()  # 0.9 + 1.0 > 1.5
    t["now"] = 2.0
    assert not gate.muted()


def test_dtmf_mute_delays_but_delivers_everything_without_digits():
    async def scenario():
        radio, mumble = MockRadio(), MockMumbleClient()
        gate = DtmfMuteGate(hold=1.0)
        # mute_delay=0.02 s -> delay_bytes = 1920 = exactly one 20 ms frame held back.
        bridge, _ = _bridge(radio, mumble, dtmf_mute=gate, mute_delay=0.02)
        await bridge.start()
        try:
            bridge._audio_hub.publish(FRAME)
            bridge._audio_hub.publish(VOICE)
            # The hub then goes quiet (a squelch close): the idle flush must deliver the tail —
            # everything arrives, in order, just late.
            await asyncio.sleep(0.08)
            assert mumble.sent_audio == [FRAME, VOICE]
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_dtmf_digit_condemns_the_buffered_tone_and_holds_mute():
    async def scenario():
        radio, mumble = MockRadio(), MockMumbleClient()
        t = {"now": 0.0}
        gate = DtmfMuteGate(hold=1.0, clock=lambda: t["now"])
        bridge, _ = _bridge(radio, mumble, dtmf_mute=gate, mute_delay=0.02)
        await bridge.start()
        try:
            # The tone hits the hub first (multimon hasn't decoded it yet)...
            bridge._audio_hub.publish(FRAME)
            await asyncio.sleep(0.005)
            # ...then the digit decodes: everything buffered is condemned, feed muted.
            gate.note_digit()
            bridge._audio_hub.publish(VOICE)  # still inside the hold -> dropped
            await asyncio.sleep(0.08)
            assert mumble.sent_audio == []  # the tone never reached Mumble
            # Hold expires: fresh audio flows again (delivered by the idle flush).
            t["now"] = 2.0
            bridge._audio_hub.publish(VOICE)
            await asyncio.sleep(0.08)
            assert mumble.sent_audio == [VOICE]
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_tx_stats_count_every_inbound_frame_into_one_bucket():
    async def scenario():
        radio, mumble = MockRadio(), MockMumbleClient()
        rx_live = {"on": True}
        bridge, _ = _bridge(radio, mumble, rx_active=lambda: rx_live["on"])
        await bridge.start()
        try:
            mumble.inject(VOICE)  # dropped: deferring to a live RF signal
            await asyncio.sleep(0.02)
            assert radio.tx_log == []
            rx_live["on"] = False
            assert bridge._tx_slot.try_acquire()  # a browser talker takes the slot
            mumble.inject(VOICE)  # dropped: slot busy
            await asyncio.sleep(0.02)
            bridge._tx_slot.release()
            mumble.inject(VOICE)  # transmitted: keys one over
            await asyncio.sleep(0.02)
            stats = bridge.tx_stats()
            assert stats["frames_in"] == 3
            assert stats["dropped_rx_active"] == 1
            assert stats["dropped_slot_busy"] == 1
            assert stats["overs_keyed"] == 1
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_start_and_stop_are_idempotent():
    async def scenario():
        radio, mumble = MockRadio(), MockMumbleClient()
        bridge, demand = _bridge(radio, mumble)
        await bridge.start()
        await bridge.start()  # second start is a no-op (no double demand / double task)
        assert demand["n"] == 1
        await bridge.stop()
        await bridge.stop()  # second stop is a no-op
        assert demand["n"] == 0

    asyncio.run(scenario())


def test_no_station_id_transmits_unidentified():
    async def scenario():
        radio, mumble = MockRadio(), MockMumbleClient()
        bridge, _ = _bridge(radio, mumble, station_id=None)
        await bridge.start()
        try:
            mumble.inject(VOICE)
            await asyncio.sleep(0.02)
            assert [f.samples for f in radio.tx_log] == [VOICE]  # no callsign -> no ID
        finally:
            await bridge.stop()

    asyncio.run(scenario())
