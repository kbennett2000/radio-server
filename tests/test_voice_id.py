"""VoiceId: the phonetic map, spoken station ID over a TtsEngine, and RADIO_ID_MODE (ADR 0010).

Three layers are proven separately, mirroring how voice_id.py is built:

- The *phonetic map* (`PHONETIC`, `spell_callsign`) is pure and exactly assertable with no TTS
  engine — "AE9S" spells "alpha echo niner sierra", and an unknown character fails loud.
- `VoiceId` on `StubTts` is byte-exact (the stub is deterministic), so the encoder, its
  protocol conformance, and the end-to-end auth -> dispatch -> voice-ID path all assert
  precisely, the same way the CW tests do.
- `RADIO_ID_MODE` selection (`load_id_mode`, `build_id_encoder`) is proven model-free: CW is
  the default, voice mode is selectable with an injected `StubTts`, and voice mode with no
  configured voice fails loud rather than silently falling back to CW.

The one real-engine test needs piper + onnxruntime + a voice model; it is `skipif`-gated on
all three and skips cleanly where any is absent (guardrail 1). Its output is
property-asserted (format, nonzero, plausible duration), never byte-asserted.
"""

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
    IdEncoder,
    PiperTts,
    ServiceContext,
    ServiceRegistry,
    StationId,
    StubTts,
    VoiceId,
    build_id_encoder,
    format_spoken_time,
    load_id_mode,
    load_tts_voice,
    TIME_DIGIT,
    TIME_NAME,
    time_service,
    spell_callsign,
)

from .conftest import make_settings

TZ = ZoneInfo("UTC")
CALLSIGN = "AE9S"


def _samples(frame: AudioFrame) -> np.ndarray:
    return np.frombuffer(frame.samples, dtype="<i2")


# --- Phonetic map: pure, engine-free, exactly assertable ---------------------------------

def test_spell_callsign_spells_nato_phonetics_with_niner():
    assert spell_callsign("AE9S") == "alpha echo niner sierra"


def test_spell_callsign_upper_cases_input():
    assert spell_callsign("ae9s") == "alpha echo niner sierra"


def test_spell_callsign_handles_the_portable_slash():
    assert spell_callsign("W1AW/9") == "whiskey one alpha whiskey slash niner"


def test_spell_callsign_unknown_char_fails_loud():
    with pytest.raises(ValueError):
        spell_callsign("A#")


# --- VoiceId on StubTts: deterministic, byte-exact ---------------------------------------

def test_voiceid_speaks_the_spelled_callsign():
    # StubTts is deterministic, so the encoder output is byte-exact.
    assert VoiceId(StubTts()).encode("AE9S") == AudioFrame(b"<audio:alpha echo niner sierra>")


def test_voiceid_output_is_canonical_format():
    assert VoiceId(StubTts()).encode(CALLSIGN).format == CANONICAL_FORMAT


def test_voiceid_honors_explicit_format_argument():
    assert VoiceId(StubTts()).encode(CALLSIGN, CANONICAL_FORMAT) == VoiceId(StubTts()).encode(
        CALLSIGN
    )


def test_voiceid_propagates_unknown_char():
    with pytest.raises(ValueError):
        VoiceId(StubTts()).encode("A#")


def test_voiceid_satisfies_the_encoder_protocol():
    assert isinstance(VoiceId(StubTts()), IdEncoder)


# --- RADIO_ID_MODE selection (model-free) ------------------------------------------------

def test_load_id_mode_defaults_to_cw():
    assert load_id_mode(make_settings({})) == "cw"


def test_load_id_mode_reads_voice():
    assert load_id_mode(make_settings({"station.id_mode": "voice"})) == "voice"


def test_load_id_mode_is_case_insensitive():
    assert load_id_mode(make_settings({"station.id_mode": "VOICE"})) == "voice"


def test_load_id_mode_unknown_fails_loud():
    with pytest.raises(RuntimeError):
        make_settings({"station.id_mode": "morse"})


def test_build_id_encoder_defaults_to_cw():
    assert isinstance(build_id_encoder(make_settings({})), CwId)


def test_build_id_encoder_selects_voice():
    encoder = build_id_encoder(make_settings({"station.id_mode": "voice"}), tts=StubTts())
    assert isinstance(encoder, VoiceId)


def test_build_id_encoder_voice_without_voice_fails_loud_no_cw_fallback():
    # id_mode=voice but no tts.voice and no injected engine: surface the PiperTts load failure
    # rather than silently degrading to CW.
    settings = make_settings({"station.id_mode": "voice"})  # tts.voice absent
    with pytest.raises(RuntimeError):
        build_id_encoder(settings)


# --- End-to-end: authed '1' with voice ID mode on StubTts, exactly assertable ------------

def test_authed_one_prepends_voice_id(verifier, clock, code_for):
    radio = MockRadio()
    registry = ServiceRegistry()
    registry.register(TIME_DIGIT, TIME_NAME, time_service(TZ))
    ctx = ServiceContext(clock=clock, tts=StubTts())
    # The ID encoder comes from id_mode=voice, proving the selection path end-to-end.
    encoder = build_id_encoder(make_settings({"station.id_mode": "voice"}), tts=StubTts())
    station = StationId(radio, encoder, CALLSIGN, clock=clock)
    dispatcher = Dispatcher(station, ctx, registry)
    gate = AuthGate(verifier, timeout=120.0, clock=clock, dispatch=dispatcher)
    session = Session()

    assert gate.on_dtmf(code_for(clock.now), session).kind is OutcomeKind.ACCEPTED
    assert gate.on_dtmf("1", session).kind is OutcomeKind.COMMAND

    expected_id = VoiceId(StubTts()).encode(CALLSIGN)
    expected_time = StubTts().render(format_spoken_time(clock.now, TZ))
    assert radio.tx_log == [expected_id + expected_time]


# --- Real engine (needs piper + onnxruntime + a voice model) -----------------------------

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
    reason="piper/onnxruntime/voice model absent; real voice ID is a hardware/installed-build check",
)


@_PIPER_SKIP
def test_real_piper_voice_id_renders_canonical_nonzero_speech():
    settings = make_settings({"tts.voice": os.environ[RADIO_TTS_VOICE_ENV_VAR]})
    frame = VoiceId(PiperTts(load_tts_voice(settings))).encode(CALLSIGN)
    assert frame.format == CANONICAL_FORMAT
    samples = _samples(frame)
    assert samples.size > round(48000 * 0.15)  # > ~150 ms of audio
    assert np.abs(samples).max() > 0
