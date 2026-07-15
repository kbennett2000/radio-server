"""Astronomy service: digit "3" speaks sun/moon times from the LAN weather station.

The sibling of `weather_service`, reading the station's ``/astronomy`` endpoint through the same
injected `Fetcher`. It pulls ``astronomy.sun.{sunrise,sunset}`` and
``astronomy.moon.{phase_name,moonrise,moonset}`` and speaks them. The station returns ISO-8601
timestamps already in its own local timezone (a UTC offset is included), so `format_spoken_astro`
formats the wall-clock time directly — no timezone conversion. ``moonrise``/``moonset`` may be ``null``
(the moon is always up or always down that day), which is spoken as "not available". A dead/garbled
station degrades to a spoken "unavailable".
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..auth import Session
from ..backends import AudioFrame
from .dispatch import Service, ServiceContext
from .fetch import Fetcher, FetchError
from .weather_service import load_weather_base_url

if TYPE_CHECKING:
    from ..config import Settings
    from .plugin import PluginBuildContext

#: Digit that invokes this service, plus its ledger name and operator-facing description.
ASTRO_DIGIT = "3"
ASTRO_NAME = "astronomy"
ASTRO_DESCRIPTION = "Announce sunrise, sunset, moon phase, moonrise, and moonset"

#: Path appended to the configured ``weather.base_url`` for the astronomy data.
ASTRO_PATH = "/astronomy"

#: Spoken when the station can't be reached or its JSON is unusable.
UNAVAILABLE = "Astronomy data unavailable"


def _spoken_time(iso: object) -> str:
    """Format one ISO-8601 timestamp as a spoken 12-hour local time; "not available" for null/bad."""
    if not isinstance(iso, str):
        return "not available"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return "not available"
    hour = dt.hour % 12 or 12  # 0/12 -> 12; the station's local wall-clock hour (offset-aware)
    meridiem = "AM" if dt.hour < 12 else "PM"
    return f"{hour}:{dt.minute:02d} {meridiem}"


def format_spoken_astro(data: Mapping[str, Any]) -> str:
    """Format sun/moon events from the station's ``/astronomy`` JSON as one spoken line.

    Pure and isolated so wording stays out of dispatch and tests assert the exact string. Raises
    `KeyError`/`TypeError` if the sun/moon blocks are missing — the service catches that and speaks
    :data:`UNAVAILABLE`.
    """
    astronomy = data["astronomy"]
    sun = astronomy["sun"]
    moon = astronomy["moon"]
    return (
        f"Sunrise {_spoken_time(sun['sunrise'])}, sunset {_spoken_time(sun['sunset'])}. "
        f"Moon phase {moon['phase_name']}. "
        f"Moonrise {_spoken_time(moon['moonrise'])}, moonset {_spoken_time(moon['moonset'])}."
    )


def astro_service(base_url: str, fetcher: Fetcher) -> Service:
    """Build the astronomy handler bound to a station base URL and a fetcher (both at construction)."""
    url = base_url.rstrip("/") + ASTRO_PATH

    def announce_astro(session: Session, ctx: ServiceContext) -> AudioFrame:
        try:
            text = format_spoken_astro(fetcher.fetch_json(url))
        except (FetchError, KeyError, TypeError, ValueError):
            text = UNAVAILABLE
        return ctx.tts.render(text)

    return announce_astro


class AstroPlugin:
    """The astronomy service as a `ServicePlugin`. Shares ``weather.base_url`` with the weather
    service (ADR 0033), so it is enabled by the same setting and reads that station's ``/astronomy``.
    """

    id = ASTRO_NAME
    description = ASTRO_DESCRIPTION

    def enabled(self, settings: Settings) -> bool:
        return bool(load_weather_base_url(settings))

    def build(self, ctx: PluginBuildContext) -> Service:
        return astro_service(load_weather_base_url(ctx.settings), ctx.fetcher())


#: Module-level plugin singleton, referenced from `services.plugin.PLUGINS`.
PLUGIN = AstroPlugin()
