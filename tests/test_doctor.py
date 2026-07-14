"""AIOC doctor diagnostics (ADR 0029 bring-up): RX-level measurement + RF-mode safety refusal.

All hardware-free: `measure_rx_levels` is driven by a `MockRadio` scripted with known frames and an
injected clock (no real sleeps), and the RF modes (`--tx-tone`/`--key-test`) are checked only for
their refuse-when-unattended guard — actual keying is never exercised in pytest.
"""

from __future__ import annotations

import pytest

from radio_server.audio.tone import synth_tone
from radio_server.backends import MockRadio
from radio_server.doctor import (
    classify_rx_level,
    measure_rx_levels,
    _key_test,
    _tx_tone,
)


class SeqClock:
    """A deterministic clock returning successive preset values (last value repeats forever)."""

    def __init__(self, values):
        self._values = list(values)
        self._i = 0

    def __call__(self) -> float:
        v = self._values[min(self._i, len(self._values) - 1)]
        self._i += 1
        return v


_CFG = {
    "serial_port": "/dev/ttyACM0",
    "ptt_line": "dtr",
    "input_device": "All-In-One-Cable: USB",
    "output_device": "All-In-One-Cable: USB",
    "blocksize": 960,
}


# --- measure_rx_levels -------------------------------------------------------


def test_measure_rx_levels_summarizes_scripted_frames():
    tone = synth_tone(1000.0, 20.0)  # a loud (half-scale) 20 ms tone
    radio = MockRadio(supports_cat=False, rx_frames=[tone, tone, tone])
    # start=0, three iterations see clock<1, the fourth sees 100 and stops → exactly 3 frames read.
    levels = measure_rx_levels(radio, seconds=1.0, clock=SeqClock([0, 0, 0, 0, 100]))
    assert levels.frames == 3
    assert levels.peak_block_rms > 5000  # a half-scale tone is far above any threshold
    assert levels.peak_sample > 15000
    # All three frames are identical, so the overall RMS matches the per-block RMS.
    assert levels.avg_rms == pytest.approx(levels.peak_block_rms, abs=1.0)


def test_measure_rx_levels_reports_silence_when_no_audio():
    radio = MockRadio(supports_cat=False)  # canned_rx is an empty frame — skipped
    levels = measure_rx_levels(radio, seconds=1.0, clock=SeqClock([0, 0, 100]))
    assert levels.frames == 0
    assert levels.avg_rms == 0.0
    assert levels.peak_sample == 0


# --- classify_rx_level (the recommendation branch) ---------------------------


@pytest.mark.parametrize(
    "peak, expected",
    [
        (10.0, "silent"),  # nothing arriving → volume/mixer problem
        (120.0, "gated"),  # arriving but under vad_on=500 → gated out
        (800.0, "ok"),  # above threshold → gate opens
    ],
)
def test_classify_rx_level(peak, expected):
    assert classify_rx_level(peak, vad_on=500.0) == expected


# --- RF modes refuse to run unattended ---------------------------------------


def test_tx_tone_refuses_non_interactive(monkeypatch):
    # CI set → refuse before constructing the radio or transmitting anything (RF safety).
    monkeypatch.setenv("CI", "1")
    assert _tx_tone(_CFG, seconds=2.0, freq=1000.0) == 2


def test_key_test_refuses_non_interactive(monkeypatch):
    monkeypatch.setenv("CI", "1")
    assert _key_test("/dev/ttyACM0", "dtr") == 2
