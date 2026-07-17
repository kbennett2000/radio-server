"""The voice-service plugin contract, binding resolution, and registry building (ADR 0034).

Hardware/network-free: services build against `make_settings` overrides and a `StubFetcher`, and the
registry's `catalog()` is asserted directly. These exercise the seam that replaced the imperative
registration block in `build_controller`. The in-tree set is slim now (ADR 0051: only the always-on
time service ships), so the mechanism — enable gating, remapping, graceful misses — is exercised
against small inline fake plugins.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from radio_server.auth import Session
from radio_server.services import (
    BUILTIN_IDS,
    DEFAULT_BINDINGS,
    PLUGINS,
    PluginBuildContext,
    ServiceContext,
    ServicePlugin,
    StubFetcher,
    StubTts,
    build_registry,
    builtin_digits,
    format_spoken_time,
    resolve_bindings,
)
from radio_server.services.fetch import UrllibFetcher

from .conftest import make_settings

IDS = {plugin.id for plugin in PLUGINS}


class FakePlugin:
    """A minimal inline `ServicePlugin` for exercising the registry mechanism."""

    def __init__(self, id, *, enabled=True, description="A fake test service"):
        self.id = id
        self.description = description
        self._enabled = enabled

    def enabled(self, settings):
        return self._enabled

    def build(self, ctx):
        def service(session, sctx):
            return sctx.tts.render(f"{self.id} speaking")

        return service


def _ctx(overrides=None, fetcher=None):
    settings = make_settings({"station.callsign": "AE9S", **(overrides or {})})
    return PluginBuildContext(settings, fetcher if fetcher is not None else StubFetcher(default={}))


def _digits(registry):
    return [entry["digit"] for entry in registry.catalog()]


# --- the plugin set ---------------------------------------------------------------------------


def test_every_plugin_conforms_to_the_protocol_and_self_describes():
    # ADR 0051 slimmed the in-tree set to what works everywhere: only the time service ships.
    assert [p.id for p in PLUGINS] == ["time"]
    for plugin in PLUGINS:
        assert isinstance(plugin, ServicePlugin)  # structural (runtime_checkable)
        assert plugin.id and plugin.description  # every entry is operator-listable


def test_default_bindings_cover_the_shipped_two_digit_layout():
    # Two-digit codes matching the shipped link combos in width (ADR 0052): 01# ID, 02# time,
    # 99# logout — the whole out-of-the-box keypad reads as one scheme.
    assert DEFAULT_BINDINGS == {
        "01": "station-id",
        "02": "time",
        "99": "logout",
    }
    # The two controller built-ins are ordinary entries in the one keypad map now (ADR 0034).
    assert {DEFAULT_BINDINGS["01"], DEFAULT_BINDINGS["99"]} == set(BUILTIN_IDS)


def test_a_fake_plugin_conforms_to_the_protocol():
    # The inline fake used below satisfies the same structural contract a local plugin must.
    assert isinstance(FakePlugin("fake"), ServicePlugin)


# --- resolve_bindings -------------------------------------------------------------------------


def test_absent_bindings_fall_back_to_the_default_layout():
    assert resolve_bindings(None, IDS) == DEFAULT_BINDINGS


def test_operator_remap_is_honored():
    assert resolve_bindings({"1": "time"}, IDS) == {"1": "time"}


def test_two_digits_may_point_at_one_service():
    assert resolve_bindings({"1": "time", "9": "time"}, IDS) == {"1": "time", "9": "time"}


@pytest.mark.parametrize("builtin_id", sorted(BUILTIN_IDS))
def test_a_builtin_may_be_bound_like_a_service(builtin_id):
    # station-id / logout are valid binding targets — the operator can put them on any digit.
    assert resolve_bindings({"0": builtin_id}, IDS) == {"0": builtin_id}


@pytest.mark.parametrize("digit", ["01", "99"])
def test_former_builtin_digits_are_now_free(digit):
    # 01 and 99 are not special — a service may take them (the built-in moves elsewhere).
    assert resolve_bindings({digit: "time"}, IDS) == {digit: "time"}


def test_unknown_service_id_is_rejected():
    with pytest.raises(RuntimeError, match="unknown service or command"):
        resolve_bindings({"1": "nonesuch"}, IDS)


def test_unknown_id_points_at_the_local_services_folder():
    # ADR 0059: the generic "known ids" list never says local services come from a folder the
    # operator creates — name it, so an operator who wrote a plugin knows where it must live.
    with pytest.raises(RuntimeError, match="local_services") as exc:
        resolve_bindings({"1": "nonesuch"}, IDS)
    assert "unknown service or command" in str(exc.value)  # the original prefix is preserved


def test_removed_service_id_names_its_example_file():
    # ADR 0059: an id that shipped in-tree before ADR 0051 — the exact upgrade that stranded
    # stations — names the example file to copy, not just "unknown".
    with pytest.raises(
        RuntimeError, match=r"examples/local_services/weather_service\.py"
    ) as exc:
        resolve_bindings({"2": "weather"}, IDS)
    assert "ADR 0051" in str(exc.value)


def test_absent_local_services_folder_is_called_out(tmp_path, monkeypatch):
    # discover_local_plugins returns () silently on a missing folder; when a binding names an
    # unknown id and the folder isn't there, the error says so (ADR 0059).
    monkeypatch.chdir(tmp_path)  # a cwd with no local_services/
    with pytest.raises(RuntimeError, match="doesn't exist yet") as exc:
        resolve_bindings({"1": "nonesuch"}, IDS)
    assert "create it" in str(exc.value)


def test_present_local_services_folder_is_not_flagged_absent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "local_services").mkdir()
    with pytest.raises(RuntimeError, match="unknown service or command") as exc:
        resolve_bindings({"1": "nonesuch"}, IDS)
    assert "doesn't exist yet" not in str(exc.value)


def test_non_dtmf_digit_is_rejected():
    with pytest.raises(RuntimeError, match="DTMF"):
        resolve_bindings({"1x": "time"}, IDS)


# --- builtin_digits ---------------------------------------------------------------------------


def test_builtin_digits_reads_back_the_operator_layout():
    bindings = resolve_bindings({"5": "station-id", "0": "logout", "00": "logout"}, IDS)
    assert builtin_digits(bindings, "station-id") == frozenset({"5"})
    assert builtin_digits(bindings, "logout") == frozenset({"0", "00"})  # more than one digit is fine


def test_builtin_digits_empty_when_the_builtin_is_omitted():
    # A [services] table that lists no logout leaves that command off the keypad entirely.
    assert builtin_digits(resolve_bindings({"1": "time"}, IDS), "logout") == frozenset()


# --- build_registry (enable-gating) -----------------------------------------------------------


def test_default_layout_registers_only_the_time_service():
    registry = build_registry(PLUGINS, DEFAULT_BINDINGS, _ctx())
    # DEFAULT_BINDINGS carries the 01/99 built-ins too, but build_registry skips them (the engine
    # runs them) — only the always-on time service registers here.
    assert _digits(registry) == ["02"]


def test_enabled_plugin_registers_and_disabled_is_a_graceful_miss():
    # The old per-service URL gating, exercised through the mechanism: a plugin whose `enabled`
    # gate is False stays off its digit (no crash, just absent), exactly like an unconfigured URL.
    on, off = FakePlugin("fake-on"), FakePlugin("fake-off", enabled=False)
    bindings = {"02": "time", "5": "fake-on", "6": "fake-off"}
    registry = build_registry(PLUGINS + (on, off), bindings, _ctx())
    assert _digits(registry) == ["02", "5"]


def test_remapped_plugin_registers_under_its_new_digit():
    fake = FakePlugin("fake-data")
    bindings = resolve_bindings({"8": "fake-data"}, IDS | {"fake-data"})
    registry = build_registry(PLUGINS + (fake,), bindings, _ctx())
    assert {e["digit"]: e["name"] for e in registry.catalog()} == {"8": "fake-data"}


def test_bound_but_disabled_plugin_registers_nothing():
    # Bound but gated off → an empty registry (its digit is a silent no-op downstream).
    fake = FakePlugin("fake-data", enabled=False)
    bindings = resolve_bindings({"5": "fake-data"}, IDS | {"fake-data"})
    assert _digits(build_registry((fake,), bindings, _ctx())) == []


def test_built_service_actually_speaks():
    # A built Service is callable and renders the spoken time through the context's TTS (settings
    # have no time.tz → the default UTC, so the string is deterministic).
    registry = build_registry(PLUGINS, {"02": "time"}, _ctx())
    _name, service = registry.get("02")
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
    assert isinstance(first, UrllibFetcher)  # built on demand, bound to DEFAULT_FETCH_TIMEOUT
    assert ctx.fetcher() is first  # one shared instance across all fetch services
