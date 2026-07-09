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
