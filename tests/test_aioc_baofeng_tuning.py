"""Baofeng mode with a UV-K5 tuner attached — the part that finally answers the operator's question.

"What good is any of this if I have to set the radio to a different channel each time?" The answer
has to be that applying a preset moves the radio, so what is pinned here is: the right channel
reaches the tuner, a whole preset costs exactly **one** tune rather than four, and nothing tunes
while the transmitter is up.
"""

from __future__ import annotations

import logging

import pytest

from radio_server.backends import SHARED_CAPS
from radio_server.backends.aioc_baofeng import BOOT_MODULATION
from radio_server.backends.base import (
    BroadcastFm,
    Capability,
    RadioUnavailable,
    UnsupportedCapability,
)
from radio_server.backends.uvk5 import frames as f
from radio_server.audio import AudioFrame
from radio_server.backends.uvk5.tuner import (
    SERIAL_TX_LOCKOUT_S, TUNING_CAPS, VALID_MODULATIONS, EepromTuner, HybridTuner, SetVfoTuner,
    TuneError, Uvk5Tuner,
)
from radio_server.backends.uvk5.vfo import POWER_HIGH, PowerLevel, VfoImage
from radio_server.backends import create_radio
from radio_server.presets import DEFAULT_MODULATION, Preset, apply_preset
from tests.test_aioc_baofeng import FakeAudio, FakeSerial
# The dock-radio fake, imported rather than re-modelled: the startup assert must run the REAL
# SetVfoTuner, whose 0x0878 reply checking is half of what "the belief is true" means, and a second
# local model of 0x0877 would be a second wire spec in a repo whose whole ADR 0148/0149 argument is
# that there is one. Cross-module test imports are the house style (FakeAudio/FakeSerial above).
from tests.test_uvk5_tuner import FakeSetVfoRadio


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

    def __init__(
        self, mod_fail=None, fm_fail=None, fm_stuck=False, radio_in_fm=None, fm_blocks_tx=None, **kw
    ):
        super().__init__(**kw)
        self.modulation: str | None = None
        self.tx_ok: bool | None = None
        self.mod_calls: list[str] = []
        #: A radio that does not confirm — switched off, or pre-F7 firmware that drops 0x0877. The
        #: `fail`/`reassert_fail` idiom of the tuners above. Since ADR 0155 this is the ONLY way a
        #: capable tuner still reports `None` after construction, so it is how the tests that pin
        #: the unknown-never-blocks rule reach that state.
        self.mod_fail = mod_fail
        #: The same, for `0x0879` (ADR 0157): a radio that never answers, so nothing is learned and
        #: the block stays the honest `None`. Written when F8 was an unmerged fork branch and this
        #: was every radio in existence; F8 merged to fork `main` at `d086a23` on 2026-07-31, so it
        #: is now the *older-firmware* case rather than the universal one (ADR 0158).
        self.fm_fail = fm_fail
        #: The dangerous state, and the one this fake could not reach before ADR 0158: the radio
        #: ANSWERED and reported the second receiver still running. Not an error — a measurement,
        #: and the only one that gates a key-up.
        self.fm_stuck = fm_stuck
        #: **The radio's ACTUAL state, which is not the same thing as the last reading** (ADR 0161).
        #: Before this cycle the two could not diverge in a test, because nothing re-read: the block
        #: was written once at construction and then only the block existed. This is what an operator
        #: pressing F+0 changes, and what the OFF leg changes back — so a test can now set it after
        #: construction and ask what the host does about it.
        self.radio_in_fm = fm_stuck if radio_in_fm is None else radio_in_fm
        #: `0x087A` flags **bit 1** as this radio reports it (ADR 0159's F9 interlock). `None` is a
        #: pre-F9 image, which answers 0 and is indistinguishable from "not blocked" — the polarity
        #: that decision on the wire exists to protect.
        self.fm_blocks_tx = fm_blocks_tx
        self.broadcast_fm: BroadcastFm | None = None
        self.fm_calls = 0
        self._fm_seen = False

    def capabilities(self):
        caps = TUNING_CAPS | {Capability.SET_MODULATION}
        # Earned, exactly as `SetVfoTuner` earns it: only a radio that ANSWERED `0x0879` proves the
        # firmware has the command, so this fake must not hand it out from configuration either.
        return caps | {Capability.CLEAR_BROADCAST_FM} if self._fm_seen else caps

    def set_modulation(self, modulation: str) -> bool:
        self.mod_calls.append(modulation)   # the frame went out either way...
        if self.mod_fail is not None:
            raise self.mod_fail             # ...and the radio did not confirm it, so nothing is
        self.modulation = modulation        # recorded — SetVfoTuner's record-only-on-success rule
        self.tx_ok = modulation == "FM"     # the firmware's own rule
        return self.tx_ok

    def clear_broadcast_fm(self) -> bool:
        self.fm_calls += 1                  # the frame went out either way...
        if self.fm_fail is not None:
            # Nothing was learned — and since ADR 0161 that BLANKS any earlier reading rather than
            # leaving it standing. With a per-key-up caller, a previous key-up's `on=False` left in
            # place after a re-read that failed is a reading old enough to be a lie, rendered as a
            # measurement.
            self.broadcast_fm = None
            raise self.fm_fail
        self._fm_seen = True                # ANY answer earns the capability, refusals included
        # The OFF leg does not merely observe: it stops the receiver. That is the whole shape of the
        # wire — there is no read-only action, so the one frame that reports live state is the frame
        # that gives the station its ears back (ADR 0161). A `fm_stuck` radio is the exception, and
        # is a MEASUREMENT rather than an error: `SetVfoTuner` records `on=True` and returns False
        # rather than raising, because the caller needs the reading more than the exception.
        if not self.fm_stuck:
            self.radio_in_fm = False
        self.broadcast_fm = BroadcastFm(
            on=self.radio_in_fm, hz=103_200_000, blocks_tx=self.fm_blocks_tx
        )
        return not self.radio_in_fm


def test_a_modulation_capable_tuner_adds_the_demodulator_and_earns_the_second_receiver():
    """Two capabilities, reaching two different chips, arriving by two different routes.

    `SET_MODULATION` is advertised from configuration — it is a claim about the tuner mode. The
    broadcast-FM one is **not**: it appears only once a radio has actually answered `0x0879`, which
    is what makes it a claim about the firmware rather than about a config key (ADR 0157). The
    assertion is written as a before/after precisely so a regression to a static member would fail
    here as well as in the tuner's own tests.
    """
    tuner = ModulationTuner(fm_fail=TuneError("pre-F8 firmware drops 0x0879"))
    assert _with_tuner(tuner).capabilities() == SHARED_CAPS | TUNING_CAPS | {
        Capability.SET_MODULATION
    }
    assert tuner.fm_calls == 1              # ...and it was asked, rather than skipped

    earned = _with_tuner(ModulationTuner()).capabilities()
    assert earned == SHARED_CAPS | TUNING_CAPS | {
        Capability.SET_MODULATION, Capability.CLEAR_BROADCAST_FM
    }


def test_set_modulation_reaches_the_tuner_and_is_reported_in_status():
    radio = _with_tuner(ModulationTuner())
    # FM because this server ASSERTED it at construction and the radio confirmed it (ADR 0155) —
    # still NOT the FM the firmware happens to seed, which is what ADR 0132 forbids adopting. The
    # distinction is the whole of that cycle: one is a measurement we caused, the other a guess
    # about state we never touched.
    assert radio.status().modulation == "FM"
    assert radio.status().tx_ok is True

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
    """`None` is "nobody has asked this radio", and it must not refuse. A station that would not
    transmit until someone had chosen a demodulator would be a worse failure than the one this
    prevents.

    Reached through a startup assert that FAILED, because since ADR 0155 that is the only way a
    capable tuner still reports `None` — the radio was switched off when the server came up. Which
    makes this the best-effort path's most important consequence: a station whose radio came up
    second must still key.
    """
    tuner = ModulationTuner(mod_fail=TuneError("no 0x0878 reply — the radio is not powered on"))
    radio = _with_tuner(tuner)
    radio.set_frequency(CHANNEL.rx_hz)
    assert tuner.mod_calls == ["FM"]     # the frame went out...
    assert tuner.tx_ok is None           # ...and was not confirmed, so nothing was asserted

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
    inventing an assertion here would move the radio off whatever the operator left it on.

    Since ADR 0155 the startup assert normally satisfies that condition, so the state this pins is
    now reached only when the startup assert did not land — the radio was off, the frame went out,
    nothing came back. The invariant is unchanged: the key-up re-assert fires off what this server
    KNOWS, and it knows nothing here.
    """
    tuner = ModulationTuner(mod_fail=TuneError("no 0x0878 reply — the radio is not powered on"))
    radio = _with_tuner(tuner)
    radio.set_frequency(CHANNEL.rx_hz)
    assert tuner.mod_calls == ["FM"]            # the startup assert went out...

    radio.ptt(True)
    try:
        assert tuner.mod_calls == ["FM"]        # ...was not confirmed, so nothing re-asserts it
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


# --- the demodulator this server states at startup (ADR 0155) ----------------------------------

def test_a_restart_against_a_radio_left_on_am_reports_what_it_is_actually_demodulating():
    """The whole cycle in one assertion: the server's belief equals the radio's state.

    The operator was listening to airband and restarted the host. The firmware keeps its modulation
    in the dock SESSION — RAM this process restarting does not touch — so the radio is still on AM,
    and there is no opcode that can be asked. Before ADR 0155 the server reported
    `modulation=None, tx_ok=None`; `_refuse_if_tx_disabled` refuses only a MEASURED False, so the
    first key-up drove DTR into a transmitter VFO_STATE_TX_DISABLE had already disabled — silence
    over the air, `status()` reporting `transmitting`, and the transmission it ate was the station
    ID (guardrail 5).

    Fixed by WRITING, not reading: the server states FM, the radio confirms it on 0x0878, and the
    belief is true because we made it true rather than because we guessed well.
    """
    fake = FakeSetVfoRadio(left_on=f.DockModulation.AM)
    radio = _with_tuner(SetVfoTuner(fake))

    # Not "reports FM" — reports what the radio is ON. The equality is the point: asserting the
    # literal would still pass on a host that had guessed right by luck.
    assert radio.status().modulation == fake.demodulating
    assert fake.demodulating == "FM"        # ...and it MOVED, rather than us adopting the AM found
    assert radio.status().tx_ok is True     # measured on the same 0x0878, not assumed


def test_a_radio_that_is_not_there_at_startup_still_builds_the_backend(caplog):
    """Best-effort, and the asymmetry with the fail-loud port setup above is the point.

    That one fails when the serial HANDLE is unusable, after which nothing works, so there is
    nothing to degrade to. This one fails when a good handle has a radio switched off at the far end
    — an ordinary Tuesday, because the operator powers the radio after the server. Failing the
    construction would make such a station unstartable, to fix a fault that only bites on a key-up.

    Also the pin on a conversion this backend depends on but does not perform: `set_modulation`
    turns `Uvk5Timeout` into `TuneError`, so `Uvk5Timeout` cannot reach the backend's except clause
    and is deliberately absent from it. If a refactor ever drops that conversion, THIS goes red at
    construction — which widening the tuple would have hidden.
    """
    fake = FakeSetVfoRadio(mod_silent=True, fm_silent=True)   # a radio that is not there answers NOTHING
    with caplog.at_level(logging.WARNING):
        radio = _with_tuner(SetVfoTuner(fake, timeout=0.01))

    assert radio.status().backend == "baofeng"          # it came up
    assert radio.status().modulation is None            # ...knowing nothing, and saying so
    assert radio.status().tx_ok is None
    assert radio.status().broadcast_fm is None          # ...about the second receiver too

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "could not state the demodulator" in warnings[0]     # the cause...
    assert "POST /modulation" in warnings[0]                    # ...and the remedy
    # And the broadcast-FM leg said nothing at WARNING, deliberately: its silence is
    # indistinguishable from pre-F8 firmware, which today is EVERY radio. A warning on every boot of
    # every station would train operators to ignore the one beside it, which is the one that matters.


def test_an_eeprom_tuner_is_never_asked_for_a_modulation_it_cannot_set():
    """The real `EepromTuner`, so `capabilities()` is the shipped `TUNING_CAPS` rather than a fake's
    guess. This is what makes the CAPABILITY gate different from a `hasattr` gate: `EepromTuner` has
    a `set_modulation`, and it exists only to raise `UnsupportedCapability` (guardrail 3), so a
    `hasattr` gate would put that exception straight through a constructor."""
    fake = FakeSetVfoRadio()
    radio = _with_tuner(EepromTuner(fake))

    assert fake.sent == []                              # not one frame
    assert radio.status().modulation is None
    assert fake.demodulating == "FM"                    # the firmware's own seed, left alone


def test_a_plain_uv5r_says_nothing_at_startup():
    """No tuner at all — a UV-5R has no UART on that jack. Construction must stay what it was: open
    the handle, force both lines low, and nothing else."""
    serial = FakeSerial()
    radio = create_radio(
        "baofeng", ptt_line="dtr", tx_lead_seconds=0.0,
        _serial_factory=lambda port: serial, _audio=FakeAudio(),
    )
    assert serial.events == [("rts", False), ("dtr", False)]
    assert radio.status().modulation is None
    assert radio.status().tx_ok is None


def test_the_startup_assert_arms_no_transmit_lockout(monkeypatch):
    """A boot step that cost six seconds of dead transmitter would be a different, worse cycle.

    `0x0877` is a dock opcode and the dock opcodes do not arm `SERIAL_TX_LOCKOUT_S` — only the EEPROM
    path does. Asserted twice: the tuner reports no deadline, and the key-up that follows waits for
    nothing.
    """
    slept: list[float] = []
    radio = _with_tuner(SetVfoTuner(FakeSetVfoRadio()))
    assert radio.tx_ready_in() is None
    assert radio.status().tx_ready_in is None

    radio.set_frequency(CHANNEL.rx_hz)
    monkeypatch.setattr("radio_server.backends.aioc_baofeng.time.sleep", slept.append)
    radio.ptt(True)
    try:
        assert slept == []
    finally:
        radio.ptt(False)


def test_the_startup_assert_is_exactly_two_frames_in_this_order_and_no_session():
    """`0x0879` then `0x0877`, and nothing else. Still no HELLO — that would arm the six-second
    lockout the test above exists to prevent — and still no `0x0873`, which would move the radio off
    a channel nobody chose.

    **The order is the assertion, not an artefact.** Severity-first: a station in broadcast FM hears
    nothing at all, which is strictly worse than a station on the wrong demodulator, so the worse
    fault is repaired first (ADR 0157).

    Two orderings arguments were considered and REJECTED on the evidence, and are recorded here so
    they do not get re-invented:

    * *"modulation-first re-deafens the radio via `Dock_RestoreFmAudio`"* — no. That helper restores
      audio for a BK1080 that is **already** running; it preserves the deafness, it cannot create it.
    * *"OFF-first stalls the following `0x0877` behind the OFF leg's flash erase and it gets
      dropped"* — no. `dock.c` sends the reply as its **last** statement, after `hal->set_fm`
      returns, and this tuner is synchronous request/reply, so the erase is absorbed by the round
      trip rather than colliding with the next frame.
    """
    fake = FakeSetVfoRadio()
    _with_tuner(SetVfoTuner(fake))

    assert len(fake.sent) == 2
    assert isinstance(fake.sent[0], f.ClearBroadcastFm)
    assert isinstance(fake.sent[1], f.SetModulation)
    assert fake.sent[1].modulation == f.DockModulation.FM


def test_a_radio_that_refuses_to_leave_am_is_reported_as_unknown_not_as_am(caplog):
    """The residual, pinned rather than fixed.

    A radio that will not move is the one case where writing cannot make the belief true. What must
    stay true is the weaker claim: an unconfirmed write reports `None` — never the value it asked
    for, and never the value it found. Recording what a refusal reported would also be recording
    nothing, because the firmware blanks the reply's modulation to 0xFF on any non-APPLIED status.
    """
    fake = FakeSetVfoRadio(left_on=f.DockModulation.AM, mod_status=f.ModulationStatus.ERR_BUSY)
    with caplog.at_level(logging.WARNING):
        radio = _with_tuner(SetVfoTuner(fake))

    assert radio.status().modulation is None            # not "FM" (asked) and not "AM" (found)
    assert radio.status().tx_ok is None
    assert fake.demodulating == "AM"                    # the radio never moved
    assert [r.levelname for r in caplog.records] == ["WARNING"]


def test_the_boot_modulation_is_one_the_wire_can_carry():
    """The only guard against a typo'd constant, since `ValueError` is deliberately NOT caught by
    the startup assert — a bad value must fail at construction as one clear traceback.

    The second assertion documents today's coincidence and is NOT a coupling: the two constants
    answer different questions and would change for different reasons (see `BOOT_MODULATION`'s own
    comment). If a cycle ever moves the preset default, this line is where that argument gets
    re-had rather than silently lost.
    """
    assert BOOT_MODULATION in VALID_MODULATIONS
    assert BOOT_MODULATION == DEFAULT_MODULATION


# --- the second receiver this server switches off at startup (ADR 0157) ------------------------

def test_a_restart_against_a_radio_left_in_broadcast_fm_reports_it_off_and_sent_the_frame():
    """**The fail-first for this cycle.** A server started against a deaf radio must report it off
    and have sent the frame that made it so.

    The operator was listening to broadcast FM and the host crashed. The firmware persists that mode
    to flash behind the host — `app.c:1761-1767` calls `FM_Start()` about five seconds after any
    squelch close — so the radio comes back up with the BK1080 running, holding the speaker line the
    AIOC listens on. The station therefore hears **nothing** on its own channel.

    And it transmits anyway: `RADIO_PrepareTX` has no `gFmRadioMode` term at all (its only reference
    to it in the whole of `radio.c` is the VOX gate). So the automatic station ID goes out into a
    channel nobody is monitoring — guardrail 5's requirement met in form and defeated in substance.

    Both halves are asserted, because either alone is a false pass: reporting off without sending
    proves only that the default is `False`, and sending without reading back proves only that a
    frame left the host.
    """
    fake = FakeSetVfoRadio(left_in_fm=True)
    radio = _with_tuner(SetVfoTuner(fake))

    assert isinstance(fake.sent[0], f.ClearBroadcastFm)      # ...the frame that made it so
    assert fake.broadcast_fm_on is False                     # ...and the radio actually stopped
    assert radio.status().broadcast_fm == BroadcastFm(on=False, hz=103_200_000, blocks_tx=False)


def test_unknown_and_off_are_different_answers():
    """The distinction this whole arc exists to preserve, at the one place it is most dangerous.

    A `null` block means the server does not know whether the station can hear. A block saying
    `on=False` means it does know, and the answer is no. Collapsing them would let "we never asked"
    render identically to "verified hearing" — which is exactly how a deaf station gets trusted.
    """
    unknown = _with_tuner(SetVfoTuner(FakeSetVfoRadio(fm_silent=True), timeout=0.01))
    assert unknown.status().broadcast_fm is None

    known = _with_tuner(SetVfoTuner(FakeSetVfoRadio(left_in_fm=True)))
    assert known.status().broadcast_fm is not None
    assert known.status().broadcast_fm.on is False


def test_a_radio_that_will_not_leave_broadcast_fm_is_reported_as_still_deaf_and_loudly(caplog):
    """The residual, pinned rather than fixed — and the one case that warrants a WARNING.

    A reply that REFUSED is an F8 radio talking, so its silence-vs-refusal distinction is real: this
    is not "we could not tell", it is "we asked and the radio would not". `on=True` alongside a
    perfectly healthy `tx_ok` is not a contradiction; it is the dangerous combination itself.
    """
    fake = FakeSetVfoRadio(left_in_fm=True, fm_stuck=True)
    with caplog.at_level(logging.WARNING):
        radio = _with_tuner(SetVfoTuner(fake))

    assert radio.status().broadcast_fm == BroadcastFm(on=True, hz=103_200_000, blocks_tx=True)
    assert radio.status().tx_ok is True          # deaf, and will still key. Both true at once.
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "broadcast FM" in warnings[0]


def test_a_failed_broadcast_fm_clear_does_not_skip_the_demodulator_assert(caplog):
    """Independent `try/except` per assert — ADR 0153's lesson, in the place it would bite next.

    One shared handler would have a radio on pre-F8 firmware (which is every radio today) lose the
    ADR 0155 demodulator assert as collateral, silently, for ever. The frame still goes out and the
    second assert still lands.
    """
    fake = FakeSetVfoRadio(fm_silent=True, left_on=f.DockModulation.AM)
    with caplog.at_level(logging.INFO):
        radio = _with_tuner(SetVfoTuner(fake, timeout=0.01))

    assert radio.status().broadcast_fm is None      # the first assert failed...
    assert radio.status().modulation == "FM"        # ...and the second one still happened
    assert fake.demodulating == "FM"


def test_an_eeprom_tuner_is_never_asked_to_clear_a_receiver_it_cannot_reach():
    """Gated on the CAPABILITY, never on `hasattr`: `EepromTuner` HAS a `clear_broadcast_fm`, and it
    exists only to raise (guardrail 3), so a `hasattr` gate would put that exception through a
    constructor. Stock firmware has no `0x0879` case at all."""
    fake = FakeSetVfoRadio()
    radio = _with_tuner(EepromTuner(fake))

    assert fake.sent == []                              # not one frame, of either kind
    assert radio.status().broadcast_fm is None


def test_a_plain_uv5r_is_not_asked_about_a_receiver_it_does_not_have():
    """No tuner at all. Construction stays exactly what it was."""
    serial = FakeSerial()
    radio = create_radio(
        "baofeng", ptt_line="dtr", tx_lead_seconds=0.0,
        _serial_factory=lambda port: serial, _audio=FakeAudio(),
    )
    assert serial.events == [("rts", False), ("dtr", False)]
    assert radio.status().broadcast_fm is None


def test_clearing_broadcast_fm_arms_no_transmit_lockout(monkeypatch):
    """`0x0879` is a dock opcode, and dock opcodes do not arm `SERIAL_TX_LOCKOUT_S` — only the EEPROM
    path does. Worth its own test despite the sibling above, because this leg is the one that writes
    flash: the OFF leg calls `SETTINGS_WriteCurrentState()` to undo what `app.c`'s restore countdown
    put there. Flash traffic and the six-second lockout are independent facts, and the whole reason
    ADR 0156 checked is that assuming otherwise had them backwards.
    """
    slept: list[float] = []
    radio = _with_tuner(SetVfoTuner(FakeSetVfoRadio(left_in_fm=True)))
    assert radio.tx_ready_in() is None
    assert radio.status().tx_ready_in is None

    radio.set_frequency(CHANNEL.rx_hz)
    monkeypatch.setattr("radio_server.backends.aioc_baofeng.time.sleep", slept.append)
    radio.ptt(True)
    try:
        assert slept == []
    finally:
        radio.ptt(False)


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


# --- ADR 0158: the host refuses to key a station that cannot hear itself -----------------------
#
# ADR 0157 gave the server a broadcast-FM status block. This is what the block is FOR. The BK1080
# holds the speaker line the AIOC listens on, and `gFmRadioMode` does not gate `RADIO_PrepareTX` —
# so the radio hears nothing on its own channel and transmits anyway, station ID included. The
# firmware cannot be relied on to stop it, so the host must.


def test_a_station_left_in_broadcast_fm_refuses_the_key_up():
    # THE FAIL-FIRST, backend half. `fm_stuck` is a radio that ANSWERED 0x0879 and reported the
    # receiver still running — a measurement, not an error, which is why it reaches the gate at all.
    tuner = ModulationTuner(fm_stuck=True)
    radio = _with_tuner(tuner)
    assert radio.status().broadcast_fm == BroadcastFm(on=True, hz=103_200_000)

    with pytest.raises(RadioUnavailable) as exc:
        radio.ptt(True)
    assert "second receiver" in str(exc.value)
    assert radio.status().transmitting is False


def test_the_refusal_names_a_different_fault_than_the_am_one():
    # "Cannot transmit" with two possible causes and no way to tell them apart is half a diagnosis.
    # The two refusals send an operator to two different places — one to the Demodulation control,
    # one to the radio's own EXIT key — so they may never collapse into one sentence.
    stuck = ModulationTuner(fm_stuck=True)
    radio_deaf = _with_tuner(stuck)
    with pytest.raises(RadioUnavailable) as deaf:
        radio_deaf.ptt(True)

    am = ModulationTuner()
    radio_am = _with_tuner(am)
    am.set_modulation("AM")
    with pytest.raises(RadioUnavailable) as non_fm:
        radio_am.ptt(True)

    assert "demodulating" in str(non_fm.value)
    assert "demodulating" not in str(deaf.value)
    assert "second receiver" in str(deaf.value)
    assert "second receiver" not in str(non_fm.value)


def test_a_station_that_never_learned_is_not_gated():
    # THE LOAD-BEARING DIRECTION, and the one that would take the whole fleet off the air if it
    # regressed. `fm_fail` is a radio that never answered — older firmware, or switched off at
    # startup — so the block is `None`. An unmeasured field must never lock a transmitter; the same
    # rule `tx_ok` has carried since ADR 0155, for the same reason.
    tuner = ModulationTuner(fm_fail=TuneError("no 0x087A reply"))
    radio = _with_tuner(tuner)
    assert radio.status().broadcast_fm is None

    radio.ptt(True)
    assert radio.status().transmitting is True


def test_a_measured_off_is_not_gated_either():
    # And the third state is distinct from both: the server asked, and the answer was no.
    tuner = ModulationTuner()
    radio = _with_tuner(tuner)
    assert radio.status().broadcast_fm == BroadcastFm(on=False, hz=103_200_000)
    radio.ptt(True)
    assert radio.status().transmitting is True


# Placement is still pinned, but by `test_the_clear_still_comes_before_the_channel_reassert` in the
# ADR 0161 section below: the assertion this test made is a strict subset of that one's, and the
# reason for the ordering changed when the check became a frame rather than an attribute read.


def test_deafness_is_named_before_the_demodulator_when_both_are_wrong():
    # Severity ordering, and it is not cosmetic. An operator told only about AM would set FM and get
    # a station that now DOES transmit and still cannot hear — strictly worse than where they
    # started. Same ordering ADR 0157 gave the two boot asserts.
    tuner = ModulationTuner(fm_stuck=True)
    radio = _with_tuner(tuner)
    tuner.set_modulation("AM")
    assert tuner.tx_ok is False      # both faults are live

    with pytest.raises(RadioUnavailable) as exc:
        radio.ptt(True)
    assert "second receiver" in str(exc.value)


def test_a_one_shot_transmit_is_gated_too():
    # `transmit()` self-keys through the same `_key_on`, which is how every StationId call and every
    # DTMF voice service reaches the air. Gating only `ptt()` would leave the Part 97 ID ungated.
    tuner = ModulationTuner(fm_stuck=True)
    radio = _with_tuner(tuner)
    with pytest.raises(RadioUnavailable):
        radio.transmit(AudioFrame(b"\x00" * 32))
    assert radio.status().transmitting is False


def test_unkeying_a_deaf_station_is_never_refused():
    # A redundant unkey is harmless; a missed one is a stuck key (ADR 0090/0099). The gate is on the
    # key-up only, and must never stand between a keyed transmitter and its release.
    tuner = ModulationTuner(fm_stuck=True)
    radio = _with_tuner(tuner)
    radio.ptt(False)
    assert radio.status().transmitting is False


# --- ADR 0161: the host asks the radio instead of remembering ---------------------------------
#
# ADR 0160 measured, on hardware, that the gate above can never fire from the path that actually
# creates the state at runtime. `tuner.broadcast_fm` is written in ONE place — the boot assert — and
# that assert CLEARS the very condition the gate tests. So after startup the block is permanently
# `{on: false}`, and an operator pressing F+0 on the radio's own keypad leaves the station deaf with
# every gate above silent. The gate is not bypassed; it is blind.
#
# The fix is not a better gate, it is a fresh reading. And the wire decides what "fresh reading"
# means: `0x0879` has no read-only action, `ClearBroadcastFm` builds only OFF, and the firmware
# reports state AFTER acting — so the one frame that can answer "is this station deaf" is the frame
# that gives it its ears back. A key-up on a deaf station is therefore REPAIRED rather than refused,
# and the refusal below survives only for the radio that will not comply.


def test_broadcast_fm_switched_on_after_startup_is_seen_at_the_next_key_up():
    """**The fail-first for this cycle**, and it is ADR 0160 item 6 without a radio: F+0 at the
    front panel, after the boot assert has already run and recorded `off`.

    On master the host never asks again, so the boot memory still says `on: false`, the gate stays
    silent, and the key-up goes out into a channel the station cannot hear — station ID included.
    """
    tuner = ModulationTuner()
    radio = _with_tuner(tuner)
    assert radio.status().broadcast_fm == BroadcastFm(on=False, hz=103_200_000)  # the boot assert
    at_boot = tuner.fm_calls

    tuner.radio_in_fm = True              # the operator presses F+0. Nothing tells the server.
    radio.ptt(True)

    assert tuner.fm_calls == at_boot + 1, "the key-up asked the radio rather than its own memory"
    assert tuner.radio_in_fm is False, "and the answer it got back was the station hearing again"
    assert radio.status().transmitting is True


def test_a_radio_that_will_not_leave_broadcast_fm_still_refuses_the_key_up():
    """The other half of the same frame. Asking is not the same as being obeyed: a radio that
    answers APPLIED and reports the receiver still running is the malfunction the read-back doctrine
    exists to catch, and it is the only case left where the host refuses for deafness at all.

    Red on master for a different reason than the test above — there the host keys because it never
    asked; here it would key because the boot memory says `off` while the radio says `on`.
    """
    tuner = ModulationTuner()
    radio = _with_tuner(tuner)
    tuner.radio_in_fm = True
    tuner.fm_stuck = True                 # ...and it will not stop when told

    with pytest.raises(RadioUnavailable) as exc:
        radio.ptt(True)
    assert "second receiver" in str(exc.value)
    assert radio.status().transmitting is False
    assert radio.status().broadcast_fm.on is True


def test_the_latch_is_gone_and_clearing_it_at_the_radio_is_enough():
    """ADR 0158 decision 5, reversed on purpose. That latch was never a mechanism — it was the
    emergent property of never re-reading — and with F9 refusing in the firmware a host lockout that
    cannot clear stops protecting anything and starts refusing key-ups the radio would allow.

    Red on master: once the block reads `on: true` nothing ever rewrites it, so this second key-up
    is refused for ever, on a station that has been hearing perfectly well since EXIT was pressed.
    """
    tuner = ModulationTuner(fm_stuck=True)
    radio = _with_tuner(tuner)
    with pytest.raises(RadioUnavailable):
        radio.ptt(True)                   # deaf, and the radio would not comply

    tuner.fm_stuck = False                # the operator presses EXIT. No restart, no /radio/select.
    tuner.radio_in_fm = False

    radio.ptt(True)
    assert radio.status().transmitting is True
    assert radio.status().broadcast_fm == BroadcastFm(on=False, hz=103_200_000)


def test_a_pre_key_up_clear_that_the_radio_never_answers_refuses_and_says_so():
    """A clear that was ATTEMPTED and failed is not the same as one that was never sent, and the
    operator must not get a generic key-up failure for it.

    It refuses, and that is not the "an unmeasured field must never lock a transmitter" rule being
    broken: that rule is about a radio nobody has asked. This radio EARNED the capability by
    answering `0x087A` at least once and has now stopped — which `_reassert_channel` would refuse on
    three lines later anyway, on the same timeout, for the same reason.
    """
    tuner = ModulationTuner()
    radio = _with_tuner(tuner)            # boot assert answers, so the capability is earned
    assert Capability.CLEAR_BROADCAST_FM in radio.capabilities()

    tuner.fm_fail = TuneError("no 0x087A reply to the clear-broadcast-FM frame")
    with pytest.raises(RadioUnavailable) as exc:
        radio.ptt(True)

    reason = str(exc.value)
    assert "could not ask the radio" in reason, "it names the attempt, not just the outcome"
    # The discriminator is the CLAIM, not the vocabulary. Both messages are about the same chip and
    # both may say so; what may never be shared is the assertion. The deafened message states that
    # the receiver IS running and that it was asked to stop; this one states the opposite — that
    # nothing is known — and sends the operator somewhere else entirely.
    assert "is running" not in reason
    assert "does NOT know" in reason
    assert "demodulating" not in reason, "and it is not the AM refusal either"
    assert radio.status().transmitting is False


def test_a_failed_re_read_blanks_the_reading_it_could_not_refresh():
    """`null`, not a previous key-up's `on: false`. The block was honest when it was written and is
    now a reading old enough to be a lie — and rendering it as a measurement is precisely how a deaf
    station gets trusted (ADR 0157's whole tri-state argument, arriving one cycle later)."""
    tuner = ModulationTuner()
    radio = _with_tuner(tuner)
    assert radio.status().broadcast_fm == BroadcastFm(on=False, hz=103_200_000)

    tuner.fm_fail = TuneError("the radio stopped answering")
    with pytest.raises(RadioUnavailable):
        radio.ptt(True)
    assert radio.status().broadcast_fm is None


def test_a_radio_that_never_earned_the_capability_pays_nothing_at_key_up():
    """**Not evidence — this passes on master too**, and it is here because it is the direction that
    would take the whole fleet off the air if it regressed.

    The gate is the EARNED `Capability.CLEAR_BROADCAST_FM`, so a radio on firmware that cannot answer
    pays one frozenset membership test per key-up: no frame, no 3.0 s `SetVfoTuner` timeout, no
    `TuneError` refusing every over on every station (ADR 0158 R2's warning, answered).
    """
    tuner = ModulationTuner(fm_fail=TuneError("pre-F8 firmware drops 0x0879"))
    radio = _with_tuner(tuner)
    assert Capability.CLEAR_BROADCAST_FM not in radio.capabilities()
    at_boot = tuner.fm_calls

    radio.ptt(True)
    assert tuner.fm_calls == at_boot, "no frame is spent on a radio that cannot answer it"
    assert radio.status().transmitting is True


def test_the_clear_still_comes_before_the_channel_reassert():
    """Placement, re-argued rather than inherited. ADR 0158 put this first partly because it was
    strictly cheaper than the step it displaced — one attribute read against `_reassert_channel`'s
    two frames. **That argument dies here**: this is a frame now too.

    The other two stand, and they are the ones that decide it. Severity-first matches the boot
    asserts (and a test already pins that order there); and `_reassert_channel` can raise a
    `TuneError` of its own, so refusing after it would let an unrelated tune failure hand the
    operator a message about the channel on a station whose real problem is that it cannot hear.
    """
    tuner = ModulationTuner(fm_stuck=True)
    radio = _with_tuner(tuner)
    radio.set_frequency(CHANNEL.rx_hz)
    tuner.reasserted.clear()
    tuner.mod_calls.clear()
    at_boot = tuner.fm_calls

    with pytest.raises(RadioUnavailable):
        radio.ptt(True)

    assert tuner.fm_calls == at_boot + 1   # the deafness question was asked...
    assert tuner.reasserted == []          # ...and answered before a 0x0873 was spent
    assert tuner.mod_calls == []           # or a 0x0877


def test_one_key_up_asks_once_no_matter_how_many_frames_follow():
    """Streaming `transmit()` returns early when already keyed, so the frame is per KEY-UP, not per
    audio frame. At ~0.1 s a round trip (ADR 0160), per-frame would be a transmitter that never
    keys."""
    tuner = ModulationTuner()
    radio = _with_tuner(tuner)
    at_boot = tuner.fm_calls

    radio.ptt(True)
    for _ in range(5):
        radio.transmit(AudioFrame(b"\x00" * 32))
    assert tuner.fm_calls == at_boot + 1


def test_unkeying_never_sends_the_frame_either():
    """Refusing to STOP is the dangerous direction in a transmitter (ADR 0090/0093/0099), and a
    frame in the un-key path is one more thing that can raise between a keyed transmitter and its
    release.

    **Not evidence — vacuously green on master**, where no key path sends the frame at all. It is a
    regression guard for the direction that would be worst to get wrong.
    """
    tuner = ModulationTuner()
    radio = _with_tuner(tuner)
    radio.ptt(True)
    at_key_up = tuner.fm_calls

    radio.ptt(False)
    assert tuner.fm_calls == at_key_up
    assert radio.status().transmitting is False


def test_the_block_carries_the_firmwares_own_refusal_bit():
    """`0x087A` flags bit 1 — F9's report that the radio is refusing to key on this build, right now
    (ADR 0159). It rides in the broadcast-FM block because that is the frame it came from; it is
    NOT written to `tx_ok`, which belongs to the demodulator and to `0x0878`.
    """
    tuner = ModulationTuner(fm_stuck=True, fm_blocks_tx=True)
    radio = _with_tuner(tuner)
    assert radio.status().broadcast_fm == BroadcastFm(
        on=True, hz=103_200_000, blocks_tx=True
    )
    assert radio.status().tx_ok is True    # the BK4819 is on FM and says so, orthogonally

    with pytest.raises(RadioUnavailable) as exc:
        radio.ptt(True)
    # The wording follows the bit: on an F9 radio the firmware is refusing too, so the host must
    # stop claiming to be the thing that refuses.
    assert "the radio itself" in str(exc.value)


def test_a_pre_f9_radio_is_told_the_truth_about_itself_instead():
    """Bit 1 clear on a deaf radio is not a contradiction — it is an F8 or non-interlock image
    saying, correctly, that it WILL key. That is the more dangerous state and gets the harsher
    sentence, which is the whole reason the bit reports blocking rather than readiness.

    **Not evidence — green on master too**, because master has only this one sentence and no bit to
    branch on. Its value is as the negative half of the pair: it is what fails if the F9 wording of
    `test_the_block_carries_the_firmwares_own_refusal_bit` is ever applied unconditionally.
    """
    tuner = ModulationTuner(fm_stuck=True, fm_blocks_tx=False)
    radio = _with_tuner(tuner)
    with pytest.raises(RadioUnavailable) as exc:
        radio.ptt(True)
    assert "would transmit into it anyway" in str(exc.value)
    assert "the radio itself" not in str(exc.value)


def test_the_refusal_no_longer_tells_anyone_to_restart_the_server():
    """The remedy that is no longer true. With the block re-read before every key-up, pressing EXIT
    at the radio IS the whole remedy — and a message still demanding a restart would send an
    operator to reboot a station that would have worked on the next press."""
    tuner = ModulationTuner(fm_stuck=True)
    radio = _with_tuner(tuner)
    with pytest.raises(RadioUnavailable) as exc:
        radio.ptt(True)
    assert "restart" not in str(exc.value).lower()


# --- ADR 0158: tuner conformance is structural, not remembered --------------------------------
#
# ADR 0157's finding 7 was that a fake advertising SET_MODULATION gets asked to clear broadcast FM,
# and one lacking the method raises AttributeError out of a CONSTRUCTOR, which no
# `(TuneError, OSError)` handler catches. The recorded fix — "declare the method on the protocol" —
# was already true and therefore a no-op. The real defect is that nothing ever CHECKED the protocol.


def test_every_production_tuner_satisfies_the_protocol():
    fake = FakeSetVfoRadio()
    assert isinstance(SetVfoTuner(fake), Uvk5Tuner)
    assert isinstance(EepromTuner(fake), Uvk5Tuner)
    assert isinstance(HybridTuner(fake, fake), Uvk5Tuner)


def test_the_fake_that_reaches_the_boot_assert_satisfies_it_too():
    # `ModulationTuner` is the only double here that advertises SET_MODULATION, so it is the only
    # one the boot assert calls into. Pinning its conformance is what turns finding 7's trap into a
    # named failure instead of an AttributeError from a constructor three files away.
    assert isinstance(ModulationTuner(), Uvk5Tuner)


class HalfATuner(VolatileTuner):
    """Finding 7's exact shape: advertises SET_MODULATION, implements it — and has no
    `clear_broadcast_fm` and no `broadcast_fm` at all.

    This is what a fake looks like the moment somebody adds a protocol member and updates only the
    production tuners. On master it takes the whole backend down at construction.
    """

    def capabilities(self):
        return TUNING_CAPS | {Capability.SET_MODULATION}

    def set_modulation(self, modulation: str) -> bool:
        self.modulation = modulation
        self.tx_ok = modulation == "FM"
        return self.tx_ok

    modulation = None
    tx_ok = None


def test_a_tuner_that_claims_the_capability_without_the_method_is_skipped_not_crashed():
    """A non-conforming tuner must land exactly where a failed assert lands: `None`, and a warning.

    Not "off" — that is the one wrong answer that matters, because it is an affirmative claim that
    the station can hear. And not a crash: this runs inside `AiocBaofeng.__init__`, so an
    AttributeError here takes the whole backend down at construction.
    """
    assert not isinstance(HalfATuner(), Uvk5Tuner)
    radio = _with_tuner(HalfATuner())          # must not raise
    assert radio.status().broadcast_fm is None  # honest: the server does not know

    # AND the demodulator assert still ran. This is the half that pins the decision NOT to put the
    # same guard on `_assert_boot_modulation`: `Uvk5Tuner` is a fat protocol and `isinstance` is
    # all-or-nothing across ten members, so gating the ADR 0155 assert on it would let a missing
    # `clear_broadcast_fm` — a member with nothing to do with the demodulator — silently cost this
    # station its demodulator assert. That is ADR 0153's "a failure of the first must not skip the
    # second" reappearing through a conformance check instead of a shared try/except.
    assert radio.status().modulation == BOOT_MODULATION


def test_the_skip_says_which_tuner_did_not_conform(caplog):
    with caplog.at_level(logging.WARNING, logger="radio_server.backends.aioc_baofeng"):
        _with_tuner(HalfATuner())
    # WARNING, not INFO — a non-conforming tuner is a programming fault, unlike a silent radio,
    # which is an ordinary fact of life on firmware older than F8. And it must NAME the type, or
    # the operator gets a warning they cannot act on.
    assert "HalfATuner" in caplog.text


def test_a_tuner_that_does_not_claim_the_capability_is_skipped_silently(caplog):
    # `SpyTuner` has never conformed and never will — it predates all of this and exists to prove a
    # duck-typed tuner still keys. It does not advertise SET_MODULATION, so the capability gate
    # excludes it FIRST and no warning fires. The warning must mark the coupling bug, not the suite:
    # a line that appears on every construction is one operators learn to scroll past.
    with caplog.at_level(logging.WARNING, logger="radio_server.backends.aioc_baofeng"):
        make_tuned()
    assert "SpyTuner" not in caplog.text
