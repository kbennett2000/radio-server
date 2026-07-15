"""Weather service: digit "2" speaks the outdoor conditions from a LAN weather station.

Mirrors `time_service`, but the data comes from an HTTP endpoint instead of the clock. The station's
``/current`` JSON is read through an injected `Fetcher` (so tests use canned JSON, no network), the
outdoor temp / feels-like / absolute humidity are pulled from
``sensors.outdoor.derived.{temperature_f, feels_like_f, absolute_humidity_g_m3}``, and a pure
`format_spoken_weather` turns them into one spoken line. A dead/garbled station degrades to a spoken
"unavailable" — never a crashed controller loop.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ..auth import Session
from ..backends import AudioFrame
from .dispatch import Service, ServiceContext, ServiceRegistry
from .fetch import Fetcher, FetchError

if TYPE_CHECKING:
    from ..config import Settings

#: Digit that invokes this service, plus its ledger name and operator-facing description.
WEATHER_DIGIT = "2"
WEATHER_NAME = "weather"
WEATHER_DESCRIPTION = "Announce outdoor temperature, feels-like, and absolute humidity"

#: Path appended to the configured ``weather.base_url`` for current conditions.
CURRENT_PATH = "/current"

#: Spoken when the station can't be reached or its JSON is unusable.
UNAVAILABLE = "Weather station unavailable"

RADIO_WEATHER_URL_ENV_VAR = "RADIO_WEATHER_URL"
RADIO_WEATHER_TIMEOUT_ENV_VAR = "RADIO_WEATHER_TIMEOUT"


def load_weather_base_url(settings: Settings) -> str:
    """Return the weather-station base URL (`weather.base_url`); empty string when unset."""
    return settings.get("weather.base_url")


def load_weather_timeout(settings: Settings) -> float:
    """Return the weather HTTP timeout in seconds (`weather.timeout`)."""
    return settings.get("weather.timeout")


def format_spoken_weather(data: Mapping[str, Any]) -> str:
    """Format the outdoor conditions from the station's ``/current`` JSON as one spoken line.

    Pure and isolated (like `format_spoken_time`) so wording changes stay out of dispatch and tests
    assert the exact string. Raises `KeyError`/`TypeError` if the expected fields are missing — the
    service catches that and speaks :data:`UNAVAILABLE`.
    """
    derived = data["sensors"]["outdoor"]["derived"]
    temp = round(derived["temperature_f"])
    feels = round(derived["feels_like_f"])
    humidity = float(derived["absolute_humidity_g_m3"])
    return (
        f"Outdoor temperature {temp} degrees. Feels like {feels}. "
        f"Absolute humidity {humidity:.1f} grams per cubic meter."
    )


def weather_service(base_url: str, fetcher: Fetcher) -> Service:
    """Build the weather handler bound to a station base URL and a fetcher (both at construction)."""
    url = base_url.rstrip("/") + CURRENT_PATH

    def announce_weather(session: Session, ctx: ServiceContext) -> AudioFrame:
        try:
            text = format_spoken_weather(fetcher.fetch_json(url))
        except (FetchError, KeyError, TypeError, ValueError):
            text = UNAVAILABLE
        return ctx.tts.render(text)

    return announce_weather


def register(registry: ServiceRegistry, base_url: str, fetcher: Fetcher) -> None:
    """Register the weather service under its digit into `registry`."""
    registry.register(
        WEATHER_DIGIT, WEATHER_NAME, weather_service(base_url, fetcher), WEATHER_DESCRIPTION
    )
