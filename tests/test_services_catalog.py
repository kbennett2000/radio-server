"""Conditional weather/astro registration, the service catalog, and the GET /services endpoint.

Weather (2#) and astronomy (3#) are wired only when `weather.base_url` is configured — so the catalog
(and the UI panel it drives) reflects exactly what the operator can actually invoke.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from radio_server.api import create_app
from radio_server.backends import MockRadio
from radio_server.controller import build_controller
from radio_server.services import StubFetcher, StubTts

from .conftest import TEST_SECRET, make_settings

CALLSIGN = "AE9S"
TOKEN = "test-lan-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _controller(clock, radio, *, weather_url="", quote_url="", battery_url="", bible_url=""):
    overrides = {"station.callsign": CALLSIGN}
    for key, url in (
        ("weather.base_url", weather_url),
        ("quote.base_url", quote_url),
        ("battery.base_url", battery_url),
        ("bible.base_url", bible_url),
    ):
        if url:
            overrides[key] = url
    return build_controller(
        make_settings(overrides),
        radio=radio,
        totp_secret=TEST_SECRET,
        tts=StubTts(),
        fetcher=StubFetcher(default={}),
        clock=clock,
    )


# The built-in controller commands always appear in the catalog (they aren't registry services).
BUILTINS = ["4", "99"]


def test_only_time_and_builtins_registered_without_weather_url(clock):
    ctrl = _controller(clock, MockRadio())
    assert [s["digit"] for s in ctrl.service_catalog] == ["1", *BUILTINS]


def test_weather_and_astro_registered_when_url_set(clock):
    cat = _controller(clock, MockRadio(), weather_url="http://w/api").service_catalog
    assert [s["digit"] for s in cat] == ["1", "2", "3", *BUILTINS]
    by_digit = {s["digit"]: s["name"] for s in cat}
    assert by_digit["1"] == "time" and by_digit["2"] == "weather" and by_digit["3"] == "astronomy"
    assert by_digit["4"] == "station-id" and by_digit["99"] == "logout"
    assert all(s["description"] for s in cat)  # every entry carries an operator-facing description


def test_fetch_services_registered_when_their_urls_set(clock):
    cat = _controller(
        clock,
        MockRadio(),
        weather_url="http://w/api",
        quote_url="http://q",
        battery_url="http://b",
        bible_url="http://c",
    ).service_catalog
    # Digits sort lexically, so the built-in "4" falls between "3" and "5".
    assert [s["digit"] for s in cat] == ["1", "2", "3", "4", "5", "6", "7", "99"]
    by_digit = {s["digit"]: s["name"] for s in cat}
    assert by_digit["5"] == "quote" and by_digit["6"] == "battery" and by_digit["7"] == "bible"
    assert all(s["description"] for s in cat)


def test_each_fetch_service_registers_independently(clock):
    cat = _controller(clock, MockRadio(), quote_url="http://q").service_catalog
    assert [s["digit"] for s in cat] == ["1", "4", "5", "99"]  # no weather/battery/bible


def test_services_endpoint_lists_the_catalog(clock):
    radio = MockRadio()
    ctrl = _controller(clock, radio, weather_url="http://w/api")
    with TestClient(create_app(radio, api_token=TOKEN, controller=ctrl)) as client:
        body = client.get("/services", headers=AUTH).json()
    assert [s["digit"] for s in body] == ["1", "2", "3", *BUILTINS]


def test_services_endpoint_empty_without_controller():
    with TestClient(create_app(MockRadio(), api_token=TOKEN)) as client:
        assert client.get("/services", headers=AUTH).json() == []


def test_services_endpoint_requires_token():
    radio = MockRadio()
    ctrl = _controller(None, radio)
    with TestClient(create_app(radio, api_token=TOKEN, controller=ctrl)) as client:
        assert client.get("/services").status_code == 401


# --- POST /services/{digit}: fire a service/command over the air from the web UI ------------

def test_trigger_endpoint_runs_a_service_and_transmits(clock):
    radio = MockRadio()
    ctrl = _controller(clock, radio)  # time (1#) + builtins, no RF login needed
    with TestClient(create_app(radio, api_token=TOKEN, controller=ctrl)) as client:
        body = client.post("/services/1", headers=AUTH).json()
    assert body["service"] == "time" and body["transmitted"] is True
    assert len(radio.tx_log) == 1  # ID + time announcement, the fresh station's first over


def test_trigger_endpoint_plays_station_id(clock):
    radio = MockRadio()
    ctrl = _controller(clock, radio)
    with TestClient(create_app(radio, api_token=TOKEN, controller=ctrl)) as client:
        body = client.post("/services/4", headers=AUTH).json()
    assert body["builtin"] is True
    assert len(radio.tx_log) == 1  # an ID-only over


def test_trigger_endpoint_logout_without_session_is_a_noop(clock):
    radio = MockRadio()
    ctrl = _controller(clock, radio)
    with TestClient(create_app(radio, api_token=TOKEN, controller=ctrl)) as client:
        body = client.post("/services/99", headers=AUTH).json()
    assert body["builtin"] is True
    assert radio.tx_log == []  # nothing to close, nothing keyed


def test_trigger_endpoint_unknown_digit_transmits_nothing(clock):
    radio = MockRadio()
    ctrl = _controller(clock, radio)  # only 1# + builtins registered — 8 is never a service
    with TestClient(create_app(radio, api_token=TOKEN, controller=ctrl)) as client:
        body = client.post("/services/8", headers=AUTH).json()
    assert body["transmitted"] is False
    assert radio.tx_log == []


def test_trigger_endpoint_503_without_controller():
    with TestClient(create_app(MockRadio(), api_token=TOKEN)) as client:
        assert client.post("/services/1", headers=AUTH).status_code == 503


def test_trigger_endpoint_requires_token(clock):
    radio = MockRadio()
    ctrl = _controller(clock, radio)
    with TestClient(create_app(radio, api_token=TOKEN, controller=ctrl)) as client:
        assert client.post("/services/1").status_code == 401
