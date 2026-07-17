"""The voice-service plugin contract, the in-tree ``PLUGINS`` list, and the digit-binding loader.

This is the seam that turns hand-wired services into a declarative registry (ADR 0034). It
formalizes — it does not replace — the stable dispatch shape from ADR 0004: a plugin still produces a
`Service` (`(Session, ServiceContext) -> AudioFrame`), and the `Dispatcher` still owns the transmit.

A `ServicePlugin` bundles what `build_controller` used to spell out inline for each service: a stable
``id`` (referenced from config), an operator-facing ``description``, an ``enabled(settings)`` gate,
and a ``build(ctx)`` factory. `build_registry` walks the operator's digit→id bindings and registers
every enabled plugin; a bound-but-disabled service is a graceful miss, exactly as before.

``PLUGINS`` is the hand-maintained in-tree tuple — not pip/entry-point discovery. Operator-authored
plugins live in the ``local_services/`` folder instead (`local.discover_local_plugins`, ADR 0051):
creating that folder is the explicit opt-in ADR 0034 reserved room for.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from collections.abc import Mapping

from .dispatch import Service, ServiceRegistry
from .fetch import DEFAULT_FETCH_TIMEOUT, Fetcher, UrllibFetcher

# Concrete plugin singletons, one per service module. Imported here so ``PLUGINS`` is the single list
# a new in-tree service is appended to. The ``_DIGIT`` constants supply each plugin's default keypad
# slot (see `DEFAULT_BINDINGS`).
from .time_service import TIME_DIGIT, PLUGIN as TIME_PLUGIN

if TYPE_CHECKING:
    from ..config import Settings

#: The DTMF alphabet a bound digit may be drawn from (multi-character entries like "12" are allowed).
_DTMF_ALPHABET = frozenset("0123456789ABCD*#")

#: Stable ids of the two controller built-ins, referenced from `[services]` and matched in the engine.
ID_BUILTIN = "station-id"
LOGOUT_BUILTIN = "logout"

#: Default keypad slots for the built-ins — historically hard-wired in the engine, now overridable via
#: the same `[services]` table as any service (see `DEFAULT_BINDINGS`). The digit is not special; only
#: the behavior behind it is.
ID_DIGIT = "01"
LOGOUT_DIGIT = "99"

#: The controller's built-in commands: stable id → operator-facing description. They are NOT
#: `ServiceRegistry` services — they need `StationId`/`Session` authority `ServiceContext` withholds
#: (ADR 0004, guardrails 2 & 4), so the engine runs them, not the dispatcher. But they share the
#: operator's `[services]` keypad map, so their digit is assignable exactly like a service's:
#: `resolve_bindings` accepts their ids, `build_registry` skips them, and the engine resolves their
#: digits via `builtin_digits`.
BUILTIN_IDS: dict[str, str] = {
    ID_BUILTIN: "Play the station ID",
    LOGOUT_BUILTIN: "End the session (voice confirmation)",
}


class PluginBuildContext:
    """The construction-time capabilities a plugin's `build` may draw on.

    Carries the resolved `Settings` and a single shared `Fetcher` for LAN-fetch plugins. The fetcher
    is built lazily and memoized: only a fetch-backed plugin calls `fetcher()`, and the first call
    constructs the one `UrllibFetcher` (bound to `DEFAULT_FETCH_TIMEOUT`) that the rest reuse —
    reproducing ADR 0033's "one fetcher on the first enabled fetch service" for free. A plugin that
    needs a different timeout builds its own `UrllibFetcher` from its own ``[plugins.*]`` config
    (ADR 0051). Tests inject a `StubFetcher`.
    """

    __slots__ = ("_settings", "_fetcher")

    def __init__(self, settings: Settings, fetcher: Fetcher | None = None) -> None:
        self._settings = settings
        self._fetcher = fetcher

    @property
    def settings(self) -> Settings:
        return self._settings

    def fetcher(self) -> Fetcher:
        """The shared LAN fetcher, built on first use (bound to `DEFAULT_FETCH_TIMEOUT`)."""
        if self._fetcher is None:
            self._fetcher = UrllibFetcher(DEFAULT_FETCH_TIMEOUT)
        return self._fetcher


@runtime_checkable
class ServicePlugin(Protocol):
    """A pluggable DTMF voice service. Structural — a service module conforms without importing this.

    ``id`` is the stable name an operator references from ``[services]`` in ``radio.toml`` and the
    name recorded in the ledger; ``description`` is the operator-facing line for ``/services`` and the
    example file. ``enabled`` is the per-service config gate (e.g. a required base URL is set);
    ``build`` returns the `Service` the dispatcher will call and transmit.
    """

    id: str
    description: str

    def enabled(self, settings: Settings) -> bool: ...

    def build(self, ctx: PluginBuildContext) -> Service: ...


#: The in-tree voice services, in default-digit order. Append a plugin here to add a service; the
#: `DEFAULT_BINDINGS` below assigns its out-of-the-box digit. Operator-authored plugins are
#: discovered from ``local_services/`` instead (`local.discover_local_plugins`, ADR 0051).
PLUGINS: tuple[ServicePlugin, ...] = (TIME_PLUGIN,)

#: The keypad layout used when ``radio.toml`` has no ``[services]`` table. Two-digit codes matching
#: the shipped link combos (``10#`` connect / ``98#`` link-off, ADR 0052) in width, so the whole
#: out-of-the-box keypad reads as one scheme: ``01#`` ID, ``02#`` time, ``99#`` logout. Includes the
#: two controller built-ins: they are ordinary entries in this one map, so an operator can move them
#: like any service. Built from each module's ``_DIGIT`` and plugin ``id`` (never a bare string), so
#: the default digit stays defined in one place.
DEFAULT_BINDINGS: dict[str, str] = {
    ID_DIGIT: ID_BUILTIN,
    TIME_DIGIT: TIME_PLUGIN.id,
    LOGOUT_DIGIT: LOGOUT_BUILTIN,
}


def _is_dtmf_digit(digit: str) -> bool:
    return len(digit) >= 1 and all(ch in _DTMF_ALPHABET for ch in digit)


def resolve_bindings(
    raw: Mapping[str, str] | None, plugin_ids: set[str]
) -> dict[str, str]:
    """Validate a digit→id map, returning `DEFAULT_BINDINGS` when ``raw`` is absent.

    A target id is either a service plugin id or a controller built-in (``station-id`` / ``logout``);
    both share this one keypad map, so a built-in's digit is operator-assignable just like a service's.
    Fails loud (``RuntimeError``) on a non-DTMF digit or an unknown id — a keypad typo is a startup
    error, not a silent dead digit. Two digits may map to the same target (it can answer more than one
    digit). A `[services]` table is the *complete* layout: a built-in the operator omits is simply not
    on the keypad (its automatic safety net still holds — auto-ID fires on interval and at session end,
    and the idle timeout still closes sessions).
    """
    if raw is None:
        return dict(DEFAULT_BINDINGS)
    known = set(plugin_ids) | set(BUILTIN_IDS)
    bindings: dict[str, str] = {}
    for raw_digit, target_id in raw.items():
        digit = str(raw_digit)
        if not _is_dtmf_digit(digit):
            raise RuntimeError(
                f"[services] digit {digit!r} is not a valid DTMF entry "
                f"(use characters from 0-9, A-D, * or #)"
            )
        if target_id not in known:
            raise RuntimeError(
                f"[services] {digit!r} = {target_id!r}: unknown service or command; "
                f"known ids are {sorted(known)}"
            )
        bindings[digit] = target_id
    return bindings


def build_registry(
    plugins: tuple[ServicePlugin, ...],
    bindings: Mapping[str, str],
    ctx: PluginBuildContext,
) -> ServiceRegistry:
    """Register every enabled, bound *service* plugin into a fresh `ServiceRegistry`.

    ``bindings`` must already be validated (see `resolve_bindings`). A built-in id
    (``station-id`` / ``logout``) is skipped — the engine runs those directly, not the dispatcher.
    A plugin whose `enabled` gate is False (its data source unconfigured) is likewise skipped — its
    digit is a graceful miss, matching the pre-plugin behavior.
    """
    by_id = {plugin.id: plugin for plugin in plugins}
    registry = ServiceRegistry()
    for digit, target_id in bindings.items():
        plugin = by_id.get(target_id)
        if plugin is None:
            continue  # a controller built-in — resolved by the engine via `builtin_digits`
        if plugin.enabled(ctx.settings):
            registry.register(digit, plugin.id, plugin.build(ctx), plugin.description)
    return registry


def builtin_digits(bindings: Mapping[str, str], builtin_id: str) -> frozenset[str]:
    """The digits the operator bound to a controller built-in (``station-id`` / ``logout``).

    A built-in may sit on any digit, on more than one, or on none at all (an omitted built-in just
    isn't on the keypad). The engine matches an incoming digit against these sets to run the command.
    """
    return frozenset(digit for digit, target_id in bindings.items() if target_id == builtin_id)
