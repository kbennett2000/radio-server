"""Activity detection: RMS energy, the audio-VAD gate (hysteresis + hang), the CAT-busy gate,
config selection, and the wired pump (ADR 0015).

Pure signal processing on synthetic canonical PCM, `FakeClock` for the hang timer — no hardware,
no real sleeps. Frames are built as constant-amplitude int16 so a frame's RMS is exactly its
value, which makes the threshold crossings deterministic (a raised-cosine `synth_tone` would smear
the RMS via its envelope). The pump-integration proof reuses `test_rx_audio`'s `_pump_out` helper.

The load-bearing proofs: a loud frame opens the gate and a silent one closes it; a level between
the on/off thresholds neither opens a closed gate nor closes an open one (hysteresis, no chatter);
the hang holds the gate open through a short gap then closes once the window lapses; the CAT gate
tracks scripted `MockRadio` busy status; `build_rx_gate` selects the right gate per `RADIO_SQUELCH`
and fails loud on bad config; and, wired into the cycle-13 pump, dead-air frames are suppressed
while live frames pass.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from radio_server.activity import (
    AudioLevelGate,
    CatBusyGate,
    SquelchMode,
    build_rx_gate,
    frame_rms,
)
from radio_server.audio import AudioFrame, synth_tone
from radio_server.backends import MockRadio
from radio_server.rx import pass_through_gate

from .conftest import FakeClock, make_settings
from .test_rx_audio import _pump_out

FRAME_LEN = 480  # 10 ms @ 48 kHz — an arbitrary non-empty length


def pcm(rms: int, n: int = FRAME_LEN) -> AudioFrame:
    """A constant-amplitude canonical frame whose RMS is exactly `rms` (int16 units)."""
    return AudioFrame(np.full(n, rms, dtype="<i2").tobytes())


# --- frame_rms -----------------------------------------------------------------------------

def test_frame_rms_silence_is_zero():
    assert frame_rms(AudioFrame(b"\x00\x00" * FRAME_LEN)) == 0.0


def test_frame_rms_empty_frame_is_zero():
    assert frame_rms(AudioFrame(b"")) == 0.0


def test_frame_rms_loud_tone_is_large():
    # A 0.8-full-scale sine sits well above any sane squelch threshold.
    assert frame_rms(synth_tone(1000, 50, amplitude=0.8)) > 10_000


def test_frame_rms_constant_frame_equals_amplitude():
    assert frame_rms(pcm(700)) == pytest.approx(700.0)


# --- AudioLevelGate: open/close, hysteresis, hang ------------------------------------------

def gate(clock: FakeClock, *, hang: float = 1.0) -> AudioLevelGate:
    return AudioLevelGate(on_threshold=1000, off_threshold=500, hang=hang, clock=clock)


def test_on_off_thresholds_must_have_hysteresis():
    with pytest.raises(ValueError):
        AudioLevelGate(on_threshold=500, off_threshold=500, hang=1.0)


def test_loud_frame_opens_silent_frame_closes():
    g = gate(FakeClock(), hang=0.0)  # no hang: a below-off frame closes immediately
    assert g(pcm(2000)) is True  # above on-threshold → open
    assert g(pcm(0)) is False  # below off-threshold, hang lapsed → closed


def test_hysteresis_holds_open_between_thresholds():
    g = gate(FakeClock(), hang=0.0)
    assert g(pcm(2000)) is True  # open
    # A level between off (500) and on (1000) holds the open gate open — no chatter.
    assert g(pcm(700)) is True
    assert g(pcm(700)) is True


def test_hysteresis_does_not_open_a_closed_gate_between_thresholds():
    g = gate(FakeClock(), hang=0.0)
    # Starting closed, a between-thresholds level is not enough to open — it needs the on-threshold.
    assert g(pcm(700)) is False
    assert g(pcm(700)) is False


def test_hang_holds_open_through_a_short_gap_then_closes():
    clock = FakeClock()
    g = gate(clock, hang=1.0)
    assert g(pcm(2000)) is True  # open, hang window armed to now+1.0
    clock.advance(0.5)  # a short gap, within the hang window
    assert g(pcm(0)) is True  # silent, but hang keeps it open
    clock.advance(0.6)  # now past the hang window (total 1.1 s > 1.0)
    assert g(pcm(0)) is False  # closed


# --- CatBusyGate ---------------------------------------------------------------------------

def test_cat_gate_tracks_radio_busy_status():
    radio = MockRadio(busy=False)
    g = CatBusyGate(radio)
    frame = pcm(0)  # frame content is irrelevant to a CAT gate
    assert g(frame) is False
    radio.busy = True
    assert g(frame) is True
    radio.busy = False
    assert g(frame) is False


def test_cat_gate_ignores_frame_content():
    # A loud frame does not open a CAT gate when the radio reports the channel clear.
    radio = MockRadio(busy=False)
    assert CatBusyGate(radio)(pcm(30000)) is False


# --- build_rx_gate: config selection -------------------------------------------------------

def test_build_rx_gate_off_is_pass_through():
    assert build_rx_gate(make_settings({}), radio=MockRadio()) is pass_through_gate


def test_build_rx_gate_audio_builds_level_gate():
    g = build_rx_gate(make_settings({"audio.squelch": "audio"}), radio=MockRadio())
    assert isinstance(g, AudioLevelGate)


def test_build_rx_gate_cat_builds_busy_gate():
    g = build_rx_gate(make_settings({"audio.squelch": "cat"}), radio=MockRadio())
    assert isinstance(g, CatBusyGate)


def test_build_rx_gate_reads_vad_thresholds_from_env():
    settings = make_settings(
        {
            "audio.squelch": "audio",
            "audio.vad_on_rms": 2000,
            "audio.vad_off_rms": 1000,
            "audio.vad_hang": 0.0,
        }
    )
    g = build_rx_gate(settings, radio=MockRadio())
    assert g(pcm(1500)) is False  # below the configured on-threshold → stays closed
    assert g(pcm(2500)) is True  # above it → opens


def test_build_rx_gate_bad_hysteresis_fails_loud():
    # Both thresholds are individually valid; the on>off invariant is a cross-field check that
    # AudioLevelGate.__init__ raises (ValueError), not the schema.
    settings = make_settings(
        {"audio.squelch": "audio", "audio.vad_on_rms": 500, "audio.vad_off_rms": 800}
    )
    with pytest.raises(ValueError):
        build_rx_gate(settings, radio=MockRadio())


def test_build_rx_gate_unknown_mode_fails_loud():
    with pytest.raises(RuntimeError):
        make_settings({"audio.squelch": "loud"})


def test_squelch_mode_values():
    assert {m.value for m in SquelchMode} == {"off", "audio", "cat"}


# --- wired into the cycle-13 pump ----------------------------------------------------------

def test_pump_suppresses_dead_air_passes_live_frames():
    loud, silent = pcm(2000), pcm(0)
    frames = [loud, silent, loud, silent]
    # hang=0 makes suppression independent of the pump's real-clock timing.
    g = AudioLevelGate(on_threshold=1000, off_threshold=500, hang=0.0)
    out = asyncio.run(_pump_out(frames, gate=g))
    assert out == [loud.samples, loud.samples]  # only the live frames reached the hub
