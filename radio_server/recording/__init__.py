"""Audio recording package (ADR 0020): received audio saved to disk as WAV.

A :class:`Recorder` is a **passive frame-tap subscriber** — the same subscriber discipline as the
event log. It consumes the canonical PCM frames the RX pump already fans out (it opens no second
capture reader) and writes them to timestamped WAV files via the stdlib ``wave`` module. Recording
is **gated by default** (only live, post-squelch audio — no dead-air files) and **opt-in** via
``RADIO_RECORD`` (default off); the output directory (``RADIO_RECORD_PATH``) is validated fail-loud
at construction. A write fault is always caught and dropped, so recording can never break the pump,
a TX, or the audio stream.

``RADIO_RECORD_MODE`` is a seam for a future full/pre-gate capture mode; only ``gated`` is built.
TX recording is a future cycle that reuses this :class:`Recorder` via ``TxSession``'s ``on_key``
edges. See the module docstring in ``recorder.py``.
"""

from .recorder import (
    DEFAULT_RECORD_MODE,
    DEFAULT_RECORD_PATH,
    RADIO_RECORD_ENV_VAR,
    RADIO_RECORD_MODE_ENV_VAR,
    RADIO_RECORD_PATH_ENV_VAR,
    Clock,
    RecordMode,
    Recorder,
    build_recorder,
    load_record_enabled,
    load_record_mode,
    load_record_path,
)

__all__ = [
    "Recorder",
    "RecordMode",
    "Clock",
    "build_recorder",
    "load_record_enabled",
    "load_record_path",
    "load_record_mode",
    "RADIO_RECORD_ENV_VAR",
    "RADIO_RECORD_PATH_ENV_VAR",
    "RADIO_RECORD_MODE_ENV_VAR",
    "DEFAULT_RECORD_PATH",
    "DEFAULT_RECORD_MODE",
]
