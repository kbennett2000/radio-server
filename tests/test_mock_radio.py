"""Shared-surface behavior of MockRadio: TX recording, canned RX, PTT, busy."""

from radio_server.audio import AudioFrame
from radio_server.backends import MockRadio, Radio, RadioStatus


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
