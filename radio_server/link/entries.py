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
    "link_username",
    "resolve_mumble_entries",
    "slugify",
    "validate_link_digits",
    "mumble_password_secret",
    "DEFAULT_MUMBLE_DISCONNECT_DTMF",
    "LINK_DTMF_ALPHABET",
    "MAX_NAME_LENGTH",
]

#: Combo that disconnects whatever entry is linked (``98#`` — pairs with the two-digit shipped
#: keypad, ADR 0051/0052). A schema setting (``mumble.disconnect_dtmf``), remappable; validated
#: against the same rules as entry combos.
DEFAULT_MUMBLE_DISCONNECT_DTMF = "98"

#: Characters allowed in a link combo. Deliberately narrower than the ``[services]`` alphabet:
#: ``#`` submits and ``*`` clears in `DtmfFramer`, so neither can appear inside a matchable combo.
LINK_DTMF_ALPHABET = frozenset("0123456789ABCD")

#: Entry names are free text (ADR 0052) — anything printable, up to this length. The identifier
#: roles the name used to play (secret name, env-var suffix, URL segment, dict key) moved to the
#: derived ``slug``.
MAX_NAME_LENGTH = 64

#: The slug alphabet: bare-TOML-key-, URL-path- and env-var-safe (no dash/underscore ambiguity
#: when uppercased into ``RADIO_MUMBLE_PASSWORD_<SLUG>``). Every ADR-0042 slug name slugifies to
#: itself, so pre-0052 configs keep their secret names and URLs.
_SLUG_MAX = 32
_SLUG_RUNS = re.compile(r"[^a-z0-9]+")

#: The fields an entry table may carry; anything else is a typo and fails loud. The read-only
#: serialization extras ``slug`` and ``password_set`` are tolerated on input — the settings editor
#: round-trips whole serialized entries, so an exact echo of GET must PUT cleanly — but they are
#: always recomputed/ignored, never trusted.
_READONLY_FIELDS = frozenset({"slug", "password_set"})
_KNOWN_FIELDS = (
    frozenset({"name", "host", "port", "channel", "dtmf", "tx_to_rf", "autoconnect", "password"})
    | _READONLY_FIELDS
)


@dataclass(frozen=True)
class MumbleEntry:
    """One connectable Mumble destination — a server (or a channel on one), operator-named."""

    name: str
    host: str
    #: Derived identifier (`slugify(name)`): the secret/env/URL/dict key. Computed, never input.
    slug: str = ""
    port: int = DEFAULT_MUMBLE_PORT
    channel: str = DEFAULT_MUMBLE_CHANNEL
    #: DTMF combo (digits before ``#``) that connects this entry; ``""`` = no combo assigned.
    dtmf: str = ""
    tx_to_rf: bool = DEFAULT_MUMBLE_TX_TO_RF
    #: Connect this entry on server boot (at most one entry may set this).
    autoconnect: bool = False
    #: Plaintext join password (ADR 0052) — for *public* gate codes like the demo server's. The
    #: secrets channel (``mumble_password_<slug>``) overrides this when set; private servers
    #: should keep using it.
    password: str = ""


def slugify(name: str) -> str:
    """The derived identifier for an entry name (ADR 0052): lowercase, non-alphanumeric runs
    collapse to ``_``, trimmed, capped at 32 chars. A valid ADR-0042 slug maps to itself."""
    slug = _SLUG_RUNS.sub("_", name.lower()).strip("_")[:_SLUG_MAX].rstrip("_")
    return slug


def mumble_password_secret(slug: str) -> str:
    """The dynamic secret name holding this entry's server password (ADR 0042/0052), by slug."""
    return f"mumble_password_{slug}"


def link_username(callsign: str | None) -> str:
    """The Mumble nick the station presents on every server: ``<CALLSIGN> (radio-server)``.

    Not per-entry configuration — the station identifies as the licensee everywhere, so the nick
    is computed from ``station.callsign``. Callsign-less deployments (bench/mock, which never
    transmit) fall back to the bare default. The space and parens are bench-confirmed against a
    stock Murmur (mumblevoip/mumble-server, default username policy): accepted verbatim
    (guardrail 1).
    """
    return f"{callsign} (radio-server)" if callsign else DEFAULT_MUMBLE_USERNAME


def resolve_mumble_entries(raw: Sequence[Mapping] | None) -> tuple[MumbleEntry, ...]:
    """Validate the raw ``[[mumble.servers]]`` list into `MumbleEntry` values. Fails loud.

    Shape rules: every entry needs a ``name`` (free text, ADR 0052; its derived slug must be
    non-empty and unique) and a non-empty ``host``; unknown fields are typos; at most one entry
    may ``autoconnect``. Combo digits are validated here for charset/duplicates; cross-checking
    against the keypad layout is :func:`validate_link_digits`' job (the consumer holds the
    resolved service bindings).
    """
    if not raw:
        return ()
    entries: list[MumbleEntry] = []
    seen_slugs: dict[str, str] = {}
    seen_dtmf: dict[str, str] = {}
    autoconnect: str | None = None
    for index, table in enumerate(raw):
        if "username" in table:
            raise RuntimeError(
                f"[[mumble.servers]] entry {index + 1}: username is no longer configurable — "
                f"delete the line; the station identifies as '<callsign> (radio-server)' "
                f"from station.callsign"
            )
        unknown = set(table) - _KNOWN_FIELDS
        if unknown:
            raise RuntimeError(
                f"[[mumble.servers]] entry {index + 1}: unknown field(s) "
                f"{', '.join(sorted(unknown))}; known: "
                f"{', '.join(sorted(_KNOWN_FIELDS - _READONLY_FIELDS))}"
            )
        name = str(table.get("name", "") or "").strip()
        if not name or len(name) > MAX_NAME_LENGTH:
            raise RuntimeError(
                f"[[mumble.servers]] entry {index + 1}: name {name!r} must be 1-"
                f"{MAX_NAME_LENGTH} characters"
            )
        slug = slugify(name)
        if not slug:
            raise RuntimeError(
                f"[[mumble.servers]] entry {index + 1}: name {name!r} needs at least one "
                f"letter or digit"
            )
        if slug in seen_slugs:
            raise RuntimeError(
                f"[[mumble.servers]] name {name!r} collides with {seen_slugs[slug]!r} "
                f"(both shorten to {slug!r})"
            )
        seen_slugs[slug] = name
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
            slug=slug,
            port=_coerce_int(table.get("port", DEFAULT_MUMBLE_PORT), name, "port"),
            channel=str(table.get("channel", DEFAULT_MUMBLE_CHANNEL) or ""),
            dtmf=dtmf,
            tx_to_rf=_coerce_bool(table.get("tx_to_rf", DEFAULT_MUMBLE_TX_TO_RF), name, "tx_to_rf"),
            autoconnect=_coerce_bool(table.get("autoconnect", False), name, "autoconnect"),
            password=str(table.get("password", "") or ""),
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
