"""Tests for ``Uvk5Radio`` — the UV-K5 Quansheng Dock CatRadio backend (ADR 0112).

Hardware-free: the class drives an injected ``Uvk5Transport`` over the cycle-2
``FirmwareFakeSerial`` (which already models the firmware's accept/CRC rules and a BK4819
register file — 0x0850 writes land, 0x0851 reads serve). Assertions are **byte-exact register
sequences** for tune / tone / mode / key, decoded off the wire with the real codec, checked
against the pinned BK4819.cs mapping. The load-bearing test proves key-up **raises** when the
radio withholds TX confirmation — a silent no-key never becomes dead air.
"""

from __future__ import annotations

import logging

import pytest

from radio_server.audio import CANONICAL_FORMAT, AudioFormat, AudioFormatMismatch, AudioFrame
from radio_server.backends.base import Capability, RadioStatus, UnsupportedCapability
from radio_server.backends.uvk5.frames import (
    ReadRegisters,
    Uvk5Decoder,
    WriteRegisters,
    parse_frame,
)
from radio_server.backends.uvk5.radio import Uvk5KeyingError, Uvk5Radio, _block_rms
from radio_server.backends.uvk5.transport import Uvk5Transport

from tests.test_aioc_baofeng import FakeAudio, FakeInputStream
from tests.test_uvk5_transport import FirmwareFakeSerial


def make_radio(fake: FirmwareFakeSerial, **kwargs) -> Uvk5Radio:
    """Build a Uvk5Radio over the firmware-accurate fake serial + a fake AIOC sound card.

    ``ptt(True)`` / ``transmit()`` open a playout stream now, so a fake ``_audio`` is injected
    unless the caller supplies one; read ``radio._audio_mod.outputs`` for playback assertions. The
    TX lead-in defaults to **0** here (not the backend's 0.5 s) so register/playback assertions see
    only the caller's frames; the lead-in tests pass an explicit ``tx_lead_seconds``.
    """
    kwargs.setdefault("_audio", FakeAudio())
    kwargs.setdefault("tx_lead_seconds", 0.0)
    kwargs.setdefault("_enter_settle_s", 0.0)  # no real sleep in the EnterHwMode verify (ADR 0122)
    transport = Uvk5Transport(_serial_factory=lambda port, baud: fake)
    return Uvk5Radio(_transport=transport, **kwargs)


def a_frame(nsamples: int = 4) -> AudioFrame:
    return AudioFrame(b"\x01\x02" * nsamples, CANONICAL_FORMAT)


def _drain(radio, timeout: float = 2.0) -> None:
    """Wait for the keying's pacer to finish writing everything queued (writes land off-thread)."""
    pacer = radio._pacer
    if pacer is not None:
        assert pacer.wait_drained(timeout)


def _lead(seconds: float) -> bytes:
    return b"\x00" * (round(CANONICAL_FORMAT.rate * seconds) * CANONICAL_FORMAT.frame_bytes)


def written(fake: FirmwareFakeSerial) -> list:
    """Decode every frame the transport has written to the fake into typed messages."""
    dec = Uvk5Decoder(obfuscated=True, validate_crc=False)
    out = []
    for frame in fake.writes:
        for payload in dec.feed(frame):
            out.append(parse_frame(payload))
    return out


def reg_writes(fake: FirmwareFakeSerial) -> list[tuple[int, int]]:
    """The flat ``(register, value)`` sequence of every WriteRegisters written since last clear."""
    pairs: list[tuple[int, int]] = []
    for msg in written(fake):
        if isinstance(msg, WriteRegisters):
            pairs.extend(msg.registers)
    return pairs


# ---------------------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------------------


def test_construct_enters_full_control_and_seeds_from_readback():
    fake = FirmwareFakeSerial()
    fake.registers.update({0x30: 0x1A1A, 0x33: 0x0007, 0x38: 0x0FB0, 0x39: 0x00DE})
    radio = make_radio(fake)
    try:
        assert fake.full_control is True  # 0x0870 was sent and accepted
        # Seeded frequency = ((0x00DE << 16) | 0x0FB0) * 10 Hz.
        assert radio.status().frequency == ((0x00DE << 16) | 0x0FB0) * 10
    finally:
        radio.close()


def test_close_unkeys_exits_full_control_and_is_idempotent():
    fake = FirmwareFakeSerial()
    radio = make_radio(fake)
    radio.close()
    assert fake.full_control is False  # 0x0871 returned the radio to standalone
    radio.close()  # idempotent, no raise


# --- first-start dead-RX: the EnterHwMode verify/retry (radio leg, ADR 0122) -------------


def test_enter_hw_mode_healthy_sends_once():
    # F3 build: the first 0x0870 runs the firmware force-open (REG_47 → FM), so verify confirms on the
    # first send — no re-send, no warning.
    fake = FirmwareFakeSerial()  # f3=True by default
    radio = make_radio(fake)
    try:
        assert fake.enter_hw_count == 1  # exactly one 0x0870 — no retry on a healthy start
        assert fake.full_control is True
        assert fake.registers[0x47] == 0x6142  # AF=FM/unmute — the force-open ran
    finally:
        radio.close()


def test_enter_hw_mode_retries_a_dropped_first_0870(caplog):
    # The boot race: the first 0x0870 is lost, so the firmware force-open never runs (REG_47 stays
    # mute). The verify sees REG_47 not FM and RE-SENDS; the second 0x0870 lands and RX comes alive.
    fake = FirmwareFakeSerial()
    fake.drop_enter_hw = 1  # lose exactly the first 0x0870, as a reset-on-open race would
    with caplog.at_level(logging.WARNING):
        radio = make_radio(fake)
    try:
        assert fake.enter_hw_count == 2  # the re-send happened
        assert fake.full_control is True
        assert fake.registers[0x47] == 0x6142  # REG_47 reads alive after the retry
        assert not any(r.levelno >= logging.WARNING for r in caplog.records)  # recovered → no warning
    finally:
        radio.close()


def test_enter_hw_mode_on_non_f3_is_a_bounded_noop_with_warning(caplog):
    # A pre-F3 dock never sets REG_47 (no firmware force-open), so verify can never confirm. It must be
    # BOUNDED — exhaust the retries, log a warning, and return (never hang, never claim a false fix).
    fake = FirmwareFakeSerial()
    fake.f3 = False
    fake.registers[0x47] = 0x6042  # idle mute — the FM bit never sets on a pre-F3 build
    with caplog.at_level(logging.WARNING):
        radio = make_radio(fake)
    try:
        from radio_server.backends.uvk5.radio import _ENTER_HW_MODE_RETRIES

        assert fake.enter_hw_count == _ENTER_HW_MODE_RETRIES  # bounded — did not loop forever
        assert fake.full_control is True  # the 0x0870 did land; only REG_47 never came alive
        assert any(
            "did not confirm open" in r.getMessage() and r.levelno == logging.WARNING
            for r in caplog.records
        )
    finally:
        radio.close()


# --- first-start dead-RX: the capture reopen-on-floor (host-audio leg, ADR 0122) ---------


class _LoudInputStream(FakeInputStream):
    """A capture stream that reads a loud (well-above-floor) block — models a settled USB device."""

    def read(self, frames):
        self.reads += 1
        return b"\x00\x40" * frames, False  # 0x4000 per sample → RMS 16384, far above the floor


class _SettlingAudio(FakeAudio):
    """The first capture stream reads floor (device still USB-settling); every later one reads loud."""

    def RawInputStream(self, **kw):
        stream = FakeInputStream(**kw) if not self.inputs else _LoudInputStream(**kw)
        self.inputs.append(stream)
        return stream


class _LoudAudio(FakeAudio):
    """Every capture stream reads loud from the first block (a healthy first-open)."""

    def RawInputStream(self, **kw):
        stream = _LoudInputStream(**kw)
        self.inputs.append(stream)
        return stream


def test_block_rms_matches_the_floor_boundary():
    assert _block_rms(b"\x00\x00" * 480) == 0.0  # silence
    assert _block_rms(b"\x00\x40" * 480) == pytest.approx(16384.0)  # loud, above the 50.0 floor


def test_capture_reopens_once_on_a_floor_first_block():
    fake = FirmwareFakeSerial()
    audio = _SettlingAudio()
    radio = make_radio(fake, _audio=audio, capture_reopen_on_floor=True)
    try:
        frame = radio.receive()
        assert len(audio.inputs) == 2  # floor first block → reopened once
        assert audio.inputs[0].closed  # the settling stream was torn down
        assert _block_rms(frame.samples) > 1000  # the returned audio came from the reopened stream
    finally:
        radio.close()


def test_capture_does_not_reopen_when_the_first_block_is_healthy():
    fake = FirmwareFakeSerial()
    audio = _LoudAudio()
    radio = make_radio(fake, _audio=audio, capture_reopen_on_floor=True)
    try:
        radio.receive()
        assert len(audio.inputs) == 1  # healthy first block → the stream is kept, no reopen
    finally:
        radio.close()


def test_capture_reopen_is_off_by_default_receive_is_unchanged():
    # Guards the byte-identical default: no probe read, exactly one stream, one read per receive().
    fake = FirmwareFakeSerial()
    audio = _LoudAudio()
    radio = make_radio(fake, _audio=audio)  # capture_reopen_on_floor defaults False
    try:
        radio.receive()
        assert len(audio.inputs) == 1
        assert audio.inputs[0].reads == 1  # no extra probe read — receive() is byte-identical
    finally:
        radio.close()


# ---------------------------------------------------------------------------------------
# Tuning — byte-exact sequences + fail-loud units
# ---------------------------------------------------------------------------------------


def test_set_frequency_vhf_writes_exact_sequence():
    fake = FirmwareFakeSerial()  # reg30/reg33 seed to 0
    radio = make_radio(fake)
    try:
        fake.writes.clear()
        radio.set_frequency(145_500_000)
        freq10 = 145_500_000 // 10
        assert reg_writes(fake) == [
            (0x38, freq10 & 0xFFFF),
            (0x39, (freq10 >> 16) & 0xFFFF),
            (0x33, 0b100),  # VHF band bit (freq10 < 28_000_000), reg33 seed 0
            (0x30, 0),
            (0x30, 0),  # reg30 seed 0
        ]
        assert radio.status().frequency == 145_500_000
    finally:
        radio.close()


def test_set_frequency_uhf_sets_the_other_band_bit():
    fake = FirmwareFakeSerial()
    radio = make_radio(fake)
    try:
        fake.writes.clear()
        radio.set_frequency(446_000_000)
        assert (0x33, 0b1000) in reg_writes(fake)  # UHF band bit
    finally:
        radio.close()


def test_set_frequency_rejects_off_raster_and_out_of_band():
    fake = FirmwareFakeSerial()
    radio = make_radio(fake)
    try:
        with pytest.raises(ValueError):
            radio.set_frequency(145_500_005)  # not a multiple of 10 Hz — never rounded
        with pytest.raises(ValueError):
            radio.set_frequency(5_000_000)  # below the band
        with pytest.raises(ValueError):
            radio.set_frequency(2_000_000_000)  # above the band
    finally:
        radio.close()


# ---------------------------------------------------------------------------------------
# Mode / tone
# ---------------------------------------------------------------------------------------


def test_set_mode_maps_to_bandwidth_register():
    fake = FirmwareFakeSerial()
    radio = make_radio(fake)
    try:
        fake.writes.clear()
        radio.set_mode("FM")
        assert reg_writes(fake) == [(0x43, 18856)]
        assert radio.status().mode == "FM"
        fake.writes.clear()
        radio.set_mode("nfm")
        assert reg_writes(fake) == [(0x43, 18440)]
        assert radio.status().mode == "NFM"
    finally:
        radio.close()


def test_set_mode_rejects_unknown():
    fake = FirmwareFakeSerial()
    radio = make_radio(fake)
    try:
        with pytest.raises(ValueError):
            radio.set_mode("AM")
    finally:
        radio.close()


def test_set_tone_encodes_ctcss_and_none_disables():
    fake = FirmwareFakeSerial()
    radio = make_radio(fake)
    try:
        fake.writes.clear()
        radio.set_tone(88.5)
        code = ((round(88.5 * 10) * 206488) + 50000) // 100000
        assert reg_writes(fake) == [(0x51, 0x904A), (0x07, code)]
        assert radio.status().tone == 88.5
        fake.writes.clear()
        radio.set_tone(None)
        assert reg_writes(fake) == [(0x51, 0)]
        assert radio.status().tone is None
    finally:
        radio.close()


def test_set_tone_rejects_out_of_range():
    fake = FirmwareFakeSerial()
    radio = make_radio(fake)
    try:
        with pytest.raises(ValueError):
            radio.set_tone(50.0)  # below the CTCSS band
        with pytest.raises(ValueError):
            radio.set_tone(300.0)  # above it
    finally:
        radio.close()


# ---------------------------------------------------------------------------------------
# Keying — confirmation or raise
# ---------------------------------------------------------------------------------------


def test_key_up_writes_tx_enable_confirms_and_reports_transmitting():
    fake = FirmwareFakeSerial()
    fake.registers[0x30] = 0x2000  # a plausible RX value, seeded into reg30
    radio = make_radio(fake)
    try:
        radio.set_frequency(145_500_000)
        radio.set_tone(None)
        fake.writes.clear()
        radio.ptt(True)
        pairs = reg_writes(fake)
        assert (0x30, 0xC1FE) in pairs  # TX enable was written
        assert pairs[-1] == (0x30, 0xC1FE)  # ... last, after PA/tone
        assert radio.status().transmitting is True
    finally:
        radio.close()


def test_key_up_raises_and_restores_rx_when_confirmation_withheld():
    class NoKeyFake(FirmwareFakeSerial):
        """A radio that refuses to key: reg 0x30 never latches the TX-enable value."""

        def __init__(self):
            super().__init__()
            self.registers[0x30] = 0x2000  # fixed RX value

        def write(self, data: bytes) -> int:
            n = super().write(data)
            self.registers[0x30] = 0x2000  # never leaves RX, whatever was written
            return n

    fake = NoKeyFake()
    radio = make_radio(fake)
    try:
        radio.set_frequency(145_500_000)
        with pytest.raises(Uvk5KeyingError):
            radio.ptt(True)
        assert radio.status().transmitting is False  # left un-keyed
        # The fail-safe restored RX: the final write was (0x30, reg30).
        assert reg_writes(fake)[-1] == (0x30, radio._reg30)
    finally:
        radio.close()


def test_key_down_restores_rx_unconditionally():
    fake = FirmwareFakeSerial()
    fake.registers[0x30] = 0x2000
    radio = make_radio(fake)
    try:
        radio.set_frequency(145_500_000)
        radio.ptt(True)
        fake.writes.clear()
        radio.ptt(False)
        assert reg_writes(fake) == [(0x30, 0), (0x30, radio._reg30)]
        assert radio.status().transmitting is False
    finally:
        radio.close()


# ---------------------------------------------------------------------------------------
# Status / busy / capabilities
# ---------------------------------------------------------------------------------------


def test_busy_reflects_rssi_threshold():
    fake = FirmwareFakeSerial()
    radio = make_radio(fake, squelch_threshold=40)
    try:
        fake.registers[0x67] = 100  # RSSI above threshold
        assert radio.status().busy is True
        fake.registers[0x67] = 10  # below threshold
        assert radio.status().busy is False
    finally:
        radio.close()


def test_capabilities_and_unsupported_channel():
    fake = FirmwareFakeSerial()
    radio = make_radio(fake)
    try:
        caps = radio.capabilities()
        assert Capability.SET_FREQUENCY in caps
        assert Capability.SET_TONE in caps
        assert Capability.SET_MODE in caps
        assert Capability.SCAN in caps
        assert Capability.SET_CHANNEL not in caps
        with pytest.raises(UnsupportedCapability):
            radio.set_channel(3)
    finally:
        radio.close()


def test_scan_toggle_raises_but_cap_gates_software_engine():
    fake = FirmwareFakeSerial()
    radio = make_radio(fake)
    try:
        with pytest.raises(NotImplementedError):
            radio.scan(True)
        # SCAN is still advertised (it gates the software ScanEngine via set_frequency + busy).
        assert Capability.SCAN in radio.capabilities()
    finally:
        radio.close()


# ---------------------------------------------------------------------------------------
# Audio — receive / transmit over the shared soundcard seam (ADR 0113)
# ---------------------------------------------------------------------------------------


def test_receive_lazily_opens_capture_and_returns_canonical_frame():
    fake = FirmwareFakeSerial()
    radio = make_radio(fake, blocksize=480)
    try:
        assert radio._audio_mod.inputs == []  # not opened until first receive
        frame = radio.receive()
        assert isinstance(frame, AudioFrame)
        assert frame.format == CANONICAL_FORMAT
        assert len(frame.samples) == 480 * CANONICAL_FORMAT.frame_bytes
        assert len(radio._audio_mod.inputs) == 1 and radio._audio_mod.inputs[0].started
        radio.receive()
        assert len(radio._audio_mod.inputs) == 1  # reused, not reopened
    finally:
        radio.close()


def test_transmit_rejects_non_canonical_format():
    fake = FirmwareFakeSerial()
    radio = make_radio(fake)
    try:
        with pytest.raises(AudioFormatMismatch):
            radio.transmit(AudioFrame(b"\x00\x00", AudioFormat(8000, 2, 1)))
    finally:
        radio.close()


def test_one_shot_transmit_self_keys_plays_and_drops():
    fake = FirmwareFakeSerial()
    fake.registers[0x30] = 0x2000  # TX-enable write must latch 0xC1FE for the confirm
    radio = make_radio(fake)
    try:
        radio.set_frequency(145_500_000)
        frame = a_frame()
        radio.transmit(frame)  # one-shot: self-keys, plays (blocking until drained), unkeys
        out = radio._audio_mod.outputs[-1]
        assert out.written == [frame.samples]  # tx_lead_seconds=0 -> only the clip
        assert out.stopped and out.closed  # drained + torn down
        assert radio.status().transmitting is False  # key dropped after the clip
    finally:
        radio.close()


def test_one_shot_transmit_writes_lead_in_silence_before_audio():
    fake = FirmwareFakeSerial()
    fake.registers[0x30] = 0x2000
    radio = make_radio(fake, tx_lead_seconds=0.02)
    try:
        radio.set_frequency(145_500_000)
        frame = a_frame()
        radio.transmit(frame)
        out = radio._audio_mod.outputs[-1]
        # The silent lead-in plays first (radio keys up during it), then the real clip.
        assert out.written == [_lead(0.02), frame.samples]
    finally:
        radio.close()


def test_streaming_holds_one_stream_across_frames():
    fake = FirmwareFakeSerial()
    fake.registers[0x30] = 0x2000
    radio = make_radio(fake)
    try:
        radio.set_frequency(145_500_000)
        radio.ptt(True)  # key-up opens exactly one playout stream
        assert radio.status().transmitting is True
        assert len(radio._audio_mod.outputs) == 1

        f1, f2 = a_frame(2), a_frame(3)
        radio.transmit(f1)
        radio.transmit(f2)
        _drain(radio)  # writes land on the pacer thread
        # Same single stream got both frames — the key was NOT dropped between them.
        assert len(radio._audio_mod.outputs) == 1
        assert radio._audio_mod.outputs[0].written == [f1.samples, f2.samples]
        assert radio.status().transmitting is True

        radio.ptt(False)
        assert radio.status().transmitting is False
        assert radio._audio_mod.outputs[0].stopped and radio._audio_mod.outputs[0].closed
    finally:
        radio.close()


def test_full_sequence_tune_key_transmit_unkey_over_both_fakes():
    # The load-bearing integration: dock serial (register keying, read-back-confirmed) + the AIOC
    # sound card (playout) driven together through one tune -> key -> transmit -> unkey cycle.
    fake = FirmwareFakeSerial()
    fake.registers[0x30] = 0x2000  # RX seed; ptt(True)'s TX-enable must latch 0xC1FE to confirm
    radio = make_radio(fake, tx_lead_seconds=0.02)
    try:
        radio.set_frequency(146_520_000)
        radio.set_tone(None)
        radio.ptt(True)  # register-confirmed key-up (else Uvk5KeyingError)
        assert radio.status().transmitting is True
        assert fake.registers[0x30] == 0xC1FE  # the radio really reported TX enabled

        frame = a_frame(5)
        radio.transmit(frame)
        _drain(radio)
        out = radio._audio_mod.outputs[-1]
        assert out.written == [_lead(0.02), frame.samples]  # lead-in once at key-up, then the frame

        radio.ptt(False)
        assert radio.status().transmitting is False
        assert fake.registers[0x30] == radio._reg30  # RX restored on the wire
        assert out.stopped and out.closed
    finally:
        radio.close()


def test_status_is_a_radiostatus_snapshot():
    fake = FirmwareFakeSerial()
    radio = make_radio(fake)
    try:
        radio.set_frequency(145_500_000)
        st = radio.status()
        assert isinstance(st, RadioStatus)
        assert st.backend == "uvk5"
        assert st.frequency == 145_500_000
        assert st.channel is None
    finally:
        radio.close()


# ---------------------------------------------------------------------------------------
# Constructor: initial tone / mode, and the tx_allowed RF gate (ADR 0114)
# ---------------------------------------------------------------------------------------


def test_construct_applies_initial_tone():
    fake = FirmwareFakeSerial()
    radio = make_radio(fake, frequency=145_500_000, tone=88.5)
    try:
        # set_tone runs at construction: the CTCSS enable + code pair, and status reflects it.
        code = ((round(88.5 * 10) * 206488) + 50000) // 100000
        pairs = reg_writes(fake)
        assert (0x51, 0x904A) in pairs and (0x07, code) in pairs
        assert radio.status().tone == 88.5
    finally:
        radio.close()


def test_construct_applies_initial_mode():
    fake = FirmwareFakeSerial()
    radio = make_radio(fake, frequency=145_500_000, mode="NFM")
    try:
        assert (0x43, 18440) in reg_writes(fake)  # NFM bandwidth (Defines.cs:169-170)
        assert radio.status().mode == "NFM"
    finally:
        radio.close()


def test_construct_rejects_out_of_range_tone():
    fake = FirmwareFakeSerial()
    with pytest.raises(ValueError):
        make_radio(fake, frequency=145_500_000, tone=300.0)  # above the CTCSS band


def test_construct_rejects_unknown_mode():
    fake = FirmwareFakeSerial()
    with pytest.raises(ValueError):
        make_radio(fake, frequency=145_500_000, mode="AM")


def test_construct_without_tone_or_mode_leaves_them_unset():
    fake = FirmwareFakeSerial()
    radio = make_radio(fake, frequency=145_500_000)
    try:
        assert radio.status().tone is None
        assert radio.status().mode is None
        pairs = reg_writes(fake)
        assert not any(reg in (0x51, 0x07, 0x43) for reg, _ in pairs)  # no tone/mode writes
    finally:
        radio.close()


def test_tx_allowed_false_refuses_ptt_and_keys_nothing():
    fake = FirmwareFakeSerial()
    radio = make_radio(fake, frequency=145_500_000, tx_allowed=False)
    try:
        fake.writes.clear()
        with pytest.raises(Uvk5KeyingError):
            radio.ptt(True)
        assert radio.status().transmitting is False
        # RF-safety: the refusal happens before any audio open or TX-enable write.
        assert radio._audio_mod.outputs == []  # no playout stream opened
        assert (0x30, 0xC1FE) not in reg_writes(fake)  # TX enable never written
    finally:
        radio.close()


def test_tx_allowed_false_refuses_one_shot_transmit():
    fake = FirmwareFakeSerial()
    radio = make_radio(fake, frequency=145_500_000, tx_allowed=False)
    try:
        with pytest.raises(Uvk5KeyingError):
            radio.transmit(a_frame())
        assert radio.status().transmitting is False
        assert radio._audio_mod.outputs == []
    finally:
        radio.close()


def test_tx_allowed_true_keys_normally():
    fake = FirmwareFakeSerial()
    radio = make_radio(fake, frequency=145_500_000, tx_allowed=True)
    try:
        radio.ptt(True)
        assert radio.status().transmitting is True
        assert (0x30, 0xC1FE) in reg_writes(fake)
    finally:
        radio.close()
