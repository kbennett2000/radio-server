"""Unit tests for the kv4p HT **Opus** audio edge (ADR 0064, ADR 0065).

Replaces the retired IMA-ADPCM suite. The codec-behaviour tests need a loadable libopus, so they
``importorskip`` (after the shared ``ensure_opus_loadable`` shim) — a bare ``uv run pytest`` skips them
green, and ``uv sync --extra mumble`` runs them for real (the Mumble integration-test precedent). The
missing-libopus test forces the *absent* path and always runs.
"""

from __future__ import annotations

import sys

import numpy as np
import pytest

from radio_server.audio import CANONICAL_FORMAT, AudioFrame
from radio_server.backends.kv4p import audio as kv4p_audio
from radio_server.backends.kv4p.audio import (
    FRAME_BYTES,
    FRAME_SAMPLES,
    Kv4pOpusUnavailable,
    RxAudioDecoder,
    TxAudioEncoder,
)


def _opus_or_skip():
    """Make libopus loadable (ADR 0056/0057), then import opuslib or skip if it isn't installed."""
    from radio_server.link._opus import ensure_opus_loadable

    ensure_opus_loadable()
    return pytest.importorskip("opuslib")


def _pcm(nsamples: int, *, freq: float = 0.0) -> bytes:
    """``nsamples`` of 48k mono s16 — silence by default, or a low-amplitude tone."""
    if freq == 0.0:
        return b"\x00\x00" * nsamples
    t = np.arange(nsamples)
    return (np.sin(t * freq) * 8000).astype("<i2").tobytes()


# --------------------------------------------------------------------------------------
# Frame geometry (no libopus needed)
# --------------------------------------------------------------------------------------


def test_frame_geometry_is_40ms_at_48k():
    assert FRAME_SAMPLES == 1920  # 40 ms @ 48 kHz (OPUS_FRAMESIZE_40_MS)
    assert FRAME_BYTES == 3840  # mono s16le
    assert kv4p_audio.OPUS_RATE == CANONICAL_FORMAT.rate == 48000


# --------------------------------------------------------------------------------------
# RX decode / TX re-block (need libopus)
# --------------------------------------------------------------------------------------


def test_rx_decodes_one_packet_to_one_1920_sample_canonical_frame():
    opuslib = _opus_or_skip()
    enc = opuslib.Encoder(kv4p_audio.OPUS_RATE, kv4p_audio.OPUS_CHANNELS, opuslib.APPLICATION_AUDIO)
    enc.max_bandwidth = opuslib.BANDWIDTH_NARROWBAND
    packet = enc.encode(_pcm(FRAME_SAMPLES, freq=0.05), FRAME_SAMPLES)

    frame = RxAudioDecoder().push(packet)
    assert isinstance(frame, AudioFrame)
    assert frame.format == CANONICAL_FORMAT
    assert len(frame.samples) == FRAME_BYTES  # one 40 ms packet -> one 1920-sample frame


def test_rx_drops_a_corrupt_packet_without_raising():
    _opus_or_skip()
    dec = RxAudioDecoder()
    frame = dec.push(b"\xff\xff\xff")  # a corrupted Opus stream
    assert frame.samples == b""  # dropped, empty frame
    assert frame.format == CANONICAL_FORMAT
    # The decoder survives: a subsequent good packet still decodes.
    import opuslib

    enc = opuslib.Encoder(kv4p_audio.OPUS_RATE, kv4p_audio.OPUS_CHANNELS, opuslib.APPLICATION_AUDIO)
    good = enc.encode(_pcm(FRAME_SAMPLES), FRAME_SAMPLES)
    assert len(dec.push(good).samples) == FRAME_BYTES


def test_tx_reblocks_ragged_input_to_exact_frames_losing_no_samples():
    _opus_or_skip()
    enc = TxAudioEncoder()
    # Ragged pushes that don't line up on a 1920 boundary; 3800 total < 2 whole frames.
    ragged = [1000, 2500, 300]
    packets: list[bytes] = []
    for n in ragged:
        packets += enc.push(AudioFrame(_pcm(n, freq=0.03)))
    assert enc.pending_samples > 0  # a partial frame is held, not dropped
    packets += enc.flush()
    assert enc.pending_samples == 0  # flush drained the remainder

    total_in = sum(ragged)  # 3800
    # Every input sample ships: the emitted frames cover the input, padded up to a whole frame.
    dec = RxAudioDecoder()
    decoded = sum(len(dec.push(p).samples) // 2 for p in packets)
    assert decoded == len(packets) * FRAME_SAMPLES
    assert total_in <= decoded < total_in + FRAME_SAMPLES  # no lost samples, no spurious extra frame


# --------------------------------------------------------------------------------------
# Missing libopus -> a clear, actionable error (always runs)
# --------------------------------------------------------------------------------------


def test_missing_opus_raises_actionable_error_not_bare_importerror(monkeypatch):
    monkeypatch.setitem(sys.modules, "opuslib", None)  # make `import opuslib` raise ImportError
    with pytest.raises(Kv4pOpusUnavailable) as ei:
        RxAudioDecoder().push(b"\x00\x00")
    assert "mumble" in str(ei.value).lower()  # the actionable install hint, not an ImportError

    with pytest.raises(Kv4pOpusUnavailable):
        TxAudioEncoder().push(AudioFrame(b"\x00\x00" * FRAME_SAMPLES))
