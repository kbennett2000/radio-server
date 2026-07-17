"""`dtmf.decode_mode = auto` — resolve to `native` unconditionally (ADR 0060).

`auto` is the default. ADR 0055 made it pick `streaming` when the multimon-ng binary was present and
`native` otherwise; ADR 0060 settled the deferred real-RF A/B (native decodes better on the reference
AIOC + UV-5R station) and flipped the preference: `auto` now resolves to `native` regardless of whether
multimon-ng is on PATH. multimon-ng is no longer a dependency — it survives only for the explicit
`streaming`/`buffered` escape hatches, which stay a contract and still raise if the binary is missing.

Availability is injected via `shutil.which` (no `skipif`), so these run on every machine — here we use
it to prove `auto` *ignores* it.
"""

from __future__ import annotations

import pytest

import radio_server.audio.dtmf as dtmf_mod
import radio_server.doctor as doctor_mod
from radio_server.audio import (
    GoertzelStream,
    MultimonStream,
    StreamingDtmfInput,
    resolve_decode_mode,
    synth_dtmf,
)
from radio_server.backends import MockRadio
from radio_server.controller import build_controller
from radio_server.services import StubTts
from radio_server.config import resolve_settings

_FOUND = "/usr/bin/multimon-ng"  # a stand-in PATH hit for shutil.which
_MISSING_BIN = "radio-server-no-such-binary"  # genuinely absent, for the deterministic raise
_AUTO_NATIVE = ("native", "bench-verified, ADR 0060")  # what `auto` now always resolves to


def _set_multimon_present(monkeypatch, present: bool) -> None:
    """Make `shutil.which` (as `resolve_decode_mode` sees it) report multimon-ng present or absent."""
    monkeypatch.setattr(dtmf_mod.shutil, "which", lambda name: _FOUND if present else None)


def _build(mode: str, monkeypatch, *, present: bool, multimon_bin: str | None = None):
    """A controller wired via the real stack with `dtmf.decode_mode = mode` and injected availability."""
    _set_multimon_present(monkeypatch, present)
    overrides = {"dtmf.decode_mode": mode, "station.callsign": "W1AW"}
    if multimon_bin is not None:
        overrides["dtmf.multimon_bin"] = multimon_bin
    ctrl = build_controller(
        resolve_settings(overrides), radio=MockRadio(), totp_secret=None, tts=StubTts()
    )
    return ctrl._dtmf  # noqa: SLF001 — asserting the wired decode path


# --- the resolver, in isolation -------------------------------------------------------------------

@pytest.mark.parametrize("present", [True, False])
def test_resolver_auto_is_native_regardless_of_the_binary(present, monkeypatch):
    # ADR 0060: `auto` -> `native` with the binary present AND absent — the flip that dropped the
    # `shutil.which` check. The reason names the bench result so `doctor` can report why.
    _set_multimon_present(monkeypatch, present)
    assert resolve_decode_mode("auto", "multimon-ng") == _AUTO_NATIVE


@pytest.mark.parametrize("mode", ["streaming", "buffered", "native"])
@pytest.mark.parametrize("present", [True, False])
def test_resolver_passes_explicit_modes_through_unchanged(mode, present, monkeypatch):
    _set_multimon_present(monkeypatch, present)  # explicit modes must ignore availability
    assert resolve_decode_mode(mode, "multimon-ng") == (mode, "")


# --- auto wiring through build_controller ---------------------------------------------------------

@pytest.mark.parametrize("present", [True, False])
def test_auto_wires_native_regardless_of_the_binary(present, monkeypatch):
    # The wiring consequence of the resolver flip: auto reaches the in-process Goertzel decoder whether
    # or not multimon-ng is on PATH — the binary's presence no longer routes anything (ADR 0060).
    dtmf = _build("auto", monkeypatch, present=present)
    assert isinstance(dtmf, StreamingDtmfInput)
    assert isinstance(dtmf._stream, GoertzelStream)  # noqa: SLF001


def test_auto_never_raises_even_with_no_binary(monkeypatch):
    # The old `streaming`-when-present default raised on first write with no binary; auto now always
    # decodes in-process, so it pumps clean regardless of a missing multimon-ng.
    auto = _build("auto", monkeypatch, present=False, multimon_bin=_MISSING_BIN)
    assert isinstance(auto._stream, GoertzelStream)  # noqa: SLF001
    auto.pump(synth_dtmf("1"), 0.0)  # does not raise — a working decoder with no external binary


# --- explicit modes are unaffected by availability ------------------------------------------------

def test_explicit_native_ignores_present_binary(monkeypatch):
    dtmf = _build("native", monkeypatch, present=True)
    assert isinstance(dtmf._stream, GoertzelStream)  # noqa: SLF001


def test_explicit_streaming_ignores_absent_binary(monkeypatch):
    # Type only — no pump, so no spawn: streaming stays streaming even with no binary.
    dtmf = _build("streaming", monkeypatch, present=False)
    assert isinstance(dtmf._stream, MultimonStream)  # noqa: SLF001


def test_explicit_streaming_without_binary_still_raises(monkeypatch):
    # An explicit mode is a contract, not a fallback: asking for multimon and not having it must fail
    # loud, not silently downgrade to native. The raise fires on the first write.
    dtmf = _build("streaming", monkeypatch, present=False, multimon_bin=_MISSING_BIN)
    with pytest.raises(RuntimeError, match="multimon-ng"):
        dtmf.pump(synth_dtmf("1"), 0.0)


# --- doctor reports the resolution and the reason -------------------------------------------------

@pytest.mark.parametrize("present", [True, False])
def test_doctor_prints_resolved_mode_and_reason(present, monkeypatch, capsys):
    # `auto` reports native with the same bench-verified reason whether or not the binary is present.
    _set_multimon_present(monkeypatch, present)
    monkeypatch.setattr(
        "radio_server.config.load_settings",
        lambda *a, **k: resolve_settings({"dtmf.decode_mode": "auto"}),
    )
    # No radio hardware in CI: fail the backend open fast, after the decode-mode line has printed.
    monkeypatch.setattr(doctor_mod, "_build_backend", lambda cfg: (_ for _ in ()).throw(RuntimeError("no hw")))
    doctor_mod._dtmf({}, 0)  # noqa: SLF001 — the CLI diagnostic entry
    assert "decode mode: auto -> native (bench-verified, ADR 0060)" in capsys.readouterr().out


def test_native_mode_is_accepted_by_config():
    assert resolve_settings({"dtmf.decode_mode": "auto"}).get("dtmf.decode_mode") == "auto"
