"""Schema-driven configuration (ADR 0025).

One `SettingSpec` per setting (`spec.SETTINGS`) is the single source of truth: it resolves the
running config from a TOML file (`load_settings`), writes it back round-trip (`save_settings`),
generates ``radio.toml.example`` (`render_example`), and — in later cycles — drives the settings
REST API and UI. Secrets live on a separate channel (`load_secrets` / `save_secret` / `rotate`),
never in ``radio.toml`` and never in `SETTINGS`.
"""

from __future__ import annotations

from .save import render_example, save_settings
from .secrets import (
    DEFAULT_SECRETS_PATH,
    KNOWN_SECRETS,
    Secrets,
    load_secrets,
    rotate,
    save_secret,
)
from .settings import Settings, load_settings, resolve_settings
from .spec import SETTINGS, SettingSpec

__all__ = [
    "SETTINGS",
    "SettingSpec",
    "Settings",
    "Secrets",
    "load_settings",
    "resolve_settings",
    "save_settings",
    "render_example",
    "load_secrets",
    "save_secret",
    "rotate",
    "DEFAULT_SECRETS_PATH",
    "KNOWN_SECRETS",
]
