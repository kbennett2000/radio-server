"""Real-time DTMF tone-presence detection for the Mumble bridge (ADR 0049).

The ADR-0045 mute waited for multimon-ng to *decode* a digit before condemning a short delay
line; under a continuous (``squelch="off"``) stream that decode arrives later than the delay
window, so the tones have already fanned out to Mumble. This detector instead looks for DTMF
*tone energy* directly in each RF frame — a single-bin DFT (Goertzel-equivalent) evaluated at
the eight standard DTMF frequencies — so the bridge can drop the frame from the Mumble feed in
real time and, symmetrically, yield the Mumble→RF keying so the operator's command isn't
transmitted over.

It does NOT decode digits (multimon-ng still owns that, feeding `AuthGate`); it only answers
"is a DTMF dual-tone present in this frame?". Detection latency is one frame (~20 ms).

Thresholds are marked, tunable defaults biased toward **sensitivity**: briefly muting a frame of
link audio (or withholding one Mumble→RF frame) on a false positive is harmless; leaking a
control tone, or keying over a command, is not. VERIFY AGAINST HARDWARE (guardrail 1): bench with
an HT dialing digits and tune if a tone leaks or ordinary voice is over-muted.
"""

from __future__ import annotations

import numpy as np

from ..audio.dtmf import DTMF_FREQS
from ..audio.format import CANONICAL_FORMAT

#: The 4 low-row and 4 high-column DTMF frequencies (Hz), derived from the standard pairs so the
#: table stays single-sourced with the decoder.
_LOW_FREQS = tuple(sorted({lo for lo, _ in DTMF_FREQS.values()}))
_HIGH_FREQS = tuple(sorted({hi for _, hi in DTMF_FREQS.values()}))

#: numpy dtype for canonical signed-16-bit little-endian samples.
_PCM_DTYPE = np.dtype("<i2")
_INT16_FULL_SCALE = 32768.0

# --- marked, tunable detection thresholds (guardrail 1) -------------------------------------
#: Minimum mean-square level (normalized, full-scale = 1.0) before a frame is even considered —
#: rejects silence / near-silence. ~ -40 dBFS.
DEFAULT_MS_FLOOR = 1e-4
#: Each of the dominant low- and high-group tones must hold at least this fraction of the frame's
#: energy. A clean DTMF pair splits ~half the energy two ways (~0.25 each), so 0.08 is generous.
DEFAULT_MIN_GROUP_FRAC = 0.08
#: The two dominant tones together must hold at least this fraction of the frame energy — the main
#: rejecter of broadband voice, which spreads energy across the spectrum rather than into two
#: exact bins.
DEFAULT_MIN_COMBINED_FRAC = 0.25


class DtmfToneDetector:
    """Per-frame "is a DTMF dual-tone present?" detector (no digit decode).

    Pure and cheap: one cached ``(8, N)`` complex basis per frame length, a matrix-vector product,
    and three threshold tests. Safe to call on the event loop for every RF→Mumble frame.
    """

    def __init__(
        self,
        *,
        rate: int = CANONICAL_FORMAT.rate,
        ms_floor: float = DEFAULT_MS_FLOOR,
        min_group_frac: float = DEFAULT_MIN_GROUP_FRAC,
        min_combined_frac: float = DEFAULT_MIN_COMBINED_FRAC,
    ) -> None:
        self._rate = rate
        self._ms_floor = ms_floor
        self._min_group_frac = min_group_frac
        self._min_combined_frac = min_combined_frac
        self._freqs = np.array(_LOW_FREQS + _HIGH_FREQS, dtype=np.float64)
        self._n_low = len(_LOW_FREQS)
        #: Complex DFT basis per frame length; frames are a fixed ~960 samples in practice, so this
        #: holds a single entry.
        self._basis_cache: dict[int, np.ndarray] = {}

    def _basis(self, n: int) -> np.ndarray:
        basis = self._basis_cache.get(n)
        if basis is None:
            k = np.arange(n, dtype=np.float64)
            # (8, n): one row of unit-magnitude phasors per DTMF frequency.
            basis = np.exp(-2j * np.pi * np.outer(self._freqs, k) / self._rate)
            self._basis_cache[n] = basis
        return basis

    def detect(self, frame: bytes | bytearray | memoryview | np.ndarray) -> bool:
        """Whether ``frame`` (canonical s16le PCM, or a sample array) holds a DTMF dual-tone."""
        if isinstance(frame, (bytes, bytearray, memoryview)):
            x = np.frombuffer(bytes(frame), dtype=_PCM_DTYPE).astype(np.float64) / _INT16_FULL_SCALE
        else:
            x = np.asarray(frame, dtype=np.float64)
        n = x.size
        if n == 0:
            return False
        energy = float(x @ x)
        if energy <= 0.0 or energy / n < self._ms_floor:
            return False  # silence / near-silence
        mags = np.abs(self._basis(n) @ x)
        # Normalized per-bin energy fraction: |X_f|^2 / (N·E) ≈ 0.25 per tone for a clean pair.
        fracs = (mags * mags) / (n * energy)
        low_peak = float(fracs[: self._n_low].max())
        high_peak = float(fracs[self._n_low :].max())
        # A DTMF key is exactly one low AND one high tone, each strong, together dominating energy.
        if low_peak < self._min_group_frac or high_peak < self._min_group_frac:
            return False
        return (low_peak + high_peak) >= self._min_combined_frac
