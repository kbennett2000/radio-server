"""The canonical format and the fail-loud AudioFrame contract (ADR 0006).

The point of replacing the old `AudioFrame = bytes` alias is that a format mismatch now
*raises* instead of silently producing garbage. These tests pin both halves of that: the
canonical constants, and the fail-loud behaviour on concat and on transmit.
"""

import pytest

from radio_server.audio import (
    CANONICAL_CHANNELS,
    CANONICAL_FORMAT,
    CANONICAL_RATE,
    CANONICAL_WIDTH,
    AudioFormat,
    AudioFormatMismatch,
    AudioFrame,
)
from radio_server.backends import MockRadio

OTHER = AudioFormat(8000, 2, 1)  # a legal-but-different format


# --- canonical constants -----------------------------------------------------


def test_canonical_format_is_48k_s16_mono():
    assert CANONICAL_RATE == 48000
    assert CANONICAL_WIDTH == 2
    assert CANONICAL_CHANNELS == 1
    assert CANONICAL_FORMAT == AudioFormat(48000, 2, 1)


def test_audio_format_value_equality_and_frame_bytes():
    assert AudioFormat(48000, 2, 1) == AudioFormat(48000, 2, 1)
    assert AudioFormat(48000, 2, 1) != AudioFormat(44100, 2, 1)
    assert CANONICAL_FORMAT.frame_bytes == 2  # 16-bit mono
    assert AudioFormat(48000, 2, 2).frame_bytes == 4  # 16-bit stereo


def test_frame_defaults_to_canonical_format():
    assert AudioFrame(b"x").format == CANONICAL_FORMAT


# --- fail-loud on concatenation ---------------------------------------------


def test_same_format_concat_joins_samples_and_keeps_format():
    joined = AudioFrame(b"aa") + AudioFrame(b"bb")
    assert joined == AudioFrame(b"aabb")
    assert joined.format == CANONICAL_FORMAT


def test_mismatched_format_concat_raises():
    with pytest.raises(AudioFormatMismatch):
        AudioFrame(b"aa", CANONICAL_FORMAT) + AudioFrame(b"bb", OTHER)


def test_concat_with_non_frame_raises():
    with pytest.raises(AudioFormatMismatch):
        AudioFrame(b"aa") + b"bb"  # raw bytes is exactly the old silent-coercion trap


# --- fail-loud on transmit ---------------------------------------------------


def test_transmit_records_a_canonical_frame():
    radio = MockRadio()
    radio.transmit(AudioFrame(b"one"))
    assert radio.tx_log == [AudioFrame(b"one")]


def test_transmit_of_wrong_format_raises():
    radio = MockRadio()  # accepts canonical
    with pytest.raises(AudioFormatMismatch):
        radio.transmit(AudioFrame(b"one", OTHER))
    assert radio.tx_log == []  # nothing recorded on rejection


def test_radio_can_be_built_for_a_non_canonical_format():
    radio = MockRadio(format=OTHER)
    radio.transmit(AudioFrame(b"ok", OTHER))
    assert radio.tx_log == [AudioFrame(b"ok", OTHER)]
    with pytest.raises(AudioFormatMismatch):
        radio.transmit(AudioFrame(b"no", CANONICAL_FORMAT))
