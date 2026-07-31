"""The capability split: CAT operations exist and are advertised only where supported."""

import pytest

from radio_server.backends import (
    CAT_CAPS,
    FULL_CAPS,
    SHARED_CAPS,
    Capability,
    CatRadio,
    MockRadio,
    UnsupportedCapability,
)


# --- full-capability mock (V71-like) -----------------------------------------


def test_full_mock_advertises_every_capability():
    assert MockRadio().capabilities() == FULL_CAPS


def test_full_mock_is_a_cat_radio():
    assert isinstance(MockRadio(), CatRadio)


def test_cat_methods_run_and_reflect_in_status():
    radio = MockRadio()
    radio.set_frequency(146_520_000)
    radio.set_channel(7)
    radio.set_tone(88.5)
    radio.set_mode("FM")

    status = radio.status()
    assert status.frequency == 146_520_000
    assert status.channel == 7
    assert status.tone == 88.5
    assert status.mode == "FM"
    assert status.tx_frequency is None  # simplex until a split is armed


def test_a_split_shows_in_status_and_a_retune_clears_it():
    """`set_frequency` disarming the split is the fail-safe contract every backend keeps (ADR 0133)."""
    radio = MockRadio()
    radio.set_frequency(146_940_000)
    radio.set_split(146_340_000)
    assert radio.status().tx_frequency == 146_340_000

    radio.set_frequency(147_000_000)
    assert radio.status().tx_frequency is None


def test_scan_toggles():
    radio = MockRadio()
    assert radio.scanning is False
    radio.scan(True)
    assert radio.scanning is True
    radio.scan(False)
    assert radio.scanning is False


# --- audio-only mock (Baofeng-like) ------------------------------------------


def test_audio_only_mock_advertises_shared_only():
    caps = MockRadio(supports_cat=False).capabilities()
    assert caps == SHARED_CAPS
    assert not (caps & CAT_CAPS)


def test_audio_only_mock_is_not_a_cat_radio():
    # It still satisfies the shared Radio protocol...
    from radio_server.backends import Radio

    radio = MockRadio(supports_cat=False)
    assert isinstance(radio, Radio)
    # ...but runtime_checkable only inspects method presence, so we assert the real
    # contract: the CAT methods refuse rather than acting.
    with pytest.raises(UnsupportedCapability):
        radio.set_frequency(146_520_000)


@pytest.mark.parametrize(
    "call, expected_cap",
    [
        (lambda r: r.set_frequency(1), Capability.SET_FREQUENCY),
        (lambda r: r.set_channel(1), Capability.SET_CHANNEL),
        (lambda r: r.set_split(146_340_000), Capability.SET_SPLIT),
        (lambda r: r.set_tone(88.5), Capability.SET_TONE),
        (lambda r: r.set_mode("FM"), Capability.SET_MODE),
        (lambda r: r.scan(True), Capability.SCAN),
    ],
)
def test_cat_methods_raise_with_the_attempted_capability(call, expected_cap):
    radio = MockRadio(supports_cat=False)
    with pytest.raises(UnsupportedCapability) as excinfo:
        call(radio)
    assert excinfo.value.capability is expected_cap


def test_audio_only_status_omits_cat_fields():
    status = MockRadio(supports_cat=False).status()
    assert status.frequency is None
    assert status.channel is None
    assert status.tone is None
    assert status.mode is None


# --- capability set bookkeeping ----------------------------------------------


def test_cap_sets_partition_cleanly():
    assert SHARED_CAPS | CAT_CAPS == FULL_CAPS
    assert SHARED_CAPS.isdisjoint(CAT_CAPS)
    assert len(FULL_CAPS) == len(Capability)


def test_setting_the_bandwidth_and_setting_the_demodulator_are_separate_capabilities():
    """`SET_MODE` is wide/narrow bandwidth; `SET_MODULATION` is FM/AM demodulation (ADR 0150).

    Two different radio settings, reached by two different frames, and either can exist without the
    other — a kv4p has bandwidth and no demodulator control, a UV-K5 on stock firmware has both
    only if you flash it. They are pinned apart here because they are easy to read as synonyms:
    both spell one of their values `"FM"`.
    """
    assert Capability.SET_MODE != Capability.SET_MODULATION
    assert {Capability.SET_MODE, Capability.SET_MODULATION} <= CAT_CAPS
    assert str(Capability.SET_MODULATION) == "set_modulation"


def test_the_demodulator_setter_refuses_on_an_audio_only_backend_like_the_rest():
    radio = MockRadio(supports_cat=False)
    with pytest.raises(UnsupportedCapability) as excinfo:
        radio.set_modulation("AM")
    assert excinfo.value.capability is Capability.SET_MODULATION


def test_the_mock_reports_the_transmit_consequence_of_a_demodulator():
    """The mock mirrors the firmware's own rule — a UV-K5 built without `ENABLE_TX_WHEN_AM`
    refuses its own PTT in anything but FM — so `tx_ok` is exercised without hardware.

    It does not then refuse `transmit`/`ptt`: that refusal belongs to the backend whose keying runs
    through the radio's PTT pin, and there is no radio here to decline.
    """
    radio = MockRadio(supports_cat=True)
    assert radio.status().modulation is None and radio.status().tx_ok is None

    radio.set_modulation("am")                      # accepted in any case, like set_power
    assert (radio.status().modulation, radio.status().tx_ok) == ("AM", False)
    radio.set_modulation("FM")
    assert (radio.status().modulation, radio.status().tx_ok) == ("FM", True)

    with pytest.raises(ValueError, match="FM or AM"):
        radio.set_modulation("USB")
