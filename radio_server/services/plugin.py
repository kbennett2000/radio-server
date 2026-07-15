"""The voice-service plugin contract, the in-tree ``PLUGINS`` list, and the digit-binding loader.

This is the seam that turns the six hand-wired services into a declarative registry (ADR 0034). It
formalizes — it does not replace — the stable dispatch shape from ADR 0004: a plugin still produces a
`Service` (`(Session, ServiceContext) -> AudioFrame`), and the `Dispatcher` still owns the transmit.

A `ServicePlugin` bundles what `build_controller` used to spell out inline for each service: a stable
``id`` (referenced from config), an operator-facing ``description``, an ``enabled(settings)`` gate
(the old ``if <svc>.base_url:``), and a ``build(ctx)`` factory (the old ``<svc>_service(...)`` call).
`build_registry` walks the operator's digit→id bindings and registers every enabled plugin; a
bound-but-disabled service is a graceful miss, exactly as before.

Scope is in-tree: ``PLUGINS`` is a hand-maintained tuple, not pip/entry-point discovery — auto-running
external code that keys the licensee's transmitter is a Part-97 decision left out of scope (ADR 0034).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from collections.abc import Mapping

from .dispatch import Service, ServiceRegistry
from .fetch import Fetcher, UrllibFetcher
from .weather_service import load_weather_timeout

# Concrete plugin singletons, one per service module. Imported here so ``PLUGINS`` is the single list
# a new in-tree service is appended to. The ``_DIGIT`` constants supply each plugin's default keypad
# slot (see `DEFAULT_BINDINGS`).
from .astro_service import ASTRO_DIGIT, PLUGIN as ASTRO_PLUGIN
from .battery_service import BATTERY_DIGIT, PLUGIN as BATTERY_PLUGIN
from .bible_service import BIBLE_DIGIT, PLUGIN as BIBLE_PLUGIN
from .quote_service import QUOTE_DIGIT, PLUGIN as QUOTE_PLUGIN
from .time_service import TIME_DIGIT, PLUGIN as TIME_PLUGIN
from .weather_service import WEATHER_DIGIT, PLUGIN as WEATHER_PLUGIN

if TYPE_CHECKING:
    from ..config import Settings

#: The DTMF alphabet a bound digit may be drawn from (multi-character entries like "12" are allowed).
_DTMF_ALPHABET = frozenset("0123456789ABCD*#")

#: Digits handled by the controller itself (not `ServiceRegistry` services) — they need
#: `StationId`/`Session` authority `ServiceContext` withholds (ADR 0004, guardrails 2 & 4). A plugin
#: may not be bound to one; `resolve_bindings` rejects it. Value is the built-in's spoken name, for
#: the error message. Kept in sync with `controller.engine.PLAY_ID_DIGIT` / `LOGOUT_DIGITS`.
RESERVED_DIGITS: dict[str, str] = {"4": "station-id", "99": "logout"}


class PluginBuildContext:
    """The construction-time capabilities a plugin's `build` may draw on.

    Carries the resolved `Settings` and a single shared `Fetcher` for the LAN fetch services. The
    fetcher is built lazily and memoized: only a fetch-backed plugin calls `fetcher()`, and the first
    call constructs the one `UrllibFetcher` (bound to the shared ``weather.timeout``) that the rest
    reuse — reproducing ADR 0033's "one fetcher on the first enabled fetch service" for free. Tests
    inject a `StubFetcher`.
    """

    __slots__ = ("_settings", "_fetcher")

    def __init__(self, settings: Settings, fetcher: Fetcher | None = None) -> None:
        self._settings = settings
        self._fetcher = fetcher

    @property
    def settings(self) -> Settings:
        return self._settings

    def fetcher(self) -> Fetcher:
        """The shared LAN fetcher, built on first use (bound to ``weather.timeout``)."""
        if self._fetcher is None:
            self._fetcher = UrllibFetcher(load_weather_timeout(self._settings))
        return self._fetcher


@runtime_checkable
class ServicePlugin(Protocol):
    """A pluggable DTMF voice service. Structural — a service module conforms without importing this.

    ``id`` is the stable name an operator references from ``[services]`` in ``radio.toml`` and the
    name recorded in the ledger; ``description`` is the operator-facing line for ``/services`` and the
    example file. ``enabled`` is the old per-service config gate (e.g. ``weather.base_url`` is set);
    ``build`` returns the `Service` the dispatcher will call and transmit.
    """

    id: str
    description: str

    def enabled(self, settings: Settings) -> bool: ...

    def build(self, ctx: PluginBuildContext) -> Service: ...


#: The in-tree voice services, in default-digit order. Append a plugin here to add a service; the
#: `DEFAULT_BINDINGS` below assigns its out-of-the-box digit.
PLUGINS: tuple[ServicePlugin, ...] = (
    TIME_PLUGIN,
    WEATHER_PLUGIN,
    ASTRO_PLUGIN,
    QUOTE_PLUGIN,
    BATTERY_PLUGIN,
    BIBLE_PLUGIN,
)

#: The keypad layout used when ``radio.toml`` has no ``[services]`` table — the historical digits, so
#: an existing deployment is unchanged. Built from each module's ``_DIGIT`` and plugin ``id`` (never a
#: bare string), so the default digit stays defined in one place.
DEFAULT_BINDINGS: dict[str, str] = {
    TIME_DIGIT: TIME_PLUGIN.id,
    WEATHER_DIGIT: WEATHER_PLUGIN.id,
    ASTRO_DIGIT: ASTRO_PLUGIN.id,
    QUOTE_DIGIT: QUOTE_PLUGIN.id,
    BATTERY_DIGIT: BATTERY_PLUGIN.id,
    BIBLE_DIGIT: BIBLE_PLUGIN.id,
}


def _is_dtmf_digit(digit: str) -> bool:
    return len(digit) >= 1 and all(ch in _DTMF_ALPHABET for ch in digit)


def resolve_bindings(
    raw: Mapping[str, str] | None, plugin_ids: set[str]
) -> dict[str, str]:
    """Validate a digit→plugin-id map, returning `DEFAULT_BINDINGS` when ``raw`` is absent.

    Fails loud (``RuntimeError``) on a reserved digit (4/99), a non-DTMF digit, or an unknown plugin
    id — a keypad typo is a startup error, not a silent dead digit. Two digits may map to the same
    service (a service can answer more than one digit).
    """
    if raw is None:
        return dict(DEFAULT_BINDINGS)
    bindings: dict[str, str] = {}
    for raw_digit, plugin_id in raw.items():
        digit = str(raw_digit)
        if digit in RESERVED_DIGITS:
            raise RuntimeError(
                f"[services] digit {digit!r} is reserved for the built-in "
                f"{RESERVED_DIGITS[digit]!r} command and cannot be bound to a service"
            )
        if not _is_dtmf_digit(digit):
            raise RuntimeError(
                f"[services] digit {digit!r} is not a valid DTMF entry "
                f"(use characters from 0-9, A-D, * or #)"
            )
        if plugin_id not in plugin_ids:
            raise RuntimeError(
                f"[services] {digit!r} = {plugin_id!r}: unknown service; "
                f"known services are {sorted(plugin_ids)}"
            )
        bindings[digit] = plugin_id
    return bindings


def build_registry(
    plugins: tuple[ServicePlugin, ...],
    bindings: Mapping[str, str],
    ctx: PluginBuildContext,
) -> ServiceRegistry:
    """Register every enabled, bound plugin into a fresh `ServiceRegistry`.

    ``bindings`` must already be validated (see `resolve_bindings`), so every id is known. A plugin
    whose `enabled` gate is False (its data source unconfigured) is skipped — its digit is a graceful
    miss, matching the pre-plugin behavior.
    """
    by_id = {plugin.id: plugin for plugin in plugins}
    registry = ServiceRegistry()
    for digit, plugin_id in bindings.items():
        plugin = by_id[plugin_id]
        if plugin.enabled(ctx.settings):
            registry.register(digit, plugin.id, plugin.build(ctx), plugin.description)
    return registry
