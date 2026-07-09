"""Passive RX audio recorder: received audio → timestamped WAV files (ADR 0020).

A :class:`Recorder` is a **passive frame sink**, not a capture reader. It consumes the canonical
PCM frames the :class:`radio_server.rx.pump.RxPump` already fans out (48000 Hz / s16le / mono) and
writes them to disk as WAV via the stdlib ``wave`` module — no new dependency, and because the
canonical format is fixed the WAV header is deterministic. It never opens ``radio.receive()`` (the
single-reader discipline from the arbiter era stands); the pump drives it.

**Gated by default.** The pump only publishes gate-open frames, so a :class:`Recorder` tapping that
flow records only live audio — no dead-air files. ``RADIO_RECORD_MODE`` is a seam for a future
un-gated/full-capture mode; only ``gated`` is built this cycle.

**Segmentation.** One WAV per RX activity session: the pump calls :meth:`write` for each live frame
and :meth:`end_segment` at the gate-close edge, so a gate-open → gate-close span becomes one
timestamped file. (With ``RADIO_SQUELCH=off`` there is no gate-close edge, so all RX accumulates
into a single file that finalizes on pump stop — recording is most useful with a real squelch gate.)

**Bounded segments (ADR 0021).** A segment also rolls when it hits ``max_seconds`` (the injected
clock): :meth:`write` finalizes the current file and opens a fresh one, so no single WAV can grow
without bound even under ``RADIO_SQUELCH=off`` (no gate-close edge). The cap is always on.

**Failure isolation.** Every write is fire-and-forget: a disk error is caught and dropped (a bad
segment is abandoned), so a recording fault can never break the RX pump, a TX, or the audio stream.
The path is validated **at construction** (fail loud on an unwritable target), like ``JsonlSink``.

**TX recording (ADR 0021).** The same :class:`Recorder` records transmitted audio, reused via
:meth:`radio_server.tx.session.TxSession.feed` (each fed frame → :meth:`write`) and ``close``
(key-down/idle → :meth:`end_segment`), with a ``tx-`` filename prefix and its own ``RADIO_RECORD_TX``
toggle. Only the filename prefix differs; the WAV format and failure isolation are identical.
"""

from __future__ import annotations

import os
import tempfile
import time
import wave
from datetime import datetime, timezone
from enum import StrEnum
from typing import Callable

from ..audio import CANONICAL_CHANNELS, CANONICAL_RATE, CANONICAL_WIDTH

#: A monotonic wall-clock source (seconds). Injectable so tests drive a `FakeClock`.
Clock = Callable[[], float]

#: Opt-in master switch. Unset/falsey → recording off (the default); recording writes nothing.
RADIO_RECORD_ENV_VAR = "RADIO_RECORD"

#: Output directory for WAV segments. Optional: unset falls back to the marked default below.
RADIO_RECORD_PATH_ENV_VAR = "RADIO_RECORD_PATH"

#: Recording mode seam (``gated`` built; ``full``/pre-gate reserved). Optional: default ``gated``.
RADIO_RECORD_MODE_ENV_VAR = "RADIO_RECORD_MODE"

#: Opt-in switch for recording *transmitted* audio (ADR 0021). Independent of ``RADIO_RECORD``;
#: default off. When on, a second `tx-`-prefixed :class:`Recorder` captures the TX frame path.
RADIO_RECORD_TX_ENV_VAR = "RADIO_RECORD_TX"

#: Hard cap (seconds) on a single WAV segment (ADR 0021). When a segment reaches it, the recorder
#: finalizes it and rolls to a new file, so no WAV grows without bound even with no gate-close edge
#: (``RADIO_SQUELCH=off``). Optional: default below.
RADIO_RECORD_MAX_SECONDS_ENV_VAR = "RADIO_RECORD_MAX_SECONDS"

#: Marked default directory — self-hosted-friendly, relative to the working directory.
DEFAULT_RECORD_PATH = "recordings"

#: Marked default segment cap (1 hour). Bounds a single file's size; the always-on safety rail that
#: makes ``RADIO_SQUELCH=off`` recording safe. Not a hardware fact — a sane operator default.
DEFAULT_RECORD_MAX_SECONDS = 3600.0

_TRUTHY = frozenset({"1", "true", "on", "yes"})
_FALSEY = frozenset({"", "0", "false", "off", "no"})


class RecordMode(StrEnum):
    """What audio a recording captures.

    ``GATED`` (default, the only mode built this cycle): record post-gate audio — only what the
    squelch/VAD passes as live. ``FULL`` is reserved for a future pre-gate/full-capture tap.
    """

    GATED = "gated"
    FULL = "full"


DEFAULT_RECORD_MODE = RecordMode.GATED


class Recorder:
    """Writes canonical RX PCM frames to timestamped WAV segments.

    One WAV per activity session: :meth:`write` opens a segment lazily on the first frame and
    appends each subsequent frame; :meth:`end_segment` finalizes it. Both are fire-and-forget —
    any exception is caught and dropped so a recording fault never reaches the caller (the pump).

    The output directory is validated at construction: it is created if missing and probed for
    writability, so a bad ``record_path`` raises ``OSError`` immediately rather than silently
    dropping every frame at runtime (mirrors :class:`radio_server.eventlog.sink.JsonlSink`).
    """

    def __init__(
        self,
        record_path: str | os.PathLike[str],
        *,
        clock: Clock | None = None,
        prefix: str = "rx-",
        max_seconds: float = DEFAULT_RECORD_MAX_SECONDS,
    ) -> None:
        self._clock: Clock = clock or time.time
        # Filename prefix: `rx-` for received audio, `tx-` for transmitted (ADR 0021). Two Recorder
        # instances can share one directory and stay distinguishable (and each keeps its own counter).
        self._prefix = prefix
        # Hard segment cap (ADR 0021): finalize + roll when a segment reaches this many seconds, so
        # no single WAV grows without bound regardless of squelch mode.
        self._max_seconds = max_seconds
        # Fail loud here: create the tree, then a throwaway probe file proves writability. A
        # regular-file-in-the-path or a read-only directory raises OSError at construction.
        os.makedirs(record_path, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=record_path, prefix=".probe-", suffix=".tmp"):
            pass
        self._dir = os.fspath(record_path)
        self._wav: wave.Wave_write | None = None
        self._seq = 0
        self._segment_started = 0.0

    def write(self, pcm: bytes) -> None:
        """Append one canonical PCM frame to the current segment, opening one if needed.

        Rolls to a fresh file when the open segment has reached ``max_seconds`` (ADR 0021): the cap
        is checked against the injected clock before the lazy-open, so the triggering frame starts
        the new segment. The ``_wav is not None`` guard makes a stale ``_segment_started`` after an
        ``_abort()`` harmless — the next write simply lazy-opens and re-stamps.
        """
        try:
            if (
                self._wav is not None
                and self._clock() - self._segment_started >= self._max_seconds
            ):
                # Segment hit the duration cap: finalize it so the next line rolls a fresh file.
                self.end_segment()
            if self._wav is None:
                self._open_segment()
            self._wav.writeframes(pcm)  # type: ignore[union-attr]  # set by _open_segment
        except Exception:
            # A write fault must never reach the pump; abandon the corrupt segment and move on.
            self._abort()

    def end_segment(self) -> None:
        """Finalize the current WAV. Idempotent no-op when no segment is open."""
        if self._wav is None:
            return
        try:
            self._wav.close()  # patches the RIFF/data sizes → a valid, playable WAV
        except Exception:
            pass
        finally:
            self._wav = None

    def close(self) -> None:
        """Finalize any open segment (called at pump/app shutdown)."""
        self.end_segment()

    def _open_segment(self) -> None:
        self._seq += 1
        # Stamp the segment start from the injected clock; the duration-cap roll (see `write`)
        # measures elapsed against this. One read, reused for the human-facing filename stamp.
        now = self._clock()
        self._segment_started = now
        # Sequence-first filename: the counter guarantees uniqueness even when two segments share a
        # timestamp, and makes lexical order == chronological. The stamp is for humans.
        stamp = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = os.path.join(self._dir, f"{self._prefix}{self._seq:06d}-{stamp}.wav")
        wav = wave.open(path, "wb")
        wav.setnchannels(CANONICAL_CHANNELS)
        wav.setsampwidth(CANONICAL_WIDTH)
        wav.setframerate(CANONICAL_RATE)
        self._wav = wav

    def _abort(self) -> None:
        wav, self._wav = self._wav, None
        if wav is not None:
            try:
                wav.close()
            except Exception:
                pass


def load_record_enabled(env: dict[str, str] | os._Environ = os.environ) -> bool:
    """Return whether recording is enabled from ``RADIO_RECORD`` (default off).

    Unset is off. A set value must be a recognizable boolean; anything else fails loud rather than
    silently defaulting (a misconfigured toggle should be obvious, not silently ignored).
    """
    raw = env.get(RADIO_RECORD_ENV_VAR)
    if raw is None:
        return False
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSEY:
        return False
    raise RuntimeError(
        f"{RADIO_RECORD_ENV_VAR}={raw!r} is not a boolean (on/off/true/false/1/0/yes/no)"
    )


def load_record_tx_enabled(env: dict[str, str] | os._Environ = os.environ) -> bool:
    """Return whether *transmit* recording is enabled from ``RADIO_RECORD_TX`` (default off).

    Independent of ``RADIO_RECORD`` (which gates RX recording). Same fail-loud boolean parsing as
    :func:`load_record_enabled` — a set-but-unrecognizable value is an error, not a silent default.
    """
    raw = env.get(RADIO_RECORD_TX_ENV_VAR)
    if raw is None:
        return False
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSEY:
        return False
    raise RuntimeError(
        f"{RADIO_RECORD_TX_ENV_VAR}={raw!r} is not a boolean (on/off/true/false/1/0/yes/no)"
    )


def load_record_path(env: dict[str, str] | os._Environ = os.environ) -> str:
    """Return the recording directory from ``RADIO_RECORD_PATH``, or the marked default."""
    return env.get(RADIO_RECORD_PATH_ENV_VAR) or DEFAULT_RECORD_PATH


def load_record_max_seconds(env: dict[str, str] | os._Environ = os.environ) -> float:
    """Return the segment duration cap (s) from ``RADIO_RECORD_MAX_SECONDS``, or the marked default.

    Marked-default policy (the :func:`radio_server.tx.session.load_tx_idle_timeout` idiom): the
    default when unset/empty, else a positive float or fail loud. There is deliberately **no**
    disable sentinel — the cap is the always-on safety rail that bounds a WAV regardless of squelch
    mode, so a zero/negative value is an error rather than "unbounded".
    """
    raw = env.get(RADIO_RECORD_MAX_SECONDS_ENV_VAR)
    if raw is None or raw == "":
        return DEFAULT_RECORD_MAX_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"{RADIO_RECORD_MAX_SECONDS_ENV_VAR}={raw!r} is not a number"
        ) from exc
    if value <= 0:
        raise RuntimeError(
            f"{RADIO_RECORD_MAX_SECONDS_ENV_VAR}={raw!r} must be positive"
        )
    return value


def load_record_mode(env: dict[str, str] | os._Environ = os.environ) -> RecordMode:
    """Return the recording mode from ``RADIO_RECORD_MODE``, or the marked default (``gated``).

    A set value outside the known modes fails loud rather than silently defaulting.
    """
    raw = env.get(RADIO_RECORD_MODE_ENV_VAR)
    if raw is None or raw == "":
        return DEFAULT_RECORD_MODE
    try:
        return RecordMode(raw.lower())
    except ValueError as exc:
        modes = ", ".join(m.value for m in RecordMode)
        raise RuntimeError(
            f"{RADIO_RECORD_MODE_ENV_VAR}={raw!r} is not one of: {modes}"
        ) from exc


def build_recorder(
    env: dict[str, str] | os._Environ = os.environ, *, clock: Clock | None = None
) -> Recorder | None:
    """Compose the recorder from the environment — the composition root for recording.

    Returns ``None`` when ``RADIO_RECORD`` is off (the default), so the pump gets no recorder and
    writes nothing. When on, opens the :class:`Recorder` fail-loud on a bad ``RADIO_RECORD_PATH``.
    ``full`` mode is not implemented this cycle and raises rather than silently recording gated.
    """
    if not load_record_enabled(env):
        return None
    mode = load_record_mode(env)
    if mode is not RecordMode.GATED:
        raise NotImplementedError(
            f"{RADIO_RECORD_MODE_ENV_VAR}={mode.value!r} is not implemented yet; "
            "only 'gated' recording is supported"
        )
    return Recorder(
        load_record_path(env), clock=clock, max_seconds=load_record_max_seconds(env)
    )


def build_tx_recorder(
    env: dict[str, str] | os._Environ = os.environ, *, clock: Clock | None = None
) -> Recorder | None:
    """Compose the *transmit* recorder from the environment (ADR 0021).

    Returns ``None`` when ``RADIO_RECORD_TX`` is off (the default). When on, opens a ``tx-``-prefixed
    :class:`Recorder` in the shared ``RADIO_RECORD_PATH`` (fail-loud on a bad path), inheriting the
    ``RADIO_RECORD_MAX_SECONDS`` cap. It is independent of ``RADIO_RECORD`` and ignores
    ``RADIO_RECORD_MODE`` — squelch gating is an RX concept; TX has no gate.
    """
    if not load_record_tx_enabled(env):
        return None
    return Recorder(
        load_record_path(env),
        clock=clock,
        prefix="tx-",
        max_seconds=load_record_max_seconds(env),
    )
