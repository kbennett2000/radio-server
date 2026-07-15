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


def _controller(clock, radio, *, weather_url=""):
    overrides = {"station.callsign": CALLSIGN}
    if weather_url:
        overrides["weather.base_url"] = weather_url
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
