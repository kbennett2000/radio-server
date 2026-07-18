"""Make libopus loadable for the Mumble link, and say how to fix it when it isn't (ADR 0056/0057).

The ``mumble`` extra's ``opuslib`` (3.0.1) is a ctypes wrapper: at import time it calls
``ctypes.util.find_library('opus')`` and raises a bare ``Exception`` if it comes back empty. libopus
does not ship with a stock OS, so ADR 0057 makes it a dependency: the ``mumble`` extra pulls
``opuslib-next-bundled``, whose per-platform wheels each carry a self-contained libopus
(``libopus.so`` / ``libopus.dylib`` / ``opus.dll``). We never import that package for its bindings —
we only harvest its binary.

:func:`ensure_opus_loadable` locates that binary and **patches** ``ctypes.util.find_library`` so a
lookup of ``'opus'`` returns its path and every other name delegates unchanged. That patch is the only
option that works on all three platforms: on Linux/macOS ``find_library`` shells out to
``ldconfig``/``gcc``/``ld`` and will not see a wheel's private directory, and ``LD_LIBRARY_PATH``
cannot be set usefully after process start (ADR 0057). It must run **before** ``opuslib`` is imported,
since opuslib caches the lookup at import — callers run it right before ``import pymumble_py3`` and can
print what it did (explicit, not import-order luck).
"""

from __future__ import annotations

import ctypes.util
import importlib.util
import platform
from pathlib import Path

#: The carrier package (opuslib-next-bundled) exposes this import name; its wheels drop a self-contained
#: libopus into ``<pkg>/_native/``. We locate the file without importing the package's bindings.
_CARRIER_PACKAGE = "opuslib_next"

#: Candidate libopus filenames inside the carrier's ``_native`` dir, one per platform family.
_CARRIER_LIB_NAMES = ("libopus.so", "libopus.dylib", "opus.dll")

#: Set once we have wrapped ``ctypes.util.find_library`` — so the wrap is idempotent.
_patched = False


def _carrier_lib() -> Path | None:
    """Path to the bundled libopus inside opuslib-next-bundled, or ``None`` if it isn't installed.

    Uses ``find_spec`` so we never execute the carrier package (we want its binary, not its bindings).
    """
    spec = importlib.util.find_spec(_CARRIER_PACKAGE)
    if spec is None or not spec.submodule_search_locations:
        return None
    native = Path(next(iter(spec.submodule_search_locations))) / "_native"
    for name in _CARRIER_LIB_NAMES:
        candidate = native / name
        if candidate.exists():
            return candidate
    return None


def ensure_opus_loadable(*, carrier_lib: str | Path | None = None) -> str:
    """Point ``opuslib``'s ``find_library('opus')`` at the bundled libopus; return a short reason.

    Resolves the carrier's libopus (the ``carrier_lib`` seam for tests, else :func:`_carrier_lib`) and,
    if found, wraps ``ctypes.util.find_library`` once so ``'opus'`` answers with that path and all other
    names delegate to the original resolver. If the carrier isn't installed (e.g. a bare ``uv sync``
    with no ``--extra mumble``, or a no-wheel platform), it does nothing and says so — the subsequent
    ``import pymumble_py3`` then fails into :func:`opus_install_hint`.
    """
    global _patched
    lib = Path(carrier_lib) if carrier_lib is not None else _carrier_lib()
    if lib is None:
        return "no bundled libopus (mumble extra not installed?) — using system libopus"

    if not _patched:
        original = ctypes.util.find_library
        target = str(lib)

        def _find_library(name, _original=original, _target=target):
            return _target if name == "opus" else _original(name)

        ctypes.util.find_library = _find_library
        _patched = True
    return f"bundled libopus ({lib})"


def check_mumble_importable() -> tuple[bool, str]:
    """Run the opus shim and try to import pymumble; return ``(ok, message)`` for the installer banner.

    This is the single source of the "earn 'All set.'" check (ADR 0057): it exercises exactly the shim
    that was silently broken. ``ImportError`` means the extra isn't installed; any other error is the
    opus load failing (opuslib raises a bare ``Exception`` when libopus is missing).
    """
    reason = ensure_opus_loadable()
    try:
        import pymumble_py3  # noqa: F401
    except ImportError:
        return False, "the Mumble link isn't installed — run: uv sync --extra mumble"
    except Exception:  # noqa: BLE001 — opuslib raises a bare Exception (not OSError); see ADR 0056
        return False, opus_install_hint()
    return True, f"pymumble + libopus import OK ({reason})"


def opus_install_hint(*, extra: str = "mumble", system: str | None = None) -> str:
    """A per-platform remediation for a missing/unloadable libopus (ADR 0056/0057/0067).

    libopus ships with whichever extra carries the opus stack (a bundled-wheel carrier), so the real
    fix is almost always reinstalling that extra; the per-platform system-lib tail only matters on a
    platform with no carrier wheel (Windows arm64, 32-bit, musl/Alpine). ``extra`` names the caller's
    extra — the Mumble link (default ``"mumble"``) and the kv4p codec (``"kv4p"``) compose the same
    ``opus`` leaf, so each points the operator at the extra they actually installed (ADR 0067).
    ``system`` is an injection seam for tests.
    """
    system = system if system is not None else platform.system()
    base = f"libopus ships with the {extra} extra — reinstall it: uv sync --extra {extra}"
    if system == "Darwin":
        tail = "; if your Mac has no bundled wheel, install Homebrew (https://brew.sh) then: brew install opus"
    elif system == "Windows":
        tail = "; if you're on an unsupported CPU (e.g. arm64), there is no bundled opus for it yet"
    else:
        tail = "; if your platform has no bundled wheel, install the system lib: sudo apt install libopus0"
    return base + tail
