"""The software scan engine (ADR 0012).

A V71/CAT-only scan *loop* over the :class:`~radio_server.backends.CatRadio` surface —
tune, settle, poll ``status().busy``, act on activity (dwell / resume / hold / lockout /
priority). Pure and clock-injectable; emits progress through an injected ``on_event``
callback so it stays below the API (the API adapts :class:`ScanEvent` to a ``"scan"``
event on the cycle-10 ``EventHub``). Fully testable against ``MockRadio`` — no hardware.
"""

from .engine import (
    DEFAULT_SCAN_DWELL,
    DEFAULT_SCAN_MODE,
    DEFAULT_SCAN_POLL,
    DEFAULT_SCAN_SETTLE,
    RADIO_SCAN_DWELL_ENV_VAR,
    RADIO_SCAN_MODE_ENV_VAR,
    RADIO_SCAN_POLL_ENV_VAR,
    RADIO_SCAN_SETTLE_ENV_VAR,
    SCAN_PHASES,
    ResumeMode,
    ScanEngine,
    ScanEvent,
    ScanPlan,
    build_scan_engine,
    load_scan_dwell,
    load_scan_mode,
    load_scan_poll,
    load_scan_settle,
)

__all__ = [
    "DEFAULT_SCAN_DWELL",
    "DEFAULT_SCAN_MODE",
    "DEFAULT_SCAN_POLL",
    "DEFAULT_SCAN_SETTLE",
    "RADIO_SCAN_DWELL_ENV_VAR",
    "RADIO_SCAN_MODE_ENV_VAR",
    "RADIO_SCAN_POLL_ENV_VAR",
    "RADIO_SCAN_SETTLE_ENV_VAR",
    "SCAN_PHASES",
    "ResumeMode",
    "ScanEngine",
    "ScanEvent",
    "ScanPlan",
    "build_scan_engine",
    "load_scan_dwell",
    "load_scan_mode",
    "load_scan_poll",
    "load_scan_settle",
]
