"""DStarBridge — the half-duplex reflector <-> RF state machine (ADR 0087).

Driven against `MockGatewayClient` + `MockRadio` + a fake vocoder, no gateway and no DV Dongle. Async
scenarios use `asyncio.run(...)` (the `test_link_bridge.py` convention); the hang is a real
`asyncio.wait_for` timeout, so scenarios use a small real `tx_hang` and short real sleeps. Keyed
state is asserted on the talker slot / arbiter latch (a `MockRadio.transmit` self-resets its own
`transmitting` flag).
"""

from __future__ import annotations

import asyncio

from radio_server.arbiter import RadioArbiter
from radio_server.audio import AudioFrame
from radio_server.backends import MockRadio
from radio_server.dstar import MockGatewayClient
from radio_server.dstar import dsrp, header
from radio_server.dstar.bridge import DStarBridge
from radio_server.rx import AudioHub
from radio_server.tx import TxSlot
from radio_server.vocoder.base import AMBE_BYTES_PER_FRAME, PCM_BYTES_PER_FRAME, PCM_FORMAT

# A whole-sample canonical 20 ms frame (960 samples @ 48 kHz) — resamples to one 8 kHz vocoder frame.
FRAME = b"\x01\x00" * 960
HEADER = header.build_voice_header(callsign="AE9S", module="A", ur="CQCQCQ")
INBOUND_HEADER = dsrp.build_header_packet(HEADER, 0x0777)


class FakeVocoder:
    """A pure, instant `Vocoder`: encode -> 9 deterministic bytes, decode -> a fixed 8 kHz frame."""

    def __init__(self) -> None:
        self.encoded = 0
        self.decoded = 0

    def encode(self, frame: AudioFrame) -> bytes:
        assert len(frame.samples) == PCM_BYTES_PER_FRAME and frame.format == PCM_FORMAT
        self.encoded += 1
        return bytes([self.encoded & 0xFF]) * AMBE_BYTES_PER_FRAME

    def decode(self, ambe: bytes) -> AudioFrame:
        assert len(ambe) == AMBE_BYTES_PER_FRAME
        self.decoded += 1
        return AudioFrame(b"\x03\x00" * 160, PCM_FORMAT)

    def close(self) -> None:
        pass


def _bridge(radio, gateway, vocoder, *, tx_to_rf=True, rx_to_reflector=True, tx_hang=0.05):
    demand = {"n": 0}

    async def acquire():
        demand["n"] += 1

    async def release():
        demand["n"] -= 1

    bridge = DStarBridge(
        gateway,
        radio,
        vocoder,
        arbiter=RadioArbiter(),
        tx_slot=TxSlot(),
        audio_hub=AudioHub(),
        callsign="AE9S",
        module="A",
        acquire_rx=acquire,
        release_rx=release,
        tx_to_rf=tx_to_rf,
        rx_to_reflector=rx_to_reflector,
        tx_hang=tx_hang,
    )
    return bridge, demand


def test_start_registers_and_holds_an_rx_demand():
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        bridge, demand = _bridge(radio, gateway, FakeVocoder())
        await bridge.start()
        try:
            assert bridge.running and gateway.status().registered
            assert demand["n"] == 1  # pump demand held so RF audio flows with no browser
        finally:
            await bridge.stop()
        assert not bridge.running
        assert demand["n"] == 0
        assert bridge.mode == "idle"

    asyncio.run(scenario())


def test_rf_to_reflector_sends_header_then_ambe_then_end():
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        vocoder = FakeVocoder()
        bridge, _ = _bridge(radio, gateway, vocoder)
        await bridge.start()
        try:
            for _ in range(3):
                bridge._audio_hub.publish(FRAME)  # gate-open RF audio reaches the subscriber
            await asyncio.sleep(0.03)
            kinds = [m.kind for m in gateway.sent]
            assert kinds[0] is dsrp.MessageKind.HEADER  # exactly one header opens the over
            assert kinds.count(dsrp.MessageKind.HEADER) == 1
            assert dsrp.MessageKind.DATA in kinds  # AMBE voice frames followed
            assert vocoder.encoded >= 1
            assert bridge.mode == "tx"
            await asyncio.sleep(0.08)  # past the hang: the over closes
            assert gateway.sent[-1].kind is dsrp.MessageKind.DATA and gateway.sent[-1].end
            assert bridge.mode == "idle"
            assert bridge.tx_stats()["tx_overs"] == 1
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_reflector_to_rf_keys_the_radio_and_decodes():
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        vocoder = FakeVocoder()
        bridge, _ = _bridge(radio, gateway, vocoder)
        await bridge.start()
        try:
            gateway.inject(INBOUND_HEADER)  # a reflector stream opens
            await asyncio.sleep(0.02)
            assert bridge.mode == "rx"
            for seq in range(3):
                dv = dsrp.build_dv_frame(bytes([seq]) * 9, dsrp.slow_data_for_seq(seq))
                gateway.inject(dsrp.build_data_packet(dv, 0x0777, seq))
            await asyncio.sleep(0.03)
            assert vocoder.decoded >= 1
            assert bridge._tx_slot.occupied  # radio keyed for the reflector audio
            assert len(radio.tx_log) >= 1
            # End frame closes the over and drops PTT.
            end_dv = dsrp.build_dv_frame(dsrp.NULL_AMBE)
            gateway.inject(dsrp.build_data_packet(end_dv, 0x0777, 3, end=True))
            await asyncio.sleep(0.02)
            assert not bridge._tx_slot.occupied
            assert bridge.mode == "idle"
            assert bridge.tx_stats()["rx_overs"] == 1
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_half_duplex_rx_blocks_outbound_tx():
    # While a reflector stream holds the RX latch, RF audio is dropped (never encoded) — one talker.
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        vocoder = FakeVocoder()
        bridge, _ = _bridge(radio, gateway, vocoder, tx_hang=0.5)
        await bridge.start()
        try:
            gateway.inject(INBOUND_HEADER)
            await asyncio.sleep(0.02)
            assert bridge.mode == "rx"
            for _ in range(3):
                bridge._audio_hub.publish(FRAME)  # RF audio during an inbound over
            await asyncio.sleep(0.03)
            assert vocoder.encoded == 0  # nothing encoded outbound
            assert bridge.tx_stats()["tx_dropped_busy"] >= 1
            assert not any(m.kind is dsrp.MessageKind.HEADER for m in gateway.sent)
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_half_duplex_tx_blocks_inbound_rx():
    # While RF audio holds the TX latch, an inbound reflector header is dropped as busy.
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        vocoder = FakeVocoder()
        bridge, _ = _bridge(radio, gateway, vocoder, tx_hang=0.5)
        await bridge.start()
        try:
            for _ in range(3):
                bridge._audio_hub.publish(FRAME)
            await asyncio.sleep(0.03)
            assert bridge.mode == "tx"
            gateway.inject(INBOUND_HEADER)  # a reflector stream arrives mid-transmit
            await asyncio.sleep(0.03)
            assert bridge.mode == "tx"  # still transmitting; inbound deferred
            assert bridge.tx_stats()["rx_dropped_busy"] >= 1
            assert vocoder.decoded == 0
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_receive_only_mode_never_encodes_or_holds_demand_for_rf():
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        vocoder = FakeVocoder()
        # rx_to_reflector=False: no hub subscription, no rx demand, no outbound path.
        bridge, demand = _bridge(radio, gateway, vocoder, rx_to_reflector=False)
        await bridge.start()
        try:
            assert demand["n"] == 0  # no RF demand held when not bridging RF->reflector
            gateway.inject(INBOUND_HEADER)
            gateway.inject(dsrp.build_data_packet(dsrp.build_dv_frame(bytes(9)), 0x0777, 0, end=True))
            await asyncio.sleep(0.02)
            assert vocoder.decoded >= 1  # reflector->RF still works
        finally:
            await bridge.stop()

    asyncio.run(scenario())
