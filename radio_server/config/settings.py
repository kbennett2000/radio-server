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

from ..backends import available_backends
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
    "PLUGINS_TABLE",
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

#: Top-level TOML table reserved for local-plugin config (ADR 0051). The third non-schema channel:
#: ``[plugins.<group>]`` sub-tables are flattened to ``group.leaf`` keys (the ``plugins.`` prefix is
#: dropped, so a migrated plugin's config reads keep their old spelling) and carried on `Settings`
#: **unvalidated**, read via :meth:`Settings.extra`. Everything outside this table stays strictly
#: schema-checked.
PLUGINS_TABLE = "plugins"

#: The flat [mumble] connection settings that ADR 0042 replaced with ``[[mumble.servers]]`` entries.
#: A leftover one gets a tailored migration error instead of the generic unknown-key message.
_LEGACY_MUMBLE_KEYS = frozenset({"enabled", "host", "port", "username", "channel", "tx_to_rf"})

#: Schema groups that are a backend's config block — i.e. a registered backend that also owns a
#: ``[<backend>]`` table of settings (``baofeng``, ``kv4p``; ``mock``/``v71`` have no block). Derived
#: (not hardcoded) so it tracks the registry: the presence of one of these blocks in ``radio.toml``
#: declares that backend "configured" and a switch target, validated at load even when not active
#: (ADR 0074).
BACKEND_BLOCK_GROUPS: frozenset[str] = frozenset(
    {s.key.split(".", 1)[0] for s in SETTINGS} & set(available_backends())
)


class Settings:
    """An immutable, fully-resolved configuration. Read values with :meth:`get` by dotted key.

    Every setting in the schema has an entry: either a validated value, a spec default, or the
    `UNSET_REQUIRED` marker (for a required setting left unset). Reading an unset-required key raises
    — the lazy fail-loud that keeps the default mock app (no callsign/voice) starting cleanly.
    """

    __slots__ = ("_values", "_extra", "_configured_backends")

    def __init__(
        self,
        values: Mapping[str, Any],
        extra: Mapping[str, Any] | None = None,
        configured_backends: frozenset[str] = frozenset(),
    ) -> None:
        object.__setattr__(self, "_values", dict(values))
        object.__setattr__(self, "_extra", dict(extra or {}))
        object.__setattr__(self, "_configured_backends", frozenset(configured_backends))

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

    def configured_backend_names(self) -> frozenset[str]:
        """The backends this config declares (ADR 0074): every ``[<backend>]`` block present in the
        file plus the active ``server.backend`` (which boots from defaults even with no block). The
        set the swap cycle can point the holder at and that this cycle validates at load; a
        single-backend config returns just that one backend. Empty by default on a `Settings` built
        without presence info (a bare, hand-constructed one)."""
        return self._configured_backends

    def extra(self, key: str, default: Any = None) -> Any:
        """A local-plugin setting from the ``[plugins.*]`` channel (ADR 0051), by dotted key.

        Deliberately unvalidated and default-forgiving (plugins own their own coercion/failure
        story): ``[plugins.weather] base_url = ...`` is ``extra("weather.base_url")``. Returns
        ``default`` when the key is absent.
        """
        return self._extra.get(key, default)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Settings({self._values!r})"


def _spec(key: str) -> SettingSpec:
    for spec in SETTINGS:
        if spec.key == key:
            return spec
    raise KeyError(key)


def resolve_settings(
    raw: Mapping[str, Any] | None = None, extra: Mapping[str, Any] | None = None
) -> Settings:
    """Validate ``raw`` (a flat dotted-key mapping) against the schema and return `Settings`.

    For each spec: if its key is present, coerce it (present-but-invalid fails loud, naming the key);
    a coercer that returns `USE_DEFAULT` (a blank value) falls through to the default. If the key is
    absent, use the spec default — or `UNSET_REQUIRED` for a required setting. ``extra`` is the
    already-flattened ``[plugins.*]`` channel (ADR 0051), carried through unvalidated.
    """
    raw = raw or {}
    unknown = set(raw) - {s.key for s in SETTINGS}
    if unknown:
        # An unknown key under a table that isn't a schema namespace (e.g. ``weather.base_url``,
        # whose ``weather`` is no schema group) is almost always local-plugin settings left in a
        # flat top-level ``[weather]`` instead of the ``[plugins.weather]`` channel (ADR 0051) —
        # the #1 migration that took stations down. Name the table and its home, mirroring the
        # tailored ``_LEGACY_MUMBLE_KEYS`` error in `_flatten`; a real typo whose namespace *is* a
        # schema group (``server.prot``) still gets the generic message below.
        namespaces = {s.key.split(".", 1)[0] for s in SETTINGS}
        stray: dict[str, list[str]] = {}
        for key in unknown:
            table = key.split(".", 1)[0]
            if table not in namespaces:
                stray.setdefault(table, []).append(key)
        if stray:
            detail = "; ".join(
                f"[{table}] ({', '.join(sorted(keys))}) -> [plugins.{table}]"
                for table, keys in sorted(stray.items())
            )
            raise RuntimeError(
                f"unknown config table(s): {detail}. Local-plugin settings live under "
                f"[plugins.<name>], not a top-level table (ADR 0051) — move each table under "
                f"[plugins.<name>]. The plugin reads the keys unchanged: the dotted key is "
                f"identical, only the TOML nesting moves. See examples/local_services/."
            )
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
    # The configured backend set (ADR 0074): every backend whose `[<backend>]` block is present (any
    # `<backend>.*` key in `raw`) plus the active `server.backend` (always configured — it boots from
    # defaults with no block). Presence must be captured here: every backend key has a default, so a
    # resolved `Settings` can no longer tell a written block from an absent one.
    present = {
        group
        for group in BACKEND_BLOCK_GROUPS
        if any(key == group or key.startswith(f"{group}.") for key in raw)
    }
    configured = frozenset(present | {values["server.backend"]})
    return Settings(values, extra=extra, configured_backends=configured)


def _default_of(spec: SettingSpec) -> Any:
    """A spec's resolved default: its concrete default, or the unset marker if required."""
    return UNSET_REQUIRED if spec.required else spec.default


def load_settings(toml_path: str | Path | None = None) -> Settings:
    """Read ``toml_path`` (via stdlib ``tomllib``) and resolve it. A missing/None path resolves to
    pure defaults — identical to the pre-config-file behavior."""
    raw: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    if toml_path is not None:
        path = Path(toml_path)
        if path.is_file():
            with path.open("rb") as fh:
                data = tomllib.load(fh)
            raw = _flatten(data)
            extra = _flatten_plugins(data.get(PLUGINS_TABLE, {}))
    return resolve_settings(raw, extra=extra)


def _flatten(data: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten a nested TOML table (``[group] key = ...``) to dotted keys (``group.key``).

    Only one level of nesting is expected (the schema is group→leaf); a scalar at top level is kept
    as-is so an unknown flat key still surfaces in `resolve_settings`'s unknown-key check. Three
    reserved channels are skipped: the ``[services]`` table (digit bindings, ADR 0034, read by
    `load_service_bindings`), the ``[[mumble.servers]]`` entry list (ADR 0042, read by
    `load_mumble_servers`), and the ``[plugins]`` namespace (ADR 0051, read by `_flatten_plugins`
    into `Settings.extra`). A leftover flat [mumble] connection setting (pre-0042) fails loud with
    the migration message rather than the generic unknown-key error.
    """
    flat: dict[str, Any] = {}
    for key, value in data.items():
        if key in (SERVICES_TABLE, PLUGINS_TABLE):
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
                        f"(port/channel/tx_to_rf/dtmf/autoconnect are optional per-entry "
                        f"fields; mumble.enabled is gone — an entry with autoconnect = true "
                        f"connects on boot; the station's nick is always "
                        f"'<callsign> (radio-server)', not configurable)"
                    )
                flat[f"{key}.{leaf}"] = leaf_value
        else:
            flat[key] = value
    return flat


def _flatten_plugins(table: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten the ``[plugins]`` namespace (ADR 0051) to dotted keys, dropping the prefix.

    ``[plugins.weather] base_url = ...`` → ``{"weather.base_url": ...}``, so a plugin migrated out
    of the schema keeps its old key spelling in `Settings.extra`. Deliberately tolerant: scalars
    directly under ``[plugins]`` keep their bare key, deeper nesting keeps nesting dots. No
    validation — this channel is the plugins' own.
    """
    flat: dict[str, Any] = {}

    def walk(prefix: str, node: Mapping[str, Any]) -> None:
        for key, value in node.items():
            dotted = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, Mapping):
                walk(dotted, value)
            else:
                flat[dotted] = value

    walk("", table)
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
