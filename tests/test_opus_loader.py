"""libopus loading + per-platform remediation for the Mumble link (ADR 0056).

The `mumble` extra's opuslib does `find_library('opus')` at import time. On Windows we ship an
`opus.dll` and point ctypes at it before importing pymumble; off Windows the system libopus is used.
None of this needs opuslib/pymumble/libopus to actually be installed — the platform and the import
failure are injected — so these run on every machine with **no skipif**.
"""

from __future__ import annotations

import builtins
import os

import pytest

import radio_server.doctor as doctor_mod
import radio_server.link._opus as opus_mod
from radio_server.link._opus import ensure_opus_loadable, opus_install_hint
from radio_server.link import PyMumbleClient


@pytest.fixture(autouse=True)
def _isolate_prepend_state(monkeypatch):
    """Each test starts with a clean PATH-prepend memo so idempotency assertions are deterministic."""
    monkeypatch.setattr(opus_mod, "_prepended", set())


def _fake_vendor(tmp_path):
    """A directory holding a stand-in opus.dll (contents irrelevant — the loader only stat()s it)."""
    (tmp_path / "opus.dll").write_bytes(b"MZ")  # not loaded here; presence is what the loader checks
    return tmp_path


# --- ensure_opus_loadable(): path logic -----------------------------------------------------------

def test_non_windows_is_a_noop(monkeypatch):
    before = os.environ.get("PATH", "")
    assert ensure_opus_loadable(system="Linux") == "non-Windows: using system libopus"
    assert ensure_opus_loadable(system="Darwin") == "non-Windows: using system libopus"
    assert os.environ.get("PATH", "") == before  # PATH untouched off Windows


def test_windows_amd64_prepends_vendor_dir_to_path(tmp_path):
    vendor = _fake_vendor(tmp_path)
    reason = ensure_opus_loadable(system="Windows", machine="AMD64", vendor_dir=vendor)
    assert "vendored opus.dll" in reason
    assert os.environ["PATH"].split(os.pathsep)[0] == str(vendor)  # prepended, so first on PATH


def test_windows_amd64_prepend_is_idempotent(tmp_path):
    vendor = _fake_vendor(tmp_path)
    ensure_opus_loadable(system="Windows", machine="x86_64", vendor_dir=vendor)
    first = os.environ["PATH"]
    ensure_opus_loadable(system="Windows", machine="x86_64", vendor_dir=vendor)  # second call
    assert os.environ["PATH"] == first  # not prepended twice
    assert os.environ["PATH"].split(os.pathsep).count(str(vendor)) == 1


def test_windows_arm64_is_reported_unsupported_without_raising(tmp_path):
    reason = ensure_opus_loadable(system="Windows", machine="ARM64", vendor_dir=_fake_vendor(tmp_path))
    assert "arm64" in reason.lower()
    assert "unsupported" in reason


def test_windows_amd64_missing_dll_is_reported(tmp_path):
    # An empty vendor dir (no opus.dll): reported, not a crash, so the import fails into the hint.
    reason = ensure_opus_loadable(system="Windows", machine="AMD64", vendor_dir=tmp_path)
    assert "missing" in reason


def test_the_real_vendored_dll_ships_for_windows_amd64():
    # The wheel/source tree actually contains the amd64 binary (packaging didn't drop it).
    dll = opus_mod._VENDOR_WIN_AMD64 / "opus.dll"  # noqa: SLF001 — asserting the vendored asset
    assert dll.is_file() and dll.stat().st_size > 0


# --- opus_install_hint(): per-platform + accurate -------------------------------------------------

def test_hint_macos_points_at_homebrew():
    assert "brew install opus" in opus_install_hint(system="Darwin")


def test_hint_linux_points_at_libopus0():
    assert "libopus0" in opus_install_hint(system="Linux")


def test_hint_windows_does_not_say_install_libopus():
    hint = opus_install_hint(system="Windows")
    assert "libopus0" not in hint and "apt" not in hint  # apt-on-Windows was the old dead end
    assert "amd64" in hint  # explains the DLL ships, and points at the arch caveat


# --- doctor --link: maps the import failure to the right message ----------------------------------

def _cfg(host="murmur.example"):
    return {"host": host, "port": 64738, "username": "radio-server", "channel": "", "password": ""}


def _raise_on_pymumble_import(monkeypatch, exc):
    """Force `import pymumble_py3` to raise `exc`, leaving every other import untouched."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pymumble_py3":
            raise exc
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_doctor_link_importerror_reports_missing_extra(monkeypatch, capsys):
    _raise_on_pymumble_import(monkeypatch, ImportError("no module named pymumble_py3"))
    assert doctor_mod._link(_cfg(), 0.0) == 1  # noqa: SLF001 — the CLI diagnostic entry
    out = capsys.readouterr().out
    assert "pymumble not installed" in out
    assert "uv sync --extra mumble" in out


@pytest.mark.parametrize(
    "exc",
    [
        Exception("Could not find Opus library. Make sure it is installed."),  # opuslib's real raise
        OSError("cannot load opus.dll"),  # an unloadable/mismatched DLL
    ],
)
def test_doctor_link_opus_failure_reports_per_platform_hint(exc, monkeypatch, capsys):
    # opuslib raises a *bare Exception* (not OSError) when libopus is absent — the doctor must still
    # reach the friendly, platform-correct hint (here: macOS).
    monkeypatch.setattr(opus_mod.platform, "system", lambda: "Darwin")
    _raise_on_pymumble_import(monkeypatch, exc)
    assert doctor_mod._link(_cfg(), 0.0) == 1  # noqa: SLF001
    out = capsys.readouterr().out
    assert "libopus not found" in out
    assert "brew install opus" in out  # the Darwin hint, reached despite the bare Exception


def test_doctor_link_prints_the_opus_resolution_line(monkeypatch, capsys):
    _raise_on_pymumble_import(monkeypatch, ImportError("x"))
    doctor_mod._link(_cfg(), 0.0)  # noqa: SLF001
    assert "opus:" in capsys.readouterr().out  # the debuggable resolution line always prints


# --- PyMumbleClient._pm(): same mapping on the live client path -----------------------------------

def test_client_reports_missing_libopus_with_hint(monkeypatch):
    # A bare opus Exception on import must become an actionable RuntimeError naming libopus, not a
    # raw traceback (ADR 0056). Platform pinned so the asserted hint is deterministic.
    monkeypatch.setattr(opus_mod.platform, "system", lambda: "Linux")
    _raise_on_pymumble_import(
        monkeypatch, Exception("Could not find Opus library. Make sure it is installed.")
    )
    client = PyMumbleClient(host="murmur.example")  # no injected module → real lazy import
    with pytest.raises(RuntimeError, match="libopus"):
        client.connect()


def test_client_missing_extra_still_maps_to_extra_message(monkeypatch):
    # ImportError (extra absent) keeps the original "mumble extra" message, unchanged by ADR 0056.
    _raise_on_pymumble_import(monkeypatch, ImportError("no pymumble"))
    client = PyMumbleClient(host="murmur.example")
    with pytest.raises(RuntimeError, match="mumble.*extra"):
        client.connect()
