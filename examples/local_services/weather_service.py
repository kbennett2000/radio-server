"""Weather service: digit "2" speaks the outdoor conditions from a LAN weather station.

Mirrors `time_service`, but the data comes from HTTP endpoints instead of the clock. Two endpoints
under the same ``weather.base_url`` are read through an injected `Fetcher` (so tests use canned JSON,
no network):

  * ``/current`` (**required**) — outdoor temperature / absolute humidity / density altitude from
    ``sensors.outdoor.derived.{temperature_f, absolute_humidity_g_m3, density_altitude_ft}``.
  * ``/external`` (**best-effort**) — wind speed + direction from
    ``external.{wind_speed_mph, wind_direction_cardinal}``, appended only "if available".

A pure `format_spoken_weather` turns the two payloads into one spoken line. A dead/garbled ``/current``
degrades to a spoken "unavailable" — never a crashed controller loop; a dead ``/external`` simply drops
the wind sentence (the temp/humidity/density line is still spoken).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from radio_server.auth import Session
from radio_server.backends import AudioFrame
from radio_server.services.dispatch import Service, ServiceContext
from radio_server.services.fetch import Fetcher, FetchError

if TYPE_CHECKING:
    from radio_server.config import Settings
    from radio_server.services.plugin import PluginBuildContext

#: Digit that invokes this service, plus its ledger name and operator-facing description.
WEATHER_DIGIT = "2"
WEATHER_NAME = "weather"
WEATHER_DESCRIPTION = "Announce outdoor temperature, absolute humidity, density altitude, and wind"

#: Paths appended to the configured ``weather.base_url``. ``/current`` is required; ``/external`` is
#: best-effort (wind).
CURRENT_PATH = "/current"
EXTERNAL_PATH = "/external"

#: Spoken when the station's ``/current`` can't be reached or its JSON is unusable.
UNAVAILABLE = "Weather station unavailable"

#: 16-point compass abbreviation → spoken words, so the TTS says "south southeast", not "S-S-E".
_CARDINAL_SPOKEN = {
    "N": "north",
    "NNE": "north northeast",
    "NE": "northeast",
    "ENE": "east northeast",
    "E": "east",
    "ESE": "east southeast",
    "SE": "southeast",
    "SSE": "south southeast",
    "S": "south",
    "SSW": "south southwest",
    "SW": "southwest",
    "WSW": "west southwest",
    "W": "west",
    "WNW": "west northwest",
    "NW": "northwest",
    "NNW": "north northwest",
}

RADIO_WEATHER_URL_ENV_VAR = "RADIO_WEATHER_URL"
RADIO_WEATHER_TIMEOUT_ENV_VAR = "RADIO_WEATHER_TIMEOUT"


def load_weather_base_url(settings: Settings) -> str:
    """Return the weather-station base URL (`weather.base_url`); empty string when unset."""
    return settings.extra("weather.base_url", "")


def load_weather_timeout(settings: Settings) -> float:
    """Return the weather HTTP timeout in seconds (`weather.timeout`)."""
    return settings.extra("weather.timeout", 3.0)


def _spoken_wind(external: Mapping[str, Any] | None) -> str:
    """The wind sentence from ``/external``, or "" when unavailable.

    "If available" by design: any missing/blank field (or no ``external`` payload at all) yields an
    empty string so the caller simply omits wind — it is never an error.
    """
    if not external:
        return ""
    wind = external.get("external", external)
    mph = wind.get("wind_speed_mph")
    cardinal = wind.get("wind_direction_cardinal")
    if mph is None or not cardinal:
        return ""
    heading = _CARDINAL_SPOKEN.get(str(cardinal).upper(), str(cardinal))
    return f" Wind {round(float(mph))} miles per hour from the {heading}."


def format_spoken_weather(
    current: Mapping[str, Any], external: Mapping[str, Any] | None = None
) -> str:
    """Format the outdoor conditions as one spoken line.

    Pure and isolated (like `format_spoken_time`) so wording changes stay out of dispatch and tests
    assert the exact string. Reads the required fields from ``current``; raises `KeyError`/`TypeError`
    if they are missing — the service catches that and speaks :data:`UNAVAILABLE`. The wind sentence
    from ``external`` is best-effort and simply omitted when unavailable.
    """
    derived = current["sensors"]["outdoor"]["derived"]
    temp = round(derived["temperature_f"])
    humidity = float(derived["absolute_humidity_g_m3"])
    density_alt = round(derived["density_altitude_ft"])
    return (
        f"Outdoor temperature {temp} degrees. "
        f"Absolute humidity {humidity:.1f} grams per cubic meter. "
        f"Density altitude {density_alt} feet."
        f"{_spoken_wind(external)}"
    )


def weather_service(base_url: str, fetcher: Fetcher) -> Service:
    """Build the weather handler bound to a station base URL and a fetcher (both at construction)."""
    root = base_url.rstrip("/")
    current_url = root + CURRENT_PATH
    external_url = root + EXTERNAL_PATH

    def announce_weather(session: Session, ctx: ServiceContext) -> AudioFrame:
        try:
            current = fetcher.fetch_json(current_url)
        except (FetchError, KeyError, TypeError, ValueError):
            return ctx.tts.render(UNAVAILABLE)
        # Wind is best-effort: a dead /external must not lose the temp/humidity/density line.
        try:
            external: Mapping[str, Any] | None = fetcher.fetch_json(external_url)
        except (FetchError, KeyError, TypeError, ValueError):
            external = None
        try:
            text = format_spoken_weather(current, external)
        except (KeyError, TypeError, ValueError):
            text = UNAVAILABLE
        return ctx.tts.render(text)

    return announce_weather


class WeatherPlugin:
    """The weather service as a `ServicePlugin`; enabled when ``weather.base_url`` is configured."""

    id = WEATHER_NAME
    description = WEATHER_DESCRIPTION

    def enabled(self, settings: Settings) -> bool:
        return bool(load_weather_base_url(settings))

    def build(self, ctx: PluginBuildContext) -> Service:
        return weather_service(load_weather_base_url(ctx.settings), ctx.fetcher())


#: Module-level plugin singleton, referenced from `services.plugin.PLUGINS`.
PLUGIN = WeatherPlugin()
