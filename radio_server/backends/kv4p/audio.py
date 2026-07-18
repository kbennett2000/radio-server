"""Pure audio edge for the kv4p HT (ADR 0061) — no I/O.

.. warning::

   **DEAD CODE (ADR 0064).** This whole module implements the **unreleased** ``e9935bd`` IMA-ADPCM
   audio protocol. Shipped firmware (v2.0.0.1, ``3f0e809…``) carries audio as **Opus** on vendor
   command ``0x07`` — variable-length, no 128-byte block, no 16 k↔48 k resample. The Opus cycle
   deletes this module and replaces the RX decoder / TX encoder with ``opuslib`` (ADR 0056/0057
   infra). Retained until then only so the tree is never without a decoder; ``Kv4pHt.receive``
   drops non-ADPCM (Opus) blocks rather than raising (ADR 0064).

The kv4p HT carries audio over its UART as **16 kHz 4-bit IMA ADPCM in WAV block
layout**: a 128-byte block decodes to exactly 249 samples, and 249 samples at 16 kHz
map to 747 at 48 kHz (ratio 3). radio-server's canonical audio is 48 kHz/s16le/mono
(:data:`CANONICAL_FORMAT`, ADR 0006), so this module bridges wire-16k-ADPCM ↔
canonical-48k-PCM. It is the second frame-layer cycle: still no serial I/O, no flow
control, no ``Kv4pHt`` class — just the codec, the resamplers, and the TX re-blocker.

Source of truth: an independent implementation. The block sizing (128 bytes → 249
samples → 747 @ 48 kHz) is ``static_assert``ed in the firmware's own native tests
(``microcontroller-src/test/test_audio_codec/test_audio_codec.cpp``) and defined in
``globals.h`` (``AUDIO_FRAME_BYTES`` / ``AUDIO_FRAME_SAMPLES_WIRE`` /
``AUDIO_FRAME_SAMPLES_48K``), kv4p-ht at the unreleased
``e9935bd37e7505f70ae7023c78fe6a714be90be9`` (dead — see the warning above; ADR 0064).
IMA ADPCM is a public specification,
implemented here from that spec — the firmware's C++ and the Android ``ImaAdpcm.java``
are read as a reference, **not ported** (kv4p-ht is GPL-3.0; radio-server is not).

The firmware owns the *other* half of the rate conversion (its own upsampler/decimator
between the 16 kHz wire and the 48 kHz radio hardware); this module only converts
wire-16k ↔ canonical-48k on the host side.
"""

from __future__ import annotations

import struct

import numpy as np
import soxr

from ...audio import AudioFrame, CANONICAL_FORMAT

# --------------------------------------------------------------------------------------
# Constants (globals.h; test_audio_codec.cpp static_asserts)
# --------------------------------------------------------------------------------------

#: One ADPCM frame on the wire: 4-byte header + 124 data bytes.
AUDIO_FRAME_BYTES = 128
ADPCM_HEADER_BYTES = 4
ADPCM_DATA_BYTES = 124
#: 124 data bytes = 248 nibbles; + the header predictor emitted as sample 0 = 249.
AUDIO_FRAME_SAMPLES_WIRE = 249
#: 249 wire samples at 16 kHz map to 747 at 48 kHz (integer ratio 3, per the firmware).
AUDIO_FRAME_SAMPLES_48K = 747
WIRE_SAMPLE_RATE = 16000

_CANONICAL_RATE = CANONICAL_FORMAT.rate  # 48000

# int16 ↔ float32 idiom shared with audio/dtmf.py and audio/resample.py.
_PCM_DTYPE = np.dtype("<i2")
_INT16_MAX = 32767

#: ``soxr`` streaming quality — **HQ, not the VHQ one-shot of audio/resample.py**. VHQ
#: buffers ~150 ms before emitting (the latency trap ADR 0054 caught); this is a live
#: full-duplex path, so it follows the ``GoertzelStream`` precedent (audio/dtmf.py:682).
RESAMPLE_QUALITY = "HQ"

#: Standard IMA ADPCM step-size table (89 entries) and index-adjust table (16 entries).
_STEP_TABLE: tuple[int, ...] = (
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17,
    19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118,
    130, 143, 157, 173, 190, 209, 230, 253, 279, 307,
    337, 371, 408, 449, 494, 544, 598, 658, 724, 796,
    876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066,
    2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358,
    5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899,
    15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767,
)
_INDEX_TABLE: tuple[int, ...] = (-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8)


def _clamp(value: int, lo: int, hi: int) -> int:
    return lo if value < lo else hi if value > hi else value


# --------------------------------------------------------------------------------------
# IMA ADPCM WAV-block codec (both directions)
#
# Block = 4-byte header [int16 LE predictor | uint8 step index | uint8 reserved=0] then
# 124 data bytes = 248 nibbles, LOW NIBBLE FIRST. The header predictor is emitted
# verbatim as sample 0, so 1 + 248 = 249 samples per block. The predictor feedback is
# sequential and cannot be numpy-vectorized; the per-sample loop runs in pure Python
# ints (int16 numpy arithmetic would wrap), materializing an int16 array at the end.
# --------------------------------------------------------------------------------------


def _decode_nibble(code: int, predictor: int, index: int) -> tuple[int, int]:
    step = _STEP_TABLE[index]
    diff = step >> 3
    if code & 4:
        diff += step
    if code & 2:
        diff += step >> 1
    if code & 1:
        diff += step >> 2
    predictor = predictor - diff if code & 8 else predictor + diff
    predictor = _clamp(predictor, -32768, 32767)
    index = _clamp(index + _INDEX_TABLE[code], 0, 88)
    return predictor, index


def _encode_sample(sample: int, predictor: int, index: int) -> tuple[int, int, int]:
    """Return ``(code, predictor, index)`` — vpdiff mirrors :func:`_decode_nibble` exactly
    so the encoder tracks the decoder's reconstruction."""
    step = _STEP_TABLE[index]
    diff = sample - predictor
    code = 0
    if diff < 0:
        code = 8
        diff = -diff
    vpdiff = step >> 3
    if diff >= step:
        code |= 4
        diff -= step
        vpdiff += step
    if diff >= step >> 1:
        code |= 2
        diff -= step >> 1
        vpdiff += step >> 1
    if diff >= step >> 2:
        code |= 1
        vpdiff += step >> 2
    predictor = predictor - vpdiff if code & 8 else predictor + vpdiff
    predictor = _clamp(predictor, -32768, 32767)
    index = _clamp(index + _INDEX_TABLE[code], 0, 88)
    return code, predictor, index


def decode_adpcm_block(block: bytes) -> np.ndarray:
    """Decode one 128-byte IMA ADPCM block to 249 int16 samples.

    Self-contained: the header seeds the predictor (emitted verbatim as sample 0) and the
    step index, so a block decodes without any cross-block state.
    """
    if len(block) != AUDIO_FRAME_BYTES:
        raise ValueError(f"ADPCM block must be {AUDIO_FRAME_BYTES} bytes, got {len(block)}")
    predictor, index, _reserved = struct.unpack_from("<hBB", block, 0)
    samples = [predictor]
    for byte in block[ADPCM_HEADER_BYTES:]:
        for code in (byte & 0x0F, byte >> 4):  # low nibble first
            predictor, index = _decode_nibble(code, predictor, index)
            samples.append(predictor)
    return np.array(samples, dtype=_PCM_DTYPE)


def encode_adpcm_block(samples: np.ndarray, index: int = 0) -> tuple[bytes, int]:
    """Encode exactly 249 int16 samples to a 128-byte block; return ``(block, next_index)``.

    ``samples[0]`` is written into the header predictor **verbatim** (re-anchoring the
    predictor to the true sample each block bounds round-trip drift and reconstructs
    sample 0 exactly); the incoming ``index`` is written into the header and encoding of
    ``samples[1:]`` continues from it. Decode is self-contained because the header carries
    both. See :class:`AdpcmEncoder` for the cross-block index carry.
    """
    if len(samples) != AUDIO_FRAME_SAMPLES_WIRE:
        raise ValueError(
            f"ADPCM block needs {AUDIO_FRAME_SAMPLES_WIRE} samples, got {len(samples)}"
        )
    index = _clamp(int(index), 0, 88)
    predictor = int(samples[0])
    out = bytearray(struct.pack("<hBB", predictor, index, 0))
    nibbles: list[int] = []
    for k in range(1, AUDIO_FRAME_SAMPLES_WIRE):
        code, predictor, index = _encode_sample(int(samples[k]), predictor, index)
        nibbles.append(code)
    for j in range(0, len(nibbles), 2):  # pack low nibble first
        out.append(nibbles[j] | (nibbles[j + 1] << 4))
    return bytes(out), index


class AdpcmEncoder:
    """Stateful block encoder carrying the step index across blocks.

    The index (signal-dynamics adaptation state) is carried so successive blocks don't
    reset it — avoiding a per-block adaptation artifact — while each block still
    re-anchors its predictor to its own first sample. Decode remains self-contained.
    """

    def __init__(self) -> None:
        self._index = 0

    def encode(self, samples: np.ndarray) -> bytes:
        block, self._index = encode_adpcm_block(samples, self._index)
        return block


# --------------------------------------------------------------------------------------
# Streaming 16k ↔ 48k resampler (soxr ResampleStream, HQ) — GoertzelStream precedent
# --------------------------------------------------------------------------------------


class StreamResampler:
    """A stateful mono float32 resampler over ``soxr.ResampleStream`` (quality HQ).

    ``soxr`` keeps its anti-alias filter state across :meth:`process` calls, so chunked
    feeding produces the same output as one big call. The filter has latency: the first
    chunks emit fewer samples than the ratio implies, and the tail is only released by
    :meth:`flush` (``last=True``), after which the total is exactly the rate ratio.
    """

    def __init__(self, in_rate: int, out_rate: int) -> None:
        self._rs = soxr.ResampleStream(
            in_rate, out_rate, 1, dtype="float32", quality=RESAMPLE_QUALITY
        )

    def process(self, samples: np.ndarray) -> np.ndarray:
        return self._rs.resample_chunk(samples)

    def flush(self) -> np.ndarray:
        """Drain the filter tail. After this the resampler must not be fed again."""
        return self._rs.resample_chunk(np.zeros(0, dtype=np.float32), last=True)


def _to_float32(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype=_PCM_DTYPE).astype(np.float32) / _INT16_MAX


def _to_int16(samples: np.ndarray) -> np.ndarray:
    return np.rint(np.clip(samples, -1.0, 1.0) * _INT16_MAX).astype(_PCM_DTYPE)


# --------------------------------------------------------------------------------------
# TX re-blocking (48k arbitrary → whole 128-byte blocks) and RX decode (block → 48k frame)
# --------------------------------------------------------------------------------------


class TxAudioEncoder:
    """Canonical 48k audio → whole 128-byte ADPCM blocks, holding the remainder.

    ``transmit()`` hands over arbitrary-length 48 kHz frames; this accumulates the
    48k→16k resampler output and emits blocks only on exact 249-sample-at-16k boundaries
    (a whole 128-byte ADPCM block), holding any remainder (< 249 samples) for the next
    push. RX needs no such re-blocker (see :class:`RxAudioDecoder`).
    """

    def __init__(self) -> None:
        self._resampler = StreamResampler(_CANONICAL_RATE, WIRE_SAMPLE_RATE)  # 48k -> 16k
        self._acc = np.zeros(0, dtype=np.float32)  # 16k samples awaiting a full block
        self._encoder = AdpcmEncoder()

    @property
    def pending_samples(self) -> int:
        """Held 16k samples not yet in a whole block (< 249)."""
        return int(self._acc.size)

    def _drain(self) -> list[bytes]:
        blocks: list[bytes] = []
        while self._acc.size >= AUDIO_FRAME_SAMPLES_WIRE:
            block = self._acc[:AUDIO_FRAME_SAMPLES_WIRE]
            self._acc = self._acc[AUDIO_FRAME_SAMPLES_WIRE:]
            blocks.append(self._encoder.encode(_to_int16(block)))
        return blocks

    def push(self, frame: AudioFrame) -> list[bytes]:
        wire = self._resampler.process(_to_float32(frame.samples))
        if wire.size:
            self._acc = np.concatenate([self._acc, wire])
        return self._drain()

    def flush(self) -> list[bytes]:
        """Drain the resampler tail and emit any now-complete blocks (remainder still held)."""
        wire = self._resampler.flush()
        if wire.size:
            self._acc = np.concatenate([self._acc, wire])
        return self._drain()


class RxAudioDecoder:
    """One 128-byte ADPCM block → one canonical 48k :class:`AudioFrame`.

    No re-blocking: :class:`AudioFrame` is format-identity-only with no length contract
    (audio/format.py), so each block's resampled output (≈ 747 samples; the exact count
    varies with soxr's streaming latency, cumulatively 3×) is emitted as one frame. The
    first frame may be empty while the filter fills.
    """

    def __init__(self) -> None:
        self._resampler = StreamResampler(WIRE_SAMPLE_RATE, _CANONICAL_RATE)  # 16k -> 48k

    def push(self, block: bytes) -> AudioFrame:
        wire = decode_adpcm_block(block).astype(np.float32) / _INT16_MAX
        out = self._resampler.process(wire)
        return AudioFrame(_to_int16(out).tobytes())  # defaults to CANONICAL_FORMAT
