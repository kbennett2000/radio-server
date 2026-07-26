"""The deviation probe's capture slicer (ADR 0136).

Why this is worth pinning: the probe's whole output is a RATIO between a leg the operator keys by
hand and a leg the script keys, and the receive chain's unknowns only divide out if both legs get
identical treatment. They do not arrive identical — the operator's leg sits somewhere inside a 45 s
window, the script's leg is bracketed by a 0.3 s lead-in and a 1 s tail. Whatever dead air survives
into a measurement drags that leg's amplitude down, and a leg dragged down reads as under-deviation,
which is precisely the finding the probe exists to test for.

So a slicer bug does not fail loudly here. It fabricates the answer we are looking for.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

_PROBE = Path(__file__).resolve().parent.parent / "scripts" / "bench" / "deviation_probe.py"

RATE = 48_000
FRAME = 2


@pytest.fixture(scope="module")
def probe():
    spec = importlib.util.spec_from_file_location("bench_deviation_probe", _PROBE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_deviation_probe"] = module
    spec.loader.exec_module(module)
    return module


def pcm(*segments: tuple[float, float]) -> bytes:
    """Concatenate ``(seconds, amplitude)`` segments of a 141.3 Hz tone into canonical PCM."""
    out: list[np.ndarray] = []
    phase = 0.0
    for seconds, amplitude in segments:
        n = int(seconds * RATE)
        t = (np.arange(n) + phase) / RATE
        out.append((np.sin(2 * np.pi * 141.3 * t) * amplitude * 32767).astype("<i2"))
        phase += n
    return np.concatenate(out).tobytes() if out else b""


def test_finds_a_short_transmission_inside_a_long_quiet_window(probe):
    """The operator keys 8 s somewhere inside 45 s. The other 37 s must not dilute the number."""
    capture = pcm((20.0, 0.0), (8.0, 0.5), (17.0, 0.0))
    best = probe.loudest_slice(capture, 6.0)

    assert len(best) == int(6.0 * RATE) * FRAME
    # Measured over the whole window the tone would be diluted by roughly sqrt(6/45); this asserts
    # the slicer recovers the transmission's own amplitude instead.
    assert probe.band_rms(best, 141.3) == pytest.approx(
        probe.band_rms(pcm((6.0, 0.5)), 141.3), rel=0.02)


def test_the_diluted_whole_window_really_would_have_lied(probe):
    """The failure this guards against, stated as a number rather than a worry."""
    capture = pcm((20.0, 0.0), (8.0, 0.5), (17.0, 0.0))

    whole = probe.band_rms(capture, 141.3)
    sliced = probe.band_rms(probe.loudest_slice(capture, 6.0), 141.3)

    # ~1.5x understated even in the kindest case, where the operator happened to key mid-window.
    # `compare()` calls anything under 0.5 "materially weaker", so this is most of the way to a
    # fabricated verdict on its own.
    assert sliced > whole * 1.4


def test_unsliced_the_answer_depends_on_when_the_operator_pressed_ptt(probe):
    """The strongest argument for slicing: without it the instrument is not even reproducible.

    Identical transmissions — same length, same amplitude, same tone — measured at three keying
    times inside the same window. `band_rms` applies a Hann window, so a transmission near either
    end is attenuated by the window's own taper on top of being diluted by dead air. Reaction time
    alone moves the unsliced number by ~4.5x, which dwarfs the 2x effect the probe is hunting for.
    """
    positions = {
        "early": pcm((2.0, 0.0), (8.0, 0.5), (35.0, 0.0)),
        "middle": pcm((20.0, 0.0), (8.0, 0.5), (17.0, 0.0)),
        "late": pcm((35.0, 0.0), (8.0, 0.5), (2.0, 0.0)),
    }

    unsliced = {k: probe.band_rms(v, 141.3) for k, v in positions.items()}
    sliced = {k: probe.band_rms(probe.loudest_slice(v, 6.0), 141.3) for k, v in positions.items()}

    assert max(unsliced.values()) > min(unsliced.values()) * 4       # not reproducible
    assert max(sliced.values()) == pytest.approx(min(sliced.values()), rel=0.02)   # reproducible


def test_both_legs_get_the_same_treatment_so_equal_signals_compare_equal(probe):
    """The ratio is the product. Two identical transmissions carrying DIFFERENT amounts of dead air
    must still measure the same, because that difference is exactly what separates the two legs."""
    operator_leg = pcm((30.0, 0.0), (8.0, 0.5), (7.0, 0.0))   # keyed late in a long window
    script_leg = pcm((0.3, 0.0), (6.0, 0.5), (1.0, 0.0))      # key_and_listen's bracketing

    a = probe.band_rms(probe.loudest_slice(operator_leg, 6.0), 141.3)
    b = probe.band_rms(probe.loudest_slice(script_leg, 6.0), 141.3)

    assert a == pytest.approx(b, rel=0.02)


def test_picks_the_loudest_of_several_keyings(probe):
    """A false start then the real over: measure the over, not the fumble."""
    capture = pcm((2.0, 0.05), (5.0, 0.0), (7.0, 0.6), (5.0, 0.0))
    best = probe.loudest_slice(capture, 4.0)

    assert probe.band_rms(best, 141.3) == pytest.approx(
        probe.band_rms(pcm((4.0, 0.6)), 141.3), rel=0.02)


def test_a_capture_shorter_than_the_window_is_returned_whole(probe):
    capture = pcm((2.0, 0.4))
    assert probe.loudest_slice(capture, 6.0) == capture


def test_an_odd_length_buffer_does_not_explode(probe):
    """int16 framing is an assumption about someone else's websocket, not a guarantee."""
    capture = pcm((2.0, 0.4)) + b"\x00"
    out = probe.loudest_slice(capture, 6.0)
    assert len(out) % FRAME == 0


def test_empty_capture_is_empty_not_a_crash(probe):
    assert probe.loudest_slice(b"", 6.0) == b""


def test_silence_stays_silence_and_is_still_refused(probe):
    """Slicing must not manufacture a measurable signal out of a gated-silent capture — that is the
    one confusion the whole rig exists to prevent."""
    best = probe.loudest_slice(pcm((45.0, 0.0)), 6.0)
    assert not probe.witness_heard_anything(best)


def test_measure_transmission_reports_the_slice_it_measured(probe):
    """Callers show the operator a duration; it has to be the duration that was measured."""
    capture = pcm((20.0, 0.0), (8.0, 0.5), (17.0, 0.0))
    best, m = probe.measure_transmission(capture, 141.3, 6.0)

    assert len(best) == int(6.0 * RATE) * FRAME
    assert m["bytes"] == len(best)
    assert m["tone_rms"] == pytest.approx(probe.band_rms(best, 141.3), rel=1e-6)
