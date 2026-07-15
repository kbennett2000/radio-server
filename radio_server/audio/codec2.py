"""Codec2 mode 3200 encode/decode over ``libcodec2`` via ``ctypes`` (ADR 0049).

This is the codec seam for the M17 backend arc: it turns the canonical 48 kHz s16le mono
:class:`~radio_server.audio.format.AudioFrame` into Codec2 3200 bps packets and back. It is a
**thin dynamic-linking wrapper** and deliberately nothing more, for a licensing reason that is
load-bearing: radio-server is MIT, Codec2 is LGPL-2.1, and only *dynamic* linking against the
unmodified system ``libcodec2.so`` keeps the MIT license intact. So the seam is
``ctypes.util.find_library("codec2")`` + ``ctypes.CDLL`` and the C ABI — **no** vendored source
and **no** GPL Python binding (``pycodec2`` and friends), either of which would pull the project
under copyleft.

The module is never imported at rest — only a configured M17 backend constructs :class:`Codec2`.
A missing ``libcodec2`` is therefore a config error, surfaced loudly at construction naming the
library and the ``codec2`` extra (the same shape as the missing-piper-voice path, ADR 0009).

Frame geometry (samples/bits per frame) is a property of the installed library, not a constant
to trust from memory (guardrail 1): it is queried at runtime and asserted against the mode-3200
assumptions, failing loud if the installed build disagrees.
"""

from __future__ import annotations

import ctypes
from ctypes.util import find_library  # module-global so a test can monkeypatch the loader seam

import numpy as np

from .format import CANONICAL_FORMAT, AudioFormat, AudioFormatMismatch, AudioFrame
from .resample import resample, to_canonical

#: The C enum value for ``CODEC2_MODE_3200`` (M17 full rate). The only mode this cycle supports.
CODEC2_MODE_3200 = 0

#: Codec2 3200 operates on 8 kHz signed-16-bit mono PCM; canonical audio (48 kHz, ADR 0006) is
#: resampled to/from this rate at the tolerant edge via the shared ``soxr`` resampler.
CODEC2_RATE = 8000

_LIBRARY_NAME = "codec2"

#: What mode 3200 is *assumed* to be — checked against the library's queried geometry, never
#: used as the operating geometry (guardrail 1). 160 samples (20 ms @ 8 kHz), 64 bits = 8 bytes.
_EXPECTED_SAMPLES_PER_FRAME = 160
_EXPECTED_BITS_PER_FRAME = 64

_MISSING_MSG = (
    "libcodec2 not found; the M17/Codec2 path needs the system Codec2 library "
    "(apt install codec2 / libcodec2-dev) plus the 'codec2' extra "
    "(pip install 'radio-server[codec2]'). It is loaded dynamically via ctypes."
)

#: numpy dtype for the canonical signed-16-bit little-endian byte layout (host-endian
#: independent). Native ``int16`` is used across the ctypes boundary (what the C ABI expects);
#: this layout is used only at the ``AudioFrame`` byte boundary, matching the codebase convention.
_PCM_DTYPE = np.dtype("<i2")


class Codec2:
    """Codec2 mode-3200 codec: canonical :class:`AudioFrame` <-> Codec2 3200 packets.

    Constructing this fails loud (``RuntimeError`` naming ``libcodec2``) if the system library is
    absent, so a misconfigured M17 backend refuses to start rather than crashing later. The
    frame geometry is queried from the library and asserted against the mode-3200 assumptions.
    """

    def __init__(self) -> None:
        path = find_library(_LIBRARY_NAME)
        if path is None:
            raise RuntimeError(_MISSING_MSG)
        try:
            lib = ctypes.CDLL(path)
        except OSError as exc:  # library present in name but unloadable (missing soname/deps)
            raise RuntimeError(_MISSING_MSG) from exc

        self._lib = lib
        self._bind(lib)

        state = lib.codec2_create(CODEC2_MODE_3200)
        if not state:
            raise RuntimeError("codec2_create(CODEC2_MODE_3200) returned NULL")
        self._state = state

        samples = lib.codec2_samples_per_frame(state)
        bits = lib.codec2_bits_per_frame(state)
        if samples != _EXPECTED_SAMPLES_PER_FRAME or bits != _EXPECTED_BITS_PER_FRAME:
            raise RuntimeError(
                "libcodec2 mode 3200 geometry does not match assumptions: queried "
                f"{samples} samples / {bits} bits per frame, expected "
                f"{_EXPECTED_SAMPLES_PER_FRAME} / {_EXPECTED_BITS_PER_FRAME}"
            )
        self._samples_per_frame = samples
        self._bits_per_frame = bits
        self._bytes_per_frame = (bits + 7) // 8

    @staticmethod
    def _bind(lib: ctypes.CDLL) -> None:
        """Declare the C ABI: explicit argtypes/restype so ctypes marshals pointers correctly."""
        lib.codec2_create.argtypes = [ctypes.c_int]
        lib.codec2_create.restype = ctypes.c_void_p
        lib.codec2_destroy.argtypes = [ctypes.c_void_p]
        lib.codec2_destroy.restype = None
        lib.codec2_samples_per_frame.argtypes = [ctypes.c_void_p]
        lib.codec2_samples_per_frame.restype = ctypes.c_int
        lib.codec2_bits_per_frame.argtypes = [ctypes.c_void_p]
        lib.codec2_bits_per_frame.restype = ctypes.c_int
        # void codec2_encode(struct CODEC2 *, unsigned char *bits, short *speech_in)
        lib.codec2_encode.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.POINTER(ctypes.c_short),
        ]
        lib.codec2_encode.restype = None
        # void codec2_decode(struct CODEC2 *, short *speech_out, const unsigned char *bits)
        lib.codec2_decode.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_short),
            ctypes.POINTER(ctypes.c_ubyte),
        ]
        lib.codec2_decode.restype = None

    @property
    def samples_per_frame(self) -> int:
        """8 kHz PCM samples consumed/produced per Codec2 frame (queried, 160 for 3200)."""
        return self._samples_per_frame

    @property
    def bits_per_frame(self) -> int:
        """Coded bits per Codec2 frame (queried, 64 for 3200)."""
        return self._bits_per_frame

    @property
    def bytes_per_frame(self) -> int:
        """Packed bytes per Codec2 frame: ``ceil(bits_per_frame / 8)`` (8 for 3200)."""
        return self._bytes_per_frame

    def encode(self, frame: AudioFrame) -> bytes:
        """Encode a canonical 48 kHz frame to concatenated Codec2 3200 packets.

        Resamples down to :data:`CODEC2_RATE`, then encodes whole frames; a trailing partial
        frame is silence-padded to a whole Codec2 frame. Raises :class:`AudioFormatMismatch` if
        ``frame`` is not in :data:`CANONICAL_FORMAT` (ADR 0006 fail-loud), before any codec call.
        """
        if frame.format != CANONICAL_FORMAT:
            raise AudioFormatMismatch(
                f"Codec2 encodes {CANONICAL_FORMAT}, got a frame in {frame.format}"
            )

        eightk = resample(frame, CODEC2_RATE)
        pcm = np.frombuffer(eightk.samples, dtype=_PCM_DTYPE)

        n = self._samples_per_frame
        remainder = pcm.size % n
        if remainder:
            pcm = np.concatenate([pcm, np.zeros(n - remainder, dtype=pcm.dtype)])

        packets = bytearray()
        bits = np.zeros(self._bytes_per_frame, dtype=np.uint8)
        bits_ptr = bits.ctypes.data_as(ctypes.POINTER(ctypes.c_ubyte))
        for start in range(0, pcm.size, n):
            # Native int16 copy for the C ABI; the read-only '<i2' view can't be handed over.
            speech = pcm[start : start + n].astype(np.int16)
            speech_ptr = speech.ctypes.data_as(ctypes.POINTER(ctypes.c_short))
            self._lib.codec2_encode(self._state, bits_ptr, speech_ptr)
            packets += bits.tobytes()
        return bytes(packets)

    def decode(self, packets: bytes) -> AudioFrame:
        """Decode concatenated Codec2 3200 packets to a canonical 48 kHz frame.

        ``len(packets)`` must be a whole number of :attr:`bytes_per_frame`; otherwise raises
        ``ValueError`` rather than mis-decode a partial packet. Reassembles 8 kHz PCM and
        resamples up to canonical via :func:`to_canonical`.
        """
        stride = self._bytes_per_frame
        if len(packets) % stride != 0:
            raise ValueError(
                f"Codec2 packet length {len(packets)} is not a multiple of "
                f"bytes_per_frame ({stride})"
            )

        n = self._samples_per_frame
        speech = np.zeros(n, dtype=np.int16)
        speech_ptr = speech.ctypes.data_as(ctypes.POINTER(ctypes.c_short))
        chunks: list[bytes] = []
        for start in range(0, len(packets), stride):
            bits = np.frombuffer(packets[start : start + stride], dtype=np.uint8).copy()
            bits_ptr = bits.ctypes.data_as(ctypes.POINTER(ctypes.c_ubyte))
            self._lib.codec2_decode(self._state, speech_ptr, bits_ptr)
            chunks.append(speech.astype(_PCM_DTYPE).tobytes())

        eightk = AudioFrame(b"".join(chunks), AudioFormat(CODEC2_RATE, 2, 1))
        return to_canonical(eightk)

    def close(self) -> None:
        """Free the native Codec2 state exactly once (idempotent)."""
        state = getattr(self, "_state", None)
        if state:
            self._lib.codec2_destroy(state)
            self._state = None

    def __del__(self) -> None:  # pragma: no cover - GC timing dependent
        try:
            self.close()
        except Exception:
            pass
