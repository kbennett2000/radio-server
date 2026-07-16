"""Resolution: raw config → an immutable, validated `Settings` object (ADR 0025).

The pipeline (`resolve_settings`) is deliberately small and is shared by both the file loader
(`load_settings`) and the test helper, so a value validates identically whether it came from
``radio.toml`` or a test. The one subtlety is the **lazy-required** rule: a required setting that is
absent is stored as `UNSET_REQUIRED` and only fails loud when it is actually read, exactly
reproducing the old point-of-use failure of ``load_callsign`` / ``load_tts_voice`` (which fire only
when the controller is wired). Present-but-invalid values fail loud at load; missing-required fails
at use.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .spec import SETTINGS, UNSET_REQUIRED, USE_DEFAULT, SettingSpec

__all__ = [
    "Settings",
    "resolve_settings",
    "load_settings",
    "load_service_bindings",
    "load_mumble_servers",
    "DEFAULT_CONFIG_PATH",
    "SERVICES_TABLE",
    "MUMBLE_SERVERS_KEY",
]

#: Default config file, in the working directory (self-hosting-friendly, consistent with the other
#: CWD-relative defaults like the log path). The bootstrap and the settings write API both point here
#: when no ``--config`` is given.
DEFAULT_CONFIG_PATH = Path("radio.toml")

#: Top-level TOML table reserved for the digit→service bindings channel (ADR 0034). It is deliberately
#: NOT part of the `SettingSpec` schema — its keys are arbitrary DTMF digits, which the fixed
#: one-spec-per-key schema cannot model — so it is peeled off before schema resolution (see `_flatten`)
#: and read separately by `load_service_bindings`, mirroring how secrets live on their own channel.
SERVICES_TABLE = "services"

#: Leaf reserved inside ``[mumble]`` for the ``[[mumble.servers]]`` entry list (ADR 0042). Like the
#: ``[services]`` table it is NOT a schema setting — a list of tables the flat one-spec-per-key
#: schema cannot model — so `_flatten` peels it off and `load_mumble_servers` reads it separately.
MUMBLE_SERVERS_KEY = "servers"

#: The flat [mumble] connection settings that ADR 0042 replaced with ``[[mumble.servers]]`` entries.
#: A leftover one gets a tailored migration error instead of the generic unknown-key message.
_LEGACY_MUMBLE_KEYS = frozenset({"enabled", "host", "port", "username", "channel", "tx_to_rf"})


class Settings:
    """An immutable, fully-resolved configuration. Read values with :meth:`get` by dotted key.

    Every setting in the schema has an entry: either a validated value, a spec default, or the
    `UNSET_REQUIRED` marker (for a required setting left unset). Reading an unset-required key raises
    — the lazy fail-loud that keeps the default mock app (no callsign/voice) starting cleanly.
    """

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, Any]) -> None:
        object.__setattr__(self, "_values", dict(values))

    def get(self, key: str) -> Any:
        try:
            value = self._values[key]
        except KeyError:
            raise KeyError(f"unknown setting {key!r}") from None
        if value is UNSET_REQUIRED:
            spec = _spec(key)
            raise RuntimeError(
                f"{key} is not set; add it to your radio.toml under [{spec.group}] "
                f"(was {spec.env})"
            )
        return value

    def is_set(self, key: str) -> bool:
        """Whether a required key has a value (False when it would raise on `get`)."""
        return self._values.get(key, UNSET_REQUIRED) is not UNSET_REQUIRED

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Settings({self._values!r})"


def _spec(key: str) -> SettingSpec:
    for spec in SETTINGS:
        if spec.key == key:
            return spec
    raise KeyError(key)


def resolve_settings(raw: Mapping[str, Any] | None = None) -> Settings:
    """Validate ``raw`` (a flat dotted-key mapping) against the schema and return `Settings`.

    For each spec: if its key is present, coerce it (present-but-invalid fails loud, naming the key);
    a coercer that returns `USE_DEFAULT` (a blank value) falls through to the default. If the key is
    absent, use the spec default — or `UNSET_REQUIRED` for a required setting.
    """
    raw = raw or {}
    unknown = set(raw) - {s.key for s in SETTINGS}
    if unknown:
        raise RuntimeError(
            f"unknown setting(s): {', '.join(sorted(unknown))}; not in the config schema"
        )
    values: dict[str, Any] = {}
    for spec in SETTINGS:
        if spec.key in raw:
            coerced = spec.coerce(raw[spec.key], spec.key)
            values[spec.key] = _default_of(spec) if coerced is USE_DEFAULT else coerced
        else:
            values[spec.key] = _default_of(spec)
    return Settings(values)


def _default_of(spec: SettingSpec) -> Any:
    """A spec's resolved default: its concrete default, or the unset marker if required."""
    return UNSET_REQUIRED if spec.required else spec.default


def load_settings(toml_path: str | Path | None = None) -> Settings:
    """Read ``toml_path`` (via stdlib ``tomllib``) and resolve it. A missing/None path resolves to
    pure defaults — identical to the pre-config-file behavior."""
    raw: dict[str, Any] = {}
    if toml_path is not None:
        path = Path(toml_path)
        if path.is_file():
            with path.open("rb") as fh:
                raw = _flatten(tomllib.load(fh))
    return resolve_settings(raw)


def _flatten(data: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten a nested TOML table (``[group] key = ...``) to dotted keys (``group.key``).

    Only one level of nesting is expected (the schema is group→leaf); a scalar at top level is kept
    as-is so an unknown flat key still surfaces in `resolve_settings`'s unknown-key check. Two
    reserved channels are skipped: the ``[services]`` table (digit bindings, ADR 0034, read by
    `load_service_bindings`) and the ``[[mumble.servers]]`` entry list (ADR 0042, read by
    `load_mumble_servers`). A leftover flat [mumble] connection setting (pre-0042) fails loud with
    the migration message rather than the generic unknown-key error.
    """
    flat: dict[str, Any] = {}
    for key, value in data.items():
        if key == SERVICES_TABLE:
            continue
        if isinstance(value, Mapping):
            for leaf, leaf_value in value.items():
                if key == "mumble" and leaf == MUMBLE_SERVERS_KEY:
                    continue
                if key == "mumble" and leaf in _LEGACY_MUMBLE_KEYS:
                    raise RuntimeError(
                        f"mumble.{leaf} moved: the flat [mumble] connection settings became "
                        f"[[mumble.servers]] entries (ADR 0042). Replace them with e.g.\n"
                        f"  [[mumble.servers]]\n"
                        f'  name = "home"\n'
                        f'  host = "your-murmur-host"\n'
                        f"(port/username/channel/tx_to_rf/dtmf/autoconnect are optional per-entry "
                        f"fields; mumble.enabled is gone — an entry with autoconnect = true "
                        f"connects on boot)"
                    )
                flat[f"{key}.{leaf}"] = leaf_value
        else:
            flat[key] = value
    return flat


def load_service_bindings(toml_path: str | Path | None = None) -> dict[str, str] | None:
    """Read the ``[services]`` digit→plugin-id table from ``toml_path``; ``None`` when absent.

    A separate channel from the `SettingSpec` schema (ADR 0034). ``None`` (no path, no file, or no
    ``[services]`` table) tells the caller to fall back to the default keypad layout
    (`services.plugin.DEFAULT_BINDINGS`). Keys and values are normalized to ``str``; validation
    (reserved/unknown digits, unknown plugin ids) is `services.plugin.resolve_bindings`' job.
    """
    if toml_path is None:
        return None
    path = Path(toml_path)
    if not path.is_file():
        return None
    with path.open("rb") as fh:
        table = tomllib.load(fh).get(SERVICES_TABLE)
    if table is None:
        return None
    return {str(digit): str(plugin_id) for digit, plugin_id in table.items()}


def load_mumble_servers(toml_path: str | Path | None = None) -> list[dict[str, Any]] | None:
    """Read the ``[[mumble.servers]]`` entry list from ``toml_path``; ``None`` when absent.

    The Mumble-destinations channel (ADR 0042) — a list of tables the flat schema cannot model,
    read separately exactly like the ``[services]`` table. Returns the raw tables as dicts;
    validation (slugs, hosts, combo digits) is `link.entries.resolve_mumble_entries`' job.
    """
    if toml_path is None:
        return None
    path = Path(toml_path)
    if not path.is_file():
        return None
    with path.open("rb") as fh:
        servers = tomllib.load(fh).get("mumble", {}).get(MUMBLE_SERVERS_KEY)
    if servers is None:
        return None
    return [dict(table) for table in servers]
