"""Weather service (2#): the pure formatter, the two-endpoint fetch→speak path, degradation.

Hardware/network-free — a `StubFetcher` (keyed per URL) returns canned JSON and `StubTts` embeds the
spoken text so the exact string is asserted through the returned `AudioFrame` (the
`tests/test_dispatch.py` pattern). `2#` reads ``/current`` (required) plus a best-effort ``/external``
for wind.
"""

from __future__ import annotations

from radio_server.auth import Session
from radio_server.services import ServiceContext, StubFetcher, StubTts
from radio_server.services.weather_service import (
    CURRENT_PATH,
    EXTERNAL_PATH,
    UNAVAILABLE,
    format_spoken_weather,
    weather_service,
)

BASE = "http://weather.local/api/v1"

# Shaped like the real station's /current (only the fields the service reads; feels_like is present
# but deliberately ignored now).
CURRENT = {
    "sensors": {
        "outdoor": {
            "derived": {
                "temperature_f": 78.8,
                "feels_like_f": 80.2,
                "absolute_humidity_g_m3": 7.932286,
                "density_altitude_ft": 8715.2,
            }
        }
    }
}

# Shaped like the real station's /external (wind lives under the "external" key).
EXTERNAL = {
    "external": {
        "wind_speed_mph": 16.1,
        "wind_direction_cardinal": "SSE",
        "wind_direction_deg": 151.0,
    }
}

CURRENT_URL = BASE + CURRENT_PATH
EXTERNAL_URL = BASE + EXTERNAL_PATH


def _ctx() -> ServiceContext:
    return ServiceContext(clock=lambda: 0.0, tts=StubTts())


def test_format_without_wind_drops_feels_like_and_adds_density_altitude():
    text = format_spoken_weather(CURRENT)
    assert text == (
        "Outdoor temperature 79 degrees. "
        "Absolute humidity 7.9 grams per cubic meter. "
        "Density altitude 8715 feet."
    )
    assert "feels like" not in text.lower()


def test_format_with_wind_appends_spoken_cardinal():
    text = format_spoken_weather(CURRENT, EXTERNAL)
    assert text == (
        "Outdoor temperature 79 degrees. "
        "Absolute humidity 7.9 grams per cubic meter. "
        "Density altitude 8715 feet. "
        "Wind 16 miles per hour from the south southeast."
    )


def test_cardinal_expansion_covers_the_16_points():
    for card, spoken in [("N", "north"), ("E", "east"), ("SW", "southwest"), ("WNW", "west northwest")]:
        ext = {"external": {"wind_speed_mph": 5, "wind_direction_cardinal": card}}
        assert format_spoken_weather(CURRENT, ext).endswith(f"from the {spoken}.")


def test_partial_wind_is_omitted_not_errored():
    # Missing speed → no wind sentence (best-effort), still a valid announcement.
    ext = {"external": {"wind_direction_cardinal": "SSE"}}
    assert format_spoken_weather(CURRENT, ext).endswith("Density altitude 8715 feet.")


def test_service_fetches_both_endpoints_and_speaks_wind():
    fetcher = StubFetcher({CURRENT_URL: CURRENT, EXTERNAL_URL: EXTERNAL})
    audio = weather_service(BASE, fetcher)(Session(), _ctx())
    assert audio == StubTts().render(format_spoken_weather(CURRENT, EXTERNAL))
    assert fetcher.calls == [CURRENT_URL, EXTERNAL_URL]


def test_dead_external_still_speaks_current_line_without_wind():
    # /current succeeds, /external is not stubbed → StubFetcher raises FetchError for it. The
    # temp/humidity/density line is still spoken (best-effort wind), NOT "unavailable".
    fetcher = StubFetcher({CURRENT_URL: CURRENT})
    audio = weather_service(BASE, fetcher)(Session(), _ctx())
    assert audio == StubTts().render(format_spoken_weather(CURRENT))  # no wind
    assert audio != StubTts().render(UNAVAILABLE)
    assert fetcher.calls == [CURRENT_URL, EXTERNAL_URL]  # it did try /external


def test_service_speaks_unavailable_when_current_fetch_fails():
    audio = weather_service(BASE, StubFetcher(fail=True))(Session(), _ctx())
    assert audio == StubTts().render(UNAVAILABLE)


def test_service_speaks_unavailable_on_unexpected_current_shape():
    # Reachable station, but /current lacks the outdoor/derived fields — a KeyError the service catches.
    fetcher = StubFetcher({CURRENT_URL: {"sensors": {}}})
    audio = weather_service(BASE, fetcher)(Session(), _ctx())
    assert audio == StubTts().render(UNAVAILABLE)


def test_base_url_trailing_slash_is_normalized():
    fetcher = StubFetcher({CURRENT_URL: CURRENT, EXTERNAL_URL: EXTERNAL})
    weather_service(BASE + "/", fetcher)(Session(), _ctx())
    assert fetcher.calls == [CURRENT_URL, EXTERNAL_URL]  # no double slash
