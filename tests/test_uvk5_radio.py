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
import time

import pytest

from radio_server.audio import CANONICAL_FORMAT, AudioFormat, AudioFormatMismatch, AudioFrame
from radio_server.backends.base import Capability, RadioStatus, UnsupportedCapability
from radio_server.backends.uvk5.frames import (
    ReadRegisters,
    Uvk5Decoder,
    WriteRegisters,
    parse_frame,
)
from radio_server.backends.uvk5.radio import (
    _REG30_TX_ENABLED,
    Uvk5KeyingError,
    Uvk5Radio,
    _block_rms,
)
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


# --- ALSA capture overrun (xrun) is now VISIBLE, not silently discarded (ADR 0125) -------


class _OverflowInputStream(FakeInputStream):
    """A capture stream that reports an ALSA overrun on every read — the flag receive() discarded.

    ``overflow`` is togglable so a test can show the reader catching up.
    """

    overflow = True
    #: Seconds each read takes. A real capture read blocks for one block period, so consecutive
    #: reads are naturally ~21 ms apart at 1024/48000 — that cadence is *healthy*, and an overrun
    #: reported at it is the card's own recovery, not a stall (ADR 0130/0132). To model a reader
    #: that has genuinely fallen behind, the gap between reads has to be longer than that.
    read_delay = 0.05

    def read(self, frames):
        self.reads += 1
        if self.read_delay:
            time.sleep(self.read_delay)
        return b"\x00\x00" * frames, self.overflow  # (silence, OVERFLOWED?)


class _OverflowAudio(FakeAudio):
    def RawInputStream(self, **kw):
        stream = _OverflowInputStream(**kw)
        self.inputs.append(stream)
        return stream


def test_overruns_draining_a_known_read_gap_are_not_reported_as_a_stall(caplog):
    """An overrun is only a fault if somebody was actually reading.

    Reading legitimately stops in several places — a keyed over (half-duplex blinds RX by design,
    ADR 0017), the demand-driven pump halting when the last listener leaves, a freshly opened
    stream, a restart. Counting those made the warning a *transmission* counter rather than a
    health signal. The backlog takes several reads to drain, so the allowance spans reads — but it
    is bounded, and a clean read ends it (the next test).
    """
    from radio_server.backends.uvk5.radio import _XRUN_DRAIN_READS

    fake = FirmwareFakeSerial()
    radio = make_radio(fake, _audio=_OverflowAudio())
    try:
        with caplog.at_level(logging.WARNING):
            for _ in range(_XRUN_DRAIN_READS):  # the whole post-gap drain, every read overrunning
                radio.receive()
        assert not [r for r in caplog.records if "xrun" in r.getMessage()]
    finally:
        radio.close()


def test_a_clean_read_ends_the_drain_allowance(caplog):
    """Catching up is the signal that the backlog is gone; a later overrun is then a real fault."""
    fake = FirmwareFakeSerial()
    audio = _OverflowAudio()
    radio = make_radio(fake, _audio=audio)
    try:
        radio.receive()  # opens the stream and starts the drain allowance
        audio.inputs[0].overflow = False
        radio.receive()  # a clean read: caught up, so stop excusing
        audio.inputs[0].overflow = True
        with caplog.at_level(logging.WARNING):
            radio.receive()
        assert [r for r in caplog.records if "xrun" in r.getMessage() and r.levelno == logging.WARNING]
    finally:
        radio.close()


def test_an_overrun_at_the_readers_own_cadence_is_not_called_a_stall(caplog):
    """The residue ADR 0130 chased, and the reason it mattered.

    An overrun reported one block period after the previous read cannot be the reader falling
    behind — the reader was exactly on time. ADR 0130 measured these on the bench and found no
    audio cost at all (100.4-100.8 % duty, whole over received, tone 1.000). The warning text
    nevertheless asserted "the RX reader fell behind the card and audio was dropped", which is
    false, and the acceptance runner counts that warning — so the false claim became a false
    verdict: a run with every direct audio measure perfect failed on this proxy alone.
    """
    fake = FirmwareFakeSerial()
    audio = _OverflowAudio()
    radio = make_radio(fake, _audio=audio)
    try:
        radio.receive()  # opens the stream; starts the drain allowance
        audio.inputs[0].overflow = False
        audio.inputs[0].read_delay = 0.0  # from here on the reader is punctual
        radio.receive()  # clean read ends the allowance
        audio.inputs[0].overflow = True
        with caplog.at_level(logging.INFO):
            radio.receive()
        msgs = [r for r in caplog.records if "xrun" in r.getMessage()]
        assert msgs, "an overrun must still be visible"
        assert all(r.levelno == logging.INFO for r in msgs)  # ... but not as a stall
        assert "on cadence" in msgs[0].getMessage()
    finally:
        radio.close()


def test_capture_overrun_logs_a_rate_limited_warning(caplog):
    # The overrun flag was discarded before ADR 0125, which is exactly why the RX-pump starvation
    # (ring overrunning continuously) left "zero xruns" in the journal. Now it logs — once per window.
    from radio_server.backends.uvk5.radio import _XRUN_DRAIN_READS

    fake = FirmwareFakeSerial()
    radio = make_radio(fake, _audio=_OverflowAudio())
    try:
        with caplog.at_level(logging.WARNING):
            # Past the post-gap drain allowance, so these are genuine stall overruns, back to back
            # and well inside one warn window.
            for _ in range(_XRUN_DRAIN_READS + 5):
                radio.receive()
        xruns = [r for r in caplog.records if "xrun" in r.getMessage() and r.levelno == logging.WARNING]
        assert len(xruns) == 1  # rate-limited: many overruns → ONE warning, not fifty/sec
        assert "ADR 0125" in xruns[0].getMessage()
        # The audio itself is untouched — the samples read on an xrun are still returned.
        assert radio.receive().samples == b"\x00\x00" * radio._blocksize
    finally:
        radio.close()


def test_no_overrun_logs_nothing(caplog):
    # A healthy read (not overflowed) must stay silent — no warning noise on the normal path.
    fake = FirmwareFakeSerial()
    radio = make_radio(fake, _audio=_LoudAudio())  # _LoudInputStream reports overflowed=False
    try:
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                radio.receive()
        assert not [r for r in caplog.records if "xrun" in r.getMessage()]
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
            # VHF band bit (freq10 < 28_000_000) over a reg33 seed of 0, plus RX_ENABLE: a tune
            # asserts the receiving shape rather than replaying the seed (ADR 0132).
            (0x33, 0x9000 | 0b100 | 0x40),
            (0x30, 0),
            (0x30, 0xBFF1),  # the fake's stock RX word, seeded through
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
        assert (0x33, 0x9000 | 0b1000 | 0x40) in reg_writes(fake)  # UHF band bit, receiving
    finally:
        radio.close()


def test_connecting_to_a_radio_left_with_the_receiver_off_repairs_it(caplog):
    """A radio found at `0x30 = 0` must not have that adopted as the RX model.

    `_reg30` is written back to un-key and at the end of every `set_frequency`, and was seeded from
    a bare read at connect — so a radio found in a bad state stayed bad for the whole process.
    Reproduced on the bench: writing `0x30 = 0` drops reg-0x67 RSSI from 157 to 0, and a service
    started against that radio reported `rssi 0 / busy false` and passed no audio, indefinitely.
    """
    fake = FirmwareFakeSerial()
    fake.registers[0x30] = 0  # where a lost un-key leaves it: nothing enabled, receiver off
    with caplog.at_level(logging.WARNING):
        radio = make_radio(fake)
    try:
        assert radio._reg30 == 0xBFF1  # the measured stock RX word, not the damage
        assert fake.registers[0x30] == 0xBFF1  # ... and it was WRITTEN, so the radio is repaired
        assert any("not a receiving state" in r.getMessage() for r in caplog.records)
    finally:
        radio.close()


def test_connecting_to_a_transmitting_radio_does_not_adopt_the_tx_word():
    """The other value that cannot be an RX word. Adopting it would write TX-enable on every
    retune — the one thing ADR 0112 exists to prevent."""
    fake = FirmwareFakeSerial()
    fake.registers[0x30] = _REG30_TX_ENABLED
    radio = make_radio(fake)
    try:
        assert radio._reg30 == 0xBFF1
    finally:
        radio.close()


def test_a_healthy_rx_word_is_left_exactly_alone():
    fake = FirmwareFakeSerial()
    fake.registers[0x30] = 0x2A28  # plausible, TX bit clear
    radio = make_radio(fake)
    try:
        assert radio._reg30 == 0x2A28
    finally:
        radio.close()


def test_a_gpio_byte_with_no_output_enables_is_repaired(caplog):
    """Half a fix is not a fix — and this one shipped.

    The upper bits of reg 0x33 are the pin output-enables; the firmware initialises its shadow to
    `0x9000` and only ever ORs pin bits into it (`bk4829.c:198`, `bk4829.c:434-442`). A radio found
    disabled reads the register as 0 — the same state `_seed_reg30` exists to repair — and the
    receiving shape was *computed* from that seed, producing `0x0040`: RX_ENABLE asserted on a pin
    driver that is switched off. The bench proved it: the reg-0x30 repair fired and `/status.rssi`
    stayed at 0 anyway.
    """
    fake = FirmwareFakeSerial()
    fake.registers[0x33] = 0  # a disabled radio: no enables, no pins
    with caplog.at_level(logging.WARNING):
        radio = make_radio(fake)
    try:
        radio.set_frequency(147_555_000)
        assert fake.registers[0x33] & 0x9000 == 0x9000  # the enables are back
        assert fake.registers[0x33] & 0x40  # ... so asserting RX_ENABLE means something
        assert fake.registers[0x33] & 0x0C == 0x04  # and the band bit still lands
        assert any("output-enable" in r.getMessage() for r in caplog.records)
    finally:
        radio.close()


def test_the_keyed_shape_also_carries_the_output_enables():
    fake = ForceTxFake()
    radio = make_radio(fake)
    try:
        radio.set_frequency(147_555_000)
        radio.ptt(True)
        assert fake.registers[0x33] & 0x9000 == 0x9000
        assert fake.registers[0x33] & 0x20  # PA rail up on a driver that is actually enabled
    finally:
        radio.close()


def test_retuning_asserts_the_receiving_shape_of_the_gpio_byte():
    """Same fault class as `_seed_reg30`, on reg 0x33: seeded mid-transmission, the model would
    carry RX_ENABLE clear and the PA rail up, and every tune would write that back."""
    fake = FirmwareFakeSerial()
    fake.registers[0x33] = 0x9020  # PA rail up, RX_ENABLE clear — a radio caught keyed
    radio = make_radio(fake)
    try:
        radio.set_frequency(145_500_000)
        assert fake.registers[0x33] & 0x40  # receiving
        assert not fake.registers[0x33] & 0x20  # PA rail down
        assert fake.registers[0x33] & 0x0C == 0x04  # on the right band
    finally:
        radio.close()


def test_retuning_back_to_uhf_leaves_only_the_uhf_lna_selected():
    """VHF -> UHF must not leave BOTH LNA paths enabled.

    The old clear-mask (`& 0xFFE7`) cleared bits 3-4 while the VHF selection sets bit 2, so the VHF
    bit was never cleared and a return to UHF produced `0x0C` — both paths on. Neither existing band
    test could catch it: both start from a `reg33` seed of 0, where the stale bit does not exist yet.
    """
    fake = FirmwareFakeSerial()
    radio = make_radio(fake)
    try:
        radio.set_frequency(145_500_000)
        assert fake.registers[0x33] & 0x0C == 0x04  # VHF only
        radio.set_frequency(446_000_000)
        assert fake.registers[0x33] & 0x0C == 0x08  # UHF only — not 0x0C
        radio.set_frequency(145_500_000)
        assert fake.registers[0x33] & 0x0C == 0x04  # and back, still exactly one
    finally:
        radio.close()


def test_retuning_preserves_the_firmware_bits_of_the_gpio_byte():
    """reg 0x33 carries the firmware's own GPIO state (audio-amp gate, LEDs) alongside the band
    bits. A retune selects a band; it does not get to clear bits it does not own."""
    fake = FirmwareFakeSerial()
    fake.registers[0x33] = 0x9052  # RX_ENABLE + 0x10 + 0x02, i.e. bits we must not touch
    radio = make_radio(fake)
    try:
        radio.set_frequency(145_500_000)
        assert fake.registers[0x33] == 0x9056  # ... 0x10 and 0x02 survive, VHF bit added
    finally:
        radio.close()


def test_retuning_while_keyed_is_refused_rather_than_dropping_the_carrier():
    """`set_frequency`'s write list ends with the RX reg-0x30 word, which would un-key mid-over."""
    fake = FirmwareFakeSerial()
    fake.registers[0x30] = 0x2000
    radio = make_radio(fake)
    try:
        radio.set_frequency(145_500_000)
        radio.ptt(True)
        with pytest.raises(Uvk5KeyingError):
            radio.set_frequency(146_520_000)
        assert fake.registers[0x30] == _REG30_TX_ENABLED  # still keyed, still on air
        assert radio.status().frequency == 145_500_000  # and still says where it really is
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
        # ... last of the keying batch, after the AF path and the tone. What follows it is the
        # band correction, which by construction has to land AFTER the firmware reacts to this
        # very edge (ADR 0132) — so the invariant is "nothing in the batch after TX enable",
        # not "nothing at all after TX enable".
        band = pairs.index((0x30, 0xC1FE))
        assert all(reg in (0x33, 0x36) for reg, _ in pairs[band + 1:])
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
        # The fail-safe restored RX: the un-key pair, then the receive band path (ADR 0132).
        assert reg_writes(fake)[-3:] == [
            (0x30, 0),
            (0x30, radio._reg30),
            (0x33, radio._rx_reg33()),
        ]
    finally:
        radio.close()


def test_key_up_tolerates_the_firmware_settle_window_before_confirming():
    """The dock firmware rewrites reg 0x30 mid-key-up; one read-back races it.

    On the F5 build the 0x30 TX edge triggers `Dock_ForceTx`, whose `BK4819_PrepareTransmit()`
    writes `REG_30 = 0` part-way through its own PA sequence and then sits in ~25 ms of settle
    delays. A single read into that window truthfully returns the RX word for a transmitter that
    is coming up fine. This is the "first-key settle flake" F4 saw and ADR 0126 carried forward;
    it failed a live service announcement with an HTTP 500 during the ADR 0129 acceptance runs.
    """

    class SettlingFake(FirmwareFakeSerial):
        """Reports the RX word for the first two read-backs, then the real TX value."""

        def __init__(self):
            super().__init__()
            self._settle = 2

        def write(self, data: bytes) -> int:
            n = super().write(data)
            if self.registers.get(0x30) == 0xC1FE and self._settle:
                self.registers[0x30] = 0xBFF1  # the firmware's transient, mid-PA-sequence
            return n

        def read(self, size: int = 1) -> bytes:
            if self.registers.get(0x30) == 0xBFF1 and self._settle:
                self._settle -= 1
                if not self._settle:
                    self.registers[0x30] = 0xC1FE  # settled: the transmitter really is up
            return super().read(size)

    fake = SettlingFake()
    radio = make_radio(fake)
    try:
        radio.set_frequency(445_800_000)
        radio.ptt(True)  # must NOT raise — it retries into the settle window
        assert radio.status().transmitting is True
    finally:
        radio.close()


class ForceTxFake(FirmwareFakeSerial):
    """A radio whose firmware runs `Dock_ForceTx` off the reg-0x30 TX edge — from its OWN VFO.

    This is the F5 build measured on the bench (ADR 0128/0132): on key-up it sets the PA up with
    the gain byte and LNA path for `gCurrentVfo->pTX->Frequency`, which is the frequency showing on
    the radio's screen — NOT the one the host tuned via reg 0x38/0x39. Defaults to a UHF VFO, which
    is what the bench K6 was left on.
    """

    def __init__(self, vfo_gain: int = 0xA2, vfo_lna: int = 0x08, bias: int = 12) -> None:
        super().__init__()
        self.vfo_gain, self.vfo_lna, self.bias = vfo_gain, vfo_lna, bias
        self.registers[0x33] = 0x9048  # the bench radio's idle GPIO byte: RX_ENABLE + UHF LNA
        self._was_keyed = False

    def write(self, data: bytes) -> int:
        n = super().write(data)
        # Edge-triggered, like the firmware: `dock_set_tx` fires on the reg-0x30 TX bit changing
        # (dock.c:61-68), not on every write that happens while keyed. Getting this wrong would
        # hide the whole fix, since the host's correction lands in a later write.
        keyed = self.registers.get(0x30) == _REG30_TX_ENABLED
        if keyed and not self._was_keyed:  # Dock_ForceTx (uart.c:724-734)
            self.registers[0x36] = (self.bias << 8) | 0x80 | self.vfo_gain
            self.registers[0x33] = (self.registers.get(0x33, 0) & ~0x4C) | 0x20 | self.vfo_lna
        elif self._was_keyed and not keyed:  # Dock_EndTx (uart.c:740-746): PA down, RX back...
            self.registers[0x36] = 0
            self.registers[0x33] = (self.registers.get(0x33, 0) & ~0x20) | 0x40
            # ... but the LNA path is left exactly where Dock_ForceTx put it. That is the bug.
        self._was_keyed = keyed
        return n


def test_key_up_repoints_the_pa_at_the_tuned_band_not_the_radios_own_vfo():
    """The measured VHF fault: carrier at 147.555, PA set up for UHF.

    `Dock_ForceTx` takes its band from `gCurrentVfo` and never tunes the synthesiser, so on a host
    frequency in the other band the firmware wins on the gain byte and the LNA path while the
    carrier really is where the host put it. Bench read-back keyed on 147.555: reg 0x36 = 0x0CA2
    (UHF gain) and reg 0x33 with the UHF LNA bit.
    """
    fake = ForceTxFake()  # radio's own VFO is UHF
    radio = make_radio(fake)
    try:
        radio.set_frequency(147_555_000)  # ... but the host tunes VHF
        radio.ptt(True)
        # The bias the firmware computed is kept (the host cannot read the calibration it came
        # from); only the gain byte is corrected.
        assert fake.registers[0x36] == (12 << 8) | 0x80 | 0x08
        assert fake.registers[0x33] & 0x0C == 0x04  # VHF LNA, not UHF
        assert fake.registers[0x33] & 0x20  # PA rail still up
        assert not fake.registers[0x33] & 0x40  # RX still disabled
        assert fake.registers[0x30] == _REG30_TX_ENABLED  # and still keyed
    finally:
        radio.close()


def test_key_up_leaves_the_firmwares_pa_alone_when_it_already_agrees():
    """Same band as the radio's VFO — the firmware got it right, so do not rewrite reg 0x36."""
    fake = ForceTxFake()  # UHF VFO
    radio = make_radio(fake)
    try:
        radio.set_frequency(445_800_000)  # ... and a UHF host frequency
        fake.writes.clear()
        radio.ptt(True)
        assert not [p for p in reg_writes(fake) if p[0] == 0x36]
        assert fake.registers[0x36] == (12 << 8) | 0x80 | 0xA2  # untouched, as the firmware set it
    finally:
        radio.close()


def test_key_down_puts_the_receive_band_path_back():
    """`Dock_EndTx` restores RX_ENABLE but leaves the LNA where `Dock_ForceTx` pointed it.

    Nothing else rewrites reg 0x33, so before this the receiver came back deaf after the first
    transmission and stayed that way until the next `set_frequency`. Bench read-back on 147.555:
    0x9046 (VHF LNA) before keying, 0x9048 (UHF LNA) after.
    """
    fake = ForceTxFake()
    radio = make_radio(fake)
    try:
        radio.set_frequency(147_555_000)
        assert fake.registers[0x33] & 0x0C == 0x04
        radio.ptt(True)
        radio.ptt(False)
        assert fake.registers[0x33] & 0x0C == 0x04  # still VHF after an over, not stuck on UHF
        assert fake.registers[0x33] & 0x40  # and receiving
    finally:
        radio.close()


def test_a_dropped_pa_read_back_leaves_the_over_on_air():
    """The band correction is a refinement, not a gate. Losing its read must not fail the over —
    that would trade a weaker signal for dead air, the wrong direction under ADR 0112."""
    from radio_server.backends.uvk5.transport import Uvk5Timeout

    fake = ForceTxFake()
    radio = make_radio(fake)
    real_read = radio._read_register

    def flaky(reg: int) -> int:
        if reg == 0x36:
            raise Uvk5Timeout("no matching reply within 2.0s")
        return real_read(reg)

    radio._read_register = flaky
    try:
        radio.set_frequency(147_555_000)
        radio.ptt(True)
        assert radio.status().transmitting is True
        assert fake.registers[0x30] == _REG30_TX_ENABLED
    finally:
        radio._read_register = real_read
        radio.close()


def test_key_up_retries_a_dropped_read_back_rather_than_failing_the_over():
    """A read request lost in the firmware's busy window must not fail the transmission.

    The dock's full-control loop is single-threaded and blocking: while it is inside
    `Dock_ForceTx` it is not servicing its UART, so a read sent into that window is *dropped* and
    the transport burns its whole timeout. On the bench that surfaced as
    `Uvk5Timeout: no matching reply within 2.0s` and an HTTP 500 with nothing on air.
    """
    from radio_server.backends.uvk5.transport import Uvk5Timeout

    fake = FirmwareFakeSerial()
    radio = make_radio(fake)
    calls = {"n": 0}
    real_read = radio._read_register

    def flaky(reg: int) -> int:
        calls["n"] += 1
        if reg == 0x30 and calls["n"] == 1:
            raise Uvk5Timeout("no matching reply within 2.0s")
        return real_read(reg)

    radio._read_register = flaky
    try:
        radio.set_frequency(445_800_000)
        radio.ptt(True)  # first read-back is swallowed by the firmware; the retry gets through
        assert radio.status().transmitting is True
    finally:
        radio._read_register = real_read
        radio.close()


def test_a_dropped_rssi_read_holds_the_squelch_state_instead_of_reporting_clear():
    """`busy` drives the CAT squelch gate, so answering "clear" on a dropped poll chops the over.

    The dock's full-control loop is single-threaded: a poll landing while it is busy elsewhere is
    lost and the transport waits out its whole timeout. Reporting not-busy then closes the RX gate
    on a transmission that is actually in progress — measured on the bench as 4.19 s of a 6.0 s
    over arriving, with no other symptom. A missing measurement is not evidence of silence.
    """
    from radio_server.backends.uvk5.radio import _BUSY_HOLD_READS
    from radio_server.backends.uvk5.transport import Uvk5Timeout

    fake = FirmwareFakeSerial()
    fake.registers[0x67] = 0x1FF  # a wide-open, unmistakably busy channel
    radio = make_radio(fake)
    try:
        assert radio.status().busy is True  # established from a real read

        real_read = radio._read_register

        def dropped(reg: int) -> int:
            if reg == 0x67:
                raise Uvk5Timeout("no matching reply within 2.0s")
            return real_read(reg)

        radio._read_register = dropped
        for _ in range(_BUSY_HOLD_READS):
            assert radio.status().busy is True  # held, not slammed shut
        # Bounded: a link that never answers must not latch the gate open for ever.
        assert radio.status().busy is False
    finally:
        radio._read_register = real_read
        radio.close()


def test_key_up_resends_the_write_when_a_dock_frame_is_lost():
    """A lost *write* cannot be recovered by re-reading — the key-up itself has to be re-sent.

    Dock writes are fire-and-forget over a link the firmware stops servicing while it is inside
    `Dock_ForceTx`. When the write is the casualty, reg 0x30 reads back as the RX word forever.
    On the bench that failed a live logout announcement even after five read-backs.
    """
    fake = FirmwareFakeSerial()
    radio = make_radio(fake)
    swallowed = {"done": False}
    real_write = radio._write_registers

    def lossy(pairs):
        pairs = list(pairs)
        if not swallowed["done"] and any(reg == 0x30 and val for reg, val in pairs):
            swallowed["done"] = True  # drop the first key-up frame entirely, as the link would
            return
        return real_write(pairs)

    radio._write_registers = lossy
    try:
        radio.set_frequency(445_800_000)
        radio.ptt(True)  # attempt 1 is lost; attempt 2 gets through
        assert radio.status().transmitting is True
        assert swallowed["done"] is True  # the loss really was exercised
    finally:
        radio._write_registers = real_write
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
        assert reg_writes(fake) == [
            (0x30, 0),
            (0x30, radio._reg30),
            (0x33, radio._rx_reg33()),  # ... and the receive band path back with it (ADR 0132)
        ]
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
