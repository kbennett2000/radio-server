"""Baofeng mode with a UV-K5 tuner attached — the part that finally answers the operator's question.

"What good is any of this if I have to set the radio to a different channel each time?" The answer
has to be that applying a preset moves the radio, so what is pinned here is: the right channel
reaches the tuner, a whole preset costs exactly **one** tune rather than four, and nothing tunes
while the transmitter is up.
"""

from __future__ import annotations

import pytest

from radio_server.backends import SHARED_CAPS
from radio_server.backends.base import Capability, RadioUnavailable, UnsupportedCapability
from radio_server.backends.uvk5.tuner import SERIAL_TX_LOCKOUT_S, TUNING_CAPS, TuneError
from radio_server.backends.uvk5.vfo import POWER_HIGH, PowerLevel, VfoImage
from radio_server.backends import create_radio
from radio_server.presets import Preset, apply_preset
from tests.test_aioc_baofeng import FakeAudio, FakeSerial


class SpyTuner:
    """Records every channel actually written to the radio."""

    def __init__(self, fail=None):
        self.applied: list[VfoImage] = []
        self.fail = fail

    def capabilities(self):
        return TUNING_CAPS

    def apply(self, image: VfoImage) -> None:
        if self.fail is not None:
            raise self.fail
        self.applied.append(image)

    def reassert(self, image: VfoImage) -> None:
        pass


def make_tuned(**kwargs):
    tuner = SpyTuner()
    radio = create_radio(
        "baofeng",
        ptt_line="dtr",
        tx_lead_seconds=0.0,
        tuner=tuner,
        _serial_factory=lambda port: FakeSerial(),
        _audio=FakeAudio(),
        **kwargs,
    )
    return radio, tuner


# --- capabilities ----------------------------------------------------------------------------

def test_a_tuner_adds_exactly_the_tuning_capabilities():
    radio, _ = make_tuned()
    assert radio.capabilities() == SHARED_CAPS | TUNING_CAPS
    # Still no channel select: this radio has none, and a preset is a host-side concept.
    assert Capability.SET_CHANNEL not in radio.capabilities()


# --- single setters --------------------------------------------------------------------------

def test_set_frequency_tunes_immediately():
    radio, tuner = make_tuned()
    radio.set_frequency(445_800_000)
    assert [i.rx_hz for i in tuner.applied] == [445_800_000]
    assert tuner.applied[-1].tx_hz == 445_800_000     # simplex


def test_set_frequency_clears_an_armed_split():
    """ADR 0133, and it is the fail-safe direction: a TX leg surviving a retune would let an
    unattended station ID key a repeater's uplink."""
    radio, tuner = make_tuned()
    radio.set_frequency(448_525_000)
    radio.set_split(443_525_000)
    assert tuner.applied[-1].tx_hz == 443_525_000
    radio.set_frequency(445_800_000)
    assert tuner.applied[-1].tx_hz == 445_800_000


def test_set_tone_and_mode_reach_the_radio():
    radio, tuner = make_tuned()
    radio.set_frequency(448_525_000)
    radio.set_tone(100.0)
    assert tuner.applied[-1].ctcss_tenths == 1000
    radio.set_mode("NFM")
    assert tuner.applied[-1].narrow is True
    radio.set_tone(None)
    assert tuner.applied[-1].ctcss_tenths == 0


def test_a_setter_before_any_frequency_is_refused_not_guessed():
    radio, tuner = make_tuned()
    with pytest.raises(ValueError, match="set a frequency"):
        radio.set_tone(100.0)
    assert tuner.applied == []


def test_an_out_of_band_frequency_never_reaches_the_radio():
    radio, tuner = make_tuned()
    with pytest.raises(ValueError):
        radio.set_frequency(4_485_250_000)
    assert tuner.applied == []


# --- batching --------------------------------------------------------------------------------

def test_a_whole_preset_costs_exactly_one_tune():
    """Four setter calls, one channel. On the EEPROM path each tune is a reboot and a flash
    cycle, so committing per setter would reboot the radio four times to change channel once.
    """
    radio, tuner = make_tuned()
    preset = Preset(name="K0PRA448.525", frequency=448_525_000, tx_frequency=443_525_000,
                    tx_tone=100.0, rx_tone=100.0, mode="FM")
    applied, skipped = apply_preset(radio, preset)

    assert len(tuner.applied) == 1
    written = tuner.applied[0]
    assert (written.rx_hz, written.tx_hz, written.ctcss_tenths) == (448_525_000, 443_525_000, 1000)
    assert not written.narrow
    assert str(Capability.SET_FREQUENCY) in applied and str(Capability.SET_SPLIT) in applied
    # rx_tone is still honestly reported as unhonoured (ADR 0133), not silently dropped.
    assert any(entry["field"] == "rx_tone" for entry in skipped)


def test_a_simplex_preset_disarms_the_previous_repeater_split():
    radio, tuner = make_tuned()
    apply_preset(radio, Preset(name="rpt", frequency=448_525_000,
                               tx_frequency=443_525_000, tx_tone=100.0))
    apply_preset(radio, Preset(name="simplex", frequency=445_800_000))
    last = tuner.applied[-1]
    assert last.rx_hz == last.tx_hz == 445_800_000
    assert last.ctcss_tenths == 0       # and the repeater's tone does not ride along


def test_re_applying_the_same_preset_does_not_re_tune():
    """Flash wear is the EEPROM path's running cost; a no-op must cost nothing."""
    radio, tuner = make_tuned()
    preset = Preset(name="bench", frequency=445_800_000, mode="FM")
    apply_preset(radio, preset)
    apply_preset(radio, preset)
    assert len(tuner.applied) == 1


def test_a_batch_that_raises_still_closes():
    radio, tuner = make_tuned()
    with pytest.raises(RuntimeError, match="boom"):
        with radio.tuning_batch():
            radio.set_frequency(445_800_000)
            raise RuntimeError("boom")
    # The depth is unwound, so the next tune is not swallowed into a batch that never closed.
    radio.set_frequency(446_000_000)
    assert tuner.applied[-1].rx_hz == 446_000_000


# --- safety ----------------------------------------------------------------------------------

def test_refuses_to_retune_while_transmitting():
    """Retuning mid-over moves the carrier out from under the audio, and on the EEPROM path
    reboots the radio while it is keyed."""
    radio, tuner = make_tuned()
    radio.set_frequency(445_800_000)
    radio.ptt(True)
    try:
        with pytest.raises(RuntimeError, match="while transmitting"):
            radio.set_frequency(446_000_000)
    finally:
        radio.ptt(False)
    assert [i.rx_hz for i in tuner.applied] == [445_800_000]


def test_status_reports_the_channel_this_server_set():
    radio, _ = make_tuned()
    assert radio.status().frequency is None      # never a guess before we have tuned it
    apply_preset(radio, Preset(name="K0PRA", frequency=448_525_000,
                               tx_frequency=443_525_000, tx_tone=100.0, mode="FM"))
    status = radio.status()
    assert status.frequency == 448_525_000
    assert status.tx_frequency == 443_525_000
    assert status.tone == 100.0
    assert status.mode == "FM"


def test_status_reports_no_split_for_simplex():
    radio, _ = make_tuned()
    radio.set_frequency(445_800_000)
    # None means "not split" — never a mirror of `frequency` (ADR 0133).
    assert radio.status().tx_frequency is None


def test_a_failed_tune_is_not_remembered_as_applied():
    """If the write did not take, the next attempt must not be skipped as a no-op."""
    radio, tuner = make_tuned()
    tuner.fail = RuntimeError("radio refused")
    with pytest.raises(RuntimeError):
        radio.set_frequency(445_800_000)
    tuner.fail = None
    radio.set_frequency(445_800_000)
    assert [i.rx_hz for i in tuner.applied] == [445_800_000]


def test_power_defaults_to_the_radios_high_setting():
    """A repeater is not opened at minimum power, and this is the field whose two scales already
    disagreed once."""
    radio, tuner = make_tuned()
    radio.set_frequency(448_525_000)
    assert tuner.applied[-1].power == POWER_HIGH


# --- the serial TX lockout (ADR 0144) ----------------------------------------------------------
#
# The hybrid tuner publishes a deadline instead of sleeping on it, so a channel change is audible
# at once and only a caller about to transmit waits. That makes the backend responsible for two
# things: reporting the wait so the UI can stop offering a dead button, and actually enforcing it
# before the PTT line goes high. Returning "tuned" to a radio that will swallow the next six
# seconds of PTT is the ADR 0142 fault, and it must not come back by moving the wait.

class LockedOutTuner(SpyTuner):
    """A tuner whose last tune left the radio muted until `tx_ready_at`."""

    def __init__(self, tx_ready_at=None):
        super().__init__()
        self.tx_ready_at = tx_ready_at


def _locked(tx_ready_at):
    tuner = LockedOutTuner(tx_ready_at)
    radio = create_radio(
        "baofeng", ptt_line="dtr", tx_lead_seconds=0.0, tuner=tuner,
        _serial_factory=lambda port: FakeSerial(), _audio=FakeAudio(),
    )
    return radio, tuner


def test_status_reports_the_lockout_so_the_ui_can_show_it(monkeypatch):
    monkeypatch.setattr("radio_server.backends.aioc_baofeng.time.monotonic", lambda: 100.0)
    radio, _ = _locked(tx_ready_at=104.0)
    assert radio.status().tx_ready_in == pytest.approx(4.0)


def test_status_reports_no_lockout_once_the_deadline_has_passed(monkeypatch):
    monkeypatch.setattr("radio_server.backends.aioc_baofeng.time.monotonic", lambda: 110.0)
    radio, _ = _locked(tx_ready_at=104.0)
    # None, not a negative number: "ready" is the absence of a wait, and a UI counting down from
    # -6 would disable the button forever.
    assert radio.status().tx_ready_in is None


def test_status_reports_no_lockout_on_a_tuner_that_never_has_one():
    radio, _ = make_tuned()          # SpyTuner has no tx_ready_at attribute at all
    assert radio.status().tx_ready_in is None


def test_a_key_up_waits_out_the_lockout_before_the_line_goes_high(monkeypatch):
    """The wait did not disappear when the tuner stopped sleeping — it moved here."""
    slept: list[float] = []
    monkeypatch.setattr("radio_server.backends.aioc_baofeng.time.monotonic", lambda: 100.0)
    monkeypatch.setattr("radio_server.backends.aioc_baofeng.time.sleep", slept.append)

    radio, _ = _locked(tx_ready_at=104.0)
    radio.ptt(True)
    try:
        assert slept == [pytest.approx(4.0)]
        assert radio.status().transmitting is True
    finally:
        radio.ptt(False)


def test_a_key_up_does_not_wait_when_the_radio_is_ready(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("radio_server.backends.aioc_baofeng.time.sleep", slept.append)

    radio, _ = _locked(tx_ready_at=None)
    radio.ptt(True)
    try:
        assert slept == []
    finally:
        radio.ptt(False)


def test_the_wait_is_bounded_by_the_lockout_itself(monkeypatch):
    """A nonsense deadline — a clock jump, a bad tuner — must not park the transmitter forever."""
    slept: list[float] = []
    monkeypatch.setattr("radio_server.backends.aioc_baofeng.time.monotonic", lambda: 100.0)
    monkeypatch.setattr("radio_server.backends.aioc_baofeng.time.sleep", slept.append)

    radio, _ = _locked(tx_ready_at=100_000.0)
    radio.ptt(True)
    try:
        assert slept == [pytest.approx(SERIAL_TX_LOCKOUT_S)]
    finally:
        radio.ptt(False)


# --- the key-up channel re-assert (ADR 0145) ---------------------------------------------------
# A tuner that writes only the radio's RAM loses its channel the moment somebody uses the power
# switch — but `status()` still reports the channel the server chose, and the UI still highlights
# it. Without the re-assert the next over goes out on a stale frequency with nothing anywhere
# saying so. That is what these pin: not that it is fast, but that it happens, that it happens
# before any RF, and that it never happens where it would reboot the radio.

class VolatileTuner(SpyTuner):
    """A tuner whose tunes live in the radio's RAM — `setvfo`, or hybrid with storage off."""

    volatile = True

    def __init__(self, fail=None, reassert_fail=None):
        super().__init__(fail)
        self.reasserted: list[VfoImage] = []
        self.reassert_fail = reassert_fail
        self.tx_ready_at = None

    def reassert(self, image: VfoImage) -> None:
        if self.reassert_fail is not None:
            raise self.reassert_fail
        self.reasserted.append(image)


class StoringTuner(VolatileTuner):
    """Storage holds the channel, so the radio boots onto it and there is nothing to restore."""

    volatile = False


CHANNEL = VfoImage(rx_hz=446_000_000, tx_hz=446_000_000, power=POWER_HIGH)


def _with_tuner(tuner):
    return create_radio(
        "baofeng",
        ptt_line="dtr",
        tx_lead_seconds=0.0,
        tuner=tuner,
        _serial_factory=lambda port: FakeSerial(),
        _audio=FakeAudio(),
    )


def test_a_key_up_reasserts_a_volatile_channel():
    tuner = VolatileTuner()
    radio = _with_tuner(tuner)
    radio.set_frequency(CHANNEL.rx_hz)
    assert tuner.reasserted == []

    radio.ptt(True)
    try:
        assert tuner.reasserted == [CHANNEL]
    finally:
        radio.ptt(False)


def test_a_key_up_does_not_reassert_when_storage_holds_the_channel():
    """`eeprom`, and hybrid with storage on. The radio boots onto the channel by itself, so a dock
    round-trip here would buy nothing and put serial traffic in the RF key path for free."""
    tuner = StoringTuner()
    radio = _with_tuner(tuner)
    radio.set_frequency(CHANNEL.rx_hz)

    radio.ptt(True)
    try:
        assert tuner.reasserted == []
    finally:
        radio.ptt(False)


def test_nothing_is_reasserted_before_the_server_has_tuned_anything():
    """Until it has chosen a channel the server does not know one — the radio is on whatever its
    front panel says, and inventing a tune here would move it off that."""
    tuner = VolatileTuner()
    radio = _with_tuner(tuner)

    radio.ptt(True)
    try:
        assert tuner.reasserted == []
    finally:
        radio.ptt(False)


def test_a_tuner_with_no_reassert_at_all_still_keys():
    """Duck-typed like `tuning_batch`: a backend or tuner that predates this must not stop working
    because it lacks a method it never needed."""
    radio, tuner = make_tuned()          # SpyTuner has neither `volatile` nor `reassert`
    radio.set_frequency(CHANNEL.rx_hz)
    radio.ptt(True)
    try:
        assert radio.status().transmitting is True
    finally:
        radio.ptt(False)


def test_an_unconfirmed_channel_refuses_the_key_up_with_nothing_keyed():
    """The whole point of doing this first. A radio that will not confirm where it is pointed must
    not transmit — and the refusal has to leave the line low and no audio device opened."""
    tuner = VolatileTuner(reassert_fail=TuneError("the radio did not answer"))
    radio = _with_tuner(tuner)
    radio.set_frequency(CHANNEL.rx_hz)

    with pytest.raises(TuneError):
        radio.ptt(True)

    assert radio.status().transmitting is False
    # The line was never driven high at all — not raised and lowered again. Refusing before the
    # assert is what makes that true, and a momentary key is still a key.
    assert radio._serial.dtr is False
    assert ("dtr", True) not in radio._serial.events
    assert radio._playback is None                 # nor was an audio device opened


def test_the_reassert_failure_reaches_the_operator_as_a_503_not_a_500():
    """`TuneError` is a `RadioUnavailable`, so the app-wide handler renders the sentence (ADR
    0143). A stack trace would tell the operator standing next to the radio nothing."""
    assert issubclass(TuneError, RadioUnavailable)


# --- the demodulator, and the transmitter it disables (ADR 0150) -------------------------------
#
# On a build without ENABLE_TX_WHEN_AM the firmware sets VFO_STATE_TX_DISABLE for any non-FM
# modulation, and that is the path the radio's PTT PIN drives — which is exactly where this backend
# keys, through the AIOC's DTR line. So on this station a successful set-AM stops the transmitter
# outright, and nothing about that is visible from the host unless the radio says so.

class ModulationTuner(VolatileTuner):
    """A `setvfo`/`hybrid`-shaped tuner: it speaks 0x0877 and remembers what came back."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.modulation: str | None = None
        self.tx_ok: bool | None = None
        self.mod_calls: list[str] = []

    def capabilities(self):
        return TUNING_CAPS | {Capability.SET_MODULATION}

    def set_modulation(self, modulation: str) -> bool:
        self.mod_calls.append(modulation)
        self.modulation = modulation
        self.tx_ok = modulation == "FM"     # the firmware's own rule
        return self.tx_ok


def test_a_modulation_capable_tuner_adds_exactly_that_one_capability():
    radio = _with_tuner(ModulationTuner())
    assert radio.capabilities() == SHARED_CAPS | TUNING_CAPS | {Capability.SET_MODULATION}


def test_set_modulation_reaches_the_tuner_and_is_reported_in_status():
    radio = _with_tuner(ModulationTuner())
    # Reported as unknown until asserted — not as the FM the firmware happens to seed (ADR 0132).
    assert radio.status().modulation is None
    assert radio.status().tx_ok is None

    radio.set_modulation("AM")
    assert radio.status().modulation == "AM"
    assert radio.status().tx_ok is False


def test_set_modulation_works_before_any_frequency_has_been_set():
    """Deliberately NOT staged like a tone. The modulation is not part of a channel and is not on
    `0x0873`'s wire, so it must not inherit `_stage`'s "set a frequency first" rule — which is the
    right answer for a tone and the wrong one here."""
    radio = _with_tuner(ModulationTuner())
    radio.set_modulation("AM")           # no set_frequency anywhere above
    assert radio.status().modulation == "AM"


def test_a_backend_without_a_tuner_refuses_naming_the_capability():
    audio_only = create_radio(
        "baofeng", ptt_line="dtr", tx_lead_seconds=0.0,
        _serial_factory=lambda port: FakeSerial(), _audio=FakeAudio(),
    )
    with pytest.raises(UnsupportedCapability) as excinfo:
        audio_only.set_modulation("AM")
    assert excinfo.value.capability is Capability.SET_MODULATION
    assert audio_only.status().modulation is None


def test_a_key_up_in_am_is_refused_with_nothing_keyed_and_a_reason():
    """The failure this exists to prevent: the DTR line goes high, the radio declines, and the over
    is silence with `status()` cheerfully reporting `transmitting`. Under guardrail 5 the
    transmission it swallows is the station ID, which is required rather than optional.
    """
    tuner = ModulationTuner()
    radio = _with_tuner(tuner)
    radio.set_frequency(CHANNEL.rx_hz)
    radio.set_modulation("AM")

    with pytest.raises(RadioUnavailable, match="AM"):
        radio.ptt(True)

    assert radio.status().transmitting is False
    # Never driven high at all, not raised and lowered — a momentary key is still a key.
    assert radio._serial.dtr is False
    assert ("dtr", True) not in radio._serial.events
    assert radio._playback is None


def test_going_back_to_fm_lets_the_radio_transmit_again():
    tuner = ModulationTuner()
    radio = _with_tuner(tuner)
    radio.set_frequency(CHANNEL.rx_hz)
    radio.set_modulation("AM")
    radio.set_modulation("FM")

    radio.ptt(True)
    try:
        assert radio.status().transmitting is True
        assert radio.status().tx_ok is True
    finally:
        radio.ptt(False)


def test_an_unknown_tx_ok_never_blocks_a_key_up():
    """`None` is "nobody has asked this radio", and it must not refuse. The whole tuning surface
    reports `None` before its first assertion, and a station that would not transmit until someone
    had chosen a demodulator would be a worse failure than the one this prevents."""
    tuner = ModulationTuner()
    radio = _with_tuner(tuner)
    radio.set_frequency(CHANNEL.rx_hz)
    assert tuner.tx_ok is None           # nothing asserted

    radio.ptt(True)
    try:
        assert radio.status().transmitting is True
    finally:
        radio.ptt(False)


def test_a_tuner_that_cannot_report_tx_ok_at_all_still_keys():
    """Duck-typed like `reassert`: an `eeprom` tuner has no such attribute and never will."""
    radio, _ = make_tuned()              # SpyTuner: no `tx_ok`, no `modulation`
    radio.set_frequency(CHANNEL.rx_hz)
    radio.ptt(True)
    try:
        assert radio.status().transmitting is True
        assert radio.status().tx_ok is None
    finally:
        radio.ptt(False)


def test_a_key_up_reasserts_the_modulation_before_the_channel():
    """Order is the point, not just that both happen.

    The firmware keeps the modulation in RAM and reseeds FM on a power cycle — the same staleness
    the channel re-assert exists for. Sending it FIRST means the `tx_ok` the refusal above reads
    was measured milliseconds ago rather than remembered from whenever the operator last chose.
    """
    tuner = ModulationTuner()
    radio = _with_tuner(tuner)
    radio.set_frequency(CHANNEL.rx_hz)
    radio.set_modulation("FM")
    tuner.mod_calls.clear()
    tuner.reasserted.clear()

    radio.ptt(True)
    try:
        assert tuner.mod_calls == ["FM"]        # re-asserted
        assert tuner.reasserted == [CHANNEL]    # ...and so was the channel
    finally:
        radio.ptt(False)


def test_nothing_is_reasserted_before_a_modulation_has_ever_been_chosen():
    """Same rule as the channel: until this server has asserted one it does not know one, and
    inventing an assertion here would move the radio off whatever the operator left it on."""
    tuner = ModulationTuner()
    radio = _with_tuner(tuner)
    radio.set_frequency(CHANNEL.rx_hz)

    radio.ptt(True)
    try:
        assert tuner.mod_calls == []
    finally:
        radio.ptt(False)


def test_an_am_preset_applies_through_the_backend_and_disables_transmit():
    """End to end over the real preset seam, on the deployed shape."""
    tuner = ModulationTuner()
    radio = _with_tuner(tuner)
    applied, skipped = apply_preset(
        radio, Preset("Denver Tower", 118_300_000, modulation="AM")
    )
    assert "set_modulation" in applied
    assert skipped == []
    assert radio.status().modulation == "AM"
    assert radio.status().tx_ok is False


# --- the storage switch (ADR 0145) -------------------------------------------------------------

def test_tune_persist_is_none_where_there_is_no_such_choice():
    """None and False are different answers — "no switch" versus "the switch is off" — so the UI
    can hide the control rather than render one that does nothing."""
    radio, _ = make_tuned()                       # SpyTuner: no `persist` attribute
    assert radio.tune_persist is None
    assert radio.status().tune_persist is None

    audio_only = create_radio(
        "baofeng", ptt_line="dtr", tx_lead_seconds=0.0,
        _serial_factory=lambda port: FakeSerial(), _audio=FakeAudio(),
    )
    assert audio_only.tune_persist is None        # no tuner at all — a plain UV-5R
    assert audio_only.status().tune_persist is None


class SwitchableTuner(VolatileTuner):
    """A hybrid-shaped tuner: the storage half is a flag, and turning it on stores what is there."""

    def __init__(self):
        super().__init__()
        self.persist = False
        self.stored: list[VfoImage] = []

    @property
    def volatile(self):
        return not self.persist

    def store(self, image: VfoImage) -> bool:
        self.stored.append(image)
        return True


def test_the_switch_moves_and_is_reported():
    tuner = SwitchableTuner()
    radio = _with_tuner(tuner)
    assert radio.status().tune_persist is False

    assert radio.set_tune_persist(True) is True
    assert radio.status().tune_persist is True
    assert radio.set_tune_persist(False) is False
    assert radio.status().tune_persist is False


def test_turning_storage_on_saves_the_channel_the_radio_is_already_on():
    """Otherwise "save to radio" saves nothing until the operator happens to tap a channel — the
    switch would describe a future intention rather than do what it says."""
    tuner = SwitchableTuner()
    radio = _with_tuner(tuner)
    radio.set_frequency(CHANNEL.rx_hz)

    radio.set_tune_persist(True)
    assert tuner.stored == [CHANNEL]


def test_turning_storage_on_before_any_tune_stores_nothing():
    tuner = SwitchableTuner()
    radio = _with_tuner(tuner)
    radio.set_tune_persist(True)
    assert tuner.stored == []


def test_turning_storage_off_leaves_the_radio_alone():
    """What is already in flash stays there, and is exactly what a power cycle should fall back
    to. Only the promise about future tunes changes."""
    tuner = SwitchableTuner()
    radio = _with_tuner(tuner)
    radio.set_frequency(CHANNEL.rx_hz)
    radio.set_tune_persist(True)
    tuner.stored.clear()

    radio.set_tune_persist(False)
    assert tuner.stored == []
    assert tuner.applied == [CHANNEL]              # and no retune either


def test_the_switch_is_refused_mid_transmission():
    """Storing arms the firmware's serial lockout, and `SerialConfigInProgress()` CUTS an over in
    progress. A switch that could end a transmission when flipped would be a trap."""
    tuner = SwitchableTuner()
    radio = _with_tuner(tuner)
    radio.set_frequency(CHANNEL.rx_hz)
    radio.ptt(True)
    try:
        with pytest.raises(RuntimeError, match="while transmitting"):
            radio.set_tune_persist(True)
        assert tuner.stored == []
    finally:
        radio.ptt(False)


def test_the_switch_is_refused_where_there_is_no_such_choice():
    radio, _ = make_tuned()
    with pytest.raises(UnsupportedCapability):
        radio.set_tune_persist(True)


# --- transmit power (ADR 0146) -----------------------------------------------------------------
# Power was plumbed to the radio from the start and hardcoded to HIGH above the VfoImage seam — the
# bench read back `power=7` on all 186 tunes. These pin that it is now reachable, that the station
# level and a channel's own level compose the way the ADR says, and that nothing claims a level the
# radio did not confirm.

def test_power_defaults_to_high_which_is_what_every_tune_did_before():
    radio, tuner = make_tuned()
    radio.set_frequency(CHANNEL.rx_hz)
    assert tuner.applied[-1].level is PowerLevel.HIGH


def test_the_boot_level_reaches_the_very_first_tune():
    """A first tune after the operator configured low must not go out at high. `_stage` builds from
    nothing here, so this is the path where VfoImage's own default would silently win."""
    radio, tuner = make_tuned(uvk5_power="low")
    radio.set_frequency(CHANNEL.rx_hz)
    assert tuner.applied[-1].level is PowerLevel.LOW


def test_set_power_retunes_the_current_channel():
    radio, tuner = make_tuned()
    radio.set_frequency(CHANNEL.rx_hz)
    radio.set_power("low")
    assert tuner.applied[-1].level is PowerLevel.LOW
    assert tuner.applied[-1].rx_hz == CHANNEL.rx_hz     # and stays on the same channel


def test_the_level_survives_the_next_tune():
    """It is the STATION level, not a one-shot: turning power down and then tapping another channel
    must not quietly put it back up."""
    radio, tuner = make_tuned()
    radio.set_frequency(CHANNEL.rx_hz)
    radio.set_power("mid")
    radio.set_frequency(445_800_000)
    assert tuner.applied[-1].level is PowerLevel.MID


def test_set_power_before_any_tune_records_the_level_without_tuning():
    """There is no channel to re-key yet. `_stage` would refuse with "set a frequency first", which
    is right for a tone (it belongs to a channel) and wrong for a station-wide default."""
    radio, tuner = make_tuned()
    radio.set_power("low")
    assert tuner.applied == []
    assert radio.status().power is None      # nothing tuned, so nothing to report

    radio.set_frequency(CHANNEL.rx_hz)
    assert tuner.applied[-1].level is PowerLevel.LOW


def test_a_rejected_level_leaves_the_station_alone():
    radio, tuner = make_tuned()
    radio.set_frequency(CHANNEL.rx_hz)
    with pytest.raises(ValueError, match="must be one of"):
        radio.set_power("blazing")
    assert tuner.applied[-1].level is PowerLevel.HIGH
    radio.set_frequency(445_800_000)
    assert tuner.applied[-1].level is PowerLevel.HIGH     # and did not leak into the next tune


def test_a_failed_tune_leaves_the_request_pending_and_status_unconfirmed():
    """Intent and confirmation are different questions, and this class already answers them apart.

    `commit_tuning` deliberately keeps `_pending` when the tuner raises, so a failed setter is
    retried by the next tune — that is how `set_tone` has always behaved. The station level moves
    with that pending channel, or a failed `set_power` would reach the radio on the next tune while
    the server said it had not. What `status()` reports comes from `_tuned`, which a failed tune
    never updates, so it keeps naming the last level the RADIO confirmed.
    """
    radio, tuner = make_tuned()
    radio.set_frequency(CHANNEL.rx_hz)
    assert radio.status().power == "high"

    tuner.fail = TuneError("the radio did not answer")
    with pytest.raises(TuneError):
        radio.set_power("low")
    assert radio.status().power == "high"        # unconfirmed, so unchanged

    tuner.fail = None
    radio.set_frequency(445_800_000)
    assert tuner.applied[-1].level is PowerLevel.LOW    # the request was not silently dropped
    assert radio.status().power == "low"


def test_status_reports_the_level_and_none_before_any_tune():
    radio, _ = make_tuned()
    assert radio.status().power is None
    radio.set_frequency(CHANNEL.rx_hz)
    assert radio.status().power == "high"
    radio.set_power("low")
    assert radio.status().power == "low"


def test_power_is_unsupported_without_a_tuner():
    """A plain UV-5R holds its power setting on its own front panel."""
    audio_only = create_radio(
        "baofeng", ptt_line="dtr", tx_lead_seconds=0.0,
        _serial_factory=lambda port: FakeSerial(), _audio=FakeAudio(),
    )
    assert Capability.SET_POWER not in audio_only.capabilities()
    with pytest.raises(UnsupportedCapability):
        audio_only.set_power("low")
    assert audio_only.status().power is None


def test_a_nonsense_boot_level_fails_at_construction():
    """Fail-loud at startup, where it is one clear traceback, rather than at the first tune."""
    with pytest.raises(ValueError, match="uvk5_power"):
        make_tuned(uvk5_power="turbo")
