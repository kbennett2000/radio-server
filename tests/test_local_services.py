"""Operator-authored plugin discovery from ``local_services/`` (ADR 0051).

Hardware/network-free: plugin modules are written into ``tmp_path`` folders and discovered through
the real ``sys.path`` + ``importlib`` mechanism. Modules import by bare stem and stay cached in
``sys.modules``, so every test writes uniquely-named modules (a per-test suffix) rather than
fighting the import machinery. The end-to-end test wires a discovered plugin through
`build_controller`'s ``plugins`` seam exactly as the app composition root does.
"""

from __future__ import annotations

import uuid

import pytest

from radio_server.backends import MockRadio
from radio_server.controller import build_controller
from radio_server.services import (
    DEFAULT_LOCAL_SERVICES_DIR,
    PLUGINS,
    ServicePlugin,
    StubFetcher,
    StubTts,
    discover_local_plugins,
)

from .conftest import TEST_SECRET, make_settings


def _suffix():
    """A unique per-test module/id suffix — modules import by bare stem and stay cached."""
    return uuid.uuid4().hex[:8]


def _plugin_source(plugin_id, extra_lines=""):
    """A minimal conforming plugin module: a PLUGIN with id/description/enabled/build."""
    return (
        f"{extra_lines}"
        "class _Plugin:\n"
        f'    id = "{plugin_id}"\n'
        '    description = "A local test service"\n'
        "\n"
        "    def enabled(self, settings):\n"
        "        return True\n"
        "\n"
        "    def build(self, ctx):\n"
        "        def service(session, sctx):\n"
        f'            return sctx.tts.render("{plugin_id} speaking")\n'
        "        return service\n"
        "\n"
        "PLUGIN = _Plugin()\n"
    )


# --- discovery --------------------------------------------------------------------------------


def test_missing_directory_yields_empty_tuple(tmp_path):
    # The zero-cost default for deployments without local plugins.
    assert discover_local_plugins(tmp_path / "nope") == ()


def test_empty_directory_yields_empty_tuple(tmp_path):
    root = tmp_path / "local_services"
    root.mkdir()
    assert discover_local_plugins(root) == ()


def test_default_directory_is_the_gitignored_local_services_folder():
    assert str(DEFAULT_LOCAL_SERVICES_DIR) == "local_services"


def test_a_valid_plugin_module_is_discovered(tmp_path):
    sfx = _suffix()
    pid = f"echo-{sfx}"
    (tmp_path / f"svc_{sfx}.py").write_text(_plugin_source(pid))
    plugins = discover_local_plugins(tmp_path)
    assert [p.id for p in plugins] == [pid]
    assert isinstance(plugins[0], ServicePlugin)  # satisfies the same structural contract
    assert plugins[0].description  # operator-listable, like an in-tree plugin


def test_modules_load_in_sorted_name_order(tmp_path):
    # Deterministic order → deterministic digit-collision messages downstream.
    sfx = _suffix()
    (tmp_path / f"b_svc_{sfx}.py").write_text(_plugin_source(f"beta-{sfx}"))
    (tmp_path / f"a_svc_{sfx}.py").write_text(_plugin_source(f"alpha-{sfx}"))
    assert [p.id for p in discover_local_plugins(tmp_path)] == [f"alpha-{sfx}", f"beta-{sfx}"]


def test_underscore_prefixed_files_are_skipped(tmp_path):
    sfx = _suffix()
    (tmp_path / f"_private_{sfx}.py").write_text(_plugin_source(f"hidden-{sfx}"))
    assert discover_local_plugins(tmp_path) == ()


def test_helper_module_without_plugin_is_skipped_and_importable(tmp_path):
    # A module without a PLUGIN attribute is just a helper — and the folder joins sys.path, so a
    # plugin module imports its neighbor with a plain intra-folder import.
    sfx = _suffix()
    pid = f"greeter-{sfx}"
    (tmp_path / f"helper_{sfx}.py").write_text('GREETING = "hello from the helper"\n')
    (tmp_path / f"svc_{sfx}.py").write_text(
        _plugin_source(pid, extra_lines=f"from helper_{sfx} import GREETING\n\n")
    )
    plugins = discover_local_plugins(tmp_path)
    assert [p.id for p in plugins] == [pid]  # the helper contributed no plugin


# --- fail-loud paths --------------------------------------------------------------------------


@pytest.mark.parametrize("taken_id", ["time", "station-id"])
def test_duplicate_id_against_in_tree_or_builtin_raises(tmp_path, taken_id):
    # A local plugin may not shadow an in-tree plugin or a controller built-in.
    (tmp_path / f"svc_{_suffix()}.py").write_text(_plugin_source(taken_id))
    with pytest.raises(RuntimeError, match=f"{taken_id}.*collides"):
        discover_local_plugins(tmp_path)


def test_duplicate_id_between_two_local_modules_raises(tmp_path):
    sfx = _suffix()
    pid = f"twin-{sfx}"
    (tmp_path / f"a_svc_{sfx}.py").write_text(_plugin_source(pid))
    (tmp_path / f"b_svc_{sfx}.py").write_text(_plugin_source(pid))
    with pytest.raises(RuntimeError, match="collides"):
        discover_local_plugins(tmp_path)


def test_malformed_plugin_raises(tmp_path):
    # A PLUGIN that does not satisfy the ServicePlugin protocol is a config error, not a skip.
    (tmp_path / f"svc_{_suffix()}.py").write_text('PLUGIN = "not a plugin"\n')
    with pytest.raises(RuntimeError, match="ServicePlugin"):
        discover_local_plugins(tmp_path)


def test_import_error_propagates(tmp_path):
    # A broken local plugin fails loud at startup — never a silent miss.
    (tmp_path / f"svc_{_suffix()}.py").write_text("import nonexistent_module_xyz\n")
    with pytest.raises(ImportError):
        discover_local_plugins(tmp_path)


# --- end-to-end: a discovered plugin binds through build_controller (ADR 0051) ---------------


def test_discovered_plugin_registers_through_build_controller(tmp_path, clock):
    sfx = _suffix()
    pid = f"local-{sfx}"
    (tmp_path / f"svc_{sfx}.py").write_text(_plugin_source(pid))
    discovered = discover_local_plugins(tmp_path)
    ctrl = build_controller(
        make_settings({"station.callsign": "AE9S"}),
        radio=MockRadio(),
        totp_secret=TEST_SECRET,
        tts=StubTts(),
        fetcher=StubFetcher(default={}),
        clock=clock,
        service_bindings={"02": "time", "5": pid, "01": "station-id", "99": "logout"},
        plugins=PLUGINS + discovered,
    )
    by_digit = {s["digit"]: s["name"] for s in ctrl.service_catalog}
    assert by_digit == {"01": "station-id", "02": "time", "5": pid, "99": "logout"}
