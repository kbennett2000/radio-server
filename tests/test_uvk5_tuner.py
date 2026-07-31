"""Both tuners, against fakes that model what the firmware actually does — including its traps.

The fake EEPROM writes in whole 8-byte chunks, exactly like `EEPROM_WriteBuffer`, because that is
the behaviour that turns a careless one-byte patch into seven bytes of collateral damage. And it
starts **erased** (`0xFF` everywhere), because that is the state in which the attribute word makes
the firmware ignore the VFO record entirely — the trap that would make a tune succeed and change
nothing.
"""

from __future__ import annotations

import pytest

from radio_server.backends.base import Capability, UnsupportedCapability
from radio_server.backends.uvk5 import frames as f
from radio_server.backends.uvk5.tuner import (
    SERIAL_TX_LOCKOUT_S,
    SESSION_TIMESTAMP,
    EepromTuner,
    HybridTuner,
    SetVfoTuner,
    TuneError,
)
from radio_server.backends.uvk5.transport import Uvk5Timeout
from radio_server.backends.uvk5.vfo import (
    BOOT_INDEX_BLOCK,
    FIRMWARE_POWER,
    POWER_LOW,
    VFO_RECORD_LEN,
    PowerLevel,
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
    """Answers 0x0873 with a configurable 0x0874, and 0x0877 with a configurable 0x0878."""

    def __init__(self, *, status=f.SetVfoStatus.APPLIED, rx=None, tx=None, tone=None, silent=False,
                 power=7, mod_status=f.ModulationStatus.APPLIED, mod=None, mod_raw=None,
                 mod_tx_ok=None, mod_silent=False, left_on=None):
        self.status, self.rx, self.tx, self.tone = status, rx, tx, tone
        self.silent = silent
        # The radio's OWN scale (USER, LOW1..LOW5, MID, HIGH). 7 is HIGH — what the bench measured
        # on all 186 tunes before power was settable.
        self.power = power
        #: 0x0878 knobs. `mod` overrides the modulation REPORTED BACK, so a test can model a radio
        #: that ends up somewhere other than where it was sent — the case `0x0878` exists for.
        #: `mod_tx_ok=None` follows the firmware's own rule (FM keys, AM does not).
        self.mod_status, self.mod, self.mod_raw = mod_status, mod, mod_raw
        self.mod_tx_ok, self.mod_silent = mod_tx_ok, mod_silent
        #: What this radio is ACTUALLY demodulating right now — sticky across the dock session the
        #: way the firmware's own VFO modulation is, which nothing but the radio's power switch
        #: reseeds. `left_on` seeds it with what the operator was last using, i.e. the state a
        #: RESTARTING host walks into and had no way to discover (ADR 0155). Distinct from `mod`,
        #: which overrides what the REPLY claims: `mod` models a radio that will not move, this
        #: models one that was simply left somewhere. Defaults to the firmware's own FM seed.
        self.demodulating = f.MODULATION_NAMES[
            f.DockModulation.FM if left_on is None else left_on
        ]
        self.sent: list = []

    def send(self, msg):
        self.sent.append(msg)

    def _mod_reply(self, msg) -> f.SetModulationReply:
        if not self.mod_status.ok:
            # What the firmware does on any refusal: blank to 0xFF, never to 0 (0 is FM).
            return f.SetModulationReply(status=self.mod_status)
        applied = msg.modulation if self.mod is None else self.mod
        # Where it LANDED, which is what the reply reports. Only on the applied path: the early
        # return above leaves `demodulating` untouched, because a refused set moves nothing.
        self.demodulating = f.MODULATION_NAMES.get(applied)
        tx_ok = (applied == f.DockModulation.FM) if self.mod_tx_ok is None else self.mod_tx_ok
        return f.SetModulationReply(
            status=self.mod_status,
            modulation=applied,
            raw=applied if self.mod_raw is None else self.mod_raw,
            flags=f.FLAG_TX_OK if tx_ok else 0,
        )

    def request(self, msg, match, timeout=None):
        self.sent.append(msg)
        if isinstance(msg, f.SetModulation):
            if self.mod_silent:
                raise Uvk5Timeout("no matching reply within 0.01s")
            reply = self._mod_reply(msg)
            return reply if match(reply) else None
        if self.silent:
            # What the real transport does on a deadline — it raises, it does not return None.
            raise Uvk5Timeout("no matching reply within 0.01s")
        reply = f.SetVfoReply(
            status=self.status,
            rx_hz=msg.rx_hz if self.rx is None else self.rx,
            tx_hz=msg.tx_hz if self.tx is None else self.tx,
            ctcss_tenths=msg.ctcss_tenths if self.tone is None else self.tone,
            power=self.power,
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


def test_setvfo_catches_a_radio_transmitting_at_the_wrong_power():
    """Checked rather than logged since power became settable (ADR 0146).

    `out->power` is read out of `gEeprom.VfoInfo[0].OUTPUT_POWER` *after* the firmware applied it,
    and the scale is a trap: assigning the wire's 0/1/2 raw lands "high" on LOW2, which tunes
    perfectly and never opens a repeater (ADR 0142). Asking for low and getting high is worse — an
    operator turning power down for a reason does not get told it did not happen.
    """
    radio = FakeSetVfoRadio()           # always answers power=7 (HIGH)
    SetVfoTuner(radio).apply(K0PRA)     # ...which is what K0PRA asks for

    quiet = VfoImage(rx_hz=448_525_000, tx_hz=443_525_000, ctcss_tenths=1000, power=POWER_LOW)
    with pytest.raises(TuneError, match="output power 7, not 1"):
        SetVfoTuner(FakeSetVfoRadio()).apply(quiet)


def test_setvfo_sends_the_firmware_scale_not_the_wire_s():
    """The mapping is the whole reason `FIRMWARE_POWER` exists; the frame carries the wire's step
    and the firmware maps it, so what is pinned here is that the STEP is what goes out."""
    for level, step in ((PowerLevel.LOW, 0), (PowerLevel.MID, 1), (PowerLevel.HIGH, 2)):
        radio = FakeSetVfoRadio(power=FIRMWARE_POWER[step])
        image = VfoImage(rx_hz=445_800_000, tx_hz=445_800_000, power=step)
        SetVfoTuner(radio).apply(image)
        (sent,) = radio.sent
        assert sent.power == step
        assert image.level is level


def test_setvfo_advertises_the_tuning_capabilities():
    caps = SetVfoTuner(FakeSetVfoRadio()).capabilities()
    assert caps == {Capability.SET_FREQUENCY, Capability.SET_SPLIT, Capability.SET_TONE,
                    Capability.SET_MODE, Capability.SET_MODULATION, Capability.SET_POWER}
    assert Capability.SET_CHANNEL not in caps    # no channel-select exists on this radio


# --- set-modulation, 0x0877/0x0878 (ADR 0150) --------------------------------------------------

def test_setvfo_sends_the_modulation_and_records_what_the_radio_confirmed():
    radio = FakeSetVfoRadio()
    tuner = SetVfoTuner(radio)
    # Nothing is claimed before anything is asserted: the firmware seeds its own sticky value FM,
    # and adopting that as ours would be reading the radio's state as our own (ADR 0132).
    assert tuner.modulation is None and tuner.tx_ok is None

    tx_ok = tuner.set_modulation("AM")
    (sent,) = radio.sent
    assert isinstance(sent, f.SetModulation)
    assert sent.modulation == f.DockModulation.AM
    assert tuner.modulation == "AM"
    # AM applies and the radio will NOT key its own PTT path — returned as well as recorded, so a
    # caller can act on it without a second round trip.
    assert tx_ok is False and tuner.tx_ok is False


def test_setvfo_modulation_accepts_lowercase_and_refuses_anything_else():
    tuner = SetVfoTuner(FakeSetVfoRadio())
    tuner.set_modulation(" am ")
    assert tuner.modulation == "AM"
    for bad in ("USB", "SSB", "wide", ""):
        with pytest.raises(ValueError, match="modulation"):
            tuner.set_modulation(bad)


def test_silence_to_a_set_modulation_names_pre_f7_firmware():
    """F7 answers `0x0877` in every case including a refusal, so silence means either the radio is
    not there or its dispatch has no such opcode. Both are actionable and neither is distinguishable
    from here, so the message says both — as the set-VFO path does for pre-F6."""
    radio = FakeSetVfoRadio(mod_silent=True)
    tuner = SetVfoTuner(radio)
    with pytest.raises(TuneError, match="pre-F7"):
        tuner.set_modulation("AM")
    assert tuner.modulation is None  # a failed assert must not be remembered as a success


@pytest.mark.parametrize(
    "status",
    [f.ModulationStatus.ERR_BUSY, f.ModulationStatus.ERR_FIELD, f.ModulationStatus.ERR_NO_HAL],
)
def test_a_refused_modulation_raises_and_changes_nothing(status):
    tuner = SetVfoTuner(FakeSetVfoRadio(mod_status=status))
    with pytest.raises(TuneError, match="refused"):
        tuner.set_modulation("AM")
    assert tuner.modulation is None and tuner.tx_ok is None


def test_a_reply_that_reports_a_different_modulation_is_a_failure_not_a_success():
    """`0x0878` carries what the firmware read back out of the radio's OWN VFO after applying, not
    an echo of the request. So "asked for AM, radio says FM" is the radio disagreeing about what it
    is demodulating — the exact class of silent mismatch this reply exists to surface, and it must
    not be logged and stepped over."""
    radio = FakeSetVfoRadio(mod=f.DockModulation.FM)  # asked AM, reports FM
    tuner = SetVfoTuner(radio)
    with pytest.raises(TuneError, match="not the requested AM"):
        tuner.set_modulation("AM")
    assert tuner.modulation is None


def test_a_radio_on_a_modulation_this_wire_cannot_name_is_a_failure_too():
    """A build with `ENABLE_BYP_RAW_DEMODULATORS` has demodulators the wire has no value for, and
    the firmware reports `0xFF`. That is applied-but-unnameable, which is still not what was asked
    for — and `raw` is diagnostic, so nothing may quietly branch on it instead."""
    radio = FakeSetVfoRadio(mod=f.DockModulation.UNKNOWN, mod_raw=4)
    with pytest.raises(TuneError, match="cannot name"):
        SetVfoTuner(radio).set_modulation("AM")


def test_setting_fm_reports_that_the_radio_will_transmit_again():
    tuner = SetVfoTuner(FakeSetVfoRadio())
    tuner.set_modulation("AM")
    assert tuner.tx_ok is False
    assert tuner.set_modulation("FM") is True
    assert (tuner.modulation, tuner.tx_ok) == ("FM", True)


def test_a_set_modulation_sends_exactly_one_frame_and_no_tune():
    """One-shot and self-contained: no HELLO (which would arm six seconds of transmit lockout), no
    `0x0873`, no EEPROM. The dock opcodes arm nothing, which is what makes this free in the key
    path."""
    radio = FakeSetVfoRadio()
    SetVfoTuner(radio).set_modulation("AM")
    assert len(radio.sent) == 1
    assert not any(isinstance(m, (f.SetVfo, f.Hello)) for m in radio.sent)


# --- eeprom ------------------------------------------------------------------------------------

class FakeEepromRadio:
    """A byte-addressed EEPROM that writes in 8-byte chunks, like the firmware.

    It also models the trap this fake used to miss entirely: **the session gate**. `uart.c`
    answers an EEPROM frame only while its stored timestamp matches, and a mismatch is dropped
    *silently* — `if (pCmd->Timestamp != Timestamp) return;`. So here, no session means the
    request times out with nothing on the wire, and a reset clears the session exactly as a real
    reboot does. Without that, a tuner that never checked its handshake passed every test.
    """

    def __init__(self, *, size=0x10000, version=b"F4HWN v5.7.0", aes_key=0, locked=0):
        self.mem = bytearray(b"\xFF" * size)     # erased flash, which is the dangerous state
        self.resets = 0
        self.hellos = 0
        self.writes: list[tuple[int, bytes]] = []
        self.version, self.aes_key, self.locked = version, aes_key, locked
        self.session = False                     # nothing is answered until HELLO lands
        self.deaf = False                        # powered off / in the bootloader: no HELLO either

    def send(self, msg):
        if isinstance(msg, f.Reset):
            self.resets += 1
            self.session = False                 # the timestamp does not survive a reboot

    def request(self, msg, match, timeout=None):
        if self.deaf:
            raise Uvk5Timeout("no matching reply — the radio is not answering")

        if isinstance(msg, f.Hello):
            self.hellos += 1
            self.session = True
            reply = f.ImHere(
                version=self.version.ljust(16, b"\x00"),
                has_custom_aes_key=self.aes_key,
                in_lock_screen=self.locked,
                challenge=(0, 0, 0, 0),
            )
        elif isinstance(msg, (f.EepromRead, f.EepromWrite)):
            if not self.session:
                raise Uvk5Timeout("silently dropped: the session timestamp does not match")
            assert msg.timestamp == SESSION_TIMESTAMP
            if isinstance(msg, f.EepromRead):
                data = bytes(self.mem[msg.offset:msg.offset + msg.size])
                reply = f.EepromReadReply(offset=msg.offset, size=len(data), data=data)
            else:
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
    with pytest.raises(TuneError, match="storage does not hold this channel"):
        _tune(radio)
    assert radio.resets == 0        # the load-bearing half: it did not reboot onto a bad record


def test_eeprom_names_a_radio_that_is_not_answering_at_all():
    """A dead radio must be reported as a dead radio. The operator gets this sentence, so it has
    to name the thing they can act on rather than an EEPROM address they cannot."""
    radio = FakeEepromRadio()
    radio.deaf = True
    with pytest.raises(TuneError, match="powered on"):
        _tune(radio)
    assert radio.writes == []         # nothing was attempted against a radio that is not there


def test_eeprom_re_establishes_the_session_after_the_reboot():
    """The session timestamp does not survive NVIC_SystemReset, and every EEPROM frame is checked
    against it — so a second tune without a fresh Hello would be silently refused."""
    radio = FakeEepromRadio()
    tuner = EepromTuner(radio, sleep=lambda _s: None)
    tuner.apply(K0PRA)
    assert radio.hellos == 2          # once as pre-flight, once after the reset
    tuner.apply(BENCH)
    assert radio.hellos == 4          # and the next tune re-checks rather than assuming


def test_eeprom_recovers_when_the_radio_reboots_underneath_the_server():
    """The incident, reduced to a test.

    A radio that reboots mid-session — a firmware flash, a battery swap, the operator switching
    it off and on — invalidates the session silently. That used to wedge the tuner for the life
    of the process: it had latched "hello sent" and never asked again, so every later tune
    failed even after the radio came back. One lost session must cost one handshake, not a
    service restart.
    """
    radio = FakeEepromRadio()
    tuner = EepromTuner(radio, sleep=lambda _s: None)
    tuner.apply(K0PRA)

    radio.session = False             # the operator power-cycled it between tunes
    tuner.apply(BENCH)                # must not raise

    band = BENCH.band
    assert bytes(radio.mem[vfo_addr(band, 0):vfo_addr(band, 0) + VFO_RECORD_LEN]) \
        == BENCH.pack_eeprom()


def test_eeprom_gives_up_when_a_fresh_handshake_does_not_help():
    """Retrying forever hides a real fault. One re-handshake, then say so."""
    radio = FakeEepromRadio()

    class LosesTheSessionEveryTime(FakeEepromRadio):
        def request(self, msg, match, timeout=None):
            reply = super().request(msg, match, timeout)
            self.session = False      # answers the handshake, then drops it again
            return reply

    with pytest.raises(TuneError, match="even after a fresh handshake"):
        _tune(LosesTheSessionEveryTime())
    assert radio.writes == []


def test_eeprom_refuses_a_locked_radio_rather_than_reading_zeros():
    """With a custom AES key set, a locked radio still answers reads — with zeroed data
    (`if (!bLocked) EEPROM_ReadBuffer(...)`). Silently tuning off blank reads would be a
    baffling read-back mismatch; name the cause instead."""
    with pytest.raises(TuneError, match="locked"):
        _tune(FakeEepromRadio(aes_key=1, locked=1))


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


# --- hybrid ------------------------------------------------------------------------------------
#
# The whole claim is that using both mechanisms gets what neither does alone: RF now (0x0873) and
# storage that survives the power switch (EEPROM), with no reboot because the firmware has already
# loaded the channel. These say so in the only terms that can be checked without a radio.

class FakeHybridRadio(FakeEepromRadio):
    """Answers an EEPROM session, 0x0873 and 0x0877, so one fake drives the composed tuner."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.set_vfos: list = []
        #: Every frame this fake was asked for, in order — so a test can assert the *sequence*
        #: (modulation before tune, in the key path) and not merely that both happened.
        self.sent: list = []

    def request(self, msg, match, timeout=None):
        self.sent.append(msg)
        if isinstance(msg, f.SetModulation):
            # The firmware's own rule: FM keys the radio's PTT path, anything else does not.
            tx_ok = msg.modulation == f.DockModulation.FM
            reply = f.SetModulationReply(
                status=f.ModulationStatus.APPLIED,
                modulation=msg.modulation,
                raw=msg.modulation,
                flags=f.FLAG_TX_OK if tx_ok else 0,
            )
            return reply if match(reply) else None
        if isinstance(msg, f.SetVfo):
            if self.deaf:
                raise Uvk5Timeout("no matching reply — the radio is not answering")
            self.set_vfos.append(msg)
            reply = f.SetVfoReply(
                status=f.SetVfoStatus.APPLIED, rx_hz=msg.rx_hz, tx_hz=msg.tx_hz,
                ctcss_tenths=msg.ctcss_tenths, power=7,
            )
            return reply if match(reply) else None
        return super().request(msg, match, timeout)


def _hybrid(radio, *, persist=False, now=None):
    """Default `persist=False` deliberately mirrors production (ADR 0145) — a helper that quietly
    stored would let the shipped default go untested behind every test that uses it."""
    clock = now or (lambda: 1000.0)
    return HybridTuner(
        SetVfoTuner(radio), EepromTuner(radio, sleep=lambda _s: None),
        persist=persist, now=clock,
    )


def test_hybrid_moves_the_rf_and_stores_the_channel():
    radio = FakeHybridRadio()
    _hybrid(radio, persist=True).apply(K0PRA)

    assert len(radio.set_vfos) == 1                       # RF followed, via 0x0873
    band = K0PRA.band
    assert bytes(radio.mem[vfo_addr(band, 0):vfo_addr(band, 0) + VFO_RECORD_LEN]) \
        == K0PRA.pack_eeprom()                            # and it will survive a power cycle


def test_hybrid_never_reboots_the_radio():
    """The reset existed only to make the firmware LOAD the record. 0x0873 already did that, and
    the reboot is the entire fourteen seconds."""
    radio = FakeHybridRadio()
    _hybrid(radio, persist=True).apply(K0PRA)
    assert radio.resets == 0


def test_hybrid_sets_rf_before_touching_flash():
    """Order is not cosmetic: 0x0873 fails fast on pre-F6 firmware, and finding that out before
    rewriting storage is the difference between a refusal and a half-migrated radio."""
    radio = FakeHybridRadio()

    class Recorder(FakeHybridRadio):
        order: list = []

        def request(self, msg, match, timeout=None):
            if isinstance(msg, f.SetVfo):
                self.order.append("setvfo")
            elif isinstance(msg, f.EepromWrite):
                self.order.append("write")
            return super().request(msg, match, timeout)

    recorder = Recorder()
    _hybrid(recorder, persist=True).apply(K0PRA)
    assert recorder.order[0] == "setvfo"
    assert "write" in recorder.order
    assert radio.resets == 0


def test_hybrid_arms_the_tx_lockout_on_any_eeprom_conversation_not_just_a_write():
    """Corrects ADR 0144, which armed this only when flash changed.

    The lockout is not a consequence of writing: `gSerialConfigCountDown_500ms` is armed by the
    HELLO (`uart.c:355`) and by every EEPROM READ (`393`) as well as the write (`447`), and
    `write_channel` always opens with a handshake and ends with a read-back verify. So a re-store
    that changes no flash still leaves the radio muted — and reporting it ready is exactly the ADR
    0142 fault the bench caught by failing every carrier row on attempt #1.
    """
    radio = FakeHybridRadio()
    tuner = _hybrid(radio, persist=True)

    tuner.apply(K0PRA)
    assert tuner.tx_ready_at == 1000.0 + SERIAL_TX_LOCKOUT_S
    assert radio.writes                                   # flash changed on the first pass

    radio.writes.clear()
    tuner.apply(K0PRA)                                    # same channel: nothing to write...
    assert radio.writes == []
    assert radio.hellos                                   # ...but it still handshook and read...
    assert tuner.tx_ready_at == 1000.0 + SERIAL_TX_LOCKOUT_S   # ...so the radio is still muted


def test_instant_does_not_clear_a_lockout_it_did_not_cause():
    """Flipping to instant mid-session must not report a still-muted radio as ready. The setvfo
    half arms nothing, so it has nothing to clear — and a deadline already running is still real."""
    radio = FakeHybridRadio()
    tuner = _hybrid(radio, persist=True)
    tuner.apply(K0PRA)
    armed = tuner.tx_ready_at
    assert armed is not None

    tuner.persist = False
    tuner.apply(BENCH)
    assert tuner.tx_ready_at == armed


def test_hybrid_reports_a_storage_failure_rather_than_hiding_it():
    """RF is already right at this point, so a storage failure is not fatal to the over — but it
    means the channel will not survive the power switch, and that must not pass silently."""
    class LosesTheWrite(FakeHybridRadio):
        def request(self, msg, match, timeout=None):
            if isinstance(msg, f.EepromWrite):
                self.writes.append((msg.offset, bytes(msg.data)))
                reply = f.EepromWriteReply(offset=msg.offset)   # acknowledged, never stored
                return reply if match(reply) else None
            return super().request(msg, match, timeout)

    radio = LosesTheWrite()
    with pytest.raises(TuneError, match="storage does not hold this channel"):
        _hybrid(radio, persist=True).apply(K0PRA)
    assert radio.set_vfos                                  # the RF half did happen


def test_hybrid_refuses_pre_f6_firmware_before_writing_anything():
    """Stock firmware drops 0x0873 without a word. Better a clear refusal than a radio whose
    storage was rewritten by a tuner that cannot move its RF."""
    class NoSetVfo(FakeHybridRadio):
        def request(self, msg, match, timeout=None):
            if isinstance(msg, f.SetVfo):
                raise Uvk5Timeout("stock firmware has no 0x0873 case")
            return super().request(msg, match, timeout)

    radio = NoSetVfo()
    with pytest.raises(TuneError, match="pre-F6"):
        _hybrid(radio).apply(K0PRA)
    assert radio.writes == []


def test_hybrid_advertises_the_tuning_capabilities():
    assert _hybrid(FakeHybridRadio()).capabilities() == {
        Capability.SET_FREQUENCY, Capability.SET_SPLIT, Capability.SET_TONE,
        Capability.SET_MODE, Capability.SET_MODULATION, Capability.SET_POWER,
    }


def test_eeprom_refuses_a_set_modulation_rather_than_quietly_doing_nothing():
    """The mirror-image of the capability split, and the half that actually protects anyone.

    Not advertising it is what makes the API 501; **raising** is what makes a direct call fail the
    same way instead of returning success for a radio still demodulating FM. Stock firmware has no
    `0x0877` case and drops the frame in silence, and the EEPROM record this tuner writes carries a
    hardcoded FM nibble — so there is no version of this that works, and none that should look like
    it did (guardrail 3).
    """
    radio = FakeEepromRadio()
    tuner = EepromTuner(radio)
    with pytest.raises(UnsupportedCapability) as excinfo:
        tuner.set_modulation("AM")
    # Names the operation, so the 501 body a direct caller builds is the same one the gate builds.
    assert excinfo.value.capability is Capability.SET_MODULATION
    assert radio.writes == []          # and it certainly did not write anything
    assert tuner.modulation is None    # nor claim a demodulator afterwards


def test_hybrid_delegates_the_modulation_and_never_touches_flash():
    """The deployed configuration (`baofeng` + `uvk5_tuner = "hybrid"`), so this is the path that
    actually runs — asserted directly rather than inferred from `SetVfoTuner`'s tests.

    Storing is about the *channel*: a demodulator is not part of a VFO record this firmware reads
    back, so there is nothing to persist and the frame count must not change with the switch.
    """
    for persist in (False, True):
        radio = FakeHybridRadio()
        tuner = _hybrid(radio, persist=persist)
        assert tuner.set_modulation("AM") is False
        assert tuner.modulation == "AM"   # readable through the hybrid, not just the inner tuner
        assert tuner.tx_ok is False
        assert radio.writes == [], f"persist={persist} wrote flash for a modulation change"
        (sent,) = [m for m in radio.sent if isinstance(m, f.SetModulation)]
        assert sent.modulation == f.DockModulation.AM


def test_the_three_tuners_no_longer_advertise_the_same_set():
    """The capability split, asserted as a difference rather than three separate lists (ADR 0150).

    `EepromTuner` writes its channel record into **stock** firmware, which has no `0x0877` case at
    all and whose record carries a hardcoded FM nibble. A shared frozenset would have it claiming a
    command it cannot send — the silent no-op guardrail 3 exists to forbid — and the claim would be
    invisible until an operator selected an airband preset and heard nothing.
    """
    setvfo = SetVfoTuner(FakeSetVfoRadio()).capabilities()
    eeprom = EepromTuner(FakeEepromRadio()).capabilities()
    hybrid = _hybrid(FakeHybridRadio()).capabilities()

    assert setvfo - eeprom == {Capability.SET_MODULATION}
    assert hybrid == setvfo            # hybrid is setvfo plus storage, never less
    assert eeprom | {Capability.SET_MODULATION} == setvfo  # and differs in exactly that one


# --- instant mode: persistence is a live switch, and RAM tunes say so (ADR 0145) -------------
# The half of hybrid that costs the six-second lockout is the EEPROM write, and it is the only half
# that is optional. These pin both directions of the trade, because "instant" is only true if the
# flash really is left alone, and "it survives the power switch" is only true if it really is not.

def test_hybrid_instant_moves_the_rf_and_writes_no_flash():
    """The default (ADR 0145). Anything in `writes` here means the operator was charged six seconds
    of transmit lockout for storage they asked not to have."""
    radio = FakeHybridRadio()
    tuner = _hybrid(radio)                                # persist=False, as shipped
    tuner.apply(K0PRA)

    assert len(radio.set_vfos) == 1                       # RF followed
    assert radio.writes == []                             # and nothing touched flash
    assert radio.resets == 0
    assert tuner.tx_ready_at is None                      # so the radio will key immediately


def test_hybrid_instant_does_not_even_open_a_session():
    """A HELLO is not free — `uart.c:355` arms the same six-second lockout an EEPROM write does. An
    instant tune that handshaked anyway would mute the transmitter for no reason at all."""
    radio = FakeHybridRadio()
    _hybrid(radio).apply(K0PRA)
    assert radio.hellos == 0


def test_hybrid_volatile_tracks_the_switch():
    """`volatile` is what tells the backend whether to re-assert before a key-up, so it has to
    follow the switch rather than the mode name."""
    tuner = _hybrid(FakeHybridRadio())
    assert tuner.volatile is True                         # RAM only
    tuner.persist = True
    assert tuner.volatile is False                        # storage holds it; the radio boots on it


def test_hybrid_store_saves_the_channel_the_radio_is_already_on():
    """Turning the switch on has to store where the radio IS, not merely promise to store the next
    channel — otherwise "save to radio" saves nothing until the operator happens to tap something."""
    radio = FakeHybridRadio()
    tuner = _hybrid(radio)
    tuner.apply(K0PRA)
    assert radio.writes == []

    assert tuner.store(K0PRA) is True
    band = K0PRA.band
    assert bytes(radio.mem[vfo_addr(band, 0):vfo_addr(band, 0) + VFO_RECORD_LEN]) \
        == K0PRA.pack_eeprom()
    assert tuner.tx_ready_at == 1000.0 + SERIAL_TX_LOCKOUT_S   # storing costs the lockout

    # Already there, so no flash is worn — but the handshake and read-back still muted the radio,
    # and saying otherwise is how ADR 0142 lost every carrier row on attempt #1.
    tuner.tx_ready_at = None
    assert tuner.store(K0PRA) is False
    assert tuner.tx_ready_at == 1000.0 + SERIAL_TX_LOCKOUT_S


def test_hybrid_reassert_sends_one_frame_and_never_touches_flash():
    """This runs inside the key path. A write here would arm the lockout at the exact moment the
    radio is about to transmit — the firmware would then cut the over it was arming for."""
    radio = FakeHybridRadio()
    tuner = _hybrid(radio)
    tuner.apply(K0PRA)
    radio.set_vfos.clear()

    tuner.reassert(K0PRA)
    assert len(radio.set_vfos) == 1
    assert radio.writes == []
    assert radio.hellos == 0
    assert radio.resets == 0


def test_setvfo_reassert_is_a_tune_and_is_confirmed():
    radio = FakeHybridRadio()
    tuner = SetVfoTuner(radio)
    assert tuner.volatile is True

    tuner.reassert(K0PRA)
    assert len(radio.set_vfos) == 1

    radio.deaf = True
    with pytest.raises(TuneError, match="pre-F6"):
        tuner.reassert(K0PRA)


def test_eeprom_reassert_does_nothing_and_above_all_never_reboots():
    """The empty body is the feature. `EepromTuner.apply` sends a Reset and sleeps out a lockout;
    wiring that in here would reboot the radio at every single key-up."""
    radio = FakeEepromRadio()
    tuner = EepromTuner(radio, sleep=lambda _s: None)
    assert tuner.volatile is False

    tuner.reassert(K0PRA)
    assert radio.resets == 0
    assert radio.writes == []
    assert radio.hellos == 0
