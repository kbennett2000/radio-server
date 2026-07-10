"""DTMF decode + digit framing — the audio-in → digits seam (ADR 0008).

This is the last connective piece before a full end-to-end run on the mock: received
`AudioFrame` audio → decoded DTMF digits → framed command/code entries → the existing,
unchanged `AuthGate.on_dtmf`. Two concerns live here, kept deliberately distinct:

1. **Decode** (audio → digit chars). `DtmfDecoder` is a one-method protocol so a fake
   decoder can drive tests without the `multimon-ng` binary; `MultimonDtmfDecoder` is the
   real subprocess wrapper. Audio is resampled to `MULTIMON_RATE` at the tolerant edge
   (`to_multimon`, whose anti-alias filter protects the 697–1633 Hz DTMF band; ADR 0006).
2. **Framing** (digit chars → entries). `DtmfFramer` is pure and clock-injected: DTMF
   arrives as a stream of single digits, and framing turns that stream into complete
   entries using the grammar `#` = submit, `*` = clear, plus an inter-digit timeout that
   abandons a stalled partial. No subprocess, no audio — just the grammar and a clock.

`DtmfInput` composes the two. Nothing here imports the auth layer: the module stays
auth-free (a local `Clock` alias, not auth's), so the dependency arrow keeps pointing
audio → nothing-above-it. The consumer feeds completed entries to `on_dtmf`.

Guardrail 1: `multimon-ng`'s exact invocation flags and raw-input sample rate are
verify-against-installed-build facts, not asserted here — they are marked config
targets. Real weak-signal / HT-flutter decode robustness is a hardware bring-up check,
not something this software cycle proves.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import numpy as np

from .format import CANONICAL_FORMAT, AudioFormat, AudioFrame
from .resample import MULTIMON_RATE, to_multimon

if TYPE_CHECKING:
    from ..config import Settings

#: A clock returns Unix-ish seconds as a float. Injectable so framing timeouts are exactly
#: testable with a fake clock. Defined locally rather than imported from the auth layer so
#: this module stays auth-free (the dependency arrow stays audio → nothing-above).
Clock = Callable[[], float]

#: Standard DTMF dual-tone frequency pairs (low row, high column) in Hz, for every key on a
#: 4x4 keypad. A pressed key sums its row and column tone. Rows: 697/770/852/941 Hz;
#: columns: 1209/1336/1477/1633 Hz. These are a fixed telephony standard, not a hardware
#: guess, so they are asserted constants.
DTMF_FREQS: dict[str, tuple[float, float]] = {
    "1": (697.0, 1209.0), "2": (697.0, 1336.0), "3": (697.0, 1477.0), "A": (697.0, 1633.0),
    "4": (770.0, 1209.0), "5": (770.0, 1336.0), "6": (770.0, 1477.0), "B": (770.0, 1633.0),
    "7": (852.0, 1209.0), "8": (852.0, 1336.0), "9": (852.0, 1477.0), "C": (852.0, 1633.0),
    "*": (941.0, 1209.0), "0": (941.0, 1336.0), "#": (941.0, 1477.0), "D": (941.0, 1633.0),
}

#: Framing grammar terminators.
SUBMIT = "#"  #: end an entry; the accumulated digits are emitted as one payload.
CLEAR = "*"   #: cancel the current partial entry; discard the accumulated digits.

#: Default fixture tone length. Long enough for a clean dual-tone and a confident decode.
DEFAULT_DTMF_MS = 120.0

#: Default per-tone amplitude for a synth DTMF fixture. Two tones sum, so 0.4 each keeps
#: the peak at ~0.8 of full scale — no clipping, a clean spectrum to assert against.
DEFAULT_DTMF_AMPLITUDE = 0.4

#: numpy dtype for canonical signed-16-bit little-endian samples.
_PCM_DTYPE = np.dtype("<i2")
_INT16_MAX = 32767
_INT16_MIN = -32768

#: Environment variable naming the multimon-ng binary (path or name on PATH). Optional.
RADIO_MULTIMON_BIN_ENV_VAR = "RADIO_MULTIMON_BIN"

#: Marked-default multimon-ng binary. A name resolved on PATH; override for a custom build.
DEFAULT_MULTIMON_BIN = "multimon-ng"

#: multimon-ng argument template (binary is prepended). ``-a DTMF`` selects the DTMF
#: demodulator; ``-t raw`` reads raw signed-16-bit-LE mono at :data:`MULTIMON_RATE`; ``-``
#: is stdin. VERIFY AGAINST THE INSTALLED multimon-ng BUILD (guardrail 1) — these flags and
#: the implied input rate are a config target, not a confirmed hardware fact.
MULTIMON_ARGS = ("-a", "DTMF", "-t", "raw", "-")

#: Prefix multimon-ng prints for a decoded DTMF key, e.g. ``DTMF: 5``.
_MULTIMON_DTMF_PREFIX = "DTMF:"

#: Environment variable naming the inter-digit framing timeout (seconds). Optional.
RADIO_DTMF_TIMEOUT_ENV_VAR = "RADIO_DTMF_TIMEOUT"

#: Marked-default inter-digit timeout. Generous enough that hand-keyed digits over RF are
#: not split mid-entry; a UX/tuning preference, not a hardware fact.
DEFAULT_DTMF_TIMEOUT = 3.0


def synth_dtmf(
    digit: str,
    duration_ms: float = DEFAULT_DTMF_MS,
    format: AudioFormat = CANONICAL_FORMAT,
    *,
    amplitude: float = DEFAULT_DTMF_AMPLITUDE,
) -> AudioFrame:
    """Render one DTMF key to a dual-tone `AudioFrame` (canonical format by default).

    Sums the two `synth_tone` frames at the key's standard low/high frequencies. Each
    tone already carries a raised-cosine anti-click ramp, so the mix starts and ends
    clean. Deterministic (no RNG), so fixtures are exactly reproducible. An unknown key
    fails loud rather than producing silent garbage.
    """
    from .tone import synth_tone  # local import: tone.py has no dtmf dependency

    try:
        low_hz, high_hz = DTMF_FREQS[digit]
    except KeyError as exc:
        raise ValueError(
            f"no DTMF tone pair for {digit!r}; valid keys are {''.join(DTMF_FREQS)}"
        ) from exc

    low = synth_tone(low_hz, duration_ms, format, amplitude=amplitude)
    high = synth_tone(high_hz, duration_ms, format, amplitude=amplitude)
    return _mix(low, high)


def _mix(a: AudioFrame, b: AudioFrame) -> AudioFrame:
    """Sum two same-format s16le frames sample-wise, clipping to full scale.

    Sums as int32 (headroom against overflow) then clips back into the int16 range — the
    way two DTMF tones combine into one signal. Both frames must share a format and length
    (they do when built from `synth_tone` with the same duration/format).
    """
    if a.format != b.format:
        raise ValueError(f"cannot mix frames of differing format: {a.format} vs {b.format}")
    left = np.frombuffer(a.samples, dtype=_PCM_DTYPE).astype(np.int32)
    right = np.frombuffer(b.samples, dtype=_PCM_DTYPE).astype(np.int32)
    if left.size != right.size:
        raise ValueError(
            f"cannot mix frames of differing length: {left.size} vs {right.size} samples"
        )
    mixed = np.clip(left + right, _INT16_MIN, _INT16_MAX).astype(_PCM_DTYPE)
    return AudioFrame(mixed.tobytes(), a.format)


@runtime_checkable
class DtmfDecoder(Protocol):
    """Turns a frame of audio into the DTMF digit characters it contains.

    One method, mirroring `IdEncoder`, so a fake decoder can stand in for the real
    subprocess wrapper in tests. The returned string is the digits found, in order —
    possibly empty, possibly several (e.g. ``"12#"``).
    """

    def decode(self, frame: AudioFrame) -> str: ...


class MultimonDtmfDecoder:
    """Real DTMF decode by shelling out to ``multimon-ng`` (implements `DtmfDecoder`).

    `decode` resamples the frame to `MULTIMON_RATE` at the tolerant edge (`to_multimon`),
    feeds the raw PCM to multimon-ng on stdin, and parses the ``DTMF: <key>`` lines it
    prints. The binary and its flags are marked config (guardrail 1); a missing binary
    fails loud with an install hint rather than silently decoding nothing.
    """

    def __init__(self, binary: str = DEFAULT_MULTIMON_BIN) -> None:
        self._binary = binary

    def decode(self, frame: AudioFrame) -> str:
        pcm = to_multimon(frame).samples
        try:
            proc = subprocess.run(
                [self._binary, *MULTIMON_ARGS],
                input=pcm,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"multimon-ng binary {self._binary!r} not found; install multimon-ng or set "
                f"{RADIO_MULTIMON_BIN_ENV_VAR}"
            ) from exc
        return self._parse(proc.stdout)

    @staticmethod
    def _parse(stdout: bytes) -> str:
        digits: list[str] = []
        for line in stdout.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if line.startswith(_MULTIMON_DTMF_PREFIX):
                token = line[len(_MULTIMON_DTMF_PREFIX):].strip()
                if token:
                    digits.append(token[0])  # one key per line
        return "".join(digits)


class DtmfFramer:
    """Frames a stream of single DTMF digits into complete entries (pure, clock-injected).

    Grammar: `#` submits the accumulated digits as one entry; `*` clears a partial; any
    other key is appended. An inter-digit timeout abandons a stalled partial — if the gap
    since the last key reaches `timeout`, the buffer is discarded before the new key is
    handled (a half-entered code is useless, so dropping it beats auto-submitting garbage).
    No audio and no subprocess here — just the grammar and an injected clock, so timeouts
    are exactly testable with a fake clock.
    """

    def __init__(self, *, timeout: float = DEFAULT_DTMF_TIMEOUT, clock: Clock | None = None) -> None:
        self._timeout = timeout
        self._clock = clock if clock is not None else time.monotonic
        self._buffer: list[str] = []
        self._last_at: float | None = None

    def feed(self, digit: str, now: float | None = None) -> str | None:
        """Feed one decoded key; return a completed entry on `#`, else ``None``.

        Applies the inter-digit timeout lazily: a partial that has gone stale by the time
        this key arrives is discarded first. `*` clears and returns ``None``; a `#` with an
        empty buffer emits nothing (returns ``None``).
        """
        if now is None:
            now = self._clock()
        self._expire(now)
        self._last_at = now

        if digit == CLEAR:
            self._buffer.clear()
            return None
        if digit == SUBMIT:
            if not self._buffer:
                return None
            entry = "".join(self._buffer)
            self._buffer.clear()
            return entry
        self._buffer.append(digit)
        return None

    def tick(self, now: float | None = None) -> None:
        """Apply the inter-digit timeout without feeding a key (for a real polling loop)."""
        if now is None:
            now = self._clock()
        self._expire(now)

    def _expire(self, now: float) -> None:
        if self._buffer and self._last_at is not None and now - self._last_at >= self._timeout:
            self._buffer.clear()


class DtmfInput:
    """Composes a `DtmfDecoder` and a `DtmfFramer`: audio frames → completed entries.

    `pump` decodes one frame to digit characters, feeds each through the framer, and
    returns the entries that completed. Auth-free by design — the caller feeds the
    returned entries to `AuthGate.on_dtmf`, so nothing in the auth/session/dispatch layer
    changes.
    """

    def __init__(self, decoder: DtmfDecoder, framer: DtmfFramer) -> None:
        self._decoder = decoder
        self._framer = framer

    def pump(self, frame: AudioFrame, now: float | None = None) -> list[str]:
        entries: list[str] = []
        for digit in self._decoder.decode(frame):
            entry = self._framer.feed(digit, now)
            if entry is not None:
                entries.append(entry)
        return entries


def load_multimon_bin(settings: Settings) -> str:
    """Return the multimon-ng binary path/name (`dtmf.multimon_bin`)."""
    return settings.get("dtmf.multimon_bin")


def load_dtmf_timeout(settings: Settings) -> float:
    """Return the inter-digit timeout in seconds (`dtmf.timeout`)."""
    return settings.get("dtmf.timeout")
