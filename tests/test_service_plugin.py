"""The voice-service plugin contract, binding resolution, and registry building (ADR 0034).

Hardware/network-free: services build against `make_settings` overrides and a `StubFetcher`, and the
registry's `catalog()` is asserted directly. These exercise the seam that replaced the imperative
registration block in `build_controller`.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from radio_server.auth import Session
from radio_server.services import (
    DEFAULT_BINDINGS,
    PLUGINS,
    RESERVED_DIGITS,
    PluginBuildContext,
    ServiceContext,
    ServicePlugin,
    StubFetcher,
    StubTts,
    build_registry,
    format_spoken_time,
    resolve_bindings,
)
from radio_server.services.fetch import UrllibFetcher

from .conftest import make_settings

IDS = {plugin.id for plugin in PLUGINS}


def _ctx(overrides=None, fetcher=None):
    settings = make_settings({"station.callsign": "AE9S", **(overrides or {})})
    return PluginBuildContext(settings, fetcher if fetcher is not None else StubFetcher(default={}))


def _digits(registry):
    return [entry["digit"] for entry in registry.catalog()]


# --- the plugin set ---------------------------------------------------------------------------


def test_every_plugin_conforms_to_the_protocol_and_self_describes():
    assert [p.id for p in PLUGINS] == ["time", "weather", "astronomy", "quote", "battery", "bible"]
    for plugin in PLUGINS:
        assert isinstance(plugin, ServicePlugin)  # structural (runtime_checkable)
        assert plugin.id and plugin.description  # every entry is operator-listable


def test_default_bindings_cover_the_historical_layout():
    assert DEFAULT_BINDINGS == {
        "1": "time",
        "2": "weather",
        "3": "astronomy",
        "5": "quote",
        "6": "battery",
        "7": "bible",
    }
    # 4 and 99 are the controller built-ins — never in the plugin layout.
    assert set(DEFAULT_BINDINGS) & set(RESERVED_DIGITS) == set()


# --- resolve_bindings -------------------------------------------------------------------------


def test_absent_bindings_fall_back_to_the_default_layout():
    assert resolve_bindings(None, IDS) == DEFAULT_BINDINGS


def test_operator_remap_is_honored():
    assert resolve_bindings({"1": "time", "8": "quote"}, IDS) == {"1": "time", "8": "quote"}


def test_two_digits_may_point_at_one_service():
    assert resolve_bindings({"1": "time", "9": "time"}, IDS) == {"1": "time", "9": "time"}


@pytest.mark.parametrize("digit", sorted(RESERVED_DIGITS))
def test_reserved_digit_is_rejected(digit):
    with pytest.raises(RuntimeError, match="reserved"):
        resolve_bindings({digit: "time"}, IDS)


def test_unknown_service_id_is_rejected():
    with pytest.raises(RuntimeError, match="unknown service"):
        resolve_bindings({"1": "nonesuch"}, IDS)


def test_non_dtmf_digit_is_rejected():
    with pytest.raises(RuntimeError, match="DTMF"):
        resolve_bindings({"1x": "time"}, IDS)


# --- build_registry (enable-gating) -----------------------------------------------------------


def test_only_always_on_time_registers_without_data_urls():
    registry = build_registry(PLUGINS, DEFAULT_BINDINGS, _ctx())
    assert _digits(registry) == ["1"]  # weather/astro/quote/battery/bible all gated off


def test_weather_url_enables_both_weather_and_astro():
    registry = build_registry(PLUGINS, DEFAULT_BINDINGS, _ctx({"weather.base_url": "http://w"}))
    assert _digits(registry) == ["1", "2", "3"]  # astro shares weather.base_url


def test_remapped_service_registers_under_its_new_digit():
    bindings = resolve_bindings({"8": "quote"}, IDS)
    registry = build_registry(PLUGINS, bindings, _ctx({"quote.base_url": "http://q"}))
    assert {e["digit"]: e["name"] for e in registry.catalog()} == {"8": "quote"}


def test_bound_but_unconfigured_service_is_a_graceful_miss():
    # quote is bound but its URL is unset → not registered (no crash, just absent).
    bindings = resolve_bindings({"5": "quote"}, IDS)
    registry = build_registry(PLUGINS, bindings, _ctx())
    assert _digits(registry) == []


def test_built_service_actually_speaks():
    # A built Service is callable and renders the spoken time through the context's TTS (settings
    # have no time.tz → the default UTC, so the string is deterministic).
    registry = build_registry(PLUGINS, {"1": "time"}, _ctx())
    _name, service = registry.get("1")
    ctx = ServiceContext(clock=lambda: 0.0, tts=StubTts())
    audio = service(Session(), ctx)
    assert audio == StubTts().render(format_spoken_time(0.0, ZoneInfo("UTC")))


# --- PluginBuildContext.fetcher -------------------------------------------------------------


def test_injected_fetcher_is_used_as_is():
    stub = StubFetcher(default={})
    ctx = PluginBuildContext(make_settings({"station.callsign": "AE9S"}), stub)
    assert ctx.fetcher() is stub


def test_fetcher_is_lazily_built_and_memoized():
    ctx = PluginBuildContext(make_settings({"station.callsign": "AE9S"}))
    first = ctx.fetcher()
    assert isinstance(first, UrllibFetcher)  # built on demand from weather.timeout
    assert ctx.fetcher() is first  # one shared instance across all fetch services
