"""Channel presets — host-side named tuning entries (ADR 0115/0133).

Radio channels live on the server, not in any radio's memory: the UV-K5 dock has no memory-channel
select and the `CatRadio` backends deliberately omit ``SET_CHANNEL`` (ADR 0111/0112). A "channel" is
therefore a **preset** the operator names in ``radio.toml`` and applies through the existing tuning
surface (`set_frequency` / `set_split` / `set_tone` / `set_mode`).

A preset may also carry a **repeater split** (ADR 0133): ``tx_frequency`` is where the radio
transmits while it listens on ``frequency``, and ``tx_tone`` is the CTCSS that opens the repeater.
Storage is the absolute TX frequency in Hz, never an offset — ``offset`` is *reported* (derived) but
is not an input spelling, the same `computed, never input` rule `link/entries.py` applies to slugs.
``rx_tone`` is carried for fidelity with imported channel lists but is **not honoured**: there is no
RX tone squelch, RSSI squelch still gates receive, and a preset that sets one says so in its skip
report rather than pretending.

The ``[[presets]]`` array-of-tables is a list of tables the flat `SettingSpec` schema cannot model, so
it lives outside the registry exactly like ``[[mumble.servers]]`` (ADR 0042): `config.settings.load_presets`
reads the raw list and :func:`resolve_presets` here validates it fail-loud into frozen :class:`Preset`
values — the ``load_mumble_servers`` / ``resolve_mumble_entries`` split.

The apply seam is capability-gated per field: :func:`apply_preset` always sets the frequency (the
endpoint is 501-gated on ``SET_FREQUENCY`` upstream, like ``POST /frequency``), and applies mode/tone
only when the active backend advertises ``SET_MODE`` / ``SET_TONE`` — reporting anything it skipped so a
missing capability is explicit, never silent (guardrail 3). :func:`split_preset_fields` is the pure
honoured/skipped split both the apply seam and ``GET /presets`` use.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .backends.base import Capability

__all__ = [
    "Preset",
    "CTCSS_TONES",
    "VALID_MODES",
    "DEFAULT_MODE",
    "MAX_NAME_LENGTH",
    "resolve_presets",
    "split_preset_fields",
    "apply_preset",
]

#: The standard EIA 38-tone CTCSS set (67.0 … 250.3 Hz). A public table (not firmware); a preset's
#: ``tone`` must be one of these exactly, or load fails loud. Kept here self-contained — the kv4p
#: backend holds its own private copy (`backends/kv4p/radio.py`) for SA818 index mapping; a future
#: shared-tones refactor could unify them, but presets must not couple to a backend module.
CTCSS_TONES: frozenset[float] = frozenset(
    {
        67.0, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5, 94.8,
        97.4, 100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0, 127.3, 131.8,
        136.5, 141.3, 146.2, 151.4, 156.7, 162.2, 167.9, 173.8, 179.9, 186.2,
        192.8, 203.5, 210.7, 218.1, 225.7, 233.6, 241.8, 250.3,
    }
)

#: The operating modes a preset may carry — the shared FM/NFM vocabulary every `CatRadio` backend
#: accepts (kv4p's ``_MODE_TO_BW``, uvk5's ``_MODE_ALIASES``). Upper-cased at load.
VALID_MODES: frozenset[str] = frozenset({"FM", "NFM"})

#: A preset with no ``mode`` defaults to wide FM — the safe, conventional monitor default.
DEFAULT_MODE = "FM"

#: Preset names are free text (like ADR 0052 entry names), capped so a stray blob can't be a name.
MAX_NAME_LENGTH = 64

#: The fields a preset table may carry; anything else is a typo and fails loud (mirrors the
#: ``[[mumble.servers]]`` ``_KNOWN_FIELDS`` discipline in ``link/entries.py``).
_KNOWN_FIELDS = frozenset({"name", "frequency", "tx_frequency", "tx_tone", "rx_tone", "mode"})

#: Renamed fields kept only so the old spelling fails with the rewrite instead of "unknown field"
#: (the ``_LEGACY_MUMBLE_KEYS`` shape in ``config/settings.py``). ``tone`` always meant the
#: **transmit** tone — both backends implement ``set_tone`` as TX-only CTCSS — and once ``rx_tone``
#: exists beside it, a bare ``tone`` is genuinely ambiguous (ADR 0133).
_RENAMED_FIELDS: dict[str, str] = {"tone": "tx_tone"}


@dataclass(frozen=True)
class Preset:
    """One named, host-side tuning entry: where to listen, where to transmit, and how."""

    name: str
    #: Receive frequency in Hz (positive int) — for a repeater, its *output*.
    frequency: int
    #: Transmit frequency in Hz when this is a repeater split, else ``None`` for simplex (TX = RX).
    #: Stored absolute, never as an offset; the offset is derived for display.
    tx_frequency: int | None = None
    #: CTCSS sub-audible tone transmitted, in Hz, or ``None``. Validated against :data:`CTCSS_TONES`.
    tx_tone: float | None = None
    #: The receive CTCSS tone the source channel list carried. **Stored, not honoured** — there is
    #: no RX tone squelch (ADR 0133), so this is round-trip fidelity only and is reported as
    #: unhonoured on every apply rather than silently ignored.
    rx_tone: float | None = None
    #: Operating mode, one of :data:`VALID_MODES`. Defaults to :data:`DEFAULT_MODE`.
    mode: str = DEFAULT_MODE

    @property
    def offset(self) -> int | None:
        """The signed TX offset in Hz (``tx - rx``), or ``None`` when simplex. Derived, never stored."""
        if self.tx_frequency is None:
            return None
        return self.tx_frequency - self.frequency


def resolve_presets(raw: Sequence[Mapping] | None) -> tuple[Preset, ...]:
    """Validate the raw ``[[presets]]`` list into `Preset` values. Fails loud.

    Shape rules (the same fail-loud units discipline the backends use): every entry needs a ``name``
    (free text, 1-:data:`MAX_NAME_LENGTH`, unique case-insensitively) and a positive-integer
    ``frequency`` (Hz); ``tone`` when present must be a standard CTCSS tone (:data:`CTCSS_TONES`);
    ``mode`` (default ``FM``) must be one of :data:`VALID_MODES`; unknown fields are typos. An
    empty/absent list means the feature is dormant — nothing changes anywhere.
    """
    if not raw:
        return ()
    presets: list[Preset] = []
    seen: dict[str, str] = {}
    for index, table in enumerate(raw):
        renamed = sorted(set(table) & set(_RENAMED_FIELDS))
        if renamed:
            rewrites = ", ".join(f"{old} -> {_RENAMED_FIELDS[old]}" for old in renamed)
            raise RuntimeError(
                f"[[presets]] entry {index + 1}: {rewrites}. `tone` was always the tone this "
                f"station TRANSMITS; it is spelled `tx_tone` now that `rx_tone` exists beside it "
                f"(ADR 0133). Rename the key — the value is unchanged."
            )
        unknown = set(table) - _KNOWN_FIELDS
        if unknown:
            raise RuntimeError(
                f"[[presets]] entry {index + 1}: unknown field(s) "
                f"{', '.join(sorted(unknown))}; known: {', '.join(sorted(_KNOWN_FIELDS))}"
            )
        name = str(table.get("name", "") or "").strip()
        if not name or len(name) > MAX_NAME_LENGTH:
            raise RuntimeError(
                f"[[presets]] entry {index + 1}: name {name!r} must be 1-{MAX_NAME_LENGTH} characters"
            )
        key = name.casefold()
        if key in seen:
            raise RuntimeError(
                f"[[presets]] name {name!r} collides with {seen[key]!r} "
                f"(names must be unique, case-insensitively)"
            )
        seen[key] = name
        if "frequency" not in table:
            raise RuntimeError(f"[[presets]] {name}: frequency is required (Hz)")
        frequency = _coerce_frequency(table["frequency"], name)
        tx_frequency = _coerce_tx_frequency(table.get("tx_frequency"), frequency, name)
        presets.append(
            Preset(
                name=name,
                frequency=frequency,
                tx_frequency=tx_frequency,
                tx_tone=_coerce_tone(table.get("tx_tone"), name, "tx_tone"),
                rx_tone=_coerce_tone(table.get("rx_tone"), name, "rx_tone"),
                mode=_coerce_mode(table.get("mode"), name),
            )
        )
    return tuple(presets)


#: The preset field → the `Capability` a backend must advertise to honour it. Frequency is the anchor
#: (the endpoint is 501-gated on it, like ``POST /frequency``); mode/tone are honoured per capability.
#: Order matters: it is the order :func:`apply_preset` writes in, so ``honoured`` and ``applied``
#: come back in the same sequence.
_FIELD_CAPABILITY: tuple[tuple[str, Capability], ...] = (
    ("frequency", Capability.SET_FREQUENCY),
    ("tx_frequency", Capability.SET_SPLIT),
    ("mode", Capability.SET_MODE),
    ("tx_tone", Capability.SET_TONE),
)

#: Fields no backend can honour, with the reason. ``rx_tone`` is stored for fidelity with imported
#: channel lists but nothing implements RX tone squelch, so it is reported as skipped on every
#: apply — silently dropping it is precisely what guardrail 3 forbids (ADR 0133).
_UNHONOURED_FIELDS: tuple[tuple[str, str], ...] = (
    ("rx_tone", "rx tone squelch is not implemented (v1); RSSI squelch gates receive"),
)


def _present_fields(preset: Preset) -> tuple[tuple[str, Capability], ...]:
    """The preset's applicable ``(field, capability)`` pairs — an unset optional field is absent."""
    return tuple(
        (field, cap)
        for field, cap in _FIELD_CAPABILITY
        if getattr(preset, field) is not None
    )


def split_preset_fields(
    preset: Preset, capabilities: frozenset[Capability]
) -> tuple[list[str], list[dict[str, str]]]:
    """Split a preset's present fields into what ``capabilities`` can honour vs. must skip. Pure.

    Returns ``(honoured, skipped)`` where ``honoured`` is the list of `Capability` strings the
    backend advertises and ``skipped`` is ``[{"field", "capability"}]`` for each present field it
    lacks — the same machine-readable capability vocabulary the 501 body uses, so the UI greys the
    matching control. Used by both ``GET /presets`` and :func:`apply_preset`.
    """
    honoured: list[str] = []
    skipped: list[dict[str, str]] = []
    for field, cap in _present_fields(preset):
        if cap in capabilities:
            honoured.append(str(cap))
        else:
            skipped.append({"field": field, "capability": str(cap)})
    for field, reason in _UNHONOURED_FIELDS:
        if getattr(preset, field) is not None:
            # No capability backs these — inventing a string no `Capability` member has would
            # pollute the vocabulary the UI parses — so the capability is empty and the reason
            # carries the explanation. The UI's notice is built from the field name alone.
            skipped.append({"field": field, "capability": "", "reason": reason})
    return honoured, skipped


def apply_preset(radio, preset: Preset) -> tuple[list[str], list[dict[str, str]]]:
    """Apply ``preset`` through ``radio``'s existing tuning surface, capability-gated per field.

    Assumes the caller has already 501-gated on ``SET_FREQUENCY`` (as ``POST /frequency`` does), so the
    frequency is always set; mode then tone are applied only when the backend advertises the matching
    capability, and any missing one is reported rather than silently dropped. Order is anchor-first:
    frequency → mode → tone. Returns ``(applied, skipped)`` — ``applied`` the `Capability` strings
    actually written, ``skipped`` the ``[{"field","capability"}]`` list. Takes ``radio`` as a
    parameter (no captured reference) so it composes with a live backend switch and is testable with
    any `Radio`.
    """
    caps = radio.capabilities()
    _honoured, skipped = split_preset_fields(preset, caps)
    applied: list[str] = []
    # Anchor. The endpoint gates on this upstream, so it is expected to be present here.
    radio.set_frequency(preset.frequency)
    applied.append(str(Capability.SET_FREQUENCY))
    # Split goes immediately after the tune and never before it: `set_frequency` clears the split
    # on every backend (ADR 0133), so the reverse order would silently disarm what it just set.
    # A simplex preset still calls it — that is how switching from a repeater back to a simplex
    # channel disarms the old TX leg instead of inheriting it.
    if Capability.SET_SPLIT in caps:
        radio.set_split(preset.tx_frequency)
        if preset.tx_frequency is not None:
            applied.append(str(Capability.SET_SPLIT))
    if Capability.SET_MODE in caps:
        radio.set_mode(preset.mode)
        applied.append(str(Capability.SET_MODE))
    # Unconditional, like the split, and for the same reason: a preset describes a COMPLETE channel,
    # so a preset without a tone means "no tone", not "leave the last one running". Guarding this on
    # `tx_tone is not None` leaked a repeater's CTCSS onto the next simplex channel — caught on the
    # bench, where applying a tone-less preset after a repeater one left `tone: 100.0` still set and
    # riding on every subsequent transmission (ADR 0133). Harmless-looking until the channel list is
    # full of repeaters with different tones.
    if Capability.SET_TONE in caps:
        radio.set_tone(preset.tx_tone)
        if preset.tx_tone is not None:
            applied.append(str(Capability.SET_TONE))
    return applied, skipped


def _coerce_frequency(raw: object, name: str) -> int:
    # bool is an int subclass — reject it explicitly so `frequency = true` isn't read as 1.
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise RuntimeError(
            f"[[presets]] {name}: frequency={raw!r} must be an integer number of Hz"
        )
    if raw <= 0:
        raise RuntimeError(f"[[presets]] {name}: frequency={raw!r} must be positive (Hz)")
    return raw


def _coerce_tx_frequency(raw: object, frequency: int, name: str) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise RuntimeError(
            f"[[presets]] {name}: tx_frequency={raw!r} must be an integer number of Hz "
            f"(the absolute transmit frequency, not an offset)"
        )
    if raw <= 0:
        raise RuntimeError(f"[[presets]] {name}: tx_frequency={raw!r} must be positive (Hz)")
    if raw == frequency:
        # Not harmless: it would advertise a split the operator does not have, and report
        # `set_split` skipped on every backend that lacks the capability.
        raise RuntimeError(
            f"[[presets]] {name}: tx_frequency equals frequency ({raw} Hz) — that is simplex, "
            f"so omit tx_frequency entirely"
        )
    return raw


def _coerce_tone(raw: object, name: str, field: str) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise RuntimeError(f"[[presets]] {name}: {field}={raw!r} must be a CTCSS frequency in Hz")
    tone = float(raw)
    if tone not in CTCSS_TONES:
        raise RuntimeError(
            f"[[presets]] {name}: {field}={raw!r} is not a standard CTCSS tone "
            f"(one of {min(CTCSS_TONES)}-{max(CTCSS_TONES)} Hz, EIA 38-tone set)"
        )
    return tone


def _coerce_mode(raw: object, name: str) -> str:
    if raw is None:
        return DEFAULT_MODE
    mode = str(raw).strip().upper()
    if mode not in VALID_MODES:
        raise RuntimeError(
            f"[[presets]] {name}: mode={raw!r} must be one of {', '.join(sorted(VALID_MODES))}"
        )
    return mode
