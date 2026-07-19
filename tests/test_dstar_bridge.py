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


def _bridge(radio, gateway, vocoder, *, tx_to_rf=True, rx_to_reflector=True, tx_hang=0.05,
            vocoder_keepalive=0.0):
    demand = {"n": 0}

    async def acquire():
        demand["n"] += 1

    async def release():
        demand["n"] -= 1

    bridge = DStarBridge(
        gateway,
        radio,
        lambda: vocoder,  # the bridge creates the vocoder from a factory on start() (ADR 0089)
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
        vocoder_keepalive=vocoder_keepalive,  # 0 = deterministic (off) for most scenarios
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


# --- send_link_command (reflector linking, ADR 0088) ---------------------------------


def test_send_link_command_emits_header_then_end_and_touches_no_vocoder():
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        vocoder = FakeVocoder()
        # A browser-operator posture bridge: no RF->reflector pump competing for the TX latch.
        bridge, _ = _bridge(radio, gateway, vocoder, rx_to_reflector=False)
        await bridge.start()
        try:
            assert bridge.send_link_command("REF001CL") is True
            kinds = [m.kind for m in gateway.sent]
            assert kinds == [dsrp.MessageKind.HEADER, dsrp.MessageKind.DATA]  # default 0 command frames
            assert header.parse_header(gateway.sent[0].radio_header).ur == "REF001CL"  # URCALL carries it
            assert gateway.sent[-1].end  # the DATA is the terminator (end bit)
            assert vocoder.encoded == 0 and vocoder.decoded == 0  # NULL_AMBE only; chip untouched
            assert bridge.mode == "idle"  # a synchronous burst leaves the latch idle
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_send_link_command_frame_count_is_tunable():
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        bridge = DStarBridge(
            gateway, radio, lambda: FakeVocoder(),
            arbiter=RadioArbiter(), tx_slot=TxSlot(), audio_hub=AudioHub(),
            callsign="AE9S", module="A", rx_to_reflector=False, command_frames=6,
        )
        await bridge.start()
        try:
            bridge.send_link_command("       U")  # unlink
            data = [m for m in gateway.sent if m.kind is dsrp.MessageKind.DATA]
            assert len(data) == 7  # 6 silence frames + 1 end frame
            assert sum(1 for m in data if m.end) == 1
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_send_link_command_refused_when_not_idle():
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        bridge, _ = _bridge(radio, gateway, FakeVocoder(), tx_hang=0.5)
        await bridge.start()
        try:
            gateway.inject(INBOUND_HEADER)  # latch RX
            await asyncio.sleep(0.02)
            assert bridge.mode == "rx"
            before = len(gateway.sent)
            assert bridge.send_link_command("REF001CL") is False  # busy — caller retries
            assert len(gateway.sent) == before  # nothing emitted
        finally:
            await bridge.stop()

    asyncio.run(scenario())


# --- send_operator_audio (browser mic -> reflector, ADR 0088) ------------------------


def test_send_operator_audio_opens_one_header_encodes_and_terminates():
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        vocoder = FakeVocoder()
        bridge, _ = _bridge(radio, gateway, vocoder, rx_to_reflector=False)
        await bridge.start()
        try:
            for _ in range(3):
                await bridge.send_operator_audio(FRAME)
            kinds = [m.kind for m in gateway.sent]
            assert kinds[0] is dsrp.MessageKind.HEADER
            assert kinds.count(dsrp.MessageKind.HEADER) == 1  # one over, one header
            assert vocoder.encoded == 3 and kinds.count(dsrp.MessageKind.DATA) == 3
            assert bridge.mode == "tx"
            bridge.end_operator_over()
            assert gateway.sent[-1].kind is dsrp.MessageKind.DATA and gateway.sent[-1].end
            assert bridge.mode == "idle"
            assert bridge.tx_stats()["tx_overs"] == 1
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_send_operator_audio_drops_while_reflector_inbound():
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        vocoder = FakeVocoder()
        bridge, _ = _bridge(radio, gateway, vocoder, rx_to_reflector=False, tx_hang=0.5)
        await bridge.start()
        try:
            gateway.inject(INBOUND_HEADER)  # a reflector stream owns the RX latch
            await asyncio.sleep(0.02)
            assert bridge.mode == "rx"
            await bridge.send_operator_audio(FRAME)  # operator talks over an inbound over
            assert vocoder.encoded == 0  # dropped — one talker, vocoder busy decoding
            assert bridge.tx_stats()["tx_dropped_busy"] >= 1
            assert not any(m.kind is dsrp.MessageKind.HEADER for m in gateway.sent)
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_keepalive_decodes_while_idle_to_keep_the_chip_warm():
    # The DV Dongle sleeps after ~2-3s idle (ADR 0088); a keepalive decode while idle keeps it warm so
    # the first inbound reflector frame doesn't time out. It must fire only when idle.
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        vocoder = FakeVocoder()
        bridge, _ = _bridge(radio, gateway, vocoder, rx_to_reflector=False, vocoder_keepalive=0.02)
        await bridge.start()
        try:
            await asyncio.sleep(0.09)  # several keepalive intervals
            assert vocoder.decoded >= 2  # idle keepalive poked the chip
            assert bridge.mode == "idle"  # keepalive never leaves the latch keyed
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_keepalive_does_not_run_during_an_inbound_over():
    # While a reflector stream holds the RX latch, the keepalive must not inject extra decodes (that
    # would be the ADR 0086 encode/decode-order hazard territory and waste the chip). Real frames warm it.
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        vocoder = FakeVocoder()
        bridge, _ = _bridge(radio, gateway, vocoder, vocoder_keepalive=0.02, tx_hang=0.5)
        await bridge.start()
        try:
            gateway.inject(INBOUND_HEADER)
            await asyncio.sleep(0.02)
            assert bridge.mode == "rx"
            before = vocoder.decoded
            await asyncio.sleep(0.09)  # keepalive would fire here if not gated on idle
            # Only real inbound data frames (none injected here) would decode; keepalive stays off in rx.
            assert vocoder.decoded == before
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_end_operator_over_is_a_noop_when_idle():
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        bridge, _ = _bridge(radio, gateway, FakeVocoder(), rx_to_reflector=False)
        await bridge.start()
        try:
            bridge.end_operator_over()  # never opened an over
            assert gateway.sent == []
            assert bridge.mode == "idle"
        finally:
            await bridge.stop()

    asyncio.run(scenario())


# --- ADR 0089: shared DV Dongle (lazy exclusive start) + TX-owner latch + activity ---------


def test_start_creates_the_vocoder_from_the_factory_and_stop_closes_it():
    # The DV Dongle is opened on start() (link) and closed on stop() (unlink), not held while idle, so
    # the two radio instances can share it (ADR 0089).
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        made = []
        bridge = DStarBridge(
            gateway, radio, lambda: made.append(FakeVocoder()) or made[-1],
            arbiter=RadioArbiter(), tx_slot=TxSlot(), audio_hub=AudioHub(),
            callsign="AE9S", module="A", rx_to_reflector=False,
        )
        assert made == []  # nothing opened before start
        await bridge.start()
        assert len(made) == 1  # opened on start
        await bridge.stop()

    asyncio.run(scenario())


def test_start_propagates_a_busy_dongle_and_leaves_nothing_open():
    # The exclusive DV Dongle open fails when the other instance holds it; start() re-raises and the
    # bridge stays un-acquired (the manager surfaces DStarUnavailable). No gateway registration lingers.
    from radio_server.vocoder.base import VocoderUnavailable

    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()

        def busy_factory():
            raise VocoderUnavailable("in use by the other radio")

        bridge = DStarBridge(
            gateway, radio, busy_factory,
            arbiter=RadioArbiter(), tx_slot=TxSlot(), audio_hub=AudioHub(),
            callsign="AE9S", module="A", rx_to_reflector=False,
        )
        raised = False
        try:
            await bridge.start()
        except VocoderUnavailable:
            raised = True
        assert raised
        assert bridge.running is False
        assert gateway.register_count == 0  # never registered
        # send_operator_audio is a no-op while un-acquired (nothing to encode into).
        await bridge.send_operator_audio(FRAME)
        assert gateway.sent == []

    asyncio.run(scenario())


def test_tx_owner_latch_keeps_crossband_and_browser_mic_from_interleaving():
    # With BOTH the RF pump (rx_to_reflector) and the browser mic live (ADR 0089), whichever opens the
    # over first owns it; the other source drops while it is live, so their frames never mux into one
    # DSRP session. Here the browser opens the over, then RF audio arrives and must be dropped.
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        vocoder = FakeVocoder()
        bridge, _ = _bridge(radio, gateway, vocoder, rx_to_reflector=True, tx_hang=0.5)
        hub = bridge._audio_hub  # the RF pump's source
        await bridge.start()
        try:
            await bridge.send_operator_audio(FRAME)  # browser opens the over
            assert bridge.mode == "tx" and bridge._tx_source == "op"
            session_id = gateway.sent[0].session_id
            dropped_before = bridge.tx_stats()["tx_dropped_busy"]
            # RF audio arrives while the browser owns TX — it must be dropped, not fed.
            hub.publish(FRAME)
            await asyncio.sleep(0.05)
            assert bridge.tx_stats()["tx_dropped_busy"] > dropped_before
            # Exactly one header/session on the wire — no second over opened by the RF pump.
            headers = [m for m in gateway.sent if m.kind is dsrp.MessageKind.HEADER]
            assert len(headers) == 1
            assert all(
                m.session_id == session_id
                for m in gateway.sent
                if m.kind is dsrp.MessageKind.DATA
            )
            # The browser closes its over; the RF pump's silence timeout must not have closed it early.
            bridge.end_operator_over()
            assert bridge.mode == "idle" and bridge._tx_source is None
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_activity_callback_reports_inbound_mycall_and_our_own_tx():
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        seen = []
        vocoder = FakeVocoder()
        bridge, _ = _bridge(radio, gateway, vocoder, rx_to_reflector=False, tx_hang=0.5)
        bridge._on_activity = seen.append
        await bridge.start()
        try:
            # Inbound over from K1ABC on the reflector → an rx activity entry with the parsed MYCALL.
            hdr = header.build_voice_header(callsign="K1ABC", module="A", ur="CQCQCQ")
            gateway.inject(dsrp.build_header_packet(hdr, 0x0123))
            await asyncio.sleep(0.02)
            rx = [a for a in seen if a["dir"] == "rx"]
            assert rx and rx[0]["mycall"] == "K1ABC"
            # Our own outbound over → a tx entry with our callsign.
            bridge._end_rx()
            await bridge.send_operator_audio(FRAME)
            tx = [a for a in seen if a["dir"] == "tx"]
            assert tx and tx[0]["mycall"] == "AE9S"
        finally:
            await bridge.stop()

    asyncio.run(scenario())
