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

import atexit
import queue
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import numpy as np
import soxr

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

#: Depth (chunks) of `MultimonStream`'s stdin hand-off queue before it drops the oldest chunk instead
#: of blocking the event-loop caller (ADR 0040). At the DTMF decode rate (~882 bytes per 20 ms frame),
#: 64 chunks is ~1.3 s of buffered audio — ample for a healthy multimon, bounded for a stuck one.
WRITE_QUEUE_MAXSIZE = 64

#: Prefix multimon-ng prints for a decoded DTMF key, e.g. ``DTMF: 5``.
_MULTIMON_DTMF_PREFIX = "DTMF:"


def _dtmf_digit(line: str) -> str:
    """The single DTMF key in one multimon-ng output line, or ``""`` if the line isn't a DTMF hit.

    Shared by the per-window decoder (`MultimonDtmfDecoder`) and the streaming decoder
    (`MultimonStream`) so both parse ``DTMF: <key>`` identically. One key per line.
    """
    line = line.strip()
    if line.startswith(_MULTIMON_DTMF_PREFIX):
        token = line[len(_MULTIMON_DTMF_PREFIX):].strip()
        if token:
            return token[0]
    return ""


#: DTMF decode strategies (`dtmf.decode_mode`). ``streaming`` pipes the continuous RX stream through
#: one persistent multimon-ng process (ADR 0038 — resolves repeated digits like ``99#``); ``buffered``
#: is the ADR 0030 fixed-window accumulator kept as an in-field fallback; ``native`` is the in-process
#: Goertzel decoder (ADR 0054) — no multimon-ng binary, so it works on native Windows and runs in CI;
#: ``auto`` (ADR 0055) resolves to ``native`` unconditionally now (ADR 0060 — bench-verified better on RF).
DECODE_MODE_STREAMING = "streaming"
DECODE_MODE_BUFFERED = "buffered"
DECODE_MODE_NATIVE = "native"
DECODE_MODE_AUTO = "auto"
DECODE_MODES = (DECODE_MODE_STREAMING, DECODE_MODE_BUFFERED, DECODE_MODE_NATIVE, DECODE_MODE_AUTO)

#: Environment variable naming the DTMF decode mode. Optional.
RADIO_DTMF_DECODE_MODE_ENV_VAR = "RADIO_DTMF_DECODE_MODE"

#: Marked default: ``auto`` (ADR 0055) — resolves at construction to ``native`` (in-process Goertzel,
#: no binary, ADR 0054). ADR 0060 settled the deferred A/B: ``native`` decodes better than multimon on
#: the reference RF station, so ``auto`` no longer inspects the multimon-ng binary. An explicit
#: ``streaming``/``buffered``/``native`` overrides; ``streaming``/``buffered`` still need the binary.
DEFAULT_DTMF_DECODE_MODE = DECODE_MODE_AUTO

# --- Native (Goertzel) decoder parameters (ADR 0054) ---------------------------------------------
# Every constant below is a MARKED, TUNABLE DEFAULT — VERIFY AGAINST HARDWARE (guardrail 1). They were
# calibrated in software to reproduce ADR 0038's empirical multimon table (the oracle), NOT measured on
# RF. Talk-off on real voice and weak-signal/HT-flutter robustness are open bring-up items (ADR 0054).

#: Sample rate the Goertzel detector runs at. 8 kHz + :data:`NATIVE_BLOCK_N` = 205 is the canonical
#: DTMF block where all eight tones land on clean bins. Input arrives at :data:`MULTIMON_RATE`, so the
#: stream decimates 22050 → 8000 first.
NATIVE_DECODE_RATE = 8000

#: Samples per Goertzel block (~25.6 ms at 8 kHz). Blocks are contiguous and non-overlapping; a digit
#: spanning a boundary is never split because filter/resampler state is carried across writes.
NATIVE_BLOCK_N = 205

#: ``soxr`` quality for the streaming 22050 → 8000 decimation. **HQ, not the VHQ of `to_multimon`** —
#: VHQ's long filter buffers ~150 ms before emitting output for a short chunk, which would delay a
#: code's terminating ``#`` in real time; HQ holds back < one block (< ~15 ms) while its anti-alias
#: filter still protects the 697–1633 Hz band from the 4–11 kHz fold (ADR 0054).
NATIVE_RESAMPLE_QUALITY = "HQ"

#: Absolute per-tone energy floor (normalized power on float samples in [-1, 1]); below it a block is
#: silence. Its only job is rejecting digital silence — talk-off / non-tone rejection is done by the
#: scale-invariant ratio gates below (group dominance, twist, second harmonic), not by this floor.
#: The single most level/AGC-dependent constant here, so verify on hardware (guardrail 1).
#: VERIFIED cycle-16 against the bench capture (ADR 0072): real received DTMF lands ~10x quieter than
#: the 0.4-amplitude synth fixtures — measured low-tone power ~0.012 vs the old 0.02 floor, so every
#: block failed this gate and nothing decoded. 0.002 clears real tones with ~6x headroom while
#: full-scale white-noise talk-off stays clean down to 0.001 (dominance, not the floor, rejects noise).
NATIVE_ENERGY_FLOOR = 0.002

#: Consecutive stable blocks before a digit emits, and consecutive drop-out blocks (silence or a
#: different digit) before the same digit may emit again. Pinned to **one** block each by ADR 0038's
#: "two 9s @ 30 ms gap → 99" row: a 30 ms gap is sub-block, so a ≥2-block re-arm could not see it.
NATIVE_ONSET_BLOCKS = 1
NATIVE_RELEASE_BLOCKS = 1

#: Twist limits in dB: the high group may lead the low group by at most :data:`NATIVE_FORWARD_TWIST_DB`
#: (forward twist), the low group lead the high by at most :data:`NATIVE_REVERSE_TWIST_DB` (reverse).
NATIVE_FORWARD_TWIST_DB = 8.0
NATIVE_REVERSE_TWIST_DB = 4.0

#: The winning bin in each group must exceed the strongest other bin in its group by this power ratio
#: (rejects broadband noise/speech that has no single dominant tone).
NATIVE_GROUP_DOMINANCE = 4.0

#: A tone's fundamental must exceed its own second harmonic by this power ratio (rejects harmonically
#: rich non-DTMF audio whose energy happens to fall near a tone).
NATIVE_SECOND_HARMONIC = 4.0

#: The four low-row and four high-column tones, and the key each (low, high) pair selects — derived
#: from :data:`DTMF_FREQS` so the tone table stays single-sourced.
_NATIVE_LOWS = sorted({lo for lo, _ in DTMF_FREQS.values()})
_NATIVE_HIGHS = sorted({hi for _, hi in DTMF_FREQS.values()})
_NATIVE_KEY_BY_PAIR = {(lo, hi): key for key, (lo, hi) in DTMF_FREQS.items()}

#: Precomputed DFT basis (rows: 4 low + 4 high fundamentals, then their 8 second harmonics) evaluated
#: over one block. ``|_NATIVE_BASIS @ block|**2 / N**2`` is the Goertzel power at each frequency in a
#: single matmul — the whole per-block detector is one cheap vector op, so `write` never blocks the
#: event loop (ADR 0040).
_NATIVE_FUNDAMENTALS = _NATIVE_LOWS + _NATIVE_HIGHS
_NATIVE_BASIS = np.exp(
    -2j
    * np.pi
    * np.outer(
        np.array(_NATIVE_FUNDAMENTALS + [2.0 * f for f in _NATIVE_FUNDAMENTALS]),
        np.arange(NATIVE_BLOCK_N),
    )
    / NATIVE_DECODE_RATE
)

#: Environment variable naming the inter-digit framing timeout (seconds). Optional.
RADIO_DTMF_TIMEOUT_ENV_VAR = "RADIO_DTMF_TIMEOUT"

#: Marked-default inter-digit timeout. Generous enough that hand-keyed digits over RF are
#: not split mid-entry; a UX/tuning preference, not a hardware fact.
DEFAULT_DTMF_TIMEOUT = 3.0

#: How much received audio :class:`BufferedDtmfInput` accumulates before running one decode, in
#: seconds. A single ~20 ms ``receive()`` block is far too short for multimon-ng to lock onto a tone;
#: a keyed DTMF digit (~40–200 ms) fits comfortably in half a second. VERIFY AGAINST HARDWARE
#: (guardrail 1) — trades decode latency (added once per digit) against decode reliability.
DEFAULT_DTMF_BUFFER_SECONDS = 0.5

#: Environment variable naming the DTMF accumulation window (seconds). Optional.
RADIO_DTMF_BUFFER_SECONDS_ENV_VAR = "RADIO_DTMF_BUFFER_SECONDS"


def dtmf_window_bytes(seconds: float) -> int:
    """Bytes of canonical audio spanning ``seconds`` — the accumulation window for buffered decode."""
    return int(seconds * CANONICAL_FORMAT.rate) * CANONICAL_FORMAT.frame_bytes


#: The default accumulation window as a byte count (½ s of canonical 48 kHz audio).
DEFAULT_DTMF_CHUNK_BYTES = dtmf_window_bytes(DEFAULT_DTMF_BUFFER_SECONDS)


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
            digit = _dtmf_digit(line)
            if digit:
                digits.append(digit)
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


class BufferedDtmfInput:
    """Like :class:`DtmfInput`, but accumulates audio into a fixed window before decoding (ADR 0030).

    A single short ``receive()`` frame (~20 ms on the AIOC) is far too brief for multimon-ng to lock
    onto a DTMF tone, so :meth:`pump` buffers incoming frame bytes and only runs the decoder once it
    holds ``window_bytes`` (~0.5 s) of audio — the accumulate step the ``doctor --dtmf`` tool proved
    on the bench, now shared by the live controller.

    Held-tone de-dup (``dedup``, default on) collapses a key held across detections/windows to a
    single press: multimon re-emits a digit for as long as the key is held, and a tone can straddle a
    window boundary, so consecutive identical detections are suppressed until a **silent window** (no
    tone = a gap) resets the run — a genuinely repeated key (e.g. ``55`` in a code) still registers
    twice as long as there is a pause between the presses. :meth:`flush` decodes a partial tail.

    Same public surface as :class:`DtmfInput` (``pump(frame, now) -> list[str]``), so the controller
    uses it interchangeably. ``on_digit`` is an optional per-key hook (post-dedup) for a live display
    — the ``doctor --dtmf`` tool passes it to print each digit as heard; the controller leaves it
    ``None``. Auth-free, like :class:`DtmfInput`.
    """

    def __init__(
        self,
        decoder: DtmfDecoder,
        framer: DtmfFramer,
        *,
        window_bytes: int = DEFAULT_DTMF_CHUNK_BYTES,
        dedup: bool = True,
        on_digit: Callable[[str], None] | None = None,
    ) -> None:
        self._decoder = decoder
        self._framer = framer
        self._window_bytes = window_bytes
        self._dedup = dedup
        # Public and rebindable: the composition root re-points it after construction (the
        # controller forwards digits to the Mumble DTMF mute, ADR 0045).
        self.on_digit = on_digit
        self._buf = bytearray()
        self._last_digit: str | None = None  # de-dup state: the last key still being held

    def pump(self, frame: AudioFrame, now: float | None = None) -> list[str]:
        """Buffer ``frame``; decode + return any completed entries once a full window accumulates."""
        if frame.samples:
            self._buf.extend(frame.samples)
        if len(self._buf) >= self._window_bytes:
            return self._decode_window(now)
        return []

    def flush(self, now: float | None = None) -> list[str]:
        """Decode whatever partial audio remains buffered (the tail); returns completed entries."""
        return self._decode_window(now)

    def _decode_window(self, now: float | None) -> list[str]:
        if not self._buf:
            return []
        chunk = AudioFrame(bytes(self._buf), CANONICAL_FORMAT)
        self._buf.clear()
        digits = self._decoder.decode(chunk)
        if self._dedup and not digits:
            # A window with no tone is a gap: the next same key is a fresh press, not a held one.
            self._last_digit = None
            return []
        entries: list[str] = []
        for digit in digits:
            if self._dedup:
                if digit == self._last_digit:
                    continue  # same key still held — multimon re-emits it; count it once
                self._last_digit = digit
            if self.on_digit is not None:
                self.on_digit(digit)
            entry = self._framer.feed(digit, now)
            if entry is not None:
                entries.append(entry)
        return entries


@runtime_checkable
class DtmfStream(Protocol):
    """A live, stateful DTMF decoder fed a continuous audio stream (contrast `DtmfDecoder`).

    `write` feeds a chunk of `MULTIMON_RATE` raw PCM; `read` non-blockingly drains whatever keys the
    decoder has recognized *so far* (possibly ``""``); `close` releases the underlying resource. One
    method more than `DtmfDecoder` because a stream keeps detector state across writes — which is the
    whole point: it does its own tone-onset/gap detection, so held tones emit once and genuine repeats
    emit twice, with no window-boundary double count to de-dup (ADR 0038). Injectable so a fake stream
    drives tests without the multimon-ng binary.
    """

    def write(self, pcm: bytes) -> None: ...

    def read(self) -> str: ...

    def close(self) -> None: ...


class MultimonStream:
    """Real `DtmfStream`: one persistent ``multimon-ng -a DTMF -t raw -`` process (ADR 0038).

    Unlike `MultimonDtmfDecoder`, which spawns a fresh process per decode window, this keeps a single
    long-lived process so multimon's own detector state spans the whole RX stream — the fix for
    repeated-digit codes like ``99#`` that per-window re-decoding + de-dup could not resolve. A daemon
    reader thread blocks on the process's stdout and pushes decoded keys onto a thread-safe queue, so
    `read` never blocks the caller's loop.

    `write` is likewise non-blocking (ADR 0040): it hands the PCM to a bounded queue drained by a
    daemon **writer** thread that does the actual `stdin.write`/`flush`. The RX pump calls `write` on
    the single event-loop task ahead of the browser audio fan-out, so a blocking pipe write here — the
    OS pipe to multimon is finite — would freeze the whole event loop and stall every `/audio/rx`
    listener whenever multimon drained slower than real time. Instead the queue drops its oldest chunk
    on overflow (mirroring `AudioHub.publish`): a slow multimon costs a little DTMF audio, never a
    stalled capture task. A dead process (its stdout closed, a `BrokenPipeError`) is respawned so a
    transient multimon crash self-heals. `close` tears the process down; an `atexit` backstop reaps a
    leaked instance so no orphan process lingers.

    The binary/flags are marked config (guardrail 1); a missing binary fails loud with an install hint.
    """

    def __init__(self, binary: str = DEFAULT_MULTIMON_BIN) -> None:
        self._binary = binary
        self._proc: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._writer: threading.Thread | None = None
        self._queue: queue.Queue[str] = queue.Queue()  # decoded keys (stdout side)
        #: PCM bound for multimon's stdin. Bounded + drop-oldest so a slow/stuck pipe can never block
        #: the event-loop caller (ADR 0040). `None` is the close sentinel. Sized ~1.3 s of DTMF-rate
        #: audio — ample for a healthy multimon, small enough to bound a stuck one's backlog.
        self._write_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=WRITE_QUEUE_MAXSIZE)
        self._lock = threading.Lock()
        self._closed = False
        atexit.register(self.close)

    def _spawn(self) -> subprocess.Popen[bytes]:
        try:
            proc = subprocess.Popen(
                [self._binary, *MULTIMON_ARGS],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"multimon-ng binary {self._binary!r} not found; install multimon-ng or set "
                f"{RADIO_MULTIMON_BIN_ENV_VAR}"
            ) from exc
        reader = threading.Thread(
            target=self._drain, args=(proc.stdout,), name="multimon-dtmf-reader", daemon=True
        )
        reader.start()
        self._proc = proc
        self._reader = reader
        # One persistent writer thread spans respawns — start it lazily on the first spawn.
        if self._writer is None or not self._writer.is_alive():
            self._writer = threading.Thread(
                target=self._pump_stdin, name="multimon-dtmf-writer", daemon=True
            )
            self._writer.start()
        return proc

    def _drain(self, stdout: object) -> None:
        # Runs on the daemon reader thread: block on stdout, push each decoded key onto the queue.
        # A closed/dead stdout ends the loop; `write` respawns a fresh process (and reader) on demand.
        for raw in stdout:  # type: ignore[attr-defined]
            digit = _dtmf_digit(raw.decode("utf-8", "replace"))
            if digit:
                self._queue.put(digit)

    def _pump_stdin(self) -> None:
        # Runs on the daemon writer thread: drain queued PCM to the process's stdin OFF the event
        # loop, so a full/slow multimon pipe can never block the shared RX capture task (ADR 0040).
        # Persistent across respawns; reads the current process fresh each chunk. A write error marks
        # the process dead so the next `write` respawns it (self-healing, as before).
        while not self._closed:
            pcm = self._write_queue.get()
            if pcm is None or self._closed:  # close sentinel (or closed while draining)
                return
            with self._lock:
                proc = self._proc
            if proc is None or proc.stdin is None:
                continue  # no live process right now; drop — a live `write` respawns on the loop side
            try:
                proc.stdin.write(pcm)
                proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                with self._lock:
                    if self._proc is proc:
                        self._proc = None  # mark dead; the next `write` respawns

    def write(self, pcm: bytes) -> None:
        if self._closed or not pcm:
            return
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                # First write, or the previous process died — (re)start it here on the caller so a
                # missing binary still fails loud on the caller, not silently in the writer thread.
                self._spawn()
        # Non-blocking hand-off to the writer thread: drop the oldest chunk on a full queue rather
        # than block the event loop (mirrors AudioHub.publish's drop-oldest).
        try:
            self._write_queue.put_nowait(pcm)
        except queue.Full:
            try:
                self._write_queue.get_nowait()  # evict oldest, keep the feed near-live
            except queue.Empty:
                pass
            try:
                self._write_queue.put_nowait(pcm)
            except queue.Full:
                pass

    def read(self) -> str:
        digits: list[str] = []
        while True:
            try:
                digits.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return "".join(digits)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            proc = self._proc
            self._proc = None
            writer = self._writer
        atexit.unregister(self.close)  # drop the backstop's reference once we've closed cleanly
        # Terminate the process FIRST. A writer stuck in a blocking `stdin.flush()` (multimon not
        # draining) is holding both the stream's buffer lock and a full write queue; killing the
        # process breaks the pipe so that flush raises, the writer loop sees `self._closed` and
        # exits, and its stdin buffer lock is released — making the join and `stdin.close()` below
        # safe. It also drains the queue so the sentinel can be delivered without blocking.
        if proc is not None:
            proc.terminate()
        # Wake a writer that's parked on an empty-queue `get()`. Non-blocking + drop-oldest: never
        # block `close` on a full queue (a flush-stuck writer never drains it — that path exits via
        # the terminate above instead).
        try:
            self._write_queue.put_nowait(None)
        except queue.Full:
            try:
                self._write_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._write_queue.put_nowait(None)
            except queue.Full:
                pass
        if writer is not None:
            writer.join(timeout=1.0)
        if proc is None:
            return
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            proc.kill()
        if self._reader is not None:
            self._reader.join(timeout=1.0)


class GoertzelStream:
    """In-process `DtmfStream`: a Goertzel tone detector, no multimon-ng binary (ADR 0054).

    A second implementation of `DtmfStream` alongside `MultimonStream`, selected by
    ``dtmf.decode_mode = native``. Because it runs entirely in Python it works on native Windows (where
    multimon-ng has no build) and — unlike every multimon path — is exercised unconditionally in CI.

    `write` decimates the 22050 Hz input to 8 kHz with a *stateful* resampler, then consumes the stream
    in contiguous `NATIVE_BLOCK_N`-sample blocks. Each block is classified by evaluating the Goertzel
    power at the eight DTMF tones (and their second harmonics) in one matmul against `_NATIVE_BASIS`,
    then run through a validity gauntlet (energy floor, twist, group dominance, harmonic rejection). An
    onset/gap state machine over the per-block digit gives the same guarantee the persistent multimon
    process gives (ADR 0038): a held tone emits once, a genuine repeat emits twice — so `read`, like
    `MultimonStream.read`, hands `StreamingDtmfInput` a stream of keys with no de-dup needed.

    Everything is synchronous and cheap (one vector op per ~26 ms block), so `write` never blocks the
    RX event loop (ADR 0040) without needing multimon's writer thread. Detector parameters are marked,
    tunable defaults — VERIFY AGAINST HARDWARE (guardrail 1); see the ``NATIVE_*`` constants.
    """

    def __init__(self, reverse_twist_db: float = NATIVE_REVERSE_TWIST_DB) -> None:
        self._resampler = soxr.ResampleStream(
            MULTIMON_RATE, NATIVE_DECODE_RATE, 1, dtype="float32", quality=NATIVE_RESAMPLE_QUALITY
        )
        self._buf = np.zeros(0, dtype=np.float32)  # 8 kHz samples awaiting a full block
        self._keys: list[str] = []  # recognized keys drained by read()
        self._forward_twist = 10.0 ** (NATIVE_FORWARD_TWIST_DB / 10.0)
        # Reverse-twist tolerance is configurable (ADR 0075): a few non-spec encoders (the UV-5R Mini)
        # send the low group much hotter than the high, tripping the default −4 dB gate. dB → power
        # ratio (10**(dB/10)) because `power` below is magnitude-squared, not amplitude.
        self._reverse_twist = 10.0 ** (reverse_twist_db / 10.0)
        # Onset/gap state carried across writes and blocks.
        self._held: str | None = None  # digit currently emitted-and-still-present
        self._gap_run = 0  # consecutive drop-out blocks since the held digit was last seen
        self._candidate: str | None = None  # digit accumulating toward an onset
        self._candidate_run = 0
        self._closed = False

    def write(self, pcm: bytes) -> None:
        if self._closed or not pcm:
            return
        samples = np.frombuffer(pcm, dtype=_PCM_DTYPE).astype(np.float32) / _INT16_MAX
        resampled = self._resampler.resample_chunk(samples)
        if resampled.size:
            self._buf = np.concatenate([self._buf, resampled])
        n = NATIVE_BLOCK_N
        consumed = 0
        while self._buf.size - consumed >= n:
            self._advance(self._classify(self._buf[consumed : consumed + n]))
            consumed += n
        if consumed:
            self._buf = self._buf[consumed:]

    def read(self) -> str:
        keys = "".join(self._keys)
        self._keys.clear()
        return keys

    def close(self) -> None:
        self._closed = True

    def _classify(self, block: np.ndarray) -> str | None:
        """The DTMF key present in one block, or ``None`` (silence / not a clean dual tone)."""
        power = np.abs(_NATIVE_BASIS @ block) ** 2 / (NATIVE_BLOCK_N * NATIVE_BLOCK_N)
        lo, hi = power[0:4], power[4:8]  # fundamentals, split into the two groups
        lo_h2, hi_h2 = power[8:12], power[12:16]  # matching second harmonics
        li, hj = int(np.argmax(lo)), int(np.argmax(hi))
        lo_max, hi_max = lo[li], hi[hj]
        if lo_max < NATIVE_ENERGY_FLOOR or hi_max < NATIVE_ENERGY_FLOOR:
            return None  # below the energy floor — silence (also the talk-off floor)
        lo_rest = np.max(np.delete(lo, li))
        hi_rest = np.max(np.delete(hi, hj))
        if lo_max < NATIVE_GROUP_DOMINANCE * lo_rest or hi_max < NATIVE_GROUP_DOMINANCE * hi_rest:
            return None  # no single dominant tone in a group — broadband noise/speech
        if hi_max > self._forward_twist * lo_max or lo_max > self._reverse_twist * hi_max:
            return None  # the two tones are too unbalanced to be a keyed digit
        if lo_max < NATIVE_SECOND_HARMONIC * lo_h2[li] or hi_max < NATIVE_SECOND_HARMONIC * hi_h2[hj]:
            return None  # harmonically rich (voice) rather than a pure tone pair
        return _NATIVE_KEY_BY_PAIR[(_NATIVE_LOWS[li], _NATIVE_HIGHS[hj])]

    def _advance(self, digit: str | None) -> None:
        """Feed one block's classification through the onset/gap state machine, emitting on onset."""
        if digit is not None and digit == self._held:
            self._gap_run = 0  # still holding the same tone — no re-emit
            return
        # Silence, or a digit different from the held one: count toward re-arming the held digit.
        self._gap_run += 1
        if self._gap_run >= NATIVE_RELEASE_BLOCKS:
            self._held = None
        if digit is None:
            self._candidate = None
            self._candidate_run = 0
            return
        if digit == self._candidate:
            self._candidate_run += 1
        else:
            self._candidate = digit
            self._candidate_run = 1
        if self._candidate_run >= NATIVE_ONSET_BLOCKS and self._held is None:
            self._keys.append(digit)
            self._held = digit
            self._gap_run = 0
            self._candidate = None
            self._candidate_run = 0


class StreamingDtmfInput:
    """Composes a `DtmfStream` and a `DtmfFramer`: continuous audio frames → completed entries (ADR 0038).

    Same public surface as `BufferedDtmfInput` (`pump(frame, now) -> list[str]`, `flush(now)`, optional
    `on_digit`), so the controller and `doctor --dtmf` use it interchangeably. `pump` resamples the
    frame at the tolerant edge (`to_multimon`) and hands it to the stream, then feeds any keys the
    stream has decoded so far to the framer. There is **no de-dup**: the persistent multimon process
    does its own onset/gap detection, so a held tone yields one key and a genuine repeat (e.g. the two
    ``9``s of ``99#``) yields two — the boundary double-count that forced `BufferedDtmfInput`'s lossy
    de-dup simply doesn't arise. Auth-free, like `DtmfInput`.
    """

    def __init__(
        self,
        stream: DtmfStream,
        framer: DtmfFramer,
        *,
        on_digit: Callable[[str], None] | None = None,
    ) -> None:
        self._stream = stream
        self._framer = framer
        # Public and rebindable, like `BufferedDtmfInput.on_digit` (ADR 0045).
        self.on_digit = on_digit

    def pump(self, frame: AudioFrame, now: float | None = None) -> list[str]:
        """Feed ``frame`` to the stream; return entries completed by keys decoded so far."""
        if frame.samples:
            self._stream.write(to_multimon(frame).samples)
        return self._feed(self._stream.read(), now)

    def flush(self, now: float | None = None) -> list[str]:
        """Drain any keys the stream has decoded but not yet reported; returns completed entries."""
        return self._feed(self._stream.read(), now)

    def close(self) -> None:
        """Release the underlying stream (reaps the multimon process for `MultimonStream`)."""
        self._stream.close()

    def _feed(self, digits: str, now: float | None) -> list[str]:
        entries: list[str] = []
        for digit in digits:
            if self.on_digit is not None:
                self.on_digit(digit)
            entry = self._framer.feed(digit, now)
            if entry is not None:
                entries.append(entry)
        return entries


def load_multimon_bin(settings: Settings) -> str:
    """Return the multimon-ng binary path/name (`dtmf.multimon_bin`)."""
    return settings.get("dtmf.multimon_bin")


def load_dtmf_decode_mode(settings: Settings) -> str:
    """Return the DTMF decode mode — ``streaming``, ``buffered``, ``native``, or ``auto``
    (`dtmf.decode_mode`)."""
    return settings.get("dtmf.decode_mode")


def resolve_decode_mode(mode: str, multimon_bin: str) -> tuple[str, str]:
    """Resolve ``auto`` to ``native`` (ADR 0060). ``multimon_bin`` is unused; kept for call-site stability.

    ``auto`` → ``native`` (in-process Goertzel, no binary, ADR 0054) **unconditionally**. The bench A/B
    that ADR 0055 deferred is settled: on the reference station (AIOC + UV-5R) ``native`` decodes
    noticeably better than multimon, so the binary's presence no longer decides anything — this is the
    one-line flip ADR 0055 named. Any explicit mode passes through unchanged: an explicit
    ``streaming``/``buffered`` is a contract (and still raises later if ``multimon_bin`` is absent);
    ``auto`` is no longer a multimon fallback.

    Returns ``(resolved_mode, reason)``; ``reason`` is ``""`` for an explicit mode, or a short human
    phrase for ``auto`` so `doctor` can report which decoder went live.
    """
    if mode != DECODE_MODE_AUTO:
        return mode, ""
    return DECODE_MODE_NATIVE, "bench-verified, ADR 0060"


def load_dtmf_timeout(settings: Settings) -> float:
    """Return the inter-digit timeout in seconds (`dtmf.timeout`)."""
    return settings.get("dtmf.timeout")


def load_dtmf_buffer_seconds(settings: Settings) -> float:
    """Return the DTMF accumulation window in seconds (`dtmf.buffer_seconds`)."""
    return settings.get("dtmf.buffer_seconds")


def load_dtmf_reverse_twist_db(settings: Settings) -> float:
    """Return the native decoder's reverse-twist tolerance in dB (`audio.dtmf_reverse_twist_db`)."""
    return settings.get("audio.dtmf_reverse_twist_db")
