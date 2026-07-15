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

from .settings import Settings
from .spec import SETTINGS, SettingSpec

__all__ = ["save_settings", "render_example"]

#: Group order and one-line banner for each table in the generated example / fresh file.
_GROUP_BANNERS: dict[str, str] = {
    "station": "Station / identity (Part 97)",
    "audio": "Audio / squelch (the RX activity gate)",
    "dtmf": "DTMF decode",
    "weather": "Weather station (optional; enables the 2#/3# voice services)",
    "quote": "Quote API (optional; enables the 5# voice service)",
    "battery": "Battery monitor (optional; enables the 6# voice service)",
    "bible": "Bible / Concord Scripture API (optional; enables the 7# voice service)",
    "recording": "Audio recording",
    "tts": "Text-to-speech",
    "time": "Time",
    "tx": "Transmit",
    "scan": "Scan engine",
    "controller": "Controller loop",
    "logging": "Operating log",
    "server": "Server / web / backend",
    "baofeng": "Baofeng / AIOC hardware backend (server.backend='baofeng' only)",
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
        if not settings.is_set(spec.key):
            continue  # required-unset: never emit callsign = ""
        table = doc.get(spec.group)
        if table is None:
            table = tomlkit.table()
            doc[spec.group] = table
        table[spec.leaf] = _toml_value(settings.get(spec.key))
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
        doc[group] = table
    _add_services_table(doc)
    return tomlkit.dumps(doc)


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
    for digit, target_id in DEFAULT_BINDINGS.items():
        table[digit] = target_id
    doc["services"] = table


def _add_example_entry(table: Any, spec: SettingSpec) -> None:
    for line in _wrap(spec.description):
        table.add(tomlkit.comment(line))
    if spec.required:
        table.add(tomlkit.comment(f"{spec.leaf} = {_placeholder(spec)}   # REQUIRED — no default"))
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
