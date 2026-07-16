"""The secrets channel — kept wholly separate from the settings schema and ``radio.toml`` (ADR 0025).

``RADIO_TOTP_SECRET`` (over-RF TOTP plane) and ``RADIO_API_TOKEN`` (LAN bearer-token plane) are the
two secrets. They are the ONLY configuration still read from ``os.environ`` — and, preferably, from
a ``radio-secrets.toml`` written ``0600``. Keeping them off the settings surface means the config
file, the future settings REST API, and the future UI can never render, log, round-trip, or
overwrite a secret.

`load_secrets` is read-only and never fails on a *missing* secret (the fail-loud stays at use:
`Secrets.require` for the API token, a presence check for the TOTP secret) — it fails loud only when
the secrets file is group/world-readable. `save_secret` / `rotate` are write-only helpers built for
cycle 26's rotation endpoints (no endpoint here); they always write ``0600``.
"""

from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path
from secrets import token_urlsafe
from typing import Mapping

import pyotp
import tomlkit

__all__ = [
    "Secrets",
    "load_secrets",
    "save_secret",
    "rotate",
    "DEFAULT_SECRETS_PATH",
    "KNOWN_SECRETS",
]

#: The two secret names and the env vars they fall back to.
_ENV_NAMES: dict[str, str] = {
    "totp_secret": "RADIO_TOTP_SECRET",
    "api_token": "RADIO_API_TOKEN",
    # The Murmur server password for the Mumble link (ADR 0041), on the separate 0600 channel — a
    # credential, never in radio.toml. Optional: unset means connect with no password.
    "mumble_password": "RADIO_MUMBLE_PASSWORD",
}
KNOWN_SECRETS: tuple[str, ...] = tuple(_ENV_NAMES)

#: Default secrets file, alongside ``radio.toml`` in the working directory.
DEFAULT_SECRETS_PATH = Path("radio-secrets.toml")


class Secrets:
    """Resolved secrets. Absent secrets read as ``None``; `require` fails loud for a needed one."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, str]) -> None:
        object.__setattr__(self, "_values", {k: v for k, v in values.items() if v})

    @property
    def totp_secret(self) -> str | None:
        return self._values.get("totp_secret")

    @property
    def api_token(self) -> str | None:
        return self._values.get("api_token")

    def get(self, name: str) -> str | None:
        return self._values.get(name)

    def require(self, name: str) -> str:
        value = self._values.get(name)
        if not value:
            env = _ENV_NAMES.get(name, name)
            raise RuntimeError(
                f"{env} is not set; provide it in {DEFAULT_SECRETS_PATH} (chmod 600) or the "
                f"{env} environment variable"
            )
        return value


def _require_0600(path: Path) -> None:
    """Fail loud if ``path`` is readable by group or other — a secrets file a neighbor can read is a
    misconfiguration, not a warning."""
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise RuntimeError(
            f"{path} is group/world-accessible (mode {mode:04o}); it holds secrets — "
            f"run 'chmod 600 {path}'"
        )


def load_secrets(
    path: str | Path | None = None, *, env: Mapping[str, str] = os.environ
) -> Secrets:
    """Resolve secrets from ``env`` (the documented fallback) overlaid by ``radio-secrets.toml`` when
    present (which takes precedence). Fails loud only if the file is group/world-readable."""
    values: dict[str, str] = {}
    for name, env_name in _ENV_NAMES.items():
        env_value = env.get(env_name)
        if env_value:
            values[name] = env_value
    secrets_path = Path(path) if path is not None else DEFAULT_SECRETS_PATH
    if secrets_path.is_file():
        _require_0600(secrets_path)
        with secrets_path.open("rb") as fh:
            data = tomllib.load(fh)
        for name in KNOWN_SECRETS:
            if data.get(name):
                values[name] = str(data[name])
    return Secrets(values)


def save_secret(path: str | Path, name: str, value: str) -> None:
    """Write ``name``=``value`` into the secrets file ``0600``, preserving the other secret. Write-only:
    it never returns a secret. Used by rotation (cycle 26). Always tightens the file to ``0600``."""
    if name not in KNOWN_SECRETS:
        raise ValueError(f"unknown secret {name!r}; known: {', '.join(KNOWN_SECRETS)}")
    if not value:
        raise ValueError(f"refusing to write an empty {name}")
    secrets_path = Path(path)
    existing: dict[str, str] = {}
    if secrets_path.is_file():
        with secrets_path.open("rb") as fh:
            existing = {k: str(v) for k, v in tomllib.load(fh).items() if k in KNOWN_SECRETS}
    existing[name] = value
    doc = tomlkit.document()
    doc.add(tomlkit.comment("radio-server secrets — keep this file chmod 600. Not in radio.toml."))
    for secret_name in KNOWN_SECRETS:
        if existing.get(secret_name):
            doc[secret_name] = existing[secret_name]
    # Open with 0600 from the start, then chmod to tighten a pre-existing looser file too.
    fd = os.open(secrets_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, tomlkit.dumps(doc).encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(secrets_path, 0o600)


def rotate(path: str | Path, name: str) -> str:
    """Generate a fresh secret for ``name``, persist it ``0600``, and return the new value.

    ``totp_secret`` → a new base32 TOTP secret (pyotp); ``api_token`` → a URL-safe random token.
    """
    if name == "totp_secret":
        value = pyotp.random_base32()
    elif name == "api_token":
        value = token_urlsafe(32)
    else:
        raise ValueError(f"unknown secret {name!r}; known: {', '.join(KNOWN_SECRETS)}")
    save_secret(path, name, value)
    return value
