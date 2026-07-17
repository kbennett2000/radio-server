"""Discovery of operator-authored service plugins from ``local_services/`` (ADR 0051).

The in-tree ``PLUGINS`` tuple ships only what works everywhere; everything network- or
station-specific lives out of tree, in a gitignored folder the operator creates next to
``radio.toml``. Creating the folder and copying a module into it is the explicit opt-in ADR 0034
reserved room for — nothing is auto-installed, and the trust boundary ("code in this deployment's
working directory runs as the station") is the one ``radio.toml`` itself sits behind.

Discovery is deliberately plain: every top-level ``*.py`` (sorted, ``_``-prefixed skipped) is
imported as an ordinary module — the folder joins ``sys.path``, so plugin modules can import their
neighbors (``from weather_service import ...``) and a module without a ``PLUGIN`` attribute is just
a helper. Everything fails loud at startup: an import error propagates, a malformed ``PLUGIN``
raises, and duplicate ids (against the in-tree plugins, the controller built-ins, or another local
module) raise — a broken local plugin is a config error, not a silent miss.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from .plugin import BUILTIN_IDS, PLUGINS, ServicePlugin

__all__ = ["DEFAULT_LOCAL_SERVICES_DIR", "discover_local_plugins"]

#: Default plugin folder, in the working directory (CWD-relative like ``radio.toml`` and the log
#: path; the systemd deployment runs from the repo root). Gitignored — it belongs to the operator.
DEFAULT_LOCAL_SERVICES_DIR = Path("local_services")


def discover_local_plugins(
    directory: str | Path = DEFAULT_LOCAL_SERVICES_DIR,
) -> tuple[ServicePlugin, ...]:
    """Import every plugin module under ``directory`` and return their ``PLUGIN`` singletons.

    A missing or empty folder yields ``()`` — the zero-cost default for deployments without local
    plugins. Modules load in sorted-name order (deterministic digit-collision messages downstream).
    """
    root = Path(directory)
    if not root.is_dir():
        return ()
    module_files = sorted(
        p for p in root.glob("*.py") if not p.name.startswith("_")
    )
    if not module_files:
        return ()
    root_str = str(root.resolve())
    if root_str not in sys.path:
        # Plain-import discovery: on the path once, so intra-folder imports resolve naturally.
        sys.path.insert(0, root_str)
    seen: dict[str, str] = {p.id: "in-tree" for p in PLUGINS}
    seen.update({builtin: "built-in" for builtin in BUILTIN_IDS})
    plugins: list[ServicePlugin] = []
    for path in module_files:
        module = importlib.import_module(path.stem)
        plugin = getattr(module, "PLUGIN", None)
        if plugin is None:
            continue  # a helper module, importable by the plugins
        if not isinstance(plugin, ServicePlugin):
            raise RuntimeError(
                f"local_services/{path.name}: PLUGIN does not satisfy ServicePlugin "
                f"(needs id, description, enabled(settings), build(ctx))"
            )
        if plugin.id in seen:
            raise RuntimeError(
                f"local_services/{path.name}: plugin id {plugin.id!r} collides with "
                f"{seen[plugin.id]}"
            )
        seen[plugin.id] = f"local_services/{path.name}"
        plugins.append(plugin)
    return tuple(plugins)
