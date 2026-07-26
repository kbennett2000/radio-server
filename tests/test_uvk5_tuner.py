"""Both tuners, against fakes that model what the firmware actually does — including its traps.

The fake EEPROM writes in whole 8-byte chunks, exactly like `EEPROM_WriteBuffer`, because that is
the behaviour that turns a careless one-byte patch into seven bytes of collateral damage. And it
starts **erased** (`0xFF` everywhere), because that is the state in which the attribute word makes
the firmware ignore the VFO record entirely — the trap that would make a tune succeed and change
nothing.
"""

from __future__ import annotations

import pytest

from radio_server.backends.base import Capability
from radio_server.backends.uvk5 import frames as f
from radio_server.backends.uvk5.tuner import (
    SESSION_TIMESTAMP,
    EepromTuner,
    SetVfoTuner,
    TuneError,
)
from radio_server.backends.uvk5.transport import Uvk5Timeout
from radio_server.backends.uvk5.vfo import (
    BOOT_INDEX_BLOCK,
    VFO_RECORD_LEN,
    VfoImage,
    attr_addr,
    freq_channel,
    unpack_boot_indices,
    vfo_addr,
)

K0PRA = VfoImage(rx_hz=448_525_000, tx_hz=443_525_000, ctcss_tenths=1000)
BENCH = VfoImage(rx_hz=445_800_000, tx_hz=445_800_000)


# --- setvfo ------------------------------------------------------------------------------------

class FakeSetVfoRadio:
    """Answers 0x0873 with a configurable 0x0874."""

    def __init__(self, *, status=f.SetVfoStatus.APPLIED, rx=None, tx=None, tone=None, silent=False):
        self.status, self.rx, self.tx, self.tone = status, rx, tx, tone
        self.silent = silent
        self.sent: list = []

    def send(self, msg):
        self.sent.append(msg)

    def request(self, msg, match, timeout=None):
        self.sent.append(msg)
        if self.silent:
            # What the real transport does on a deadline — it raises, it does not return None.
            raise Uvk5Timeout("no matching reply within 0.01s")
        reply = f.SetVfoReply(
            status=self.status,
            rx_hz=msg.rx_hz if self.rx is None else self.rx,
            tx_hz=msg.tx_hz if self.tx is None else self.tx,
            ctcss_tenths=msg.ctcss_tenths if self.tone is None else self.tone,
            power=7,
        )
        return reply if match(reply) else None


def test_setvfo_sends_the_channel_and_accepts_a_matching_readback():
    radio = FakeSetVfoRadio()
    SetVfoTuner(radio).apply(K0PRA)
    (sent,) = radio.sent
    assert isinstance(sent, f.SetVfo)
    assert sent.rx_hz == 448_525_000
    assert sent.offset_hz == 5_000_000
    assert sent.direction == f.OFFSET_SUB
    assert sent.ctcss_tenths == 1000
    assert sent.tx_hz == 443_525_000     # the firmware's own arithmetic, mirrored


def test_setvfo_treats_silence_as_the_wrong_firmware():
    """Stock firmware has no 0x0873 case and drops the frame without a word. F6 always answers,
    so silence is diagnostic rather than ambiguous — and the message has to say so."""
    with pytest.raises(TuneError, match="pre-F6"):
        SetVfoTuner(FakeSetVfoRadio(silent=True), timeout=0.01).apply(K0PRA)


@pytest.mark.parametrize("status", [
    f.SetVfoStatus.ERR_BAND,
    f.SetVfoStatus.ERR_TONE,
    f.SetVfoStatus.ERR_BUSY,
    f.SetVfoStatus.ERR_DIRECTION,
    f.SetVfoStatus.ERR_FIELD,
    f.SetVfoStatus.ERR_NO_HAL,
    f.SetVfoStatus.ERR_SHORT,
])
def test_setvfo_raises_on_every_refusal(status):
    with pytest.raises(TuneError, match="refused"):
        SetVfoTuner(FakeSetVfoRadio(status=status)).apply(K0PRA)


def test_setvfo_catches_a_radio_that_landed_somewhere_else():
    """The whole point of the reply: the radio says where it ACTUALLY is."""
    with pytest.raises(TuneError, match="not the requested"):
        SetVfoTuner(FakeSetVfoRadio(rx=446_000_000)).apply(K0PRA)
    with pytest.raises(TuneError, match="not the requested"):
        SetVfoTuner(FakeSetVfoRadio(tx=448_525_000)).apply(K0PRA)


def test_setvfo_catches_a_dropped_tone():
    """On frequency but keying without the tone means the repeater stays shut — the failure that
    looks exactly like the problem being solved."""
    with pytest.raises(TuneError, match="will not open"):
        SetVfoTuner(FakeSetVfoRadio(tone=0)).apply(K0PRA)


def test_setvfo_advertises_the_tuning_capabilities():
    caps = SetVfoTuner(FakeSetVfoRadio()).capabilities()
    assert caps == {Capability.SET_FREQUENCY, Capability.SET_SPLIT,
                    Capability.SET_TONE, Capability.SET_MODE}
    assert Capability.SET_CHANNEL not in caps    # no channel-select exists on this radio


# --- eeprom ------------------------------------------------------------------------------------

class FakeEepromRadio:
    """A byte-addressed EEPROM that writes in 8-byte chunks, like the firmware."""

    def __init__(self, *, size=0x10000):
        self.mem = bytearray(b"\xFF" * size)     # erased flash, which is the dangerous state
        self.resets = 0
        self.hellos = 0
        self.writes: list[tuple[int, bytes]] = []

    def send(self, msg):
        if isinstance(msg, f.Reset):
            self.resets += 1
        elif isinstance(msg, f.Hello):
            self.hellos += 1

    def request(self, msg, match, timeout=None):
        if isinstance(msg, f.EepromRead):
            assert msg.timestamp == SESSION_TIMESTAMP
            data = bytes(self.mem[msg.offset:msg.offset + msg.size])
            reply = f.EepromReadReply(offset=msg.offset, size=len(data), data=data)
        elif isinstance(msg, f.EepromWrite):
            assert msg.timestamp == SESSION_TIMESTAMP
            assert msg.offset % 8 == 0 and len(msg.data) % 8 == 0
            self.mem[msg.offset:msg.offset + len(msg.data)] = msg.data
            self.writes.append((msg.offset, bytes(msg.data)))
            reply = f.EepromWriteReply(offset=msg.offset)
        else:
            return None
        return reply if match(reply) else None


def _tune(radio, image=K0PRA):
    EepromTuner(radio, sleep=lambda _s: None).apply(image)


def test_eeprom_writes_the_record_to_both_vfo_slots():
    radio = FakeEepromRadio()
    _tune(radio)
    band = K0PRA.band
    expected = K0PRA.pack_eeprom()
    assert bytes(radio.mem[vfo_addr(band, 0):vfo_addr(band, 0) + VFO_RECORD_LEN]) == expected
    assert bytes(radio.mem[vfo_addr(band, 1):vfo_addr(band, 1) + VFO_RECORD_LEN]) == expected


def test_eeprom_programs_the_attribute_word_or_the_tune_does_nothing():
    """0xFFFF makes radio.c:302-313 skip the VFO record and boot to the band's lower edge."""
    radio = FakeEepromRadio()
    _tune(radio)
    addr = attr_addr(freq_channel(K0PRA.band))
    assert int.from_bytes(radio.mem[addr:addr + 2], "little") != 0xFFFF


def test_eeprom_selects_frequency_mode_on_the_right_band_for_both_vfos():
    radio = FakeEepromRadio()
    _tune(radio)
    indices = unpack_boot_indices(bytes(radio.mem[BOOT_INDEX_BLOCK:BOOT_INDEX_BLOCK + 16]))
    channel = freq_channel(K0PRA.band)
    assert indices["screen"] == (channel, channel)
    assert indices["freq"] == (channel, channel)


def test_eeprom_never_writes_an_unaligned_or_partial_chunk():
    """The firmware writes whole 8-byte chunks; a short payload silently rewrites its neighbours.
    The fake asserts this too, so this test is belt and braces on the load-bearing rule."""
    radio = FakeEepromRadio()
    _tune(radio)
    assert radio.writes
    for offset, data in radio.writes:
        assert offset % 8 == 0
        assert len(data) % 8 == 0


def test_eeprom_patch_preserves_the_bytes_it_is_not_changing():
    radio = FakeEepromRadio()
    # Neighbours of the attribute word, inside the same 8-byte chunk.
    addr = attr_addr(freq_channel(K0PRA.band))
    chunk = addr - (addr % 8)
    radio.mem[chunk:chunk + 8] = bytes(range(0x40, 0x48))
    _tune(radio)
    after = bytes(radio.mem[chunk:chunk + 8])
    lo = addr - chunk
    assert after[:lo] == bytes(range(0x40, 0x40 + lo))
    assert after[lo + 2:] == bytes(range(0x40 + lo + 2, 0x48))


def test_eeprom_reboots_only_after_a_verified_readback():
    radio = FakeEepromRadio()
    _tune(radio)
    assert radio.resets == 1


def test_eeprom_refuses_to_reboot_when_the_write_did_not_take():
    """A radio still running the old channel is recoverable. One rebooted onto a half-written
    record is a puzzle, on a bench where nobody can see the screen."""
    class Dropping(FakeEepromRadio):
        def request(self, msg, match, timeout=None):
            if isinstance(msg, f.EepromWrite) and msg.offset == vfo_addr(K0PRA.band, 0):
                return f.EepromWriteReply(offset=msg.offset)      # ack, but do not store
            return super().request(msg, match, timeout)

    radio = Dropping()
    with pytest.raises(TuneError, match="not rebooting"):
        _tune(radio)
    assert radio.resets == 0


def test_eeprom_raises_when_the_radio_stops_answering():
    class Deaf(FakeEepromRadio):
        def request(self, msg, match, timeout=None):
            raise Uvk5Timeout("no matching reply within 4.0s")

    with pytest.raises(TuneError, match="no answer"):
        _tune(Deaf())


def test_eeprom_re_establishes_the_session_after_the_reboot():
    """The session timestamp does not survive NVIC_SystemReset, and every EEPROM frame is checked
    against it — so a second tune without a fresh Hello would be silently refused."""
    radio = FakeEepromRadio()
    tuner = EepromTuner(radio, sleep=lambda _s: None)
    tuner.apply(K0PRA)
    assert radio.hellos == 2          # once at the start, once after the reset
    tuner.apply(BENCH)
    assert radio.hellos == 3          # the post-reboot Hello was reused, not skipped


def test_eeprom_skips_writes_that_would_change_nothing():
    """Flash wear is the running cost of this path, so re-applying the same channel must not
    spend a write on it."""
    radio = FakeEepromRadio()
    tuner = EepromTuner(radio, sleep=lambda _s: None)
    tuner.apply(K0PRA)
    first = len(radio.writes)
    tuner.apply(K0PRA)
    assert len(radio.writes) == first


def test_eeprom_moving_bands_writes_the_other_bands_slot():
    radio = FakeEepromRadio()
    tuner = EepromTuner(radio, sleep=lambda _s: None)
    tuner.apply(K0PRA)                                     # 70 cm
    two_metre = VfoImage(145_145_000, 144_545_000, ctcss_tenths=1072)
    tuner.apply(two_metre)
    addr = vfo_addr(two_metre.band, 0)
    assert bytes(radio.mem[addr:addr + VFO_RECORD_LEN]) == two_metre.pack_eeprom()
    indices = unpack_boot_indices(bytes(radio.mem[BOOT_INDEX_BLOCK:BOOT_INDEX_BLOCK + 16]))
    assert indices["screen"] == (freq_channel(two_metre.band),) * 2
