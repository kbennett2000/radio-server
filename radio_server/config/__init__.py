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
from .settings import (
    DEFAULT_CONFIG_PATH,
    SERVICES_TABLE,
    Settings,
    load_service_bindings,
    load_settings,
    resolve_settings,
)
from .spec import BY_KEY, SETTINGS, SettingSpec

__all__ = [
    "SETTINGS",
    "BY_KEY",
    "SettingSpec",
    "Settings",
    "Secrets",
    "load_settings",
    "load_service_bindings",
    "SERVICES_TABLE",
    "resolve_settings",
    "save_settings",
    "render_example",
    "load_secrets",
    "save_secret",
    "rotate",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_SECRETS_PATH",
    "KNOWN_SECRETS",
]
