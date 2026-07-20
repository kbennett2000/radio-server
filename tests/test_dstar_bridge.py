"""DStarBridge — the half-duplex reflector <-> RF state machine (ADR 0087).

Driven against `MockGatewayClient` + `MockRadio` + a fake vocoder, no gateway and no DV Dongle. Async
scenarios use `asyncio.run(...)` (the `test_link_bridge.py` convention); the hang is a real
`asyncio.wait_for` timeout, so scenarios use a small real `tx_hang` and short real sleeps. Keyed
state is asserted on the talker slot / arbiter latch (a `MockRadio.transmit` self-resets its own
`transmitting` flag).
"""

from __future__ import annotations

import array
import asyncio
import collections
import threading

from radio_server.activity import AudioLevelGate
from radio_server.arbiter import RadioArbiter
from radio_server.audio import AudioFrame
from radio_server.backends import MockRadio
from radio_server.dstar import MockGatewayClient
from radio_server.dstar import dsrp, header
from radio_server.dstar.bridge import DStarBridge
from radio_server.rx import AudioHub
from radio_server.tx import TxSlot
from radio_server.vocoder.base import AMBE_BYTES_PER_FRAME, PCM_BYTES_PER_FRAME, PCM_FORMAT

from .conftest import FakeClock

# A loud canonical 20 ms frame (RMS well above the VAD on-threshold) and a near-silent one (RMS ~1).
LOUD_FRAME = b"\x00\x20" * 960  # int16 0x2000 = 8192 → RMS 8192
QUIET_FRAME = b"\x01\x00" * 960  # value 1 → RMS 1, below any real VAD threshold

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
            vocoder_keepalive=0.0, clock=None, rf_gate=None, rx_gate=None, max_over=0.0, tx_slot=None):
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
        tx_slot=tx_slot if tx_slot is not None else TxSlot(),
        audio_hub=AudioHub(),
        callsign="AE9S",
        module="A",
        acquire_rx=acquire,
        release_rx=release,
        tx_to_rf=tx_to_rf,
        rx_to_reflector=rx_to_reflector,
        tx_hang=tx_hang,
        vocoder_keepalive=vocoder_keepalive,  # 0 = deterministic (off) for most scenarios
        clock=clock,
        rf_gate=rf_gate,
        rx_gate=rx_gate,
        max_over=max_over,
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


# --- ADR 0091: the crossband never leaves the transmitter keyed --------------------------------


class _FlakyVocoder(FakeVocoder):
    """Decodes the first ``ok`` frames, then raises — models the DV Dongle desyncing mid-over so no
    further audio is fed to RF while inbound DATA keeps arriving."""

    def __init__(self, ok: int) -> None:
        super().__init__()
        self._ok = ok

    def decode(self, ambe: bytes) -> AudioFrame:
        if self.decoded >= self._ok:
            self.decoded += 1
            raise RuntimeError("vocoder desynced")
        return super().decode(ambe)


class _BlockingVocoder(FakeVocoder):
    """Decodes ``block_after`` frames, then blocks until ``close()`` — models a decode parked in the
    executor with PTT already asserted (the exact stuck-key). ``close()`` unblocks it (the real
    dongle's ``close`` notifies a waiting exchange)."""

    def __init__(self, block_after: int) -> None:
        super().__init__()
        self._block_after = block_after
        self._release = threading.Event()
        self.blocked = False

    def decode(self, ambe: bytes) -> AudioFrame:
        if self.decoded >= self._block_after:
            self.blocked = True
            self._release.wait(timeout=5.0)  # parks until close(); bounded so a broken test can't hang
        return super().decode(ambe)

    def close(self) -> None:
        self._release.set()


class LevelVocoder(FakeVocoder):
    """Decodes every AMBE frame to a fixed-amplitude 8 kHz frame, so a test can drive the decoded
    CONTENT level the reflector→RF content gate keys off (ADR 0097): LOUD models real speech; a
    near-silent value models dead air / garbage that decodes to junk."""

    def __init__(self, value: int) -> None:
        super().__init__()
        self._sample = int(value).to_bytes(2, "little", signed=True)

    def decode(self, ambe: bytes) -> AudioFrame:
        assert len(ambe) == AMBE_BYTES_PER_FRAME
        self.decoded += 1
        return AudioFrame(self._sample * 160, PCM_FORMAT)


def _inject_over(gateway, n_data):
    gateway.inject(INBOUND_HEADER)
    for seq in range(n_data):
        dv = dsrp.build_dv_frame(bytes([seq & 0xFF]) * 9, dsrp.slow_data_for_seq(seq))
        gateway.inject(dsrp.build_data_packet(dv, 0x0777, seq))


def test_stalled_decode_closes_the_over_via_the_idle_watchdog():
    # The incident: inbound DATA keeps arriving (queue never idles) but decodes stop feeding RF, so the
    # queue-idle timeout never fires. The independent idle watchdog must still drop PTT.
    async def scenario():
        clock = FakeClock()
        radio, gateway = MockRadio(), MockGatewayClient()
        vocoder = _FlakyVocoder(ok=1)  # first decode keys; the rest raise (no feed)
        bridge, _ = _bridge(radio, gateway, vocoder, tx_hang=0.05, clock=clock)
        await bridge.start()
        try:
            gateway.inject(INBOUND_HEADER)
            dv = dsrp.build_dv_frame(b"\x01" * 9, dsrp.slow_data_for_seq(0))
            gateway.inject(dsrp.build_data_packet(dv, 0x0777, 0))  # decode ok → keys, stamps last_active
            await asyncio.sleep(0.02)
            assert bridge._tx_slot.occupied  # keyed onto RF
            # Time passes (fake clock) while inbound DATA keeps flowing but every decode now fails.
            clock.advance(0.2)
            for seq in range(1, 6):
                dv = dsrp.build_dv_frame(bytes([seq]) * 9, dsrp.slow_data_for_seq(seq))
                gateway.inject(dsrp.build_data_packet(dv, 0x0777, seq))
                await asyncio.sleep(0.005)
            # The idle watchdog (now - last_active >= tx_hang) closed the over even though DATA never
            # stopped arriving.
            assert not bridge._tx_slot.occupied
            assert bridge.mode == "idle"
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_stop_during_a_blocked_decode_drops_ptt_and_returns():
    # A decode parked in the executor with PTT asserted must not survive teardown: stop() closes the
    # vocoder first (unblocking the parked exchange), force-drops PTT, and bounds the join — so it
    # returns instead of hanging (the field bug: only a process kill freed PTT).
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        vocoder = _BlockingVocoder(block_after=1)  # 1st decode keys; 2nd parks
        bridge, _ = _bridge(radio, gateway, vocoder, tx_hang=0.05)
        await bridge.start()
        gateway.inject(INBOUND_HEADER)
        for seq in range(2):
            dv = dsrp.build_dv_frame(bytes([seq]) * 9, dsrp.slow_data_for_seq(seq))
            gateway.inject(dsrp.build_data_packet(dv, 0x0777, seq))
        await asyncio.sleep(0.05)
        assert bridge._tx_slot.occupied and vocoder.blocked  # keyed, and a decode is parked
        # stop() must complete promptly (bounded) and leave the transmitter unkeyed.
        await asyncio.wait_for(bridge.stop(), timeout=2.0)
        assert not bridge._tx_slot.occupied
        assert bridge.mode == "idle"
        assert not radio.status().transmitting

    asyncio.run(scenario())


def test_rf_gate_keys_the_reflector_only_on_real_signal():
    # The crossband must not key the reflector on receiver hiss: a below-threshold frame opens nothing;
    # a loud frame opens the over — independent of the global audio.squelch.
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        gate = AudioLevelGate(on_threshold=500.0, off_threshold=300.0, hang=0.01)
        bridge, _ = _bridge(radio, gateway, FakeVocoder(), tx_hang=0.05, rf_gate=gate)
        await bridge.start()
        try:
            for _ in range(3):
                bridge._audio_hub.publish(QUIET_FRAME)  # hiss: gated out
            await asyncio.sleep(0.03)
            assert bridge.mode == "idle"
            assert not any(m.kind is dsrp.MessageKind.HEADER for m in gateway.sent)  # no over opened
            # A real signal opens the over.
            for _ in range(3):
                bridge._audio_hub.publish(LOUD_FRAME)
            await asyncio.sleep(0.03)
            assert bridge.mode == "tx"
            assert any(m.kind is dsrp.MessageKind.HEADER for m in gateway.sent)
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_rx_session_never_releases_a_slot_held_by_another_talker():
    # If a browser TX talker already holds the shared slot, the reflector→RF session must not release
    # it on close (the try_acquire/release accounting fix) — else it frees a slot it never owned.
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        slot = TxSlot()
        assert slot.try_acquire()  # a "browser talker" holds the slot
        bridge, _ = _bridge(radio, gateway, FakeVocoder(), tx_hang=0.05, tx_slot=slot)
        await bridge.start()
        try:
            _inject_over(gateway, 2)
            await asyncio.sleep(0.03)
            # End the reflector over; the slot must STILL be held by the browser talker.
            end_dv = dsrp.build_dv_frame(dsrp.NULL_AMBE)
            gateway.inject(dsrp.build_data_packet(end_dv, 0x0777, 2, end=True))
            await asyncio.sleep(0.03)
            assert bridge.mode == "idle"
            assert slot.occupied  # the browser talker's slot was NOT released by the rx session
        finally:
            await bridge.stop()

    asyncio.run(scenario())


# --- ADR 0092: a parked decode can no longer hold PTT (the real-hardware stuck-key) ------------


def test_parked_decode_still_drops_ptt_via_the_independent_watchdog():
    # The real-hardware stuck-key the dummy-load test exposed: a decode WEDGES (parks in the executor)
    # with PTT already asserted. `_reflector_to_rf` is parked in that await, so its loop-top idle check
    # can NEVER run — only the INDEPENDENT `_rx_watchdog` task (on the event loop, never in the
    # executor) can drop PTT. No teardown here: the over must close on its own while the decode is
    # still parked. Pre-ADR-0092 this hung keyed until the TOT.
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        vocoder = _BlockingVocoder(block_after=1)  # decode 0 keys+feeds; decode 1 parks
        bridge, _ = _bridge(radio, gateway, vocoder, tx_hang=0.1)
        await bridge.start()
        try:
            gateway.inject(INBOUND_HEADER)
            for seq in range(2):
                dv = dsrp.build_dv_frame(bytes([seq]) * 9, dsrp.slow_data_for_seq(seq))
                gateway.inject(dsrp.build_data_packet(dv, 0x0777, seq))
            await asyncio.sleep(0.04)
            assert bridge._tx_slot.occupied and vocoder.blocked  # keyed, and a decode is PARKED
            # No teardown — the independent watchdog closes the over while the decode stays parked.
            await asyncio.sleep(0.3)  # > tx_hang + a couple of watchdog poll intervals
            assert not bridge._tx_slot.occupied  # slot released → PTT dropped, not a process kill
            assert bridge.mode == "idle"
            assert vocoder.blocked  # the decode is STILL parked — proof the loop could not self-close
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_late_decode_does_not_rekey_a_closed_over():
    # The re-key race (ADR 0092): a decode that finally returns AFTER the watchdog/teardown closed the
    # over would, if fed, RE-KEY the transmitter — a stuck-key by another name. The `_play_ambe` mode
    # guard must drop the stale frame instead.
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        vocoder = _BlockingVocoder(block_after=1)  # decode 0 keys; decode 1 parks until close()
        bridge, _ = _bridge(radio, gateway, vocoder, tx_hang=0.1)
        await bridge.start()
        try:
            gateway.inject(INBOUND_HEADER)
            for seq in range(2):
                dv = dsrp.build_dv_frame(bytes([seq]) * 9, dsrp.slow_data_for_seq(seq))
                gateway.inject(dsrp.build_data_packet(dv, 0x0777, seq))
            await asyncio.sleep(0.04)
            assert bridge._tx_slot.occupied and vocoder.blocked
            await asyncio.sleep(0.3)  # the watchdog closes the over while the decode is still parked
            assert bridge.mode == "idle" and not bridge._tx_slot.occupied
            frames_before = bridge.tx_stats()["rx_frames"]
            # Unblock the parked decode (the dongle's eventual recovery): it returns a valid frame into
            # a CLOSED over — it must NOT re-key.
            vocoder.close()
            await asyncio.sleep(0.05)
            assert bridge.mode == "idle" and not bridge._tx_slot.occupied  # stayed idle
            assert bridge.tx_stats()["rx_frames"] == frames_before  # the late frame was dropped, not fed
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_leaked_operator_over_is_reaped_on_rf_silence():
    # A browser-mic over whose WS dies without calling end_operator_over must not wedge the TX latch:
    # the RF pump's silence path reaps a stale over of any source.
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        gate = AudioLevelGate(on_threshold=500.0, off_threshold=300.0, hang=0.01)
        bridge, _ = _bridge(radio, gateway, FakeVocoder(), tx_hang=0.05, rf_gate=gate)
        await bridge.start()
        try:
            await bridge.send_operator_audio(LOUD_FRAME)  # opens an "op" over
            assert bridge.mode == "tx" and bridge._tx_source == "op"
            # The WS "dies" — end_operator_over is never called. RF stays silent (gated), so the pump's
            # silence path reaps the stale op over past tx_hang.
            await asyncio.sleep(0.15)
            assert bridge.mode == "idle"
        finally:
            await bridge.stop()

    asyncio.run(scenario())


# --- ADR 0097: reflector→RF liveness follows decoded CONTENT + a hard per-over ceiling ----------


def test_rx_gate_dead_air_never_keys_under_continuous_frames():
    # The 2026-07-20 stuck-key: an inbound over whose AMBE decodes to DEAD AIR / garbage-silence, with
    # DATA frames arriving CONTINUOUSLY (the queue never idles, no end-bit). Pre-fix every decoded frame
    # `feed`s and re-stamps the idle deadline, so the watchdog sees a "healthy" over and holds PTT to the
    # TOT. With the content gate, a below-threshold decode never feeds — so it never keys the transmitter.
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        vocoder = LevelVocoder(1)  # decodes to RMS ~1: dead air
        gate = AudioLevelGate(on_threshold=500.0, off_threshold=300.0, hang=0.01)
        bridge, _ = _bridge(radio, gateway, vocoder, tx_hang=0.05, rx_gate=gate)
        await bridge.start()
        try:
            gateway.inject(INBOUND_HEADER)
            # Continuous inbound DATA faster than tx_hang, NO end-bit — the queue never idles.
            for seq in range(30):
                dv = dsrp.build_dv_frame(bytes([seq & 0xFF]) * 9, dsrp.slow_data_for_seq(seq))
                gateway.inject(dsrp.build_data_packet(dv, 0x0777, seq))
                await asyncio.sleep(0.004)
            assert vocoder.decoded >= 10  # frames WERE decoded (still published to the browser monitor)…
            assert not bridge._tx_slot.occupied  # …but dead-air content never keyed the transmitter
            assert radio.tx_log == []  # nothing was fed to RF
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_rx_gate_passes_real_speech_and_closes_on_end():
    # The content gate must not break normal operation: above-threshold decoded audio keys RF, and a
    # clean end-bit closes the over.
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        vocoder = LevelVocoder(8192)  # real-speech level
        gate = AudioLevelGate(on_threshold=500.0, off_threshold=300.0, hang=0.05)
        bridge, _ = _bridge(radio, gateway, vocoder, tx_hang=0.05, rx_gate=gate)
        await bridge.start()
        try:
            _inject_over(gateway, 3)
            await asyncio.sleep(0.03)
            assert bridge._tx_slot.occupied and len(radio.tx_log) >= 1  # keyed on real audio
            end_dv = dsrp.build_dv_frame(dsrp.NULL_AMBE)
            gateway.inject(dsrp.build_data_packet(end_dv, 0x0777, 3, end=True))
            await asyncio.sleep(0.03)
            assert not bridge._tx_slot.occupied and bridge.mode == "idle"
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_max_over_caps_a_continuous_loud_over_regardless_of_content():
    # The content-independent backstop: a continuous LOUD stream (the level gate stays open and idle is
    # continuously refreshed by fresh feeds, so NEITHER the gate NOR the idle watchdog can close it) is
    # still force-ended at the hard per-over ceiling — the case the level gate can't catch (loud garbage).
    # Uses the fake clock: the watchdog wakes on real sleeps but decides against the injected clock.
    async def scenario():
        clock = FakeClock()
        radio, gateway = MockRadio(), MockGatewayClient()
        vocoder = LevelVocoder(8192)  # loud → the gate never idles it out
        gate = AudioLevelGate(on_threshold=500.0, off_threshold=300.0, hang=10.0, clock=clock)
        bridge, _ = _bridge(
            radio, gateway, vocoder, tx_hang=0.05, rx_gate=gate, max_over=0.2, clock=clock
        )
        await bridge.start()
        try:
            gateway.inject(INBOUND_HEADER)
            # Keep feeding fresh loud frames while stepping the clock in <tx_hang increments: the idle
            # deadline is continuously refreshed (idle_elapsed can NEVER be true, gap stays 0.03 < 0.05),
            # so the ONLY thing that can close this over is the hard per-over ceiling.
            for seq in range(10):
                dv = dsrp.build_dv_frame(bytes([seq & 0xFF]) * 9, dsrp.slow_data_for_seq(seq))
                gateway.inject(dsrp.build_data_packet(dv, 0x0777, seq))
                await asyncio.sleep(0.01)  # let the loop decode + feed (stamps last_active = clock)
                clock.advance(0.03)  # < tx_hang, so idle never trips
                await asyncio.sleep(0.03)  # let the watchdog run and check the ceiling
                if not bridge._tx_slot.occupied:
                    break
            # 10 * 0.03 = 0.30 fake-sec elapsed > max_over 0.2 → the ceiling force-ended the over.
            assert not bridge._tx_slot.occupied
            assert bridge.mode == "idle"
        finally:
            await bridge.stop()

    asyncio.run(scenario())


# --- ADR 0098: ordered streaming decode over a pipelined vocoder --------------------------------


class _PipelineStream:
    """Models the AMBE2000 decode pipeline for a bridge test: buffers ``latency`` frames (priming),
    then emits one per :meth:`decode` in order; :meth:`flush` drains the buffered tail. Each frame's
    value encodes the input identity so a test can assert order + no drops after keying."""

    def __init__(self, latency: int) -> None:
        self._latency = latency
        self._buf: collections.deque = collections.deque()
        self.closed = False

    def decode(self, ambe: bytes) -> list[AudioFrame]:
        val = (ambe[0] + 1) * 100  # nonzero, distinct, ascending with the input sequence
        self._buf.append(AudioFrame(val.to_bytes(2, "little", signed=True) * 160, PCM_FORMAT))
        out = []
        while len(self._buf) > self._latency:  # past the pipeline depth → clock one out, in order
            out.append(self._buf.popleft())
        return out

    def flush(self) -> list[AudioFrame]:
        out = list(self._buf)
        self._buf.clear()
        return out

    def close(self) -> None:
        self.closed = True


class PipelinedFakeVocoder(FakeVocoder):
    """A FakeVocoder that offers the ordered streaming-decode capability with a pipeline of depth L."""

    def __init__(self, latency: int = 5) -> None:
        super().__init__()
        self._latency = latency

    def open_decode_stream(self) -> _PipelineStream:
        return _PipelineStream(self._latency)


def test_streaming_decode_keys_every_frame_in_order_end_to_end():
    # The regression that would have caught the garbled crossband: through a pipelined vocoder (L=5),
    # every inbound AMBE frame of an over must be keyed onto RF exactly once, IN ORDER — the priming
    # frames buffer, the tail is recovered by the flush at over end (ADR 0098). No drops, no reorder.
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        voc = PipelinedFakeVocoder(latency=5)
        bridge, _ = _bridge(radio, gateway, voc, tx_hang=0.2)
        assert bridge._decode_streaming is False  # set on start() from the vocoder capability
        await bridge.start()
        try:
            assert bridge._decode_streaming is True  # PipelinedFakeVocoder opts into streaming
            n = 12
            gateway.inject(INBOUND_HEADER)
            for seq in range(n):
                dv = dsrp.build_dv_frame(bytes([seq]) * 9, dsrp.slow_data_for_seq(seq))
                gateway.inject(dsrp.build_data_packet(dv, 0x0777, seq, end=(seq == n - 1)))
                await asyncio.sleep(0.005)
            await asyncio.sleep(0.15)  # let the decode executor drain and the end-bit flush run

            def mean(frame):
                a = array.array("h")
                a.frombytes(frame.samples)
                return sum(a) / len(a)

            means = [mean(f) for f in radio.tx_log]
            assert len(means) == n  # every frame keyed — none dropped, tail flushed
            assert means == sorted(means)  # in input order (the FIFO preserves ordering)
            assert all(m > 0 for m in means)  # no silence holes — a dropped frame would read ~0
            assert bridge.mode == "idle"  # the over closed cleanly after the flush
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_streaming_decode_stream_closed_on_over_end_and_teardown():
    # A fresh stream per over; it is closed on the clean end-bit path (and never leaks on teardown).
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        voc = PipelinedFakeVocoder(latency=2)
        bridge, _ = _bridge(radio, gateway, voc, tx_hang=0.2)
        await bridge.start()
        try:
            gateway.inject(INBOUND_HEADER)
            await asyncio.sleep(0.02)
            assert bridge._rx_decode_stream is not None  # opened on the header
            for seq in range(4):
                dv = dsrp.build_dv_frame(bytes([seq]) * 9, dsrp.slow_data_for_seq(seq))
                gateway.inject(dsrp.build_data_packet(dv, 0x0777, seq, end=(seq == 3)))
                await asyncio.sleep(0.005)
            await asyncio.sleep(0.1)
            assert bridge._rx_decode_stream is None  # closed when the over ended
            assert bridge.mode == "idle"
        finally:
            await bridge.stop()

    asyncio.run(scenario())


# --- ADR 0099: the crossband must fail safe when the DV Dongle wedges ------------------------


class _WedgingStream:
    """A streaming decode that emits ``ok`` real frames then RAISES on every later decode/flush —
    models the DV Dongle wedging mid-over (write timeout / dead reader) on the ADR 0098 streaming path.
    It fails FAST (raises at once, never blocks), so the over must end via the independent watchdog."""

    def __init__(self, ok: int) -> None:
        self._ok = ok
        self.decoded = 0
        self.closed = False

    def decode(self, ambe: bytes):
        self.decoded += 1
        if self.decoded > self._ok:
            raise RuntimeError("write timeout (wedged)")
        val = (ambe[0] + 1) * 100
        return [AudioFrame(val.to_bytes(2, "little", signed=True) * 160, PCM_FORMAT)]

    def flush(self):
        raise RuntimeError("write timeout (wedged)")

    def close(self) -> None:
        self.closed = True


class _WedgingVocoder(FakeVocoder):
    """A streaming FakeVocoder whose per-over decode stream wedges after ``ok`` frames (ADR 0099)."""

    def __init__(self, ok: int) -> None:
        super().__init__()
        self._ok = ok
        self.streams: list[_WedgingStream] = []

    def open_decode_stream(self) -> _WedgingStream:
        stream = _WedgingStream(self._ok)
        self.streams.append(stream)
        return stream


def test_wedged_stream_ends_the_over_and_unkeys_via_the_watchdog():
    # ADR 0099: the decode stream keys on its first frame, then WEDGES (every later decode raises). The
    # bridge must not sit keyed on dead air — the independent watchdog closes the over even though the
    # streaming decode is throwing, and the stream is closed. This is the re-proof failure, unit-caught.
    async def scenario():
        clock = FakeClock()
        radio, gateway = MockRadio(), MockGatewayClient()
        voc = _WedgingVocoder(ok=1)  # frame 0 keys; every later decode wedges (raises)
        bridge, _ = _bridge(radio, gateway, voc, tx_hang=0.05, clock=clock)
        await bridge.start()
        try:
            gateway.inject(INBOUND_HEADER)
            dv = dsrp.build_dv_frame(b"\x00" * 9, dsrp.slow_data_for_seq(0))
            gateway.inject(dsrp.build_data_packet(dv, 0x0777, 0))  # decodes → keys, stamps last_active
            await asyncio.sleep(0.02)
            assert bridge._tx_slot.occupied  # keyed onto RF
            clock.advance(0.2)  # the decode wedges while inbound DATA keeps flowing
            for seq in range(1, 6):
                dv = dsrp.build_dv_frame(bytes([seq]) * 9, dsrp.slow_data_for_seq(seq))
                gateway.inject(dsrp.build_data_packet(dv, 0x0777, seq))
                await asyncio.sleep(0.005)
            assert not bridge._tx_slot.occupied  # the watchdog dropped PTT despite the wedge
            assert bridge.mode == "idle"
        finally:
            await bridge.stop()
        assert voc.streams[0].closed  # the wedged stream was closed on teardown

    asyncio.run(scenario())


class _SlowCloseVocoder(FakeVocoder):
    """``close()`` parks until released — models ``DVDongleVocoder.close()`` stuck behind a live
    ``_recover`` holding ``_io_lock``. Teardown must drop PTT FIRST and run close OFF the event loop, so
    the unkey never waits on it (pre-ADR-0099 this stalled the unkey ~15 s, keyed until SIGKILL)."""

    def __init__(self) -> None:
        super().__init__()
        self.close_entered = threading.Event()
        self._release = threading.Event()

    def close(self) -> None:
        self.close_entered.set()
        self._release.wait(timeout=5.0)  # bounded so a broken test can't hang forever


def test_teardown_drops_ptt_before_and_independent_of_a_slow_vocoder_close():
    # ADR 0099: PTT must be down before the (possibly wedged) vocoder close is even entered, and the
    # close must not block the event loop. Prove the slot is released while close() is still parked.
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        voc = _SlowCloseVocoder()
        bridge, _ = _bridge(radio, gateway, voc, tx_hang=0.05)
        await bridge.start()
        gateway.inject(INBOUND_HEADER)
        dv = dsrp.build_dv_frame(b"\x01" * 9, dsrp.slow_data_for_seq(0))
        gateway.inject(dsrp.build_data_packet(dv, 0x0777, 0))
        await asyncio.sleep(0.03)
        assert bridge._tx_slot.occupied  # keyed
        stop_task = asyncio.create_task(bridge.stop())
        # close() runs off the loop; wait until it is entered (parked), then assert PTT is ALREADY down.
        for _ in range(200):
            if voc.close_entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert voc.close_entered.is_set()  # close is now parked
        assert not bridge._tx_slot.occupied  # PTT dropped BEFORE close proceeded (force_unkey ran first)
        assert bridge.mode == "idle"
        voc._release.set()  # let close() finish
        await asyncio.wait_for(stop_task, timeout=2.0)  # and stop() returns promptly

    asyncio.run(scenario())


# --- ADR 0102: a held arbiter must not kill the drain loop --------------------------------------


def test_arbiter_conflict_drops_frames_counted_then_recovers_when_freed():
    # The shared arbiter is held by another keyer (browser TX — or stuck from an earlier fault).
    # Pre-ADR 0102 the first decoded frame's session.feed raised ArbiterStateError UNHANDLED inside
    # the drain loop — killing the whole crossband over one contended frame. Now: the frame is
    # dropped and counted, the loop survives, and the over keys up by itself once the arbiter frees.
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        bridge, _ = _bridge(radio, gateway, FakeVocoder())
        await bridge.start()
        try:
            bridge._arbiter.acquire_tx()  # another keyer holds the radio
            gateway.inject(INBOUND_HEADER)
            await asyncio.sleep(0.02)
            for seq in range(3):
                dv = dsrp.build_dv_frame(bytes([seq + 1]) * 9, dsrp.slow_data_for_seq(seq))
                gateway.inject(dsrp.build_data_packet(dv, 0x0777, seq))
            await asyncio.sleep(0.03)
            stats = bridge.tx_stats()
            assert stats["rx_arbiter_conflicts"] >= 1  # contended frames dropped, not fatal
            assert stats["arbiter"] == "transmitting"  # the divergence is now visible in status
            assert stats["mode"] == "rx"  # the bridge's own latch: mid-over
            assert radio.tx_log == []  # nothing keyed while the arbiter was held

            bridge._arbiter.release_tx()  # the other keyer lets go mid-over
            for seq in range(3, 6):
                dv = dsrp.build_dv_frame(bytes([seq + 1]) * 9, dsrp.slow_data_for_seq(seq))
                gateway.inject(dsrp.build_data_packet(dv, 0x0777, seq))
            await asyncio.sleep(0.03)
            assert len(radio.tx_log) >= 1  # the over keyed up on its own — self-healing
            assert bridge.tx_stats()["arbiter"] == "transmitting"  # now it is OUR keying
        finally:
            await bridge.stop()
        assert bridge.tx_stats()["arbiter"] == "idle"  # released on teardown

    asyncio.run(scenario())


# --- ADR 0105: same-stream re-headers must not cut the over -------------------------------------


def test_same_stream_reheader_does_not_cut_the_over():
    # THE 12-fragment shredder (bench 2026-07-20): the gateway re-sends the stream header
    # mid-stream; treating each as a new over unkeyed/re-keyed the radio every ~0.7 s, so FM
    # played almost pure TX lead-in and the browser lost every cut's decode tail. Same session
    # id while an over is open = the same over: absorbed, counted, nothing cut.
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        bridge, _ = _bridge(radio, gateway, FakeVocoder())
        await bridge.start()
        try:
            gateway.inject(INBOUND_HEADER)
            await asyncio.sleep(0.02)
            for seq in range(3):
                dv = dsrp.build_dv_frame(bytes([seq + 1]) * 9, dsrp.slow_data_for_seq(seq))
                gateway.inject(dsrp.build_data_packet(dv, 0x0777, seq))
            await asyncio.sleep(0.02)
            frames_mid = bridge.tx_stats()["rx_frames"]
            assert frames_mid >= 1 and bridge._tx_slot.occupied  # keyed, decoding

            gateway.inject(INBOUND_HEADER)  # the gateway's periodic RE-header: same session 0x0777
            await asyncio.sleep(0.02)
            assert bridge.mode == "rx"  # over still open
            assert bridge._tx_slot.occupied  # radio NEVER unkeyed across the re-header
            for seq in range(3, 6):
                dv = dsrp.build_dv_frame(bytes([seq + 1]) * 9, dsrp.slow_data_for_seq(seq))
                gateway.inject(dsrp.build_data_packet(dv, 0x0777, seq))
            await asyncio.sleep(0.02)
            stats = bridge.tx_stats()
            assert stats["rx_overs"] == 1  # ONE over for the whole stream
            assert stats["rx_reheaders"] == 1  # the re-send was absorbed and counted
            assert stats["rx_frames"] > frames_mid  # frames kept flowing straight through

            end_dv = dsrp.build_dv_frame(dsrp.NULL_AMBE)
            gateway.inject(dsrp.build_data_packet(end_dv, 0x0777, 6, end=True))
            await asyncio.sleep(0.02)
            assert not bridge._tx_slot.occupied and bridge.mode == "idle"
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_new_stream_id_mid_over_flushes_then_opens_a_new_over():
    # A genuine talker change (different session id): the old over must close WITH its decode tail
    # flushed (the bare _end_rx stranded ~latency frames per cut, ADR 0105) and a new over opens.
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        voc = PipelinedFakeVocoder(latency=2)  # a real pipeline: the tail only exits via flush
        bridge, _ = _bridge(radio, gateway, voc)
        await bridge.start()
        try:
            gateway.inject(INBOUND_HEADER)  # stream 0x0777
            await asyncio.sleep(0.02)
            for seq in range(4):
                dv = dsrp.build_dv_frame(bytes([seq + 1]) * 9, dsrp.slow_data_for_seq(seq))
                gateway.inject(dsrp.build_data_packet(dv, 0x0777, seq))
            await asyncio.sleep(0.03)
            frames_before = bridge.tx_stats()["rx_frames"]
            assert frames_before == 2  # 4 in, latency 2 -> 2 out; the tail (2) sits in the pipeline

            gateway.inject(dsrp.build_header_packet(HEADER, 0x0888))  # a DIFFERENT stream takes over
            await asyncio.sleep(0.03)
            stats = bridge.tx_stats()
            assert stats["rx_overs"] == 2  # old over closed, new over open
            assert stats["rx_frames"] == 4  # the old stream's 2-frame TAIL was flushed, not stranded
            assert bridge.mode == "rx"  # the new over is live

            end_dv = dsrp.build_dv_frame(dsrp.NULL_AMBE)
            gateway.inject(dsrp.build_data_packet(end_dv, 0x0888, 0, end=True))
            await asyncio.sleep(0.02)
            assert bridge.mode == "idle"
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_reheader_after_an_idle_cut_relatches_a_fresh_over():
    # _end_rx clears the stream id: if the over was closed (idle/watchdog), a later header with the
    # SAME session id must open a fresh over — "absorb" applies only while an over is actually open.
    async def scenario():
        radio, gateway = MockRadio(), MockGatewayClient()
        bridge, _ = _bridge(radio, gateway, FakeVocoder(), tx_hang=0.05)
        await bridge.start()
        try:
            gateway.inject(INBOUND_HEADER)
            dv = dsrp.build_dv_frame(b"\x01" * 9, dsrp.slow_data_for_seq(0))
            gateway.inject(dsrp.build_data_packet(dv, 0x0777, 0))
            await asyncio.sleep(0.15)  # inbound goes quiet past tx_hang: the over idles out
            assert bridge.mode == "idle"
            gateway.inject(INBOUND_HEADER)  # same id again, but no over is open now
            await asyncio.sleep(0.02)
            assert bridge.mode == "rx"
            assert bridge.tx_stats()["rx_overs"] == 2  # a fresh over, not an absorbed re-header
        finally:
            await bridge.stop()

    asyncio.run(scenario())
