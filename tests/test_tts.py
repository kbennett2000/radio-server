"""TTS engines: the deterministic `StubTts` baseline and real `PiperTts` (ADR 0009).

`StubTts` stays byte-exact and text-keyed, so the cycle-7 end-to-end still asserts precisely
what was spoken. `PiperTts` is proven at three layers without a voice model or the piper
package installed:

- **Config fail-loud** — a missing `RADIO_TTS_VOICE`, a missing `.onnx`, or a missing/invalid
  sidecar all raise at load, never a silent no-op.
- **The `to_canonical` playback edge** — a voice's native-rate PCM resamples up to canonical
  48k regardless of that rate, driven with a *synthetic* buffer at a fake rate (16000, 22050)
  so the edge — the load-bearing point of this cycle — is exercised model-free.
- **`StubTts` baseline** — retained and deterministic (the tests below it).

The two real-engine tests need piper + onnxruntime + an actual voice model; they are
`skipif`-gated on all three and skip cleanly where any is absent (guardrail 1: the piper
build and neural intelligibility are hardware/installed-build checks). Neural output is
property-asserted (format, plausible duration, nonzero), never byte-asserted.
"""

import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from radio_server.audio import CANONICAL_FORMAT, AudioFrame
from radio_server.auth import AuthGate, OutcomeKind, Session
from radio_server.backends import MockRadio
from radio_server.services import (
    RADIO_TTS_VOICE_ENV_VAR,
    CwId,
    Dispatcher,
    PiperTts,
    ServiceContext,
    ServiceRegistry,
    StationId,
    StubTts,
    TtsEngine,
    load_tts_voice,
    register,
)

from .conftest import make_settings

TZ = ZoneInfo("UTC")
CALLSIGN = "AE9S"


# --- StubTts: deterministic, text-keyed audio so tx_log is assertable -------------------

def test_render_is_deterministic_for_equal_text():
    tts = StubTts()
    assert tts.render("The time is 14:26 UTC") == tts.render("The time is 14:26 UTC")


def test_render_embeds_the_text():
    assert StubTts().render("hello") == AudioFrame(b"<audio:hello>")


def test_render_returns_a_canonical_format_frame():
    assert StubTts().render("hi").format == CANONICAL_FORMAT


def test_different_text_renders_differently():
    tts = StubTts()
    assert tts.render("one") != tts.render("two")


def test_stub_satisfies_the_engine_protocol():
    assert isinstance(StubTts(), TtsEngine)


# --- PiperTts helpers -------------------------------------------------------------------

def _samples(frame: AudioFrame) -> np.ndarray:
    return np.frombuffer(frame.samples, dtype="<i2")


def _sine_pcm(rate: int, ms: float, freq: float = 440.0) -> bytes:
    """A synthetic native-rate int16 mono buffer standing in for piper's raw output."""
    n = round(rate * ms / 1000.0)
    t = np.arange(n)
    sig = 0.3 * np.sin(2.0 * np.pi * freq * t / rate)
    return np.rint(sig * 32767).astype("<i2").tobytes()


def _write_voice(dir_path, rate: int) -> str:
    """Create a dummy `.onnx` + a real `.onnx.json` sidecar declaring `rate`; return the path.

    The `.onnx` is not a real model — construction only checks it exists; the rate is read
    from the sidecar. That is exactly what lets the resample edge be tested with no model.
    """
    onnx = Path(dir_path) / "voice.onnx"
    onnx.write_bytes(b"not-a-real-onnx-model")
    sidecar = Path(dir_path) / "voice.onnx.json"
    sidecar.write_text(json.dumps({"audio": {"sample_rate": rate}}))
    return str(onnx)


class _FakeRatePiperTts(PiperTts):
    """`PiperTts` with the piper seam replaced by a fixed synthetic buffer.

    Overriding only `_synthesize_raw` exercises the real `render` → `to_canonical` path
    (using the rate read from the sidecar) with no piper install and no model."""

    def __init__(self, voice_path: str, raw: bytes) -> None:
        super().__init__(voice_path)
        self._raw = raw

    def _synthesize_raw(self, text: str) -> bytes:
        return self._raw


# --- PiperTts config fail-loud (no piper, no model) -------------------------------------

def test_load_tts_voice_unset_fails_loud():
    with pytest.raises(RuntimeError):
        load_tts_voice(make_settings({}))


def test_load_tts_voice_returns_configured_path():
    assert load_tts_voice(make_settings({"tts.voice": "/some/voice.onnx"})) == "/some/voice.onnx"


def test_missing_voice_model_fails_loud_at_load(tmp_path):
    with pytest.raises(RuntimeError):
        PiperTts(str(tmp_path / "nope.onnx"))


def test_missing_sidecar_fails_loud_at_load(tmp_path):
    onnx = tmp_path / "voice.onnx"
    onnx.write_bytes(b"x")  # model present, but no .onnx.json beside it
    with pytest.raises(RuntimeError):
        PiperTts(str(onnx))


def test_invalid_sidecar_rate_fails_loud(tmp_path):
    onnx = tmp_path / "voice.onnx"
    onnx.write_bytes(b"x")
    (tmp_path / "voice.onnx.json").write_text(json.dumps({"audio": {}}))  # no sample_rate
    with pytest.raises(RuntimeError):
        PiperTts(str(onnx))


# --- PiperTts rate read + the to_canonical playback edge (fake voice-rate, model-free) --

def test_reads_native_rate_from_sidecar(tmp_path):
    # The rate comes from config, never hardcoded to 22050.
    assert PiperTts(_write_voice(tmp_path, 16000))._rate == 16000
    assert PiperTts(_write_voice(tmp_path, 22050))._rate == 22050


def test_non_22050_voice_resamples_to_canonical_48k(tmp_path):
    voice = _write_voice(tmp_path, 16000)
    raw = _sine_pcm(16000, 300)  # 300 ms at the voice's native rate
    frame = _FakeRatePiperTts(voice, raw).render("The time is 14:26 UTC")
    assert frame.format == CANONICAL_FORMAT
    expected = round(48000 * 300 / 1000.0)  # 14400 samples once at canonical rate
    assert abs(_samples(frame).size - expected) <= 64
    assert np.abs(_samples(frame)).max() > 0  # real content, not silence


def test_22050_voice_resamples_to_canonical_48k(tmp_path):
    voice = _write_voice(tmp_path, 22050)
    frame = _FakeRatePiperTts(voice, _sine_pcm(22050, 200)).render("hi")
    assert frame.format == CANONICAL_FORMAT
    assert abs(_samples(frame).size - round(48000 * 200 / 1000.0)) <= 64


def test_piper_satisfies_the_engine_protocol(tmp_path):
    assert isinstance(PiperTts(_write_voice(tmp_path, 16000)), TtsEngine)


# --- PiperTts real engine (needs piper + onnxruntime + a voice model) -------------------

def _piper_ready() -> bool:
    voice = os.environ.get(RADIO_TTS_VOICE_ENV_VAR)
    if not voice or not Path(voice).is_file():
        return False
    try:
        import onnxruntime  # noqa: F401
        import piper  # noqa: F401
    except ImportError:
        return False
    return True


_PIPER_SKIP = pytest.mark.skipif(
    not _piper_ready(),
    reason="piper/onnxruntime/voice model absent; real TTS is a hardware/installed-build check",
)


@_PIPER_SKIP
def test_real_piper_renders_canonical_nonzero_speech():
    settings = make_settings({"tts.voice": os.environ[RADIO_TTS_VOICE_ENV_VAR]})
    frame = PiperTts(load_tts_voice(settings)).render("The time is 14 26 UTC")
    assert frame.format == CANONICAL_FORMAT
    samples = _samples(frame)
    assert samples.size > round(48000 * 0.15)  # > ~150 ms of audio
    assert np.abs(samples).max() > 0


@_PIPER_SKIP
def test_real_piper_wired_into_time_service_prepends_cw_id(verifier, clock, code_for):
    radio = MockRadio()
    registry = ServiceRegistry()
    register(registry, TZ)
    ctx = ServiceContext(
        clock=clock,
        tts=PiperTts(load_tts_voice(make_settings({"tts.voice": os.environ[RADIO_TTS_VOICE_ENV_VAR]}))),
    )
    station = StationId(radio, CwId(), CALLSIGN, clock=clock)  # ID stays CW this cycle
    dispatcher = Dispatcher(station, ctx, registry)
    gate = AuthGate(verifier, timeout=120.0, clock=clock, dispatch=dispatcher)
    session = Session()

    assert gate.on_dtmf(code_for(clock.now), session).kind is OutcomeKind.ACCEPTED
    assert gate.on_dtmf("1", session).kind is OutcomeKind.COMMAND

    # Structure asserted (not the speech bytes): one over, canonical, CW ID prepended
    # ahead of the real spoken time.
    assert len(radio.tx_log) == 1
    frame = radio.tx_log[0]
    assert frame.format == CANONICAL_FORMAT
    cw = CwId().encode(CALLSIGN)
    assert frame.samples.startswith(cw.samples)
    assert len(frame.samples) > len(cw.samples)
