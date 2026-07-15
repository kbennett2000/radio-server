"""Battery service: digit "6" announces the state of charge of each LAN battery pack.

A sibling of `weather_service`, reading a single ``/api/data`` endpoint through the injected `Fetcher`.
That endpoint returns an object keyed by pack id, each value ``{label, soc, stale, ...}``. Every pack
is spoken in payload order as ``"{label}: {soc} percent"``, with ``" (stale)"`` appended when the
monitor flags a pack's data stale (a dropped BLE link is announced, not silently dropped). A
dead/garbled endpoint — or an empty object — degrades to a spoken "unavailable".
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ..auth import Session
from ..backends import AudioFrame
from .dispatch import Service, ServiceContext
from .fetch import Fetcher, FetchError

if TYPE_CHECKING:
    from ..config import Settings
    from .plugin import PluginBuildContext

#: Digit that invokes this service, plus its ledger name and operator-facing description.
BATTERY_DIGIT = "6"
BATTERY_NAME = "battery"
BATTERY_DESCRIPTION = "Announce battery state of charge"

#: Path appended to the configured ``battery.base_url``.
BATTERY_PATH = "/api/data"

#: Spoken when the monitor can't be reached, its JSON is unusable, or it reports no packs.
UNAVAILABLE = "Battery monitor unavailable"


def load_battery_base_url(settings: Settings) -> str:
    """Return the battery monitor base URL (`battery.base_url`); empty string when unset."""
    return settings.get("battery.base_url")


def format_spoken_battery(payload: Mapping[str, Any]) -> str:
    """Format the per-pack state of charge as one spoken line.

    Pure and isolated so wording stays out of dispatch and tests assert the exact string. Raises
    `KeyError`/`TypeError` when a pack lacks ``label``/``soc``, and raises `ValueError` on an empty
    object — the service catches all three and speaks :data:`UNAVAILABLE`.
    """
    packs = list(payload.values())
    if not packs:
        raise ValueError("no battery packs reported")
    parts = []
    for pack in packs:
        label = pack["label"]
        soc = round(float(pack["soc"]))
        stale = " (stale)" if pack.get("stale") else ""
        parts.append(f"{label}: {soc} percent{stale}")
    return "Battery state of charge. " + ". ".join(parts) + "."


def battery_service(base_url: str, fetcher: Fetcher) -> Service:
    """Build the battery handler bound to a base URL and a fetcher (both at construction)."""
    url = base_url.rstrip("/") + BATTERY_PATH

    def announce_battery(session: Session, ctx: ServiceContext) -> AudioFrame:
        try:
            text = format_spoken_battery(fetcher.fetch_json(url))
        except (FetchError, KeyError, TypeError, ValueError):
            text = UNAVAILABLE
        return ctx.tts.render(text)

    return announce_battery


class BatteryPlugin:
    """The battery service as a `ServicePlugin`; enabled when ``battery.base_url`` is configured."""

    id = BATTERY_NAME
    description = BATTERY_DESCRIPTION

    def enabled(self, settings: Settings) -> bool:
        return bool(load_battery_base_url(settings))

    def build(self, ctx: PluginBuildContext) -> Service:
        return battery_service(load_battery_base_url(ctx.settings), ctx.fetcher())


#: Module-level plugin singleton, referenced from `services.plugin.PLUGINS`.
PLUGIN = BatteryPlugin()
