"""Audio recording: received audio → timestamped WAV segments (ADR 0020).

Software-first, no hardware (guardrail 1): scripted `MockRadio` RX frames, `tmp_path`, and the
`FakeClock` from conftest drive the whole recorder end to end. Two instruments, matching
`test_rx_audio.py`:

- **Recorder unit tests** drive `Recorder` directly and read the WAV back with `wave.open(..., "rb")`
  to prove the canonical header + sample data.
- **Pump-integration tests** run the real `RxPump` over a self-terminating scripted radio (the
  `test_rx_audio.py` `_ScriptedRadio` pattern, `asyncio.run`, no pytest-asyncio) with a `Recorder`
  tapped in, proving gated segmentation, one-file-per-session, and failure isolation.

The load-bearing proofs: a valid WAV with the canonical 48k/s16le/mono header; gated mode records
only live audio and one file per gate-open→gate-close session; an unwritable path fails loud at
construction; a write fault never propagates into the pump/stream; and recording off writes nothing.
"""

from __future__ import annotations

import asyncio
import re
import wave

import pytest

from radio_server.audio import AudioFrame
from radio_server.backends import MockRadio
from radio_server.recording import (
    DEFAULT_RECORD_PATH,
    RecordMode,
    Recorder,
    build_recorder,
    load_record_enabled,
    load_record_mode,
    load_record_path,
)
from radio_server.recording import recorder as recorder_module
from radio_server.rx import AudioHub, RxPump, null_recorder

# A filename shaped rx-<6-digit sequence>-<UTC timestamp>.wav (the FakeClock base 1_000_000.0 →
# 1970-01-12T13:46:40Z, so the stamp is deterministic).
NAME_RE = re.compile(r"^rx-\d{6}-\d{8}T\d{6}Z\.wav$")

LIVE1 = AudioFrame(b"\x01\x02")
LIVE2 = AudioFrame(b"\x03\x04")
LIVE3 = AudioFrame(b"\x05\x06")
SILENT = AudioFrame(b"\x00\x00")  # non-empty, but the test gate scores it as gate-closed


def _wavs(tmp_path):
    """Return the WAV segments in a directory, in lexical (== chronological) order."""
    return sorted(tmp_path.glob("*.wav"))


def _read_wav(path):
    """Return (nchannels, sampwidth, framerate, pcm_bytes) for a WAV file."""
    with wave.open(str(path), "rb") as w:
        return w.getnchannels(), w.getsampwidth(), w.getframerate(), w.readframes(w.getnframes())


# --- Recorder unit tests -------------------------------------------------------------------


def test_write_produces_a_valid_wav_with_canonical_header_and_data(tmp_path, clock):
    rec = Recorder(tmp_path, clock=clock)
    rec.write(LIVE1.samples)
    rec.write(LIVE2.samples)
    rec.end_segment()

    files = _wavs(tmp_path)
    assert len(files) == 1
    nchannels, sampwidth, framerate, pcm = _read_wav(files[0])
    assert (nchannels, sampwidth, framerate) == (1, 2, 48000)  # canonical 48k/s16le/mono
    assert pcm == LIVE1.samples + LIVE2.samples  # frames concatenated in order


def test_each_activity_session_is_its_own_timestamped_sequenced_file(tmp_path, clock):
    rec = Recorder(tmp_path, clock=clock)
    rec.write(LIVE1.samples)
    rec.end_segment()
    rec.write(LIVE2.samples)
    rec.end_segment()

    files = _wavs(tmp_path)
    assert len(files) == 2
    assert all(NAME_RE.match(f.name) for f in files), [f.name for f in files]
    # The sequence counter guarantees unique names and lexical == chronological order.
    assert files[0].name.startswith("rx-000001-")
    assert files[1].name.startswith("rx-000002-")
    assert _read_wav(files[0])[3] == LIVE1.samples
    assert _read_wav(files[1])[3] == LIVE2.samples


def test_unwritable_path_fails_loud_at_construction(tmp_path, clock):
    # A record path that is an existing regular file cannot be a directory: makedirs raises.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    with pytest.raises(OSError):
        Recorder(blocker, clock=clock)


def test_write_fault_is_swallowed_and_writes_no_file(tmp_path, clock, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(recorder_module.wave, "open", boom)
    rec = Recorder(tmp_path, clock=clock)  # constructed before the fault is injected
    rec.write(LIVE1.samples)  # must not raise
    rec.end_segment()  # idempotent no-op — nothing was opened
    assert _wavs(tmp_path) == []


def test_end_segment_is_idempotent_when_nothing_open(tmp_path, clock):
    rec = Recorder(tmp_path, clock=clock)
    rec.end_segment()  # no-op
    rec.close()  # no-op
    assert _wavs(tmp_path) == []


# --- pump-integration: a Recorder tapped into the real RxPump ------------------------------


class _ScriptedRadio(MockRadio):
    """A MockRadio that signals when its scripted RX sequence is exhausted, so a pump loop over it
    terminates deterministically (the `test_rx_audio.py` pattern)."""

    def __init__(self, frames: list[AudioFrame]) -> None:
        super().__init__(rx_frames=frames)
        self._remaining = len(frames)
        self.drained = asyncio.Event()

    def receive(self) -> AudioFrame:
        frame = super().receive()
        if self._remaining > 0:
            self._remaining -= 1
            if self._remaining == 0:
                self.drained.set()
        return frame


async def _pump_record(frames, *, gate, recorder) -> list[bytes]:
    """Run a pump (with `recorder` tapped) over `frames` until the radio drains; return the frames
    that reached the hub (so we can assert the live stream is unaffected)."""
    radio = _ScriptedRadio(frames)
    hub = AudioHub()
    queue = hub.subscribe()
    pump = RxPump(radio, hub, poll=0, gate=gate, recorder=recorder)
    pump.start()
    await radio.drained.wait()
    await asyncio.sleep(0)  # let the pump publish/record the final frame before we stop it
    await pump.stop()  # stop() -> the pump's finally finalizes any open segment
    out: list[bytes] = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


def test_gated_records_only_live_audio_one_file_per_session(tmp_path, clock):
    rec = Recorder(tmp_path, clock=clock)
    # The gate closes on the silent sentinel: [live, live | close | live] -> two segments.
    gate = lambda frame: frame.samples != SILENT.samples  # noqa: E731
    out = asyncio.run(
        _pump_record([LIVE1, LIVE2, SILENT, LIVE3], gate=gate, recorder=rec)
    )
    # The hub streamed only the live frames (the gate suppressed the silent one).
    assert out == [LIVE1.samples, LIVE2.samples, LIVE3.samples]

    files = _wavs(tmp_path)
    assert len(files) == 2  # one WAV per gate-open→gate-close activity session
    assert _read_wav(files[0])[3] == LIVE1.samples + LIVE2.samples  # first session
    assert _read_wav(files[1])[3] == LIVE3.samples  # second session — no dead air between


def test_reject_all_gate_writes_no_file(tmp_path, clock):
    rec = Recorder(tmp_path, clock=clock)
    out = asyncio.run(
        _pump_record([LIVE1, LIVE2], gate=lambda frame: False, recorder=rec)
    )
    assert out == []  # nothing streamed
    assert _wavs(tmp_path) == []  # lazy creation: a never-opened segment writes nothing


def test_pump_stop_finalizes_the_open_segment(tmp_path, clock):
    rec = Recorder(tmp_path, clock=clock)
    # Pass-through gate never closes, so the session finalizes only when the pump stops.
    out = asyncio.run(
        _pump_record([LIVE1, LIVE2], gate=lambda frame: True, recorder=rec)
    )
    assert out == [LIVE1.samples, LIVE2.samples]
    files = _wavs(tmp_path)
    assert len(files) == 1
    # The header is patched and readable — the finally-block end_segment finalized it.
    assert _read_wav(files[0]) == (1, 2, 48000, LIVE1.samples + LIVE2.samples)


class _ExplodingRecorder:
    """A recorder whose every call raises — proves the pump's guards isolate a recording fault."""

    def write(self, pcm: bytes) -> None:
        raise OSError("disk on fire")

    def end_segment(self) -> None:
        raise OSError("disk on fire")


def test_recording_fault_never_breaks_the_pump_or_stream():
    # Even with a recorder that raises on every frame and at finalization, the hub still receives
    # every live frame — mirrors the ExplodingSink / EventLog.handle isolation proof.
    out = asyncio.run(
        _pump_record(
            [LIVE1, LIVE2], gate=lambda frame: True, recorder=_ExplodingRecorder()
        )
    )
    assert out == [LIVE1.samples, LIVE2.samples]


def test_default_pump_records_nothing(tmp_path):
    # A pump built with the default null_recorder over live frames streams normally but writes
    # nothing to disk — recording is opt-in.
    out = asyncio.run(
        _pump_record([LIVE1, LIVE2], gate=lambda frame: True, recorder=null_recorder)
    )
    assert out == [LIVE1.samples, LIVE2.samples]
    assert _wavs(tmp_path) == []


# --- config loaders ------------------------------------------------------------------------


def test_load_record_enabled_defaults_off():
    assert load_record_enabled({}) is False


@pytest.mark.parametrize("value", ["on", "1", "true", "yes", "ON", "True"])
def test_load_record_enabled_truthy(value):
    assert load_record_enabled({"RADIO_RECORD": value}) is True


@pytest.mark.parametrize("value", ["off", "0", "false", "no", ""])
def test_load_record_enabled_falsey(value):
    assert load_record_enabled({"RADIO_RECORD": value}) is False


def test_load_record_enabled_invalid_raises():
    with pytest.raises(RuntimeError):
        load_record_enabled({"RADIO_RECORD": "maybe"})


def test_load_record_path_default_and_override():
    assert load_record_path({}) == DEFAULT_RECORD_PATH
    assert load_record_path({"RADIO_RECORD_PATH": "/srv/rec"}) == "/srv/rec"


def test_load_record_mode_default_and_override():
    assert load_record_mode({}) is RecordMode.GATED
    assert load_record_mode({"RADIO_RECORD_MODE": "full"}) is RecordMode.FULL


def test_load_record_mode_invalid_raises():
    with pytest.raises(RuntimeError):
        load_record_mode({"RADIO_RECORD_MODE": "sometimes"})


def test_build_recorder_off_returns_none():
    assert build_recorder({}) is None


def test_build_recorder_full_mode_not_implemented(tmp_path):
    with pytest.raises(NotImplementedError):
        build_recorder(
            {
                "RADIO_RECORD": "on",
                "RADIO_RECORD_MODE": "full",
                "RADIO_RECORD_PATH": str(tmp_path),
            }
        )


def test_build_recorder_on_returns_recorder(tmp_path, clock):
    rec = build_recorder(
        {"RADIO_RECORD": "on", "RADIO_RECORD_PATH": str(tmp_path)}, clock=clock
    )
    assert isinstance(rec, Recorder)
    rec.write(LIVE1.samples)
    rec.close()
    assert len(_wavs(tmp_path)) == 1
