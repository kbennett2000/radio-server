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


def test_status_reports_backend_name():
    status = MockRadio().status()
    assert isinstance(status, RadioStatus)
    assert status.backend == "mock"
