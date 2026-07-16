"""StreamingDtmfInput (ADR 0038): feed a continuous stream to one persistent multimon process.

Hardware-free: a `FakeDtmfStream` scripts the keys the decoder "recognizes" per `read()` call, so the
grammar/framing is exercised without the `multimon-ng` binary. One `skipif`-guarded test drives the
real `MultimonStream` end-to-end and is the regression for the dropped-repeated-digit bug (`99#`),
which the old fixed-window + dedup path could not decode without an exaggerated inter-key pause.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time

import pytest

from radio_server.audio import (
    CANONICAL_FORMAT,
    AudioFrame,
    DtmfFramer,
    MultimonStream,
    StreamingDtmfInput,
    synth_dtmf,
)
from radio_server.audio.dtmf import WRITE_QUEUE_MAXSIZE


class FakeDtmfStream:
    """A `DtmfStream` double: records writes, and returns a scripted key string per `read()` call.

    ``per_read`` is consumed one entry per `read()`; once exhausted, `read()` returns ``""`` (the
    stream has decoded nothing new). This mirrors how a real streaming decoder reports keys as they
    are recognized — no window/dedup semantics, unlike the buffered fake.
    """

    def __init__(self, per_read):
        self._per_read = list(per_read)
        self._i = 0
        self.writes = 0
        self.closed = False

    def write(self, pcm: bytes) -> None:
        self.writes += 1

    def read(self) -> str:
        v = self._per_read[self._i] if self._i < len(self._per_read) else ""
        self._i += 1
        return v

    def close(self) -> None:
        self.closed = True


def _frame(nbytes: int = 1920) -> AudioFrame:
    """A non-silent frame (content is irrelevant — the fake stream ignores it)."""
    return AudioFrame(b"\x01\x02" * (nbytes // 2), CANONICAL_FORMAT)


def _framer() -> DtmfFramer:
    # timeout huge and every feed stamped now=0.0, so the inter-digit timeout never fires here.
    return DtmfFramer(timeout=1000.0)


def test_repeated_digits_frame_into_one_entry():
    # The crux: two 9s arrive as two distinct keys (multimon's own onset detection) — no dedup drops
    # the second one — so `99#` frames as the entry "99", the bug this ADR fixes.
    stream = FakeDtmfStream(["9", "9", "#"])
    buf = StreamingDtmfInput(stream, _framer())
    f = _frame()
    assert buf.pump(f, 0.0) == []
    assert buf.pump(f, 0.0) == []
    assert buf.pump(f, 0.0) == ["99"]


def test_distinct_and_repeat_mix_frames_intact():
    stream = FakeDtmfStream(["1", "5", "5", "#"])
    buf = StreamingDtmfInput(stream, _framer())
    f = _frame()
    entries = []
    for _ in range(4):
        entries += buf.pump(f, 0.0)
    assert entries == ["155"]


def test_multiple_keys_in_one_read_are_all_fed():
    # A single read() may report several keys at once (the reader thread drained a burst).
    stream = FakeDtmfStream(["99#"])
    buf = StreamingDtmfInput(stream, _framer())
    assert buf.pump(_frame(), 0.0) == ["99"]


def test_on_digit_fires_per_key_post_decode():
    stream = FakeDtmfStream(["9", "9", "#"])
    digits: list[str] = []
    buf = StreamingDtmfInput(stream, _framer(), on_digit=digits.append)
    f = _frame()
    for _ in range(3):
        buf.pump(f, 0.0)
    assert digits == ["9", "9", "#"]


def test_star_clears_a_partial_entry():
    stream = FakeDtmfStream(["9", "*", "1", "#"])
    buf = StreamingDtmfInput(stream, _framer())
    f = _frame()
    entries = []
    for _ in range(4):
        entries += buf.pump(f, 0.0)
    assert entries == ["1"]  # the 9 was cleared by '*'


def test_silent_frame_is_not_written_but_still_reads():
    # An empty frame carries no audio (transport skip): nothing is written, but keys the stream has
    # already decoded are still reported.
    stream = FakeDtmfStream(["9", "#"])
    buf = StreamingDtmfInput(stream, _framer())
    assert buf.pump(_frame(), 0.0) == []  # real frame: write + read "9"
    assert stream.writes == 1
    assert buf.pump(AudioFrame(b"", CANONICAL_FORMAT), 0.0) == ["9"]  # empty: no write, reads "#"
    assert stream.writes == 1  # empty frame did not write


def test_flush_drains_remaining_keys():
    stream = FakeDtmfStream(["7", "#"])
    buf = StreamingDtmfInput(stream, _framer())
    # First pump writes+reads the "7"; flush reads the "#" that completes the entry.
    assert buf.pump(_frame(), 0.0) == []
    assert buf.flush(0.0) == ["7"]


def test_close_propagates_to_the_stream():
    stream = FakeDtmfStream([])
    buf = StreamingDtmfInput(stream, _framer())
    buf.close()
    assert stream.closed is True


def test_write_never_blocks_when_the_multimon_pipe_stalls(monkeypatch):
    # Regression for ADR 0040: the RX pump calls MultimonStream.write on the single event-loop task
    # ahead of the browser audio fan-out, so write MUST NOT block on multimon's stdin. Replace
    # multimon with a process that never reads its stdin, so the OS pipe fills and a direct
    # stdin.write/flush would block; assert write() still returns immediately (the writer thread
    # absorbs the stall) and the hand-off queue stays bounded (drop-oldest).
    real_popen = subprocess.Popen

    def stalled_popen(_args, **kwargs):
        # Ignore the multimon argv; launch a process that sleeps without ever draining stdin.
        return real_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)

    monkeypatch.setattr("radio_server.audio.dtmf.subprocess.Popen", stalled_popen)

    stream = MultimonStream()
    try:
        chunk = b"\x00" * 65536  # >= a Linux pipe buffer, so a blocking flush would stall here
        start = time.monotonic()
        for _ in range(200):
            stream.write(chunk)  # must return at once despite the un-drained pipe (was: blocked)
        elapsed = time.monotonic() - start
        # 200 non-blocking enqueues (+ one spawn), not 200 blocked flushes against a stuck pipe.
        assert elapsed < 2.0
        # Drop-oldest bounds the backlog: the queue never grows past its cap (one chunk may be
        # in-flight in the writer thread, blocked on flush).
        assert stream._write_queue.qsize() <= WRITE_QUEUE_MAXSIZE
    finally:
        stream.close()


@pytest.mark.skipif(
    shutil.which("multimon-ng") is None, reason="multimon-ng not installed; real-decode check"
)
def test_real_multimon_streaming_decodes_repeated_digits():
    # Regression for the dropped-repeated-digit bug: stream 9, gap, 9, gap, # as separate frames
    # through ONE persistent multimon process and assert the entry is "99" (not "9"). The old
    # per-window + dedup path collapsed the two 9s unless a full silent window fell between them.
    stream = MultimonStream()
    buf = StreamingDtmfInput(stream, _framer())
    silence = AudioFrame(b"\x00\x00" * 2400, CANONICAL_FORMAT)  # ~50 ms gap at 48 kHz
    entries: list[str] = []
    try:
        for frame in (
            synth_dtmf("9", 150),
            silence,
            synth_dtmf("9", 150),
            silence,
            synth_dtmf("#", 150),
        ):
            entries += buf.pump(frame, 0.0)
        # multimon decodes on its own reader thread; poll flush() briefly until the entry lands.
        deadline = time.monotonic() + 2.0
        while not entries and time.monotonic() < deadline:
            time.sleep(0.05)
            entries += buf.flush(0.0)
    finally:
        buf.close()
    assert entries == ["99"]
