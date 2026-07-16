"""Named Mumble server/channel entries (ADR 0042).

The operator defines N connectable destinations as ``[[mumble.servers]]`` tables in ``radio.toml``
— a channel the flat `SettingSpec` schema cannot model, so it lives outside the registry exactly
like the ``[services]`` digit-binding table (ADR 0034): `config.settings.load_mumble_servers` reads
the raw list, and :func:`resolve_mumble_entries` here validates it into frozen :class:`MumbleEntry`
values (the ``load_service_bindings`` / ``resolve_bindings`` split).

Each entry may carry a DTMF combo (``dtmf = "13"`` → keying ``13#`` over RF connects it). Combo
validation (:func:`validate_link_digits`) is fail-loud and stricter than the ``[services]``
alphabet: ``#`` submits and ``*`` clears in the framer, so neither can ever appear *inside* a
matchable combo. Matching is exact-string — ``"13"`` and ``"1"`` do not collide, because the framer
submits the whole buffered string.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .client import (
    DEFAULT_MUMBLE_CHANNEL,
    DEFAULT_MUMBLE_PORT,
    DEFAULT_MUMBLE_TX_TO_RF,
    DEFAULT_MUMBLE_USERNAME,
)

__all__ = [
    "MumbleEntry",
    "resolve_mumble_entries",
    "validate_link_digits",
    "mumble_password_secret",
    "DEFAULT_MUMBLE_DISCONNECT_DTMF",
    "LINK_DTMF_ALPHABET",
]

#: Combo that disconnects whatever entry is linked (``73#`` — best regards). A schema setting
#: (``mumble.disconnect_dtmf``), remappable; validated against the same rules as entry combos.
DEFAULT_MUMBLE_DISCONNECT_DTMF = "73"

#: Characters allowed in a link combo. Deliberately narrower than the ``[services]`` alphabet:
#: ``#`` submits and ``*`` clears in `DtmfFramer`, so neither can appear inside a matchable combo.
LINK_DTMF_ALPHABET = frozenset("0123456789ABCD")

#: Entry names are slugs: bare TOML keys, URL-path-safe, and env-var-mappable (no dash/underscore
#: ambiguity when uppercased into ``RADIO_MUMBLE_PASSWORD_<NAME>``).
_NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")

#: The fields an entry table may carry; anything else is a typo and fails loud.
_KNOWN_FIELDS = frozenset(
    {"name", "host", "port", "username", "channel", "dtmf", "tx_to_rf", "autoconnect"}
)


@dataclass(frozen=True)
class MumbleEntry:
    """One connectable Mumble destination — a server (or a channel on one), operator-named."""

    name: str
    host: str
    port: int = DEFAULT_MUMBLE_PORT
    username: str = DEFAULT_MUMBLE_USERNAME
    channel: str = DEFAULT_MUMBLE_CHANNEL
    #: DTMF combo (digits before ``#``) that connects this entry; ``""`` = no combo assigned.
    dtmf: str = ""
    tx_to_rf: bool = DEFAULT_MUMBLE_TX_TO_RF
    #: Connect this entry on server boot (at most one entry may set this).
    autoconnect: bool = False


def mumble_password_secret(name: str) -> str:
    """The dynamic secret name holding ``name``'s server password (ADR 0042)."""
    return f"mumble_password_{name}"


def resolve_mumble_entries(raw: Sequence[Mapping] | None) -> tuple[MumbleEntry, ...]:
    """Validate the raw ``[[mumble.servers]]`` list into `MumbleEntry` values. Fails loud.

    Shape rules: every entry needs a slug ``name`` (unique) and a non-empty ``host``; unknown
    fields are typos; at most one entry may ``autoconnect``. Combo digits are validated here for
    charset/duplicates; cross-checking against the keypad layout is :func:`validate_link_digits`'
    job (the consumer holds the resolved service bindings).
    """
    if not raw:
        return ()
    entries: list[MumbleEntry] = []
    seen_names: set[str] = set()
    seen_dtmf: dict[str, str] = {}
    autoconnect: str | None = None
    for index, table in enumerate(raw):
        unknown = set(table) - _KNOWN_FIELDS
        if unknown:
            raise RuntimeError(
                f"[[mumble.servers]] entry {index + 1}: unknown field(s) "
                f"{', '.join(sorted(unknown))}; known: {', '.join(sorted(_KNOWN_FIELDS))}"
            )
        name = str(table.get("name", "") or "")
        if not _NAME_RE.fullmatch(name):
            raise RuntimeError(
                f"[[mumble.servers]] entry {index + 1}: name {name!r} must match "
                f"[a-z0-9_]{{1,32}} (a lowercase slug)"
            )
        if name in seen_names:
            raise RuntimeError(f"[[mumble.servers]] name {name!r} appears more than once")
        seen_names.add(name)
        host = str(table.get("host", "") or "").strip()
        if not host:
            raise RuntimeError(f"[[mumble.servers]] {name}: host is required")
        dtmf = str(table.get("dtmf", "") or "")
        if dtmf:
            _check_combo(dtmf, f"[[mumble.servers]] {name}: dtmf")
            if dtmf in seen_dtmf:
                raise RuntimeError(
                    f"[[mumble.servers]] {name}: dtmf {dtmf!r} is already assigned to "
                    f"{seen_dtmf[dtmf]!r}"
                )
            seen_dtmf[dtmf] = name
        entry = MumbleEntry(
            name=name,
            host=host,
            port=_coerce_int(table.get("port", DEFAULT_MUMBLE_PORT), name, "port"),
            username=str(table.get("username", DEFAULT_MUMBLE_USERNAME) or DEFAULT_MUMBLE_USERNAME),
            channel=str(table.get("channel", DEFAULT_MUMBLE_CHANNEL) or ""),
            dtmf=dtmf,
            tx_to_rf=_coerce_bool(table.get("tx_to_rf", DEFAULT_MUMBLE_TX_TO_RF), name, "tx_to_rf"),
            autoconnect=_coerce_bool(table.get("autoconnect", False), name, "autoconnect"),
        )
        if entry.autoconnect:
            if autoconnect is not None:
                raise RuntimeError(
                    f"[[mumble.servers]] {name}: autoconnect is already set on {autoconnect!r}; "
                    f"only one entry may autoconnect (one active link at a time, ADR 0042)"
                )
            autoconnect = name
        entries.append(entry)
    return tuple(entries)


def validate_link_digits(
    entries: Sequence[MumbleEntry],
    disconnect_dtmf: str,
    service_bindings: Mapping[str, str],
) -> None:
    """Cross-check the link combos against the disconnect combo and the keypad layout. Fails loud.

    ``service_bindings`` is the RESOLVED digit→id map (services + built-ins, defaults included) —
    exact-string comparison only, matching how `DtmfFramer` submits whole buffered strings.
    """
    _check_combo(disconnect_dtmf, "mumble.disconnect_dtmf")
    for entry in entries:
        if not entry.dtmf:
            continue
        if entry.dtmf == disconnect_dtmf:
            raise RuntimeError(
                f"[[mumble.servers]] {entry.name}: dtmf {entry.dtmf!r} collides with "
                f"mumble.disconnect_dtmf"
            )
        if entry.dtmf in service_bindings:
            raise RuntimeError(
                f"[[mumble.servers]] {entry.name}: dtmf {entry.dtmf!r} is already bound to "
                f"{service_bindings[entry.dtmf]!r} in the [services] keypad layout"
            )
    if disconnect_dtmf in service_bindings:
        raise RuntimeError(
            f"mumble.disconnect_dtmf {disconnect_dtmf!r} is already bound to "
            f"{service_bindings[disconnect_dtmf]!r} in the [services] keypad layout"
        )


def _check_combo(digits: str, label: str) -> None:
    bad = set(digits) - LINK_DTMF_ALPHABET
    if not digits or bad:
        raise RuntimeError(
            f"{label} {digits!r} must be one or more of 0-9/A-D "
            f"('#' submits and '*' clears — they can never appear inside a combo)"
        )


def _coerce_int(raw: object, name: str, field: str) -> int:
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"[[mumble.servers]] {name}: {field}={raw!r} is not an integer") from exc


def _coerce_bool(raw: object, name: str, field: str) -> bool:
    if isinstance(raw, bool):
        return raw
    raise RuntimeError(f"[[mumble.servers]] {name}: {field}={raw!r} must be true or false")
