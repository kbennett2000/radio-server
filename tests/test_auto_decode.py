"""`dtmf.decode_mode = auto` — resolve the decoder by multimon-ng availability (ADR 0055).

`auto` is the default. It picks `streaming` when the multimon-ng binary is on PATH (RF-verified, ADR
0038), else `native` (in-process, no binary, ADR 0054), so a box without multimon-ng — native Windows,
or a Linux box that never ran `apt install multimon-ng` — still decodes instead of crashing on the
first RX write. Availability is injected here via `shutil.which` (no `skipif`), so these run on every
machine regardless of whether multimon-ng is actually installed.
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

def test_resolver_auto_prefers_streaming_when_binary_present(monkeypatch):
    _set_multimon_present(monkeypatch, True)
    assert resolve_decode_mode("auto", "multimon-ng") == ("streaming", "multimon-ng found")


def test_resolver_auto_falls_back_to_native_when_binary_absent(monkeypatch):
    _set_multimon_present(monkeypatch, False)
    assert resolve_decode_mode("auto", "multimon-ng") == ("native", "no multimon-ng on PATH")


@pytest.mark.parametrize("mode", ["streaming", "buffered", "native"])
@pytest.mark.parametrize("present", [True, False])
def test_resolver_passes_explicit_modes_through_unchanged(mode, present, monkeypatch):
    _set_multimon_present(monkeypatch, present)  # explicit modes must ignore availability
    assert resolve_decode_mode(mode, "multimon-ng") == (mode, "")


# --- auto wiring through build_controller ---------------------------------------------------------

def test_auto_wires_streaming_when_multimon_present(monkeypatch):
    dtmf = _build("auto", monkeypatch, present=True)
    assert isinstance(dtmf, StreamingDtmfInput)
    assert isinstance(dtmf._stream, MultimonStream)  # noqa: SLF001


def test_auto_wires_native_when_multimon_absent(monkeypatch):
    dtmf = _build("auto", monkeypatch, present=False)
    assert isinstance(dtmf, StreamingDtmfInput)
    assert isinstance(dtmf._stream, GoertzelStream)  # noqa: SLF001


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
    # loud (the pre-0055 behaviour), not silently downgrade. The raise fires on the first write.
    dtmf = _build("streaming", monkeypatch, present=False, multimon_bin=_MISSING_BIN)
    with pytest.raises(RuntimeError, match="multimon-ng"):
        dtmf.pump(synth_dtmf("1"), 0.0)


# --- guardrail 1: auto changes behaviour ONLY where the old default raised ------------------------

def test_auto_matches_old_streaming_default_when_binary_present(monkeypatch):
    # With multimon-ng present, auto and the old 'streaming' default wire the identical decoder —
    # no behaviour change in the state that already worked.
    auto = _build("auto", monkeypatch, present=True)
    streaming = _build("streaming", monkeypatch, present=True)
    assert type(auto._stream) is type(streaming._stream) is MultimonStream  # noqa: SLF001


def test_auto_diverges_only_in_the_previously_raising_state(monkeypatch):
    # With NO binary: the old default ('streaming') raises on first write; auto instead decodes via
    # the in-process Goertzel path. That divergence is confined to the state that previously crashed.
    streaming = _build("streaming", monkeypatch, present=False, multimon_bin=_MISSING_BIN)
    with pytest.raises(RuntimeError):
        streaming.pump(synth_dtmf("1"), 0.0)

    auto = _build("auto", monkeypatch, present=False)
    assert isinstance(auto._stream, GoertzelStream)  # noqa: SLF001
    auto.pump(synth_dtmf("1"), 0.0)  # does not raise — a working decoder where there was none


# --- doctor reports the resolution and the reason -------------------------------------------------

@pytest.mark.parametrize(
    "present, expected",
    [
        (True, "decode mode: auto -> streaming (multimon-ng found)"),
        (False, "decode mode: auto -> native (no multimon-ng on PATH)"),
    ],
)
def test_doctor_prints_resolved_mode_and_reason(present, expected, monkeypatch, capsys):
    _set_multimon_present(monkeypatch, present)
    monkeypatch.setattr(
        "radio_server.config.load_settings",
        lambda *a, **k: resolve_settings({"dtmf.decode_mode": "auto"}),
    )
    # No radio hardware in CI: fail the backend open fast, after the decode-mode line has printed.
    monkeypatch.setattr(doctor_mod, "_build_backend", lambda cfg: (_ for _ in ()).throw(RuntimeError("no hw")))
    doctor_mod._dtmf({}, 0)  # noqa: SLF001 — the CLI diagnostic entry
    assert expected in capsys.readouterr().out


def test_native_mode_is_accepted_by_config():
    assert resolve_settings({"dtmf.decode_mode": "auto"}).get("dtmf.decode_mode") == "auto"
