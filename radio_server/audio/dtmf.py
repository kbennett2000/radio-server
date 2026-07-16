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
import subprocess
import threading
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
#: is the ADR 0030 fixed-window accumulator kept as an in-field fallback.
DECODE_MODE_STREAMING = "streaming"
DECODE_MODE_BUFFERED = "buffered"
DECODE_MODES = (DECODE_MODE_STREAMING, DECODE_MODE_BUFFERED)

#: Environment variable naming the DTMF decode mode. Optional.
RADIO_DTMF_DECODE_MODE_ENV_VAR = "RADIO_DTMF_DECODE_MODE"

#: Marked default: stream through one persistent multimon process (ADR 0038). VERIFY AGAINST HARDWARE
#: (guardrail 1) — set to ``buffered`` to revert to the ADR 0030 fixed-window path without a code change.
DEFAULT_DTMF_DECODE_MODE = DECODE_MODE_STREAMING

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
        self._on_digit = on_digit
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
            if self._on_digit is not None:
                self._on_digit(digit)
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
        self._on_digit = on_digit

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
            if self._on_digit is not None:
                self._on_digit(digit)
            entry = self._framer.feed(digit, now)
            if entry is not None:
                entries.append(entry)
        return entries


def load_multimon_bin(settings: Settings) -> str:
    """Return the multimon-ng binary path/name (`dtmf.multimon_bin`)."""
    return settings.get("dtmf.multimon_bin")


def load_dtmf_decode_mode(settings: Settings) -> str:
    """Return the DTMF decode mode — ``streaming`` or ``buffered`` (`dtmf.decode_mode`)."""
    return settings.get("dtmf.decode_mode")


def load_dtmf_timeout(settings: Settings) -> float:
    """Return the inter-digit timeout in seconds (`dtmf.timeout`)."""
    return settings.get("dtmf.timeout")


def load_dtmf_buffer_seconds(settings: Settings) -> float:
    """Return the DTMF accumulation window in seconds (`dtmf.buffer_seconds`)."""
    return settings.get("dtmf.buffer_seconds")
