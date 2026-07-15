"""BufferedDtmfInput (ADR 0030): accumulate short frames into a window, decode, dedup held tones.

Hardware-free: a `FakeDtmfDecoder` scripts one digit-string per decoded window, and frames are plain
byte blobs (content is irrelevant — the fake ignores it). The real-multimon round-trip is guarded by
`skipif`. This is the same accumulate-and-dedup core the live controller and `doctor --dtmf` share.
"""

from __future__ import annotations

import shutil

import pytest

from radio_server.audio import (
    CANONICAL_FORMAT,
    AudioFrame,
    BufferedDtmfInput,
    DtmfFramer,
    MultimonDtmfDecoder,
    dtmf_window_bytes,
    synth_dtmf,
)


class FakeDtmfDecoder:
    """Returns a scripted digit string per ``decode()`` call (one per window); '' once exhausted."""

    def __init__(self, per_window):
        self._per_window = list(per_window)
        self._i = 0

    def decode(self, frame) -> str:
        v = self._per_window[self._i] if self._i < len(self._per_window) else ""
        self._i += 1
        return v


def _frame(nbytes: int) -> AudioFrame:
    """A frame of ``nbytes`` bytes of arbitrary (non-silent) audio."""
    return AudioFrame(b"\x01\x02" * (nbytes // 2), CANONICAL_FORMAT)


def _framer() -> DtmfFramer:
    # timeout huge and every feed is stamped now=0.0, so the inter-digit timeout never fires here.
    return DtmfFramer(timeout=1000.0)


def test_buffers_until_window_then_decodes_once():
    dec = FakeDtmfDecoder(["1"])
    buf = BufferedDtmfInput(dec, _framer(), window_bytes=1920)
    half = _frame(960)
    assert buf.pump(half, 0.0) == []  # 960 < 1920 — nothing decoded yet
    assert dec._i == 0  # decoder untouched while the window fills
    assert buf.pump(half, 0.0) == []  # now 1920 — one decode fires ("1" is not an entry)
    assert dec._i == 1  # decoded exactly once


def test_frames_an_entry_on_submit_across_windows():
    dec = FakeDtmfDecoder(["1", "2", "#"])  # one key per filled window
    buf = BufferedDtmfInput(dec, _framer(), window_bytes=960)
    f = _frame(960)  # each frame fills one window
    assert buf.pump(f, 0.0) == []
    assert buf.pump(f, 0.0) == []
    assert buf.pump(f, 0.0) == ["12"]  # "#" submits the accumulated entry


def test_dedups_a_held_tone_and_resets_on_a_silent_window():
    # "9" held across three windows (multimon re-emits it), then a silent window (a gap), then "9"
    # again → a second, distinct press. on_digit sees exactly two nines.
    dec = FakeDtmfDecoder(["9", "9", "9", "", "9"])
    digits: list[str] = []
    buf = BufferedDtmfInput(dec, _framer(), window_bytes=960, on_digit=digits.append)
    f = _frame(960)
    for _ in range(5):
        buf.pump(f, 0.0)
    assert digits == ["9", "9"]


def test_dedup_off_keeps_every_detection():
    dec = FakeDtmfDecoder(["5", "5", "5"])
    digits: list[str] = []
    buf = BufferedDtmfInput(dec, _framer(), window_bytes=960, dedup=False, on_digit=digits.append)
    f = _frame(960)
    for _ in range(3):
        buf.pump(f, 0.0)
    assert digits == ["5", "5", "5"]


def test_flush_decodes_the_partial_tail():
    dec = FakeDtmfDecoder(["7#"])
    buf = BufferedDtmfInput(dec, _framer(), window_bytes=1_000_000)  # never fills from one frame
    assert buf.pump(_frame(960), 0.0) == []  # buffered, not decoded
    assert dec._i == 0
    assert buf.flush(0.0) == ["7"]  # tail decoded: "7#" → entry "7"


@pytest.mark.skipif(
    shutil.which("multimon-ng") is None, reason="multimon-ng not installed; real-decode check"
)
def test_real_multimon_buffered_round_trip():
    # window_bytes=1 → each 200 ms synth tone forms its own window and decodes through real multimon.
    buf = BufferedDtmfInput(MultimonDtmfDecoder(), _framer(), window_bytes=1)
    entries: list[str] = []
    for d in "5#":
        entries += buf.pump(synth_dtmf(d, 200), 0.0)
    assert entries == ["5"]


def test_dtmf_window_bytes_matches_canonical_rate():
    # 0.5 s of 48 kHz mono s16le = 24000 samples × 2 bytes = 48000 bytes.
    assert dtmf_window_bytes(0.5) == (CANONICAL_FORMAT.rate // 2) * CANONICAL_FORMAT.frame_bytes
