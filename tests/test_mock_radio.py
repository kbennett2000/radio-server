"""Shared-surface behavior of MockRadio: TX recording, canned RX, PTT, busy."""

import pytest

from radio_server.audio import AudioFrame
from radio_server.backends import MockRadio, Radio, RadioStatus, RadioUnavailable
from radio_server.backends.base import BroadcastFm, refuse_if_deafened


def test_mock_is_a_radio():
    assert isinstance(MockRadio(), Radio)


def test_transmit_records_audio_in_order():
    radio = MockRadio()
    assert radio.tx_log == []

    radio.transmit(AudioFrame(b"one"))
    radio.transmit(AudioFrame(b"two"))

    assert radio.tx_log == [AudioFrame(b"one"), AudioFrame(b"two")]


def test_transmit_returns_to_receive_state():
    radio = MockRadio()
    radio.transmit(AudioFrame(b"chunk"))
    # transmit() blocks for audio duration on real hardware; the mock returns idle.
    assert radio.status().transmitting is False


def test_receive_serves_canned_rx():
    radio = MockRadio(canned_rx=AudioFrame(b"canned-audio"))
    assert radio.receive() == AudioFrame(b"canned-audio")


def test_canned_rx_is_settable():
    radio = MockRadio()
    assert radio.receive() == AudioFrame(b"")
    radio.canned_rx = AudioFrame(b"later")
    assert radio.receive() == AudioFrame(b"later")


def test_ptt_toggles_transmitting_in_status():
    radio = MockRadio()
    assert radio.status().transmitting is False

    radio.ptt(True)
    assert radio.status().transmitting is True

    radio.ptt(False)
    assert radio.status().transmitting is False


def test_busy_is_reflected_in_status():
    assert MockRadio(busy=True).status().busy is True
    assert MockRadio(busy=False).status().busy is False


# --- scriptable per-frequency busy (scan-engine hook) --------------------------------------

def test_busy_frequencies_reports_busy_only_when_tuned_to_a_listed_channel():
    radio = MockRadio(busy_frequencies={146_520_000})
    assert radio.status().busy is False  # not yet tuned

    radio.set_frequency(146_500_000)  # a clear channel
    assert radio.status().busy is False

    radio.set_frequency(146_520_000)  # the scripted-busy channel
    assert radio.status().busy is True


def test_busy_frequencies_is_mutable_live():
    # A test can drop the carrier mid-scan by mutating the set.
    radio = MockRadio(busy_frequencies={146_520_000})
    radio.set_frequency(146_520_000)
    assert radio.status().busy is True

    radio.busy_frequencies.discard(146_520_000)
    assert radio.status().busy is False


def test_flat_busy_flag_still_wins_regardless_of_frequency():
    # Back-compat: the flat busy flag is independent of busy_frequencies.
    radio = MockRadio(busy=True, busy_frequencies={146_520_000})
    radio.set_frequency(146_500_000)  # not a listed-busy channel
    assert radio.status().busy is True


def test_busy_frequencies_inert_on_audio_only_backend():
    # An audio-only radio never tunes, so a per-frequency map can't make it busy.
    radio = MockRadio(supports_cat=False, busy_frequencies={146_520_000})
    assert radio.status().busy is False


def test_status_reports_backend_name():
    status = MockRadio().status()
    assert isinstance(status, RadioStatus)
    assert status.backend == "mock"


# --- ADR 0158: the host refuses to key a station that cannot hear itself ----------------------
#
# One predicate, two callers. `refuse_if_deafened` is the whole interlock; `AiocBaofeng` and
# `MockRadio` both consult it so the rule cannot drift between the radio that keys a real PTT pin
# and the one every bridge, controller and browser test drives.


def test_the_predicate_refuses_a_measured_on():
    with pytest.raises(RadioUnavailable) as exc:
        refuse_if_deafened(BroadcastFm(on=True, hz=103_200_000))
    # Diagnosably distinct from the AM refusal: a different chip, a different failure, and it must
    # never be mistakable for `_refuse_if_tx_disabled`'s message (ADR 0150/0155). Two causes that
    # render as one undiagnosable "cannot transmit" is half a diagnosis.
    assert "second receiver" in str(exc.value)
    assert "103.2 MHz" in str(exc.value)
    assert "demodulating" not in str(exc.value)


def test_the_predicate_does_not_refuse_an_unknown():
    # THE LOAD-BEARING DIRECTION. `None` is "the server never learned", which is every radio on
    # firmware older than F8 and every backend without a dock tuner. An unmeasured field must never
    # lock a transmitter — the same rule, and the same reason, as `tx_ok`'s `is not False`.
    refuse_if_deafened(None)


def test_the_predicate_does_not_refuse_a_measured_off():
    # And the third state of the block is distinct from the first two: the server asked, and the
    # answer was no. This is what ADR 0157's tri-state block bought, and the predicate spends it.
    refuse_if_deafened(BroadcastFm(on=False, hz=103_200_000))


def test_the_predicate_still_refuses_without_a_frequency():
    # `hz` is blanked by the firmware on every refusal, so the dangerous state routinely arrives
    # without one. Refusing is not conditional on being able to name the station.
    with pytest.raises(RadioUnavailable) as exc:
        refuse_if_deafened(BroadcastFm(on=True, hz=None))
    assert "an unreported frequency" in str(exc.value)


def test_a_radio_left_in_broadcast_fm_refuses_to_key():
    radio = MockRadio(left_in_broadcast_fm=True)
    with pytest.raises(RadioUnavailable):
        radio.ptt(True)
    with pytest.raises(RadioUnavailable):
        radio.transmit(AudioFrame(b"blind"))
    assert radio.tx_log == []
    assert radio.status().transmitting is False


def test_unkeying_is_never_refused():
    # Refusing to STOP is the dangerous direction in a transmitter: a redundant unkey is harmless,
    # a missed one is a stuck key (ADR 0090/0099). The gate is on the key-up only.
    radio = MockRadio(left_in_broadcast_fm=True)
    radio.ptt(False)
    assert radio.status().transmitting is False


def test_clearing_the_second_receiver_lets_the_radio_key_again():
    radio = MockRadio(left_in_broadcast_fm=True)
    assert radio.clear_broadcast_fm() is True
    radio.ptt(True)
    assert radio.status().transmitting is True


def test_an_ordinary_mock_is_not_gated():
    # The default mock has never been asked, so its block is None and nothing about this cycle may
    # change what it does. This is the regression guard for ~2000 tests that key a plain MockRadio.
    radio = MockRadio()
    assert radio.status().broadcast_fm is None
    radio.ptt(True)
    radio.transmit(AudioFrame(b"fine"))
    assert radio.tx_log == [AudioFrame(b"fine")]
