"""The shipped example plugins under ``examples/local_services/`` must stay importable and valid.

ADR 0059: the five services ADR 0051 removed from the tree (weather/astronomy/quote/battery/bible)
ship as copy-ready examples, so an upgrade is ``cp examples/local_services/*.py local_services/``
rather than git archaeology. This test is the load-bearing unit for that promise: it imports every
example through the *real* discovery machinery (`discover_local_plugins`), so interface drift in
`Fetcher` / `ServiceContext` / `Service` / `ServicePlugin` fails here — in CI — instead of on a
station at its next restart. It replaces the per-service unit tests d2ff286 deleted; one import test
carries the weight.
"""

from __future__ import annotations

import sys
from pathlib import Path

from radio_server.services import ServicePlugin, discover_local_plugins

_ROOT = Path(__file__).resolve().parent.parent  # tests/ is one level below the repo root
_EXAMPLES = _ROOT / "examples" / "local_services"
_EXPECTED_IDS = {"weather", "astronomy", "quote", "battery", "bible"}


def _module_stems() -> list[str]:
    return sorted(p.stem for p in _EXAMPLES.glob("*.py") if not p.name.startswith("_"))


def test_examples_dir_ships_the_five_reference_plugins():
    assert _module_stems() == sorted(
        [
            "weather_service",
            "astro_service",
            "quote_service",
            "battery_service",
            "bible_service",
        ]
    )


def test_every_example_module_exposes_a_valid_plugin():
    # Isolate the bare-stem import cache: the dev box also has a root local_services/ carrying the
    # same stems (gitignored; absent in CI), so pop any stale entries and put the examples dir first
    # on sys.path, then restore both — the test is deterministic regardless of prior imports.
    stems = _module_stems()
    assert stems, "examples/local_services/ should ship the reference plugins"
    saved = {stem: sys.modules.pop(stem, None) for stem in stems}
    inserted = str(_EXAMPLES.resolve())
    sys.path.insert(0, inserted)
    try:
        plugins = discover_local_plugins(_EXAMPLES)
    finally:
        if sys.path and sys.path[0] == inserted:
            sys.path.pop(0)
        for stem, module in saved.items():
            sys.modules.pop(stem, None)  # drop the freshly-imported example copy
            if module is not None:
                sys.modules[stem] = module  # restore whatever was cached before

    # Every module exposed a PLUGIN (none silently skipped as a PLUGIN-less helper): counts match.
    assert len(plugins) == len(stems)
    assert {p.id for p in plugins} == _EXPECTED_IDS
    for plugin in plugins:
        assert isinstance(plugin, ServicePlugin)  # structural (runtime_checkable) contract
        assert plugin.id and plugin.description  # operator-listable, like an in-tree plugin
