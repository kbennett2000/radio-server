"""Make libopus loadable for the Mumble link, and say how to fix it when it isn't (ADR 0056).

The ``mumble`` extra's ``opuslib`` (3.0.1) is a ctypes wrapper: at import time it calls
``ctypes.util.find_library('opus')`` and raises a bare ``Exception`` if it comes back empty. On
macOS/Linux the system libopus (``brew install opus`` / ``apt install libopus0``) satisfies that. On
Windows there is no ``opus.dll`` on a stock box, so we ship one (``radio_server/_vendor/win-amd64``,
amd64 only) and point ctypes at it here.

The load path is **explicit**, not import-order luck: callers run :func:`ensure_opus_loadable` *before*
``import pymumble_py3`` and can print what it did. The one subtlety that dictates the mechanism
(verified against CPython, see ADR 0056): on Windows ``find_library`` walks ``os.environ['PATH']`` and
**ignores** ``os.add_dll_directory`` (cpython#111104) — so the load-bearing step is prepending the
vendored directory to ``PATH``; ``add_dll_directory`` is only added for the DLL's own dependent-DLL
resolution.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

#: The vendored Windows amd64 opus.dll lives at radio_server/_vendor/win-amd64/opus.dll — two levels
#: up from this file (radio_server/link/_opus.py -> radio_server/).
_VENDOR_WIN_AMD64 = Path(__file__).resolve().parent.parent / "_vendor" / "win-amd64"

#: Directories already prepended to PATH this process, so repeat calls are idempotent.
_prepended: set[str] = set()

#: platform.machine() strings that mean 64-bit x86 (Windows reports "AMD64", others "x86_64").
_AMD64 = {"amd64", "x86_64"}


def ensure_opus_loadable(
    *, system: str | None = None, machine: str | None = None, vendor_dir: str | os.PathLike | None = None
) -> str:
    """Ensure ``opuslib``'s ``find_library('opus')`` can resolve libopus; return a short reason.

    Off Windows: a no-op (the system libopus is used). On Windows amd64: prepend the vendored
    ``opus.dll`` directory to ``PATH`` (and register it with ``add_dll_directory`` for dependent-DLL
    resolution), idempotently. On Windows arm64, or if the vendored DLL is missing, do nothing and say
    so — the subsequent import fails into :func:`opus_install_hint`. The keyword args are injection
    seams for tests (mirroring the ``_pymumble`` seam in ``pymumble_client``); production passes none.
    """
    system = system if system is not None else platform.system()
    if system != "Windows":
        return "non-Windows: using system libopus"

    machine = (machine if machine is not None else platform.machine()).lower()
    if machine not in _AMD64:
        return f"win-{machine}: no vendored opus.dll (unsupported arch)"

    base = Path(vendor_dir) if vendor_dir is not None else _VENDOR_WIN_AMD64
    if not (base / "opus.dll").exists():
        return f"win-amd64: vendored opus.dll missing at {base}"

    key = str(base)
    if key not in _prepended:
        os.environ["PATH"] = key + os.pathsep + os.environ.get("PATH", "")
        _prepended.add(key)
        # Helps CDLL resolve opus.dll's own dependent DLLs; does NOT help find_library (see module
        # docstring). Windows-only API, so guard for the non-Windows test host.
        add_dll_directory = getattr(os, "add_dll_directory", None)
        if add_dll_directory is not None:
            try:
                add_dll_directory(key)
            except OSError:
                pass
    return f"vendored opus.dll ({base})"


def opus_install_hint(*, system: str | None = None) -> str:
    """A per-platform, actionable remediation for a missing/unloadable libopus (ADR 0056).

    Tom's problem is mystery, not difficulty: the old "sudo apt install libopus0" was a dead end on
    macOS and Windows. ``system`` is an injection seam for tests; production passes none.
    """
    system = system if system is not None else platform.system()
    if system == "Darwin":
        return "install Homebrew (https://brew.sh), then: brew install opus"
    if system == "Windows":
        return (
            "opus.dll ships with the mumble extra for Windows amd64 — a failure here means an "
            "unsupported CPU (e.g. arm64) or a broken bundle; see radio_server/_vendor/README.md"
        )
    return "install the system library: sudo apt install libopus0 (or your distro's libopus package)"
