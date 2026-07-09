"""Activity detection (software squelch / VAD) behind the RX activity seam (ADR 0015).

The detector that fills cycle 13's ``RxActivityGate`` seam: decide whether an RX frame is live audio
worth relaying vs dead air. Two implementations behind one ``(AudioFrame) -> bool`` shape —
:class:`AudioLevelGate` (audio-level VAD, the only option for the busy-line-less Baofeng) and
:class:`CatBusyGate` (the V71's hardware squelch over ``status().busy``) — selected by config via
:func:`build_rx_gate`. Sits below ``rx`` and imports no transport, so scan can reuse the same
activity signal (e.g. :func:`frame_rms`) later.
"""

from __future__ import annotations

from .gate import (
    DEFAULT_SQUELCH_MODE,
    DEFAULT_VAD_HANG,
    DEFAULT_VAD_OFF_RMS,
    DEFAULT_VAD_ON_RMS,
    RADIO_SQUELCH_ENV_VAR,
    RADIO_VAD_HANG_ENV_VAR,
    RADIO_VAD_OFF_RMS_ENV_VAR,
    RADIO_VAD_ON_RMS_ENV_VAR,
    ActivityGate,
    AudioLevelGate,
    CatBusyGate,
    SquelchMode,
    build_rx_gate,
    frame_rms,
    load_squelch_mode,
    load_vad_hang,
    load_vad_off_rms,
    load_vad_on_rms,
)

__all__ = [
    "ActivityGate",
    "AudioLevelGate",
    "CatBusyGate",
    "SquelchMode",
    "build_rx_gate",
    "frame_rms",
    "load_squelch_mode",
    "load_vad_hang",
    "load_vad_off_rms",
    "load_vad_on_rms",
    "DEFAULT_VAD_ON_RMS",
    "DEFAULT_VAD_OFF_RMS",
    "DEFAULT_VAD_HANG",
    "DEFAULT_SQUELCH_MODE",
    "RADIO_VAD_ON_RMS_ENV_VAR",
    "RADIO_VAD_OFF_RMS_ENV_VAR",
    "RADIO_VAD_HANG_ENV_VAR",
    "RADIO_SQUELCH_ENV_VAR",
]
