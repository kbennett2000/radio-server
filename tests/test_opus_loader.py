"""libopus loading via the bundled-wheel carrier + the installer's earned-banner check (ADR 0057).

libopus ships with the `mumble` extra now (opuslib-next-bundled carries a per-platform binary).
`ensure_opus_loadable()` locates that binary and patches `ctypes.util.find_library` so opuslib's
import-time `find_library('opus')` resolves it. None of this needs opuslib/pymumble/libopus to be
installed — the carrier path and the import failure are injected — so these run everywhere with **no
skipif**.
"""

from __future__ import annotations

import builtins
import ctypes.util

import pytest

import radio_server.doctor as doctor_mod
import radio_server.link._opus as opus_mod
from radio_server.link._opus import (
    check_mumble_importable,
    ensure_opus_loadable,
    opus_install_hint,
)
from radio_server.link import PyMumbleClient


@pytest.fixture(autouse=True)
def _restore_find_library(monkeypatch):
    """Isolate each test: reset the one-shot patch flag and restore the real find_library after."""
    monkeypatch.setattr(opus_mod, "_patched", False)
    original = ctypes.util.find_library
    yield
    ctypes.util.find_library = original


# --- ensure_opus_loadable(): patch find_library to the carrier lib ---------------------------------

def test_patches_find_library_to_the_carrier_and_delegates_others(tmp_path):
    lib = tmp_path / "libopus.so"
    lib.write_bytes(b"\x7fELF")  # never dlopened here; only its path is handed to find_library
    real_m = ctypes.util.find_library("m")  # capture the real resolver's answer before patching
    reason = ensure_opus_loadable(carrier_lib=lib)
    assert str(lib) in reason and "bundled libopus" in reason
    assert ctypes.util.find_library("opus") == str(lib)  # our answer
    assert ctypes.util.find_library("m") == real_m  # every other name delegates, unchanged


def test_patch_is_idempotent(tmp_path):
    lib = tmp_path / "libopus.so"
    lib.write_bytes(b"\x7fELF")
    first = ensure_opus_loadable(carrier_lib=lib)
    wrapped = ctypes.util.find_library
    ensure_opus_loadable(carrier_lib=lib)  # second call
    assert ctypes.util.find_library is wrapped  # not re-wrapped
    assert first == ensure_opus_loadable(carrier_lib=lib)


def test_no_carrier_is_a_graceful_noop(monkeypatch):
    # e.g. a bare `uv sync` (no --extra mumble), or a no-wheel platform: don't patch, just say so.
    monkeypatch.setattr(opus_mod, "_carrier_lib", lambda: None)
    before = ctypes.util.find_library
    reason = ensure_opus_loadable()
    assert "no bundled libopus" in reason
    assert ctypes.util.find_library is before  # untouched — the import then fails into the hint


def test_carrier_lib_locates_native_dir_without_importing_bindings(monkeypatch, tmp_path):
    # _carrier_lib finds <pkg>/_native/<lib> from the package's search location (no import executed).
    native = tmp_path / "_native"
    native.mkdir()
    (native / "libopus.dylib").write_bytes(b"x")

    class _Spec:
        submodule_search_locations = [str(tmp_path)]

    monkeypatch.setattr(opus_mod.importlib.util, "find_spec", lambda name: _Spec())
    assert opus_mod._carrier_lib() == native / "libopus.dylib"  # noqa: SLF001


def test_carrier_lib_none_when_package_absent(monkeypatch):
    monkeypatch.setattr(opus_mod.importlib.util, "find_spec", lambda name: None)
    assert opus_mod._carrier_lib() is None  # noqa: SLF001


# --- check_mumble_importable(): the installer's earned-banner logic --------------------------------

def _raise_on_pymumble_import(monkeypatch, exc):
    """Force `import pymumble_py3` to raise `exc`, leaving every other import untouched."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pymumble_py3":
            raise exc
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_check_missing_extra_is_actionable(monkeypatch):
    monkeypatch.setattr(opus_mod, "_carrier_lib", lambda: None)
    _raise_on_pymumble_import(monkeypatch, ImportError("no module named pymumble_py3"))
    ok, msg = check_mumble_importable()
    assert ok is False
    assert "uv sync --extra mumble" in msg


def test_check_opus_load_failure_gives_the_hint(monkeypatch):
    monkeypatch.setattr(opus_mod, "_carrier_lib", lambda: None)
    monkeypatch.setattr(opus_mod.platform, "system", lambda: "Darwin")
    # opuslib raises a bare Exception (not OSError) when libopus is missing — must still be caught.
    _raise_on_pymumble_import(monkeypatch, Exception("Could not find Opus library. Make sure..."))
    ok, msg = check_mumble_importable()
    assert ok is False
    assert msg == opus_install_hint(system="Darwin")


def test_check_success(monkeypatch, tmp_path):
    import sys
    import types

    lib = tmp_path / "libopus.so"
    lib.write_bytes(b"x")
    monkeypatch.setattr(opus_mod, "_carrier_lib", lambda: lib)
    # Inject a stand-in pymumble_py3 so `import pymumble_py3` succeeds deterministically without the
    # real (heavy) import — independent of whether the extra is installed on the runner.
    monkeypatch.setitem(sys.modules, "pymumble_py3", types.ModuleType("pymumble_py3"))
    ok, msg = check_mumble_importable()
    assert ok is True
    assert "import OK" in msg and str(lib) in msg


# --- opus_install_hint(): leads with the extra, keeps a per-platform tail --------------------------

def test_hint_always_leads_with_the_extra():
    for system in ("Darwin", "Linux", "Windows"):
        assert "uv sync --extra mumble" in opus_install_hint(system=system)


def test_hint_macos_mentions_brew_and_linux_mentions_libopus0():
    assert "brew install opus" in opus_install_hint(system="Darwin")
    assert "libopus0" in opus_install_hint(system="Linux")


def test_hint_names_the_callers_extra(monkeypatch):
    # ADR 0067: the kv4p codec composes the same opus leaf but installs it via its own `kv4p` extra,
    # so its hint must point at `--extra kv4p`, not the Mumble link's `--extra mumble`.
    for system in ("Darwin", "Linux", "Windows"):
        kv4p_hint = opus_install_hint(extra="kv4p", system=system)
        assert "uv sync --extra kv4p" in kv4p_hint
        assert "--extra mumble" not in kv4p_hint
        # the per-platform tail is preserved regardless of which extra names it
    assert "brew install opus" in opus_install_hint(extra="kv4p", system="Darwin")
    assert "libopus0" in opus_install_hint(extra="kv4p", system="Linux")


# --- doctor --link: maps the import failure to the right message -----------------------------------

def _cfg(host="murmur.example"):
    return {"host": host, "port": 64738, "username": "radio-server", "channel": "", "password": ""}


def test_doctor_link_importerror_reports_missing_extra(monkeypatch, capsys):
    monkeypatch.setattr(opus_mod, "_carrier_lib", lambda: None)
    _raise_on_pymumble_import(monkeypatch, ImportError("no pymumble"))
    assert doctor_mod._link(_cfg(), 0.0) == 1  # noqa: SLF001 — the CLI diagnostic entry
    out = capsys.readouterr().out
    assert "pymumble not installed" in out
    assert "uv sync --extra mumble" in out


def test_doctor_link_opus_failure_reports_hint(monkeypatch, capsys):
    monkeypatch.setattr(opus_mod, "_carrier_lib", lambda: None)
    monkeypatch.setattr(opus_mod.platform, "system", lambda: "Linux")
    _raise_on_pymumble_import(monkeypatch, Exception("Could not find Opus library"))
    assert doctor_mod._link(_cfg(), 0.0) == 1  # noqa: SLF001
    out = capsys.readouterr().out
    assert "libopus not found" in out
    assert "libopus0" in out  # the Linux tail of the hint, reached despite the bare Exception


def test_doctor_link_prints_the_opus_resolution_line(monkeypatch, capsys):
    monkeypatch.setattr(opus_mod, "_carrier_lib", lambda: None)
    _raise_on_pymumble_import(monkeypatch, ImportError("x"))
    doctor_mod._link(_cfg(), 0.0)  # noqa: SLF001
    assert "opus:" in capsys.readouterr().out  # the debuggable resolution line always prints


# --- PyMumbleClient._pm(): same mapping on the live client path ------------------------------------

def test_client_missing_extra_maps_to_extra_message(monkeypatch):
    monkeypatch.setattr(opus_mod, "_carrier_lib", lambda: None)
    _raise_on_pymumble_import(monkeypatch, ImportError("no pymumble"))
    client = PyMumbleClient(host="murmur.example")  # no injected module → real lazy import
    with pytest.raises(RuntimeError, match="mumble.*extra"):
        client.connect()


def test_client_opus_failure_maps_to_libopus_message(monkeypatch):
    monkeypatch.setattr(opus_mod, "_carrier_lib", lambda: None)
    _raise_on_pymumble_import(monkeypatch, Exception("Could not find Opus library"))
    client = PyMumbleClient(host="murmur.example")
    with pytest.raises(RuntimeError, match="libopus"):
        client.connect()
