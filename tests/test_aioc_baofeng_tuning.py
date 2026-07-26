"""Baofeng mode with a UV-K5 tuner attached — the part that finally answers the operator's question.

"What good is any of this if I have to set the radio to a different channel each time?" The answer
has to be that applying a preset moves the radio, so what is pinned here is: the right channel
reaches the tuner, a whole preset costs exactly **one** tune rather than four, and nothing tunes
while the transmitter is up.
"""

from __future__ import annotations

import pytest

from radio_server.backends import SHARED_CAPS
from radio_server.backends.base import Capability
from radio_server.backends.uvk5.tuner import TUNING_CAPS
from radio_server.backends.uvk5.vfo import POWER_HIGH, VfoImage
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
