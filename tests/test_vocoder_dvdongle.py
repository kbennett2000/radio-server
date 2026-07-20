"""DVDongleVocoder tests against a faked serial device (ADR 0086) — no hardware.

A ``FakeDongle`` models the DV Dongle's request/response protocol: it answers the name/start/stop
handshake and, for each encode/decode exchange, streams back a canned AMBE-result + audio-result
pair. This proves the handshake sequence, the per-frame codec surface, the fail-loud guards, and the
missing-pyserial path — all without a real dongle. RF and hardware never enter pytest.
"""

from __future__ import annotations

import threading

import pytest

from radio_server.audio import AudioFormatMismatch, AudioFrame
from radio_server.audio.format import CANONICAL_FORMAT
from radio_server.vocoder import frames as F
from radio_server.vocoder.base import (
    AMBE_BYTES_PER_FRAME,
    PCM_BYTES_PER_FRAME,
    PCM_FORMAT,
    Vocoder,
    VocoderTimeout,
    VocoderUnavailable,
)
from radio_server.vocoder import dvdongle
from radio_server.vocoder.dvdongle import DVDongleVocoder

_CANNED_AMBE = bytes(range(1, AMBE_BYTES_PER_FRAME + 1))
_CANNED_PCM = bytes((i * 3) % 256 for i in range(PCM_BYTES_PER_FRAME))


class FakeDongle:
    """A programmable serial stand-in speaking the DV Dongle protocol."""

    def __init__(self, *, answer_name=True, answer_start=True, answer_exchange=True):
        self._cond = threading.Condition()
        self._out = bytearray()
        self.written = bytearray()
        self._dec = F.DvDongleDecoder()
        self._closed = False
        self.close_calls = 0
        self._answer_name = answer_name
        self._answer_start = answer_start
        self._answer_exchange = answer_exchange
        self.canned_ambe = _CANNED_AMBE
        self.canned_pcm = _CANNED_PCM
        self.requests: list[str] = []
        self.last_ambe_request_voice: bytes | None = None

    def factory(self, port, baud):  # matches the (port, baud) -> Serial-like seam
        return self

    # --- device -> host ---
    def _emit(self, data):  # caller holds self._cond
        self._out += data
        self._cond.notify_all()

    def read(self, n):
        with self._cond:
            if not self._out and not self._closed:
                self._cond.wait(timeout=0.05)
            if not self._out:
                return b""
            chunk = bytes(self._out[:n])
            del self._out[:n]
            return chunk

    # --- host -> device ---
    def write(self, data):
        with self._cond:
            self.written += data
            for packet in self._dec.feed(bytes(data)):
                self._handle(packet)

    def _handle(self, packet):
        raw = packet.raw
        if raw == F.REQ_NAME:
            self.requests.append("name")
            if self._answer_name:
                self._emit(F.RESP_NAME)
        elif raw == F.REQ_START:
            self.requests.append("start")
            if self._answer_start:
                self._emit(F.RESP_START)
        elif raw == F.REQ_STOP:
            self.requests.append("stop")
            self._emit(F.RESP_STOP)
        elif packet.type_bits == F.TYPE_AMBE:
            self.last_ambe_request_voice = F.ambe_voice_frame(packet)  # config packet of the pair
        elif packet.type_bits == F.TYPE_AUDIO:
            self.requests.append("exchange")
            if self._answer_exchange:
                self._emit(F.build_ambe_packet(self._ambe_result()))
                self._emit(F.build_audio_packet(self.canned_pcm))

    def _ambe_result(self):
        payload = bytearray(F.AMBE_ENC_PARAMS)
        payload[F.AMBE_VOICE_OFFSET : F.AMBE_VOICE_OFFSET + F.VOICE_FRAME_LEN] = self.canned_ambe
        return bytes(payload)

    def close(self):
        with self._cond:
            self.close_calls += 1
            self._closed = True
            self._cond.notify_all()


def _pcm_frame(fill=1) -> AudioFrame:
    return AudioFrame(bytes([fill]) * PCM_BYTES_PER_FRAME, PCM_FORMAT)


def test_handshake_queries_name_then_starts():
    fake = FakeDongle()
    voc = DVDongleVocoder(_serial_factory=fake.factory)
    try:
        assert fake.requests[:2] == ["name", "start"]
        assert isinstance(voc, Vocoder)  # structural conformance to the seam
    finally:
        voc.close()


def test_no_name_response_fails_loud():
    fake = FakeDongle(answer_name=False)
    with pytest.raises(VocoderUnavailable) as exc:
        DVDongleVocoder(_serial_factory=fake.factory, handshake_timeout=0.2)
    assert "handshake" in str(exc.value).lower()


def test_no_start_response_fails_loud():
    fake = FakeDongle(answer_start=False)
    with pytest.raises(VocoderUnavailable):
        DVDongleVocoder(_serial_factory=fake.factory, handshake_timeout=0.2)


def test_encode_sends_config_plus_audio_and_returns_ambe():
    fake = FakeDongle()
    voc = DVDongleVocoder(_serial_factory=fake.factory)
    try:
        ambe = voc.encode(_pcm_frame(fill=7))
        assert ambe == _CANNED_AMBE
        # The encode config packet (no voice frame) and the audio packet were both written.
        assert fake.last_ambe_request_voice == bytes(F.VOICE_FRAME_LEN)  # zero voice region
        assert bytes([7]) * PCM_BYTES_PER_FRAME in bytes(fake.written)
        assert "exchange" in fake.requests
    finally:
        voc.close()


def test_decode_splices_ambe_and_returns_pcm_frame():
    fake = FakeDongle()
    voc = DVDongleVocoder(_serial_factory=fake.factory)
    try:
        frame = voc.decode(_CANNED_AMBE)
        assert frame.format == PCM_FORMAT
        assert frame.samples == _CANNED_PCM
        # The AMBE the host sent carried our voice frame at the splice offset.
        assert fake.last_ambe_request_voice == _CANNED_AMBE
    finally:
        voc.close()


def test_sequential_exchanges_stay_in_sync():
    fake = FakeDongle()
    voc = DVDongleVocoder(_serial_factory=fake.factory)
    try:
        for fill in (1, 2, 3):
            assert voc.encode(_pcm_frame(fill=fill)) == _CANNED_AMBE
    finally:
        voc.close()


def test_encode_rejects_wrong_format():
    fake = FakeDongle()
    voc = DVDongleVocoder(_serial_factory=fake.factory)
    try:
        with pytest.raises(AudioFormatMismatch):
            voc.encode(AudioFrame(bytes(PCM_BYTES_PER_FRAME), CANONICAL_FORMAT))
    finally:
        voc.close()


def test_encode_rejects_wrong_length():
    fake = FakeDongle()
    voc = DVDongleVocoder(_serial_factory=fake.factory)
    try:
        with pytest.raises(AudioFormatMismatch):
            voc.encode(AudioFrame(bytes(PCM_BYTES_PER_FRAME - 2), PCM_FORMAT))
    finally:
        voc.close()


def test_decode_rejects_wrong_ambe_length():
    fake = FakeDongle()
    voc = DVDongleVocoder(_serial_factory=fake.factory)
    try:
        with pytest.raises(ValueError):
            voc.decode(bytes(AMBE_BYTES_PER_FRAME - 1))
    finally:
        voc.close()


def test_exchange_once_times_out_when_no_reply():
    # The timeout PRIMITIVE, deterministically (fake clock, no real waiting). `_exchange` wraps this
    # with a recover-and-retry (see the ADR 0094 tests below); `_exchange_once` is the bare frame.
    fake = FakeDongle(answer_exchange=False)
    # connect=False skips the handshake; a fake clock jumps past the deadline so no real waiting.
    clock = iter([0.0, 100.0, 200.0]).__next__
    voc = DVDongleVocoder(
        _serial_factory=fake.factory, connect=False, reply_timeout=0.2, _clock=clock
    )
    try:
        with pytest.raises(VocoderTimeout):
            voc._exchange_once([F.build_audio_packet(bytes(PCM_BYTES_PER_FRAME))])
    finally:
        voc.close()


# --- ADR 0094: recover a wedged/asleep dongle by close+reopen+re-handshake ---------------------


class _SeqFactory:
    """A ``(port, baud) -> Serial-like`` factory that hands out a fixed sequence of ``FakeDongle``s —
    one per open — so a test can model the construct-open then the recovery reopen(s)."""

    def __init__(self, dongles):
        self._dongles = list(dongles)
        self.opens = 0

    def factory(self, port, baud):
        dongle = self._dongles[min(self.opens, len(self._dongles) - 1)]
        self.opens += 1
        return dongle


def test_exchange_recovers_a_wedged_dongle_by_reopening():
    # The AMBE2000 sleeps after idle and stops answering (VocoderTimeout); a close+reopen+re-handshake
    # wakes it (bench-proven). The exchange must recover once and complete on the reopened transport.
    wedged = FakeDongle(answer_exchange=False)  # handshakes, but never answers a codec exchange
    healthy = FakeDongle()
    seq = _SeqFactory([wedged, healthy])
    voc = DVDongleVocoder(_serial_factory=seq.factory, reply_timeout=0.15, handshake_timeout=0.5)
    try:
        assert seq.opens == 1  # opened the wedged dongle at construction (its handshake answered)
        pcm = voc.decode(bytes(AMBE_BYTES_PER_FRAME))  # times out -> recover -> healthy answers
        assert len(pcm.samples) == PCM_BYTES_PER_FRAME
        assert seq.opens == 2  # reopened exactly once for recovery
        assert wedged.close_calls >= 1  # the wedged transport was closed during recovery
        assert "start" in healthy.requests  # the reopened dongle was re-handshaked
    finally:
        voc.close()


def test_recovery_retries_a_flaky_reopen_handshake():
    # The first reopen can hit the dongle's flaky first-open (name OK, start drops), exactly like cold
    # bring-up. Recovery retries the reopen a few times before giving up.
    wedged = FakeDongle(answer_exchange=False)
    flaky = FakeDongle(answer_start=False)  # reopen #1: name OK, start drops -> handshake fails
    healthy = FakeDongle()
    seq = _SeqFactory([wedged, flaky, healthy])
    voc = DVDongleVocoder(_serial_factory=seq.factory, reply_timeout=0.15, handshake_timeout=0.15)
    try:
        pcm = voc.decode(bytes(AMBE_BYTES_PER_FRAME))
        assert len(pcm.samples) == PCM_BYTES_PER_FRAME
        assert seq.opens == 3  # construct(wedged) + recover try1(flaky) + recover try2(healthy)
    finally:
        voc.close()


def test_exchange_propagates_when_the_dongle_stays_wedged():
    # Recovery re-handshakes fine but the chip still won't answer a frame: retry once, then propagate
    # the timeout (no infinite recover loop). The ADR 0092/0093 safety net handles PTT above this.
    seq = _SeqFactory([FakeDongle(answer_exchange=False), FakeDongle(answer_exchange=False)])
    voc = DVDongleVocoder(_serial_factory=seq.factory, reply_timeout=0.12, handshake_timeout=0.5)
    try:
        with pytest.raises(VocoderTimeout):
            voc.decode(bytes(AMBE_BYTES_PER_FRAME))
        assert seq.opens == 2  # recovered once (handshake OK), the retried exchange still timed out
    finally:
        voc.close()


def test_recovery_raises_unavailable_when_the_dongle_never_comes_back():
    # If every reopen fails to handshake, recovery gives up with VocoderUnavailable (a dead dongle).
    dead = [FakeDongle(answer_name=False) for _ in range(dvdongle._RECOVER_HANDSHAKE_ATTEMPTS)]
    seq = _SeqFactory([FakeDongle(answer_exchange=False), *dead])
    voc = DVDongleVocoder(_serial_factory=seq.factory, reply_timeout=0.1, handshake_timeout=0.1)
    try:
        with pytest.raises(VocoderUnavailable):
            voc.decode(bytes(AMBE_BYTES_PER_FRAME))
        assert seq.opens == 1 + dvdongle._RECOVER_HANDSHAKE_ATTEMPTS  # construct + N failed reopen attempts
    finally:
        voc.close()


def test_close_is_idempotent():
    fake = FakeDongle()
    voc = DVDongleVocoder(_serial_factory=fake.factory)
    voc.close()
    voc.close()  # second call must be a no-op, never raise
    assert fake.close_calls == 1


def test_missing_pyserial_fails_loud(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "serial":
            raise ImportError("no pyserial in this environment")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(VocoderUnavailable) as exc:
        dvdongle._load_serial()
    assert "hardware" in str(exc.value)


# --- ADR 0098: ordered streaming decode over the pipelined chip ------------------------------


class PipelinedFakeDongle(FakeDongle):
    """A FakeDongle whose DECODE output is delayed by ``latency`` frames — models the AMBE2000
    pipeline. The PCM it emits for a decode encodes the input AMBE's identity (its first byte) so a
    test can assert the streaming decode returns frames in order with none dropped."""

    def __init__(self, latency=5):
        super().__init__()
        self._latency = latency
        self._pending: list[bytes] = []  # AMBE identities in the pipeline, awaiting emit

    def _handle(self, packet):
        raw = packet.raw
        if raw in (F.REQ_NAME, F.REQ_START, F.REQ_STOP):
            super()._handle(packet)
            return
        if packet.type_bits == F.TYPE_AMBE:
            self.last_ambe_request_voice = F.ambe_voice_frame(packet)  # the AMBE being decoded
        elif packet.type_bits == F.TYPE_AUDIO:
            self.requests.append("exchange")
            self._pending.append(self.last_ambe_request_voice or bytes(AMBE_BYTES_PER_FRAME))
            if len(self._pending) > self._latency:  # past the pipeline depth → clock one out, in order
                ident = self._pending.pop(0)
                self._emit(F.build_ambe_packet(self._ambe_result()))  # echo (ignored by decode stream)
                self._emit(F.build_audio_packet(bytes([ident[0]]) * PCM_BYTES_PER_FRAME))


def test_decode_stream_returns_frames_in_order_without_dropping():
    # The pipelined chip delays decode output by L frames and its replies arrive bursty; the ordered
    # FIFO must hand every frame back exactly once, in input order (ADR 0098) — the flush drains the
    # in-flight tail. (The legacy per-frame decode() mis-pairs/drops under the same pipeline.)
    fake = PipelinedFakeDongle(latency=5)
    voc = DVDongleVocoder(_serial_factory=fake.factory, decode_latency_frames=8)
    try:
        stream = voc.open_decode_stream()
        got = []
        n = 12
        for seq in range(1, n + 1):  # identities 1..12 (nonzero so a dropped→silence frame is visible)
            got.extend(stream.decode(bytes([seq]) * AMBE_BYTES_PER_FRAME))
        got.extend(stream.flush())
        stream.close()
        idents = [f.samples[0] for f in got]
        assert idents[:n] == list(range(1, n + 1))  # all 12 real frames, in order, none dropped
        assert all(f.format == PCM_FORMAT and len(f.samples) == PCM_BYTES_PER_FRAME for f in got)
    finally:
        voc.close()


def test_open_decode_stream_makes_it_a_streaming_vocoder():
    from radio_server.vocoder.base import StreamingVocoder

    fake = FakeDongle()
    voc = DVDongleVocoder(_serial_factory=fake.factory)
    try:
        assert isinstance(voc, StreamingVocoder)  # opts into the ordered streaming-decode capability
    finally:
        voc.close()
