"""The controller loop: drive DTMF / auth / dispatch / scan / station-ID on a live receive().

A pure, clock-injected `Controller.step(now, rx_audio)` core plus a thin async `ControllerRunner`
driver, wired from the environment by `build_controller`. See ADR 0013.
"""

from .engine import (
    CONTROLLER_PHASES,
    DEFAULT_CONTROLLER_POLL,
    DEFAULT_SESSION_TIMEOUT,
    RADIO_CONTROLLER_POLL_ENV_VAR,
    RADIO_SESSION_TIMEOUT_ENV_VAR,
    Controller,
    ControllerEvent,
    ControllerRunner,
    StepResult,
    build_controller,
    load_controller_poll,
    load_fixed_code_enabled,
    load_session_timeout,
    load_totp_enabled,
)

__all__ = [
    "CONTROLLER_PHASES",
    "Controller",
    "ControllerEvent",
    "ControllerRunner",
    "DEFAULT_CONTROLLER_POLL",
    "DEFAULT_SESSION_TIMEOUT",
    "RADIO_CONTROLLER_POLL_ENV_VAR",
    "RADIO_SESSION_TIMEOUT_ENV_VAR",
    "StepResult",
    "build_controller",
    "load_controller_poll",
    "load_fixed_code_enabled",
    "load_session_timeout",
    "load_totp_enabled",
]
