"""Pure audio-edge tests for the kv4p HT backend (ADR 0061).

No I/O, no hardware: the IMA ADPCM WAV-block codec, the streaming 16k↔48k resamplers,
and TX re-blocking. numpy is used directly (precedent: tests/test_resample.py). Real
byte-for-byte fidelity against the device's own codec is a bench item, not asserted here
(the firmware exposes only the 128/249/747 sizing, not the nibble tables).
"""

from __future__ import annotations

import struct

import numpy as np
import pytest

from radio_server.audio import CANONICAL_FORMAT, AudioFrame
from radio_server.backends.kv4p.audio import (
    AUDIO_FRAME_BYTES,
    AUDIO_FRAME_SAMPLES_WIRE,
    WIRE_SAMPLE_RATE,
    AdpcmEncoder,
    RxAudioDecoder,
    StreamResampler,
    TxAudioEncoder,
    decode_adpcm_block,
    encode_adpcm_block,
)

_CANONICAL_RATE = CANONICAL_FORMAT.rate  # 48000


def _sine_i16(freq: float, n: int, rate: int, amp: float = 0.6) -> np.ndarray:
    t = np.arange(n) / rate
    return np.rint(amp * 32767 * np.sin(2 * np.pi * freq * t)).astype("<i2")


# --------------------------------------------------------------------------------------
# IMA ADPCM codec
# --------------------------------------------------------------------------------------


def test_hand_worked_decode_fixture():
    # Block: predictor=0, index=0, reserved=0; data nibbles [4, 4, 8, 0] -> bytes 0x44, 0x08.
    # Decoded by hand from the IMA step/index tables (STEP[0]=7, STEP[2]=9, STEP[4]=11,
    # STEP[3]=10; INDEX[4]=+2, INDEX[8]=-1, INDEX[0]=-1):
    #   s0 = predictor                                   = 0     (index 0)
    #   n=4: vpdiff = 7>>3 + 7        = 7 ; p = 0+7 = 7   (index 0+2 = 2)
    #   n=4: vpdiff = 9>>3 + 9        = 10; p = 7+10 = 17 (index 2+2 = 4)
    #   n=8: vpdiff = 11>>3          = 1 ; p = 17-1 = 16 (index 4-1 = 3)
    #   n=0: vpdiff = 10>>3          = 1 ; p = 16+1 = 17 (index 3-1 = 2)
    block = struct.pack("<hBB", 0, 0, 0) + bytes([0x44, 0x08]) + bytes(122)
    assert len(block) == AUDIO_FRAME_BYTES
    decoded = decode_adpcm_block(block)
    assert decoded[:5].tolist() == [0, 7, 17, 16, 17]


def test_block_sizes():
    block = struct.pack("<hBB", 100, 5, 0) + bytes(124)
    assert decode_adpcm_block(block).size == AUDIO_FRAME_SAMPLES_WIRE  # 128 in -> 249 out
    samples = _sine_i16(440, AUDIO_FRAME_SAMPLES_WIRE, WIRE_SAMPLE_RATE)
    out, _idx = encode_adpcm_block(samples)
    assert len(out) == AUDIO_FRAME_BYTES  # 249 in -> 128 out


def test_header_predictor_verbatim_and_reserved_zero():
    samples = _sine_i16(600, AUDIO_FRAME_SAMPLES_WIRE, WIRE_SAMPLE_RATE)
    block, _ = encode_adpcm_block(samples)
    predictor, index, reserved = struct.unpack_from("<hBB", block, 0)
    assert predictor == int(samples[0])  # sample 0 stored verbatim
    assert reserved == 0
    # ...and decode reproduces sample 0 exactly from the header.
    assert decode_adpcm_block(block)[0] == int(samples[0])


def test_roundtrip_sine_snr_and_no_index_runaway():
    n = AUDIO_FRAME_SAMPLES_WIRE * 40
    sig = _sine_i16(440, n, WIRE_SAMPLE_RATE)
    enc = AdpcmEncoder()
    recovered = []
    indices = []
    for i in range(0, n, AUDIO_FRAME_SAMPLES_WIRE):
        block = enc.encode(sig[i : i + AUDIO_FRAME_SAMPLES_WIRE])
        indices.append(enc._index)
        recovered.append(decode_adpcm_block(block))
    rec = np.concatenate(recovered).astype(np.float64)
    orig = sig.astype(np.float64)
    snr = 20 * np.log10(np.linalg.norm(orig) / np.linalg.norm(orig - rec))
    assert snr > 24.0  # measured ~30.5 dB; conservative floor (not device-fidelity)
    assert all(0 <= i <= 88 for i in indices)  # step index never runs away


def test_encode_carries_index_across_blocks():
    # A stateful encoder threads the carried-out index into the next block's header.
    sig = _sine_i16(700, AUDIO_FRAME_SAMPLES_WIRE * 3, WIRE_SAMPLE_RATE)
    enc = AdpcmEncoder()
    b0 = enc.encode(sig[0:AUDIO_FRAME_SAMPLES_WIRE])
    b1 = enc.encode(sig[AUDIO_FRAME_SAMPLES_WIRE : 2 * AUDIO_FRAME_SAMPLES_WIRE])
    # b1's header index equals b0's returned index (the carry).
    _p0, idx0_out = encode_adpcm_block(sig[0:AUDIO_FRAME_SAMPLES_WIRE], 0)
    _pred, idx1_hdr, _res = struct.unpack_from("<hBB", b1, 0)
    assert idx1_hdr == idx0_out


def test_decode_rejects_wrong_block_size():
    with pytest.raises(ValueError):
        decode_adpcm_block(bytes(127))


def test_encode_rejects_wrong_sample_count():
    with pytest.raises(ValueError):
        encode_adpcm_block(np.zeros(248, dtype="<i2"))


# --------------------------------------------------------------------------------------
# Streaming resamplers
# --------------------------------------------------------------------------------------


def test_resampler_16k_to_48k_conserves_3x_with_flush():
    # soxr HQ streaming has filter latency (early chunks emit short); the flush releases
    # the tail so the total is exactly the rate ratio. Feed 249-sample blocks.
    n_blocks = 20
    r = StreamResampler(WIRE_SAMPLE_RATE, _CANONICAL_RATE)
    total_in = 0
    out = []
    for b in range(n_blocks):
        x = _sine_i16(300, AUDIO_FRAME_SAMPLES_WIRE, WIRE_SAMPLE_RATE).astype(np.float32) / 32767
        out.append(r.process(x))
        total_in += AUDIO_FRAME_SAMPLES_WIRE
    out.append(r.flush())
    total_out = int(np.concatenate(out).size)
    assert total_out == total_in * 3  # 48000/16000 == 3, exact after flush


def test_resampler_48k_to_16k_conserves_third_with_flush():
    r = StreamResampler(_CANONICAL_RATE, WIRE_SAMPLE_RATE)
    total_in = 0
    out = []
    for _ in range(10):
        x = _sine_i16(300, 960, _CANONICAL_RATE).astype(np.float32) / 32767
        out.append(r.process(x))
        total_in += 960
    out.append(r.flush())
    total_out = int(np.concatenate(out).size)
    assert total_out == total_in // 3  # 16000/48000 == 1/3, exact after flush


def test_resampler_chunked_equals_one_big_call():
    sig = (_sine_i16(350, 2000, WIRE_SAMPLE_RATE).astype(np.float32) / 32767)
    a = StreamResampler(WIRE_SAMPLE_RATE, _CANONICAL_RATE)
    chunked = np.concatenate([a.process(sig[i : i + 97]) for i in range(0, sig.size, 97)])
    b = StreamResampler(WIRE_SAMPLE_RATE, _CANONICAL_RATE)
    one_big = b.process(sig)
    assert chunked.size == one_big.size
    assert np.array_equal(chunked, one_big)  # streaming state, no per-chunk edge artifacts


# --------------------------------------------------------------------------------------
# TX re-blocking and RX decode
# --------------------------------------------------------------------------------------


def _canonical_frame(n_samples: int, freq: float = 500.0) -> AudioFrame:
    return AudioFrame(_sine_i16(freq, n_samples, _CANONICAL_RATE).tobytes())


def test_tx_reblocks_into_whole_blocks_holding_remainder():
    tx = TxAudioEncoder()
    blocks = tx.push(_canonical_frame(960))
    blocks += tx.push(_canonical_frame(100))
    # Every emitted block is a whole 128-byte ADPCM frame; the leftover is held (< 249).
    assert all(len(b) == AUDIO_FRAME_BYTES for b in blocks)
    assert 0 <= tx.pending_samples < AUDIO_FRAME_SAMPLES_WIRE


def test_tx_ragged_stream_loses_no_samples():
    # Total 16k samples the resampler has emitted must equal blocks*249 + held remainder.
    frame_lengths = [960, 100, 480, 1920, 37, 249]
    tx = TxAudioEncoder()
    n_blocks = 0
    concat = []
    for n in frame_lengths:
        frame = _canonical_frame(n)
        concat.append(np.frombuffer(frame.samples, dtype="<i2"))
        n_blocks += len(tx.push(frame))
    # Independent resampler over the concatenation emits the same total (streaming==one-shot).
    ref = StreamResampler(_CANONICAL_RATE, WIRE_SAMPLE_RATE)
    ref_out = ref.process(np.concatenate(concat).astype(np.float32) / 32767)
    assert n_blocks * AUDIO_FRAME_SAMPLES_WIRE + tx.pending_samples == int(ref_out.size)


def test_rx_decode_emits_canonical_frames():
    # A stream of blocks decodes to canonical 48k frames; cumulative length is ~3x the
    # decoded 16k samples (soxr latency makes the first frame short).
    enc = AdpcmEncoder()
    rx = RxAudioDecoder()
    total_out = 0
    n_blocks = 60
    for _ in range(n_blocks):
        samples = _sine_i16(440, AUDIO_FRAME_SAMPLES_WIRE, WIRE_SAMPLE_RATE)
        frame = rx.push(enc.encode(samples))
        assert frame.format == CANONICAL_FORMAT
        assert len(frame.samples) % 2 == 0  # whole int16 samples
        total_out += len(frame.samples) // 2
    wire_total = n_blocks * AUDIO_FRAME_SAMPLES_WIRE
    # Cumulative output never exceeds the 3x rate ratio and converges toward it.
    assert 2.8 * wire_total < total_out <= 3 * wire_total
