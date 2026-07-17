"""The service catalog, operator-assigned digits, and the GET/POST /services endpoints.

The slim in-tree set (ADR 0051) ships only the always-on time service plus the two controller
built-ins, on the two-digit shipped keypad (01# ID, 02# time, 99# logout). The catalog — and the UI
panel it drives — reflects exactly what the operator can actually invoke; the enable-gating and
local-plugin mechanics are exercised here through `build_controller`'s ``plugins`` seam with inline
fakes.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from radio_server.api import create_app
from radio_server.backends import MockRadio
from radio_server.controller import build_controller
from radio_server.services import PLUGINS, StubFetcher, StubTts

from .conftest import TEST_SECRET, make_settings

CALLSIGN = "AE9S"
TOKEN = "test-lan-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _controller(clock, radio, *, bindings=None, plugins=PLUGINS):
    return build_controller(
        make_settings({"station.callsign": CALLSIGN}),
        radio=radio,
        totp_secret=TEST_SECRET,
        tts=StubTts(),
        fetcher=StubFetcher(default={}),
        clock=clock,
        service_bindings=bindings,
        plugins=plugins,
    )


class _FakePlugin:
    """A minimal extra plugin, standing in for a local_services/ discovery (ADR 0051)."""

    id = "fake-data"
    description = "A fake gated service"

    def __init__(self, enabled=True):
        self._enabled = enabled

    def enabled(self, settings):
        return self._enabled

    def build(self, ctx):
        return lambda session, sctx: sctx.tts.render("fake data")


# The built-in controller commands always appear in the catalog (they aren't registry services).
BUILTINS = ["01", "99"]


def test_default_catalog_is_time_plus_builtins(clock):
    ctrl = _controller(clock, MockRadio())
    # Digits sort lexically: 01 (station-id), 02 (time), 99 (logout).
    assert [s["digit"] for s in ctrl.service_catalog] == ["01", "02", "99"]
    by_digit = {s["digit"]: s["name"] for s in ctrl.service_catalog}
    assert by_digit == {"01": "station-id", "02": "time", "99": "logout"}
    assert all(s["description"] for s in ctrl.service_catalog)  # every entry self-describes


def test_extra_plugin_registers_when_enabled(clock):
    # `build_controller`'s ``plugins`` seam (ADR 0051): an extra plugin — how local_services/
    # discoveries arrive — registers on its bound digit alongside the in-tree set.
    cat = _controller(
        clock,
        MockRadio(),
        bindings={"02": "time", "5": "fake-data"},
        plugins=PLUGINS + (_FakePlugin(),),
    ).service_catalog
    assert {s["digit"]: s["name"] for s in cat} == {"02": "time", "5": "fake-data"}


def test_bound_but_disabled_plugin_is_a_graceful_miss(clock):
    # A plugin whose enable gate is off (its data source unconfigured) stays off the catalog —
    # its digit is a silent no-op, not a crash.
    cat = _controller(
        clock,
        MockRadio(),
        bindings={"02": "time", "5": "fake-data"},
        plugins=PLUGINS + (_FakePlugin(enabled=False),),
    ).service_catalog
    assert [s["digit"] for s in cat] == ["02"]


# --- operator-assigned digits (ADR 0034) -----------------------------------------------------


def test_operator_can_remap_a_service_to_a_different_digit(clock):
    # A [services] table is the COMPLETE keypad. Bind time to 8 (its default is 02) and keep the
    # built-ins by listing them; everything unlisted drops out.
    cat = _controller(
        clock,
        MockRadio(),
        bindings={"8": "time", "01": "station-id", "99": "logout"},
    ).service_catalog
    assert [s["digit"] for s in cat] == ["01", "8", "99"]  # time on 8, plus the built-ins
    assert {s["digit"]: s["name"] for s in cat}["8"] == "time"


def test_operator_can_remap_the_builtins_too(clock):
    # The station-id / logout digits are operator-assignable (ADR 0034): move them off 01/99, and
    # a service may take the freed 01.
    cat = _controller(
        clock,
        MockRadio(),
        bindings={"01": "time", "5": "station-id", "0": "logout"},
    ).service_catalog
    by_digit = {s["digit"]: s["name"] for s in cat}
    assert by_digit == {"0": "logout", "01": "time", "5": "station-id"}


def test_a_services_table_may_omit_the_builtins(clock):
    # An operator whose table lists no built-in leaves them off the keypad — auto-ID and the idle
    # timeout still run, so this degrades gracefully rather than breaking controller behavior.
    cat = _controller(clock, MockRadio(), bindings={"1": "time"}).service_catalog
    assert [s["digit"] for s in cat] == ["1"]  # no 01/99


def test_binding_an_unknown_service_fails_loud(clock):
    with pytest.raises(RuntimeError, match="unknown service or command"):
        _controller(clock, MockRadio(), bindings={"1": "nonesuch"})


def test_services_endpoint_lists_the_catalog(clock):
    radio = MockRadio()
    ctrl = _controller(clock, radio)
    with TestClient(create_app(radio, api_token=TOKEN, controller=ctrl)) as client:
        body = client.get("/services", headers=AUTH).json()
    assert [s["digit"] for s in body] == ["01", "02", "99"]


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
    ctrl = _controller(clock, radio)  # time (02#) + builtins, no RF login needed
    with TestClient(create_app(radio, api_token=TOKEN, controller=ctrl)) as client:
        body = client.post("/services/02", headers=AUTH).json()
    assert body["service"] == "time" and body["transmitted"] is True
    assert len(radio.tx_log) == 1  # ID + time announcement, the fresh station's first over


def test_trigger_endpoint_plays_station_id(clock):
    radio = MockRadio()
    ctrl = _controller(clock, radio)
    with TestClient(create_app(radio, api_token=TOKEN, controller=ctrl)) as client:
        body = client.post("/services/01", headers=AUTH).json()
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
    ctrl = _controller(clock, radio)  # only 02# + builtins registered — 8 is never a service
    with TestClient(create_app(radio, api_token=TOKEN, controller=ctrl)) as client:
        body = client.post("/services/8", headers=AUTH).json()
    assert body["transmitted"] is False
    assert radio.tx_log == []


def test_trigger_endpoint_503_without_controller():
    with TestClient(create_app(MockRadio(), api_token=TOKEN)) as client:
        assert client.post("/services/02", headers=AUTH).status_code == 503


def test_trigger_endpoint_requires_token(clock):
    radio = MockRadio()
    ctrl = _controller(clock, radio)
    with TestClient(create_app(radio, api_token=TOKEN, controller=ctrl)) as client:
        assert client.post("/services/02").status_code == 401
