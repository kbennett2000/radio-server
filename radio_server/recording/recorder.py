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

**Failure isolation.** Every write is fire-and-forget: a disk error is caught and dropped (a bad
segment is abandoned), so a recording fault can never break the RX pump, a TX, or the audio stream.
The path is validated **at construction** (fail loud on an unwritable target), like ``JsonlSink``.

TX recording is a future cycle — it reuses this :class:`Recorder` via ``TxSession.feed`` and the
existing cycle-18 ``on_key`` edges (key-up opens, key-down finalizes), with a ``tx-`` prefix and its
own ``RADIO_RECORD_TX`` toggle.
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

#: Marked default directory — self-hosted-friendly, relative to the working directory.
DEFAULT_RECORD_PATH = "recordings"

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

    def __init__(self, record_path: str | os.PathLike[str], *, clock: Clock | None = None) -> None:
        self._clock: Clock = clock or time.time
        # Fail loud here: create the tree, then a throwaway probe file proves writability. A
        # regular-file-in-the-path or a read-only directory raises OSError at construction.
        os.makedirs(record_path, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=record_path, prefix=".probe-", suffix=".tmp"):
            pass
        self._dir = os.fspath(record_path)
        self._wav: wave.Wave_write | None = None
        self._seq = 0

    def write(self, pcm: bytes) -> None:
        """Append one canonical PCM frame to the current segment, opening one if needed."""
        try:
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
        # Sequence-first filename: the counter guarantees uniqueness even when two segments share a
        # timestamp, and makes lexical order == chronological. The stamp is for humans.
        stamp = datetime.fromtimestamp(self._clock(), tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = os.path.join(self._dir, f"rx-{self._seq:06d}-{stamp}.wav")
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


def load_record_path(env: dict[str, str] | os._Environ = os.environ) -> str:
    """Return the recording directory from ``RADIO_RECORD_PATH``, or the marked default."""
    return env.get(RADIO_RECORD_PATH_ENV_VAR) or DEFAULT_RECORD_PATH


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
    return Recorder(load_record_path(env), clock=clock)
