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

from .settings import DVAP_MODULES_KEY, MUMBLE_SERVERS_KEY, PRESETS_TABLE, Settings
from .spec import SETTINGS, SettingSpec

__all__ = ["save_settings", "render_example", "save_mumble_servers", "save_presets"]

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
    "uvk5": "UV-K5 Quansheng Dock hardware backend (ADR 0110-0114; server.backend='uvk5' only — "
    "see docs/uvk5-setup.md)",
    "mumble": "Mumble/Murmur link (ADR 0041/0042; destinations under [[mumble.servers]] below)",
    "dstar": "D-STAR link (ADR 0087/0088/0089; off unless dstar.callsign is set — gateway + DV Dongle "
    "vocoder; reflector picker, crossband + browser talk/listen, shared DV Dongle across instances)",
    "dvap": "DVAP control (ADR 0095; off unless [[dvap.modules]] is populated — link/unlink/monitor the "
    "DVAP gateway modules over the ircDDBGateway remote-control interface; no vocoder, no PTT)",
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
    # A backend block is persisted only when it can actually be built: if any REQUIRED key in a
    # backend group is unset, skip the whole group rather than fabricate an incomplete, unbuildable
    # block. Writing uvk5's default mode/tx_allowed without its REQUIRED serial_port/frequency would
    # make an unconfigured uvk5 look "configured" (presence-based, ADR 0074) and crash the backend
    # enumeration/validation on a value that was never set (ADR 0114). Non-backend groups keep the
    # per-key skip below (e.g. an unset station.callsign still leaves the rest of [station] written).
    from .settings import BACKEND_BLOCK_GROUPS

    incomplete_backends = {
        spec.group
        for spec in SETTINGS
        if spec.group in BACKEND_BLOCK_GROUPS and spec.required and not settings.is_set(spec.key)
    }
    for spec in SETTINGS:
        if spec.group in incomplete_backends:
            continue  # an unconfigured backend block (a REQUIRED key unset) is not persisted at all
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


#: Field order inside a written ``[[presets]]`` entry — reading order (where you listen, where you
#: transmit, how), and fixed so re-running an import is byte-identical.
_PRESET_FIELD_ORDER = (
    "name", "frequency", "tx_frequency", "tx_tone", "rx_tone", "mode", "power",
)


def save_presets(presets: list[dict[str, Any]], path: str | Path, *, replace: bool = False) -> int:
    """Merge ``presets`` into the ``[[presets]]`` array at ``path``. Returns the entry count written.

    The array is **re-emitted from the merged list**, so the written bytes are a pure function of
    that list — idempotent by construction rather than by trivia bookkeeping. Patching entries in
    place instead means inheriting tomlkit's blank-line rules: measured, an appended entry gets a
    leading ``"\\n"`` and a replaced one gets ``""``, so an import and a re-import of the same file
    differ by one blank line per entry. Idempotency is load-bearing — this writes a config a station
    reads at startup, and "did that import change anything?" has to be answerable by comparing bytes.

    Measured, of what surrounds the array: the banner comment above it, comments on a ``[[presets]]``
    header, inline comments on a key, every other section of the file — **kept**. A standalone
    comment *line* between two keys inside an entry — **lost** (it belongs to no value, so there is
    nothing to carry it). Use the ``comment`` key (or a CHIRP ``Comment`` column) for an entry note
    that always survives, since those are re-emitted on every write.

    Matching is by **casefolded name**, because `presets.resolve_presets` enforces case-insensitive
    uniqueness — a case-sensitive merge would happily write a duplicate that stops the service from
    starting on the next restart. New entries **append**: the acceptance runner picks its retune
    target as "the first preset that is not the current frequency", so prepending an imported list
    would park the bench on a live local repeater output.

    Validation is the caller's job (`presets.resolve_presets`) — this only writes. ``replace=True``
    drops any existing entries instead of merging into them.
    """
    target = Path(path)
    doc = tomlkit.parse(target.read_text(encoding="utf-8")) if target.is_file() else tomlkit.document()

    existing: list[dict[str, Any]] = []
    if not replace:
        for entry in doc.get(PRESETS_TABLE) or []:
            row = {key: value for key, value in entry.items()}
            comment = entry.trivia.comment.lstrip("# ").strip()
            if comment:
                row["comment"] = comment
            existing.append(row)

    merged = list(existing)
    index = {str(row.get("name", "")).casefold(): position for position, row in enumerate(merged)}
    for preset in presets:
        key = str(preset.get("name", "")).casefold()
        position = index.get(key)
        if position is None:
            index[key] = len(merged)
            merged.append(dict(preset))
        else:
            merged[position] = dict(preset)

    aot = tomlkit.aot()
    for row in merged:
        entry = tomlkit.table()
        for field in _PRESET_FIELD_ORDER:
            value = row.get(field)
            if value is not None:
                entry[field] = value
        for field, value in row.items():  # anything the order tuple does not know about
            if field not in _PRESET_FIELD_ORDER and field != "comment" and value is not None:
                entry[field] = value
        # `comment` is not a preset FIELD — `resolve_presets` would reject it as an unknown key.
        # It becomes an actual TOML comment, which is where a CHIRP export's Comment column belongs.
        if row.get("comment"):
            entry.comment(str(row["comment"]))
        aot.append(entry)
    doc[PRESETS_TABLE] = aot
    target.write_text(tomlkit.dumps(doc), encoding="utf-8")
    return len(aot)


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
        if group == "dvap":
            _add_dvap_modules_example(table)
        doc[group] = table
    _add_services_table(doc)
    _add_presets_table(doc)
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


def _add_dvap_modules_example(table: Any) -> None:
    """Append the ``[[dvap.modules]]`` docs and two commented example modules to ``[dvap]`` (ADR 0095).

    The module list is the DVAP-control channel, outside the `SettingSpec` schema (like
    ``[[mumble.servers]]``). Off by default — no live entry: a DVAP module only works if the operator
    has stood up a matching ``dstarrepeater`` node + gateway module, so the examples stay commented.
    """
    for line in (
        "DVAP modules: repeat one [[dvap.modules]] block per DVAP hotspot the ircDDBGateway hosts",
        "(ADR 0095). radio-server does NOT carry their audio — it links/unlinks/monitors them over",
        "the gateway remote-control interface (dvap.host/dvap.port above; the remotePassword is the",
        "secret dvap_remote_password in radio-secrets.toml). Each needs a matching dstarrepeater node",
        "+ gateway repeater band. Fields: module (single letter, matches the gateway band; required),",
        "label (display text), frequency_hz (the DVAP's RF frequency, display only). Uncomment and",
        "edit for your gateway:",
        "",
        "[[dvap.modules]]",
        'module = "B"',
        'label = "DVAP 70cm #1"',
        "frequency_hz = 441600000",
        "",
        "[[dvap.modules]]",
        'module = "C"',
        'label = "DVAP 70cm #2"',
        "frequency_hz = 441000000",
    ):
        table.add(tomlkit.comment(line) if line else tomlkit.nl())


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


def _add_presets_table(doc: Any) -> None:
    """Append the commented ``[[presets]]`` channel-preset examples (ADR 0115/0133) to the document.

    Channel presets are named host-side tuning entries, a top-level list of tables outside the
    `SettingSpec` schema (like ``[services]``). Off by default — no live entry: a preset's frequency
    is the operator's local repeater/simplex choice, so the examples stay commented. Applied via
    ``POST /presets/apply`` on a tuning backend (kv4p/uvk5/mock); ignored where the radio can't tune
    (Baofeng). Only what the backend supports is applied — anything skipped is reported, never
    silent.
    """
    doc.add(tomlkit.nl())
    for line in (
        "Channel presets (ADR 0115/0133): repeat one [[presets]] block per channel you want to",
        "recall by name. Fields:",
        "  name          required, any text, unique (case-insensitively)",
        "  frequency     required, Hz — what you LISTEN on (a repeater's output)",
        "  tx_frequency  optional, Hz — what you TRANSMIT on. Omit for simplex. Absolute, not an",
        "                offset: 145.460 minus 600 kHz is written 144860000, and the API reports",
        "                the offset back to you.",
        "  tx_tone       optional, the CTCSS tone in Hz this station transmits (e.g. 107.2) — this",
        "                is what opens a repeater. A standard EIA tone or startup fails loud.",
        "  rx_tone       optional, STORED BUT NOT HONOURED — there is no receive tone squelch, so",
        "                this is kept only so an imported channel list round-trips. Every apply",
        "                reports it as unhonoured rather than silently ignoring it.",
        "  mode          FM (default) or NFM — the channel BANDWIDTH, wide or narrow.",
        "  modulation    FM (default) or AM — the DEMODULATOR, i.e. what kind of signal this",
        "                channel carries. Not the same field as `mode`, even though both spell",
        "                one of their values FM: airband is modulation = \"AM\". Omitting it means",
        "                FM, not 'leave whatever is set' — a demodulator belongs to the channel,",
        "                so tapping a repeater after an airband channel returns you to FM.",
        "                Needs a UV-K5 on F7 firmware (baofeng.uvk5_tuner = setvfo or hybrid);",
        "                any other radio reports it skipped rather than silently ignoring it.",
        "                NOTE: this radio cannot TRANSMIT in AM — the firmware disables its own",
        "                PTT path — so an AM channel is receive-only and a key-up is refused.",
        "  power         optional, low / mid / high — how hard to transmit on THIS channel.",
        "                Omitting it is not 'default': it means leave the station's current level",
        "                alone (baofeng.uvk5_power, or whatever the UI last set). Set it on the",
        "                channels where it matters, not on all of them.",
        "Importing a CHIRP CSV export writes these for you:",
        "  python -m radio_server.chirp my-channels.csv --into radio.toml",
        "Apply one with:",
        '  curl -H "Authorization: Bearer $TOKEN" -X POST localhost:8000/presets/apply -d \'{"name":"..."}\'',
        "Uncomment and edit for your channels:",
        "",
        "[[presets]]",
        'name = "2m Simplex"',
        "frequency = 146520000",
        'mode = "FM"',
        "",
        "[[presets]]",
        'name = "Club Repeater"',
        "frequency = 146940000",
        "tx_frequency = 146340000",
        "tx_tone = 100.0",
        'mode = "FM"',
    ):
        doc.add(tomlkit.comment(line) if line else tomlkit.nl())


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
    "uvk5.tone": "tone = 100.0   # unset: no CTCSS tone",
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
        "uvk5.serial_port": '"/dev/serial/by-id/usb-...All-In-One-Cable..."',
        "uvk5.frequency": "146520000",
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
