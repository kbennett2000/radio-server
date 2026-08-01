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


def test_clearing_the_second_receiver_is_its_own_capability():
    """`CLEAR_BROADCAST_FM` is not a flavour of `SET_MODULATION` (ADR 0157).

    `SET_MODULATION` chooses how the **BK4819** demodulates the station's own channel. This one
    switches off the **BK1080**, a physically separate commercial-FM receiver that shares only the
    antenna front end and the audio amplifier. A radio can be on the right demodulator and still
    hear nothing, because the second chip holds the speaker line — which is the entire fault this
    capability exists to repair.
    """
    assert Capability.CLEAR_BROADCAST_FM in CAT_CAPS
    assert Capability.CLEAR_BROADCAST_FM is not Capability.SET_MODULATION
    assert str(Capability.CLEAR_BROADCAST_FM) == "clear_broadcast_fm"


def test_the_two_directions_are_two_capabilities_and_the_older_name_is_kept():
    """ADR 0164 supplied the on position and did **not** rename the older capability.

    This test used to assert the opposite — that no `set_broadcast_fm` existed — on the grounds that
    a capability called that "would advertise a switch with no on position". That objection was
    narrow and specific, and it expired the moment the switch got one. What did **not** expire is
    the reason for keeping `clear_broadcast_fm`: the name is already published in `/capabilities` on
    every deployed station and documented in `docs/api.md`, so a client may branch on it, and
    removing a published name to improve a word is a breaking change bought with nothing.

    They also do different jobs. `clear_broadcast_fm` still gates the **cost** of the pre-key-up
    rescue (ADR 0161 — a radio that has not earned it pays a set-membership test rather than a 3.0 s
    timeout before every over), and is still the illustration ADR 0157 wanted of a capability that
    reports what a radio has *proved* rather than gating a route.
    """
    assert "clear" in str(Capability.CLEAR_BROADCAST_FM)
    assert str(Capability.SET_BROADCAST_FM) == "set_broadcast_fm"
    assert {Capability.CLEAR_BROADCAST_FM, Capability.SET_BROADCAST_FM} <= CAT_CAPS
    # Both directions have a method behind them on every backend that advertises them — the shape
    # ADR 0158 R4 recorded as missing, and guardrail 3.
    assert hasattr(MockRadio(), "clear_broadcast_fm")
    assert hasattr(MockRadio(), "set_broadcast_fm")


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
