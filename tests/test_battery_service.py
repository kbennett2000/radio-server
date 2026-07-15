"""Battery service (6#): the pure formatter (per-pack SOC, stale flag), degradation.

Hardware/network-free — a `StubFetcher` returns canned JSON shaped like the monitor's /api/data and
`StubTts` embeds the spoken text so the exact string is asserted through the returned `AudioFrame`.
"""

from __future__ import annotations

from radio_server.auth import Session
from radio_server.services import ServiceContext, StubFetcher, StubTts
from radio_server.services.battery_service import (
    BATTERY_PATH,
    UNAVAILABLE,
    format_spoken_battery,
    battery_service,
)

BASE = "http://battery.local"
URL = BASE + BATTERY_PATH

# Shaped like the real monitor's /api/data (only the fields the service reads), insertion order kept.
DATA = {
    "200ah_01": {"label": "200Ah #1", "soc": 100, "stale": False},
    "330ah": {"label": "330Ah", "soc": 96, "stale": False},
    "ecoworthy": {"label": "ECO-WORTHY", "soc": 65.4, "stale": False},
}


def _ctx() -> ServiceContext:
    return ServiceContext(clock=lambda: 0.0, tts=StubTts())


def test_format_speaks_every_pack_in_order():
    assert format_spoken_battery(DATA) == (
        "Battery state of charge. "
        "200Ah #1: 100 percent. 330Ah: 96 percent. ECO-WORTHY: 65 percent."
    )


def test_format_marks_a_stale_pack():
    data = {"a": {"label": "Pack A", "soc": 50, "stale": True}}
    assert format_spoken_battery(data) == "Battery state of charge. Pack A: 50 percent (stale)."


def test_service_speaks_the_line():
    fetcher = StubFetcher({URL: DATA})
    audio = battery_service(BASE, fetcher)(Session(), _ctx())
    assert audio == StubTts().render(format_spoken_battery(DATA))
    assert fetcher.calls == [URL]


def test_service_speaks_unavailable_when_fetch_fails():
    audio = battery_service(BASE, StubFetcher(fail=True))(Session(), _ctx())
    assert audio == StubTts().render(UNAVAILABLE)


def test_service_speaks_unavailable_on_empty_payload():
    audio = battery_service(BASE, StubFetcher({URL: {}}))(Session(), _ctx())
    assert audio == StubTts().render(UNAVAILABLE)


def test_service_speaks_unavailable_on_unexpected_pack_shape():
    fetcher = StubFetcher({URL: {"a": {"soc": 50}}})  # missing label
    audio = battery_service(BASE, fetcher)(Session(), _ctx())
    assert audio == StubTts().render(UNAVAILABLE)


def test_base_url_trailing_slash_is_normalized():
    fetcher = StubFetcher({URL: DATA})
    battery_service(BASE + "/", fetcher)(Session(), _ctx())
    assert fetcher.calls == [URL]  # no double slash
