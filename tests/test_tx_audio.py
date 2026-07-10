"""TX audio ingest: the keying/ingest state machine, single-talker guard, format handshake, and
the wired `/audio/tx` WebSocket (ADR 0016).

The mirror of `test_rx_audio.py`, other direction: a client streams canonical PCM *in* and it
lands in `MockRadio.tx_log`. Two planes of proof:

- **WS integration** (`TestClient`, `?token=`): token gating, the declared-format handshake, ordered
  ingest into `tx_log`, single-talker refusal, and clean-close PTT drop. These never exercise the
  idle timeout (that would need a real timeout-length sleep).
- **Unit** (`FakeClock`, no asyncio, no WS): the `TxSession` keying/idle state machine, `TxSlot`,
  `parse_tx_format`, and the env loader. The idle-drop proof lives here — pure and clock-injected.

Keying discipline (guardrail 2) is proven with `_PttSpyRadio`, a `MockRadio` that records its
`ptt()` calls (the `_ScriptedRadio` spy idiom): the sequence `[True, False]` shows PTT asserted for
the stream then dropped at the end/idle, and PTT is never keyed via a CAT path.
"""

from __future__ import annotations

import re
import wave

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from radio_server.api import create_app
from radio_server.audio import CANONICAL_FORMAT, AudioFormatMismatch
from radio_server.backends import MockRadio
from radio_server.recording import Recorder
from radio_server.tx import (
    DEFAULT_TX_IDLE_TIMEOUT,
    TxSession,
    TxSlot,
    load_tx_idle_timeout,
    parse_tx_format,
)

from .conftest import FakeClock, make_settings

#: A transmitted-audio segment: tx-<6-digit sequence>-<UTC timestamp>.wav (ADR 0021).
TX_NAME_RE = re.compile(r"^tx-\d{6}-\d{8}T\d{6}Z\.wav$")

TOKEN = "test-lan-secret"

#: The canonical format-declaration header a well-behaved client opens the stream with.
CANONICAL_HEADER = {"rate": 48000, "width": 2, "channels": 1}


class _PttSpyRadio(MockRadio):
    """A MockRadio that records its `ptt()` calls, so tests can assert the keying sequence.

    MockRadio has no PTT history (ptt state is a single private bool that `transmit()` also
    toggles), so proving "keyed for the stream, then dropped" needs this spy — the `_ScriptedRadio`
    subclass idiom from `test_rx_audio.py`.
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.ptt_log: list[bool] = []

    def ptt(self, on: bool) -> None:
        self.ptt_log.append(on)
        super().ptt(on)


def _handshake(ws: object) -> dict:
    """Complete the format handshake on an accepted TX socket: declare canonical, read the ack."""
    ws.send_json(CANONICAL_HEADER)  # type: ignore[attr-defined]
    ack = ws.receive_json()  # type: ignore[attr-defined]
    assert ack["status"] == "ready"
    return ack


# --- WS integration: token gating ----------------------------------------------------------

def test_audio_tx_rejects_bad_token():
    radio = MockRadio()
    with TestClient(create_app(radio, api_token=TOKEN)) as client:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect("/audio/tx?token=nope") as ws:
                ws.receive_bytes()
        assert excinfo.value.code == 1008  # policy violation, rejected before accept
    assert radio.tx_log == []


def test_audio_tx_rejects_missing_token():
    radio = MockRadio()
    with TestClient(create_app(radio, api_token=TOKEN)) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/audio/tx") as ws:
                ws.receive_bytes()
    assert radio.tx_log == []


# --- WS integration: streaming + handshake -------------------------------------------------

def test_audio_tx_streams_frames_to_tx_log():
    radio = MockRadio()
    frames = [b"\x01\x02", b"\x03\x04\x05\x06", b"\x07\x08"]
    with TestClient(create_app(radio, api_token=TOKEN)) as client:
        with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws:
            _handshake(ws)
            for frame in frames:
                ws.send_bytes(frame)
    # Asserted after the socket (and its server task) has fully torn down, so every frame is in.
    assert [f.samples for f in radio.tx_log] == frames


def test_audio_tx_handshake_ack_reports_canonical_format():
    radio = MockRadio()
    with TestClient(create_app(radio, api_token=TOKEN)) as client:
        with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws:
            ack = _handshake(ws)
    assert ack["format"] == {"rate": 48000, "width": 2, "channels": 1}


def test_audio_tx_rejects_non_canonical_declared_format():
    radio = _PttSpyRadio()
    with TestClient(create_app(radio, api_token=TOKEN)) as client:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws:
                ws.send_json({"rate": 8000, "width": 2, "channels": 1})
                ws.receive_json()  # server closes 1003 instead of acking
        assert excinfo.value.code == 1003  # unsupported data
    assert radio.tx_log == []
    assert radio.ptt_log == []  # never keyed


def test_audio_tx_rejects_malformed_header():
    radio = MockRadio()
    with TestClient(create_app(radio, api_token=TOKEN)) as client:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws:
                ws.send_json({"rate": 48000})  # missing width/channels
                ws.receive_json()
        assert excinfo.value.code == 1003
    assert radio.tx_log == []


# --- WS integration: format validation on the frames ---------------------------------------

def test_audio_tx_partial_sample_first_frame_never_keys():
    radio = _PttSpyRadio()
    with TestClient(create_app(radio, api_token=TOKEN)) as client:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws:
                _handshake(ws)
                ws.send_bytes(b"\x01")  # odd length: not a whole 16-bit sample
                ws.receive_bytes()  # server closes 1003
        assert excinfo.value.code == 1003
    assert radio.tx_log == []
    assert radio.ptt_log == []  # validation runs before any ptt(): a bad frame never keys


def test_audio_tx_partial_sample_midstream_drops_ptt():
    radio = _PttSpyRadio()
    with TestClient(create_app(radio, api_token=TOKEN)) as client:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws:
                _handshake(ws)
                ws.send_bytes(b"\x01\x02")  # good: keys + transmits
                ws.send_bytes(b"\x01")  # odd: fails loud
                ws.receive_bytes()
        assert excinfo.value.code == 1003
    assert [f.samples for f in radio.tx_log] == [b"\x01\x02"]  # only the good frame landed
    assert radio.ptt_log == [True, False]  # keyed for the stream, dropped on the error


def test_audio_tx_skips_empty_frame():
    radio = _PttSpyRadio()
    with TestClient(create_app(radio, api_token=TOKEN)) as client:
        with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws:
            _handshake(ws)
            ws.send_bytes(b"")  # empty: carries no audio, skipped (mirrors RxPump)
            ws.send_bytes(b"\x09\x0a")  # real: keys + transmits
    assert [f.samples for f in radio.tx_log] == [b"\x09\x0a"]
    assert radio.ptt_log == [True, False]  # the empty frame did not key on its own


# --- WS integration: single-talker + clean close -------------------------------------------

def test_audio_tx_refuses_second_concurrent_client():
    radio = MockRadio()
    with TestClient(create_app(radio, api_token=TOKEN)) as client:
        with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws1:
            _handshake(ws1)
            ws1.send_bytes(b"\x01\x02")
            # A second talker while the first holds the slot is refused (can't key twice). The server
            # accepts, sends an explicit busy message (so a browser — which can't see a pre-accept
            # close code — learns why), then closes 1013.
            with pytest.raises(WebSocketDisconnect) as excinfo:
                with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws2:
                    assert ws2.receive_json() == {"status": "busy"}
                    ws2.receive_bytes()  # next read raises the 1013 close
            assert excinfo.value.code == 1013  # try again later (busy)
        # ws1 closed → slot released → a fresh client can connect and transmit.
        with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws3:
            _handshake(ws3)
            ws3.send_bytes(b"\x03\x04")
    assert [f.samples for f in radio.tx_log] == [b"\x01\x02", b"\x03\x04"]


def test_audio_tx_clean_close_drops_ptt():
    radio = _PttSpyRadio()
    with TestClient(create_app(radio, api_token=TOKEN)) as client:
        with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws:
            _handshake(ws)
            ws.send_bytes(b"\x01\x02")
            ws.send_bytes(b"\x03\x04")
    assert [f.samples for f in radio.tx_log] == [b"\x01\x02", b"\x03\x04"]
    assert radio.ptt_log == [True, False]  # keyed once at stream start, dropped on clean close


# --- Unit: TxSession keying + idle (FakeClock, no asyncio) ----------------------------------

def test_txsession_keys_and_logs_on_feed():
    radio = _PttSpyRadio()
    session = TxSession(radio, idle_timeout=2.0, clock=FakeClock())
    session.feed(b"\x01\x02")
    assert session.keyed is True
    assert radio.ptt_log == [True]  # keyed exactly once
    assert [f.samples for f in radio.tx_log] == [b"\x01\x02"]
    assert session.idle_elapsed() is False  # just stamped active


def test_txsession_keys_only_once_across_frames():
    radio = _PttSpyRadio()
    session = TxSession(radio, idle_timeout=2.0, clock=FakeClock())
    session.feed(b"\x01\x02")
    session.feed(b"\x03\x04")
    assert radio.ptt_log == [True]  # PTT held, not re-keyed per frame
    assert [f.samples for f in radio.tx_log] == [b"\x01\x02", b"\x03\x04"]


def test_txsession_on_key_fires_once_per_edge():
    # The streaming-TX ledger hook (ADR 0019): True on the key-up edge, False on key-down, once
    # each — regardless of how many frames feed in between.
    radio = _PttSpyRadio()
    keys: list[bool] = []
    session = TxSession(radio, idle_timeout=2.0, clock=FakeClock(), on_key=keys.append)
    session.feed(b"\x01\x02")
    session.feed(b"\x03\x04")
    assert keys == [True]  # keyed exactly once across frames
    session.close()
    assert keys == [True, False]


def test_txsession_on_key_silent_when_never_keyed():
    # close() on a stream that never keyed is a no-op — no spurious key-down reaches the ledger.
    radio = _PttSpyRadio()
    keys: list[bool] = []
    session = TxSession(radio, idle_timeout=2.0, clock=FakeClock(), on_key=keys.append)
    session.close()
    assert keys == []


def test_txsession_drops_ptt_after_idle_timeout():
    clock = FakeClock()
    radio = _PttSpyRadio()
    session = TxSession(radio, idle_timeout=2.0, clock=clock)
    session.feed(b"\x01\x02")
    assert session.idle_elapsed() is False
    clock.advance(2.0)  # the stream has gone silent for the full window
    assert session.idle_elapsed() is True
    assert session.on_idle() is True  # transport wakeup → drop PTT
    assert radio.ptt_log == [True, False]
    assert session.keyed is False
    assert session.on_idle() is False  # already dropped: a second wakeup is a no-op
    assert radio.ptt_log == [True, False]


def test_txsession_idle_holds_through_a_short_gap():
    clock = FakeClock()
    radio = _PttSpyRadio()
    session = TxSession(radio, idle_timeout=2.0, clock=clock)
    session.feed(b"\x01\x02")
    clock.advance(1.9)  # a gap shorter than the window
    assert session.idle_elapsed() is False
    assert session.on_idle() is False
    assert session.keyed is True  # still keyed through the gap


def test_txsession_close_idempotent_when_never_keyed():
    radio = _PttSpyRadio()
    session = TxSession(radio, idle_timeout=2.0, clock=FakeClock())
    session.close()
    session.close()
    assert radio.ptt_log == []  # no spurious ptt(False) when the stream never keyed
    assert session.keyed is False


def test_txsession_rejects_partial_sample():
    radio = _PttSpyRadio()
    session = TxSession(radio, idle_timeout=2.0, clock=FakeClock())
    with pytest.raises(AudioFormatMismatch):
        session.feed(b"\x01")  # odd length
    assert session.keyed is False
    assert radio.ptt_log == []  # validation precedes keying
    assert radio.tx_log == []


def test_txsession_skips_empty_payload():
    radio = _PttSpyRadio()
    session = TxSession(radio, idle_timeout=2.0, clock=FakeClock())
    session.feed(b"")
    assert session.keyed is False
    assert radio.ptt_log == []
    assert radio.tx_log == []
    session.feed(b"\x01\x02")  # a real frame after the skip keys normally
    assert session.keyed is True
    assert radio.ptt_log == [True]


# --- Unit: parse_tx_format -----------------------------------------------------------------

def test_parse_tx_format_canonical_passes():
    assert parse_tx_format({"rate": 48000, "width": 2, "channels": 1}) == CANONICAL_FORMAT


def test_parse_tx_format_non_canonical_raises():
    with pytest.raises(AudioFormatMismatch):
        parse_tx_format({"rate": 8000, "width": 2, "channels": 1})
    with pytest.raises(AudioFormatMismatch):
        parse_tx_format({"rate": 48000, "width": 1, "channels": 1})  # 8-bit
    with pytest.raises(AudioFormatMismatch):
        parse_tx_format({"rate": 48000, "width": 2, "channels": 2})  # stereo


def test_parse_tx_format_malformed_raises():
    with pytest.raises(AudioFormatMismatch):
        parse_tx_format({"rate": 48000, "width": 2})  # missing channels
    with pytest.raises(AudioFormatMismatch):
        parse_tx_format({"rate": "abc", "width": 2, "channels": 1})  # non-integer


# --- Unit: TxSlot + env loader -------------------------------------------------------------

def test_tx_slot_refuses_second_acquire():
    slot = TxSlot()
    assert slot.try_acquire() is True
    assert slot.occupied is True
    assert slot.try_acquire() is False  # occupied: refused, not queued
    slot.release()
    assert slot.occupied is False
    assert slot.try_acquire() is True  # freed → the next talker can claim it


def test_tx_slot_release_idempotent():
    slot = TxSlot()
    slot.release()  # release without acquire is safe (finally after refusal)
    assert slot.occupied is False


def test_load_tx_idle_timeout_default_when_unset():
    assert load_tx_idle_timeout(make_settings({})) == DEFAULT_TX_IDLE_TIMEOUT
    assert load_tx_idle_timeout(make_settings({"tx.idle_timeout": ""})) == DEFAULT_TX_IDLE_TIMEOUT


def test_load_tx_idle_timeout_parses_positive():
    assert load_tx_idle_timeout(make_settings({"tx.idle_timeout": 3.5})) == 3.5


def test_load_tx_idle_timeout_fails_loud():
    for bad in ("abc", 0, -1):
        with pytest.raises(RuntimeError):
            make_settings({"tx.idle_timeout": bad})


# --- TX recording (ADR 0021): the transmitted-audio tap on TxSession ------------------------


def _tx_wavs(tmp_path):
    return sorted(tmp_path.glob("*.wav"))


def _read_wav(path):
    with wave.open(str(path), "rb") as w:
        return w.getnchannels(), w.getsampwidth(), w.getframerate(), w.readframes(w.getnframes())


class _ExplodingRecorder:
    """A TX recorder whose every call raises — proves the session's guards isolate a disk fault so
    it can never break keying or leak the single-talker slot."""

    def write(self, pcm: bytes) -> None:
        raise OSError("disk on fire")

    def end_segment(self) -> None:
        raise OSError("disk on fire")


def test_txsession_records_fed_frames_to_a_tx_wav(tmp_path):
    radio = _PttSpyRadio()
    rec = Recorder(tmp_path, clock=FakeClock(), prefix="tx-")
    session = TxSession(radio, idle_timeout=2.0, clock=FakeClock(), recorder=rec)
    session.feed(b"\x01\x02")  # key-up: lazy-opens the tx- segment, writes the frame
    session.feed(b"\x03\x04")  # writes the next frame
    session.close()  # key-down: finalizes the segment

    files = _tx_wavs(tmp_path)
    assert len(files) == 1
    assert TX_NAME_RE.match(files[0].name), files[0].name
    assert files[0].name.startswith("tx-000001-")
    assert _read_wav(files[0]) == (1, 2, 48000, b"\x01\x02" + b"\x03\x04")


def test_txsession_recording_fault_never_breaks_keying_or_slot(tmp_path):
    # The whole point of guarding feed()/close(): a recorder that raises on every call must not stop
    # PTT keying or the arbiter/slot release. Frames still transmit and the keying sequence is intact.
    radio = _PttSpyRadio()
    session = TxSession(radio, idle_timeout=2.0, clock=FakeClock(), recorder=_ExplodingRecorder())
    session.feed(b"\x01\x02")  # must not raise despite recorder.write blowing up
    session.feed(b"\x03\x04")
    session.close()  # must not raise despite recorder.end_segment blowing up
    assert [f.samples for f in radio.tx_log] == [b"\x01\x02", b"\x03\x04"]
    assert radio.ptt_log == [True, False]  # keyed then dropped — recording fault fully isolated


def test_txsession_records_nothing_when_never_keyed(tmp_path):
    rec = Recorder(tmp_path, clock=FakeClock(), prefix="tx-")
    session = TxSession(_PttSpyRadio(), idle_timeout=2.0, clock=FakeClock(), recorder=rec)
    session.close()  # never fed → never keyed → no segment ever opened
    assert _tx_wavs(tmp_path) == []


def test_audio_tx_records_transmitted_stream_and_second_talker_does_not_corrupt(tmp_path):
    # End-to-end through the wired `/audio/tx` endpoint with a tx recorder. A second concurrent
    # talker is refused (1013) before its session is built, so the shared recorder is only ever fed
    # by one talker at a time — sequential talkers get their own clean, sequenced tx- files.
    radio = MockRadio()
    rec = Recorder(tmp_path, clock=FakeClock(), prefix="tx-")
    with TestClient(create_app(radio, api_token=TOKEN, tx_recorder=rec)) as client:
        with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws1:
            _handshake(ws1)
            ws1.send_bytes(b"\x01\x02")
            with pytest.raises(WebSocketDisconnect) as excinfo:
                with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws2:
                    assert ws2.receive_json() == {"status": "busy"}
                    ws2.receive_bytes()  # next read raises the 1013 close
            assert excinfo.value.code == 1013  # refused before a second session/recorder tap exists
        # ws1 closed → its tx- segment finalized → slot free for the next talker.
        with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws3:
            _handshake(ws3)
            ws3.send_bytes(b"\x03\x04")

    files = _tx_wavs(tmp_path)
    assert len(files) == 2  # one clean file per talker, no interleave
    assert all(TX_NAME_RE.match(f.name) for f in files), [f.name for f in files]
    assert _read_wav(files[0])[3] == b"\x01\x02"  # talker 1, uncorrupted
    assert _read_wav(files[1])[3] == b"\x03\x04"  # talker 3, its own sequenced file
