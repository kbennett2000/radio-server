"""Writing config back out (ADR 0025).

`save_settings` round-trips through **tomlkit** so a hand-edited ``radio.toml`` keeps its comments
and formatting when the (future, cycle 26) settings API rewrites it — only the values change.
`render_example` generates the shipped ``radio.toml.example`` from the same schema, so the example
can never drift from the registry. Both skip required-unset keys (never emit ``callsign = ""``) and
never touch secrets (which are not in the schema at all).
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

import tomlkit

from .settings import MUMBLE_SERVERS_KEY, Settings
from .spec import SETTINGS, SettingSpec

__all__ = ["save_settings", "render_example", "save_mumble_servers"]

#: Group order and one-line banner for each table in the generated example / fresh file.
_GROUP_BANNERS: dict[str, str] = {
    "station": "Station / identity (Part 97)",
    "auth": "Auth (over-RF TOTP/DTMF plane)",
    "audio": "Audio / squelch (the RX activity gate)",
    "dtmf": "DTMF decode",
    "recording": "Audio recording",
    "tts": "Text-to-speech",
    "time": "Time",
    "tx": "Transmit",
    "scan": "Scan engine",
    "controller": "Controller loop",
    "logging": "Operating log",
    "server": "Server / web / backend",
    "web": "Web UI preferences",
    "baofeng": "Baofeng / AIOC hardware backend (server.backend='baofeng' only)",
    "kv4p": "kv4p HT hardware backend (ADR 0061/0063; server.backend='kv4p' only)",
    "mumble": "Mumble/Murmur link (ADR 0041/0042; destinations under [[mumble.servers]] below)",
    "dstar": "D-STAR link (ADR 0087/0088; off unless dstar.callsign is set — gateway + DV Dongle vocoder; "
    "reflector picker + browser talk/listen via dstar.operator_tx)",
}


def _toml_value(value: Any) -> Any:
    """Normalize a resolved value to a TOML-native scalar (enums → their ``.value``)."""
    if isinstance(value, Enum):
        return value.value
    return value


def _groups() -> list[str]:
    """Groups in banner order, then any not listed (defensive against a new group)."""
    seen = list(_GROUP_BANNERS)
    for spec in SETTINGS:
        if spec.group not in seen:
            seen.append(spec.group)
    return [g for g in seen if any(s.group == g for s in SETTINGS)]


def save_settings(settings: Settings, path: str | Path) -> None:
    """Persist ``settings`` to ``path`` as TOML, preserving an existing file's comments/formatting.

    Required settings left unset are skipped (never written as an empty string); secrets are never
    written (they are not in the schema). Changes take effect on the next restart — this does not
    hot-reload a running server.
    """
    target = Path(path)
    if target.is_file():
        doc = tomlkit.parse(target.read_text(encoding="utf-8"))
    else:
        doc = _fresh_document()
    for spec in SETTINGS:
        if not settings.is_set(spec.key) or settings.get(spec.key) is None:
            continue  # required-unset (never emit callsign = "") or an optional None (kv4p.frequency)
        table = doc.get(spec.group)
        if table is None:
            table = tomlkit.table()
            doc[spec.group] = table
        table[spec.leaf] = _toml_value(settings.get(spec.key))
    target.write_text(tomlkit.dumps(doc), encoding="utf-8")


def save_mumble_servers(servers: list[dict[str, Any]], path: str | Path) -> None:
    """Persist the ``[[mumble.servers]]`` entry list (ADR 0042) to ``path``, preserving the rest of
    the file's comments/formatting. The whole list is replaced (the PUT endpoint's whole-list
    contract); an empty list removes the array. Validation is the caller's job
    (`link.entries.resolve_mumble_entries`) — this only writes. Restart-applied, like every setting.
    """
    target = Path(path)
    if target.is_file():
        doc = tomlkit.parse(target.read_text(encoding="utf-8"))
    else:
        doc = _fresh_document()
    table = doc.get("mumble")
    if table is None:
        table = tomlkit.table()
        doc["mumble"] = table
    if not servers:
        if MUMBLE_SERVERS_KEY in table:
            del table[MUMBLE_SERVERS_KEY]
    else:
        aot = tomlkit.aot()
        for server in servers:
            entry = tomlkit.table()
            for field, value in server.items():
                entry[field] = value
            aot.append(entry)
        table[MUMBLE_SERVERS_KEY] = aot
    target.write_text(tomlkit.dumps(doc), encoding="utf-8")


def _fresh_document() -> tomlkit.TOMLDocument:
    """An empty grouped skeleton (banner comments + tables) for a brand-new file."""
    doc = tomlkit.document()
    doc.add(tomlkit.comment("radio-server configuration (see radio.toml.example / docs)."))
    for group in _groups():
        table = tomlkit.table()
        table.comment(_GROUP_BANNERS.get(group, group))
        doc[group] = table
    return doc


def render_example() -> str:
    """Render ``radio.toml.example`` text: every non-secret setting with its default and its
    description as a comment. Required settings (no default) are shown commented, with a placeholder.
    """
    doc = tomlkit.document()
    doc.add(tomlkit.comment("radio-server example configuration (ADR 0025)."))
    doc.add(tomlkit.comment("Copy to radio.toml and edit. Every value below is the built-in default;"))
    doc.add(tomlkit.comment("delete a line to keep its default. Point the server at this file with"))
    doc.add(tomlkit.comment("  python -m radio_server --config radio.toml"))
    doc.add(tomlkit.comment("Secrets (RADIO_TOTP_SECRET, RADIO_API_TOKEN) do NOT live here — see"))
    doc.add(tomlkit.comment("radio-secrets.toml (chmod 600) or the environment."))
    doc.add(tomlkit.nl())

    for group in _groups():
        table = tomlkit.table()
        table.add(tomlkit.comment(_GROUP_BANNERS.get(group, group)))
        for spec in (s for s in SETTINGS if s.group == group):
            _add_example_entry(table, spec)
        if group == "mumble":
            _add_mumble_servers_example(table)
        doc[group] = table
    _add_services_table(doc)
    return tomlkit.dumps(doc)


def _add_mumble_servers_example(table: Any) -> None:
    """Append the ``[[mumble.servers]]`` docs and the live demo entry (ADR 0042/0052) to
    ``[mumble]``.

    The entry list is the Mumble-destinations channel, outside the `SettingSpec` schema (like
    ``[services]``) — documented in comments, plus one **live** entry: the public demo server, so a
    fresh config can key ``10#`` and land in a real channel out of the box (ADR 0052).
    """
    for line in (
        "Destinations: repeat one [[mumble.servers]] block per server/channel (ADR 0042).",
        "One link is active at a time; connecting another entry switches. Fields: name",
        "(required; any text, e.g. \"Club Net\"), host (required), port (64738),",
        "channel ('' = root), dtmf ('' = no combo; digits 0-9/A-D keyed before '#' connect this",
        "entry from an authenticated DTMF session), tx_to_rf (true; false = receive-only",
        "monitor), autoconnect (false; at most one entry, connects on boot), password ('' = none;",
        "fine here for a public join code, like the demo's). For a private server, prefer the",
        "secrets channel — the secret mumble_password_<slug> (radio-secrets.toml, chmod 600) or",
        "the RADIO_MUMBLE_PASSWORD_<SLUG> environment variable, where <slug> is the name",
        "lowercased with punctuation/spaces as '_' — it overrides any password set here. The",
        "station's nick on every server is '<callsign> (radio-server)', from station.callsign.",
        "",
        "[[mumble.servers]]",
        'name = "Club Net"',
        'host = "murmur.example.net"',
        'channel = "Club Net"',
        'dtmf = "11"',
        "",
        "The public demo server — live by default so 10# works out of the box (ADR 0052). Its",
        "password is a public gate code, not a secret. Delete this block if you don't want it.",
    ):
        table.add(tomlkit.comment(line) if line else tomlkit.nl())
    demo = tomlkit.table()
    demo["name"] = "Radio Server Demo"
    demo["host"] = "104.168.125.41"
    demo["port"] = 64738
    demo["dtmf"] = "10"
    demo["password"] = "github.com/kbennett2000/radio-server"
    aot = tomlkit.aot()
    aot.append(demo)
    table[MUMBLE_SERVERS_KEY] = aot


def _add_services_table(doc: Any) -> None:
    """Append the ``[services]`` digit→id binding table (ADR 0034) to the example document.

    This is the operator's complete keypad layout, a separate channel from the `SettingSpec` schema.
    Values are service ids or the two controller built-ins (``station-id`` / ``logout``) — the
    built-ins are ordinary entries here, so their digit is remappable like any service's. Edit a
    digit to remap; delete the whole table to fall back to these defaults. A service whose data
    source is unconfigured stays a silent no-op on its digit; a built-in you omit is simply off the
    keypad (auto-ID and the idle timeout still run regardless).
    """
    # Imported here (not at module top) to keep the import direction obvious: this is the one place
    # config reaches into the service plugin registry for its default layout.
    from ..services.plugin import BUILTIN_IDS, DEFAULT_BINDINGS

    table = tomlkit.table()
    table.add(tomlkit.comment("Keypad layout: which DTMF digit invokes which service or command."))
    table.add(
        tomlkit.comment(
            "Values are service ids; remap a digit by changing its value, or delete this table to"
        )
    )
    table.add(tomlkit.comment("keep the defaults below. A service whose data source is unconfigured"))
    table.add(tomlkit.comment("stays a silent no-op on its digit."))
    builtins = ", ".join(f"{name} ({desc})" for name, desc in BUILTIN_IDS.items())
    table.add(tomlkit.comment(f"Controller built-ins, movable like any service: {builtins}."))
    table.add(
        tomlkit.comment(
            "The Mumble link combos (10# connect / 98# link off) live under [mumble], not here."
        )
    )
    for digit, target_id in DEFAULT_BINDINGS.items():
        table[digit] = target_id
    doc["services"] = table
    _add_plugins_note(doc)


def _add_plugins_note(doc: Any) -> None:
    """Append the commented ``[plugins.*]`` note (ADR 0051) after the ``[services]`` table.

    The plugins namespace is the third non-schema channel (after ``[services]`` and
    ``[[mumble.servers]]``): deliberately unvalidated, reserved for operator-authored service
    plugins in ``local_services/``. All comments — there is nothing to ship by default.
    """
    doc.add(tomlkit.nl())
    for line in (
        "Your own services (ADR 0051): drop a plugin module in ./local_services/, bind a digit to",
        "its id in [services] above, and put its settings in a [plugins.<name>] table — e.g. a",
        "plugin reading settings.extra(\"weather.base_url\") gets it from:",
        "[plugins.weather]",
        'base_url = "http://192.168.1.62:8005/api/v1"',
        "Tables under [plugins] are not schema-checked (any key is allowed) and survive settings",
        "saves untouched. Name plugin files to avoid shadowing installed modules (the folder joins",
        "the import path).",
    ):
        doc.add(tomlkit.comment(line))


#: Settings whose built-in default is machine-specific (an absolute path resolved at runtime from the
#: install location). Emitting the literal default would bake one machine's path into the shipped
#: example, so these are shown as a commented, portable placeholder instead. The runtime default
#: still applies when the line is absent — the example is only documenting it.
_COMMENTED_DEFAULTS: dict[str, str] = {
    "server.web_dir": 'web_dir = "/path/to/radio-server/web/dist"   # default: <repo>/web/dist',
    "kv4p.frequency": "frequency = 146520000   # unset: keep the device's last-used (NVS) frequency",
}


def _add_example_entry(table: Any, spec: SettingSpec) -> None:
    for line in _wrap(spec.description):
        table.add(tomlkit.comment(line))
    if spec.required:
        table.add(tomlkit.comment(f"{spec.leaf} = {_placeholder(spec)}   # REQUIRED — no default"))
    elif spec.key in _COMMENTED_DEFAULTS:
        table.add(tomlkit.comment(_COMMENTED_DEFAULTS[spec.key]))
    else:
        table[spec.leaf] = _toml_value(spec.default)
    table.add(tomlkit.nl())


def _placeholder(spec: SettingSpec) -> str:
    return {
        "station.callsign": '"N0CALL"',
        "tts.voice": '"/path/to/voice.onnx"',
    }.get(spec.key, '""')


def _wrap(text: str, width: int = 92) -> list[str]:
    """Greedy word-wrap for a comment block (keeps the example readable)."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines
