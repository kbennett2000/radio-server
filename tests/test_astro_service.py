"""Astronomy service (3#): the pure formatter (incl. null moonrise/set) and graceful degradation."""

from __future__ import annotations

from radio_server.auth import Session
from radio_server.services import ServiceContext, StubFetcher, StubTts
from radio_server.services.astro_service import (
    ASTRO_PATH,
    UNAVAILABLE,
    astro_service,
    format_spoken_astro,
)

BASE = "http://weather.local/api/v1"

# Shaped like the real station's /astronomy (ISO timestamps carry the station's local UTC offset).
CANNED = {
    "astronomy": {
        "sun": {
            "sunrise": "2026-07-14T05:43:50.866814-06:00",
            "sunset": "2026-07-14T20:26:11.579057-06:00",
        },
        "moon": {
            "phase_name": "New Moon",
            "moonrise": "2026-07-15T07:03:24.655266-06:00",
            "moonset": "2026-07-14T21:10:54.434667-06:00",
        },
    }
}


def _ctx() -> ServiceContext:
    return ServiceContext(clock=lambda: 0.0, tts=StubTts())


def test_format_spoken_astro_uses_local_12_hour():
    # Times are formatted in the timestamp's own offset (station local), 12-hour with AM/PM.
    assert format_spoken_astro(CANNED) == (
        "Sunrise 5:43 AM, sunset 8:26 PM. Moon phase New Moon. "
        "Moonrise 7:03 AM, moonset 9:10 PM."
    )


def test_format_spoken_astro_handles_null_moon_times():
    data = {
        "astronomy": {
            "sun": {"sunrise": "2026-07-14T05:43:00-06:00", "sunset": "2026-07-14T20:26:00-06:00"},
            "moon": {"phase_name": "Full Moon", "moonrise": None, "moonset": None},
        }
    }
    assert format_spoken_astro(data) == (
        "Sunrise 5:43 AM, sunset 8:26 PM. Moon phase Full Moon. "
        "Moonrise not available, moonset not available."
    )


def test_service_fetches_astronomy_and_speaks_it():
    fetcher = StubFetcher({BASE + ASTRO_PATH: CANNED})
    audio = astro_service(BASE, fetcher)(Session(), _ctx())
    assert audio == StubTts().render(format_spoken_astro(CANNED))
    assert fetcher.calls == [BASE + ASTRO_PATH]


def test_service_speaks_unavailable_when_fetch_fails():
    audio = astro_service(BASE, StubFetcher(fail=True))(Session(), _ctx())
    assert audio == StubTts().render(UNAVAILABLE)
