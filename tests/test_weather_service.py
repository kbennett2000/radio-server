"""Weather service (2#): the pure formatter, the fetch→speak path, and graceful degradation.

Hardware/network-free — a `StubFetcher` returns canned JSON and `StubTts` embeds the spoken text so
the exact string is asserted through the returned `AudioFrame` (the `tests/test_dispatch.py` pattern).
"""

from __future__ import annotations

from radio_server.auth import Session
from radio_server.services import ServiceContext, StubFetcher, StubTts
from radio_server.services.weather_service import (
    CURRENT_PATH,
    UNAVAILABLE,
    format_spoken_weather,
    weather_service,
)

BASE = "http://weather.local/api/v1"

# Shaped like the real station's /current (only the fields the service reads).
CANNED = {
    "sensors": {
        "outdoor": {
            "derived": {
                "temperature_f": 78.8,
                "feels_like_f": 80.2,
                "absolute_humidity_g_m3": 7.932286,
            }
        }
    }
}


def _ctx() -> ServiceContext:
    return ServiceContext(clock=lambda: 0.0, tts=StubTts())


def test_format_spoken_weather_rounds_and_phrases():
    assert format_spoken_weather(CANNED) == (
        "Outdoor temperature 79 degrees. Feels like 80. "
        "Absolute humidity 7.9 grams per cubic meter."
    )


def test_service_fetches_current_and_speaks_it():
    fetcher = StubFetcher({BASE + CURRENT_PATH: CANNED})
    audio = weather_service(BASE, fetcher)(Session(), _ctx())
    assert audio == StubTts().render(format_spoken_weather(CANNED))
    assert fetcher.calls == [BASE + CURRENT_PATH]  # hit /current exactly once


def test_service_speaks_unavailable_when_fetch_fails():
    audio = weather_service(BASE, StubFetcher(fail=True))(Session(), _ctx())
    assert audio == StubTts().render(UNAVAILABLE)


def test_service_speaks_unavailable_on_unexpected_shape():
    # Reachable station, but the JSON lacks the outdoor/derived fields — a KeyError the service catches.
    audio = weather_service(BASE, StubFetcher(default={"sensors": {}}))(Session(), _ctx())
    assert audio == StubTts().render(UNAVAILABLE)


def test_base_url_trailing_slash_is_normalized():
    fetcher = StubFetcher({BASE + CURRENT_PATH: CANNED})
    weather_service(BASE + "/", fetcher)(Session(), _ctx())
    assert fetcher.calls == [BASE + CURRENT_PATH]  # no double slash
