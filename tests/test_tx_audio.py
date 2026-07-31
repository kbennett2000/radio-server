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
from radio_server.arbiter import RadioArbiter, RadioMode
from radio_server.audio import CANONICAL_FORMAT, AudioFormatMismatch
from radio_server.backends import MockRadio, RadioUnavailable
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


def test_audio_tx_publishes_a_status_snapshot_after_the_over():
    """A `ptt` frame carries the keyed flag and nothing else, so fields written only BY an over —
    `pa`, ADR 0134 — would stay stale in the UI for exactly the operator who just held the button
    and heard nothing come back. Every REST route that keys already publishes one; this path did not.
    """
    radio = _PttSpyRadio()
    app = create_app(radio, api_token=TOKEN)
    with TestClient(app) as client:
        with client.websocket_connect(f"/events?token={TOKEN}") as events:
            assert events.receive_json()["type"] == "status"  # the on-connect snapshot
            with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws:
                _handshake(ws)
                ws.send_bytes(b"\x01\x02")
            # Drain until the post-over snapshot arrives (arbiter frames interleave with the ptt
            # edges, and their exact order is not what this test is about).
            seen = []
            while "status" not in seen and len(seen) < 8:
                seen.append(events.receive_json()["type"])
    assert seen[-1] == "status", seen           # the snapshot arrived...
    assert seen.count("ptt") == 2, seen         # ...after BOTH ptt edges, i.e. after the over
    assert seen.index("status") > seen.index("ptt"), seen


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


# --- streaming station ID (ADR 0041, Part 97) ----------------------------------------------
#
# The optional `station_id` seam identifies BOTH streaming TX sources (the browser `/audio/tx`
# talker and the Mumble bridge). ID audio is transmitted into the same keyed over as content: at
# key-up (first over of a fresh transmission), across the <=10-minute boundary, and at key-down
# (due-gated). `StubId` renders a deterministic `<id:CALL>` payload so `tx_log` is exactly
# assertable, and the whole thing is clock-injected — no real sleeps.

from radio_server.services import StreamingId
from radio_server.services.station_id import StubId

_ID = b"<id:AE9S>"


def _streaming_id(clock) -> StreamingId:
    return StreamingId(StubId(), "AE9S", interval=600.0, clock=clock)


def test_txsession_ids_on_key_up():
    radio = MockRadio()
    clock = FakeClock()
    session = TxSession(radio, idle_timeout=2.0, clock=clock, station_id=_streaming_id(clock))
    session.feed(b"\x01\x02")
    # First over of a fresh transmission carries the ID, prepended into the same keyed over.
    assert [f.samples for f in radio.tx_log] == [_ID, b"\x01\x02"]


def test_txsession_ids_once_per_over_within_interval():
    radio = MockRadio()
    clock = FakeClock()
    session = TxSession(radio, idle_timeout=2.0, clock=clock, station_id=_streaming_id(clock))
    session.feed(b"\x01\x02")  # ID + content
    session.feed(b"\x03\x04")  # still within the interval -> content only, no repeat ID
    assert [f.samples for f in radio.tx_log] == [_ID, b"\x01\x02", b"\x03\x04"]


def test_txsession_reids_across_the_interval_boundary():
    radio = MockRadio()
    clock = FakeClock()
    session = TxSession(radio, idle_timeout=2.0, clock=clock, station_id=_streaming_id(clock))
    session.feed(b"\x01\x02")  # ID + content, last_id = t0
    clock.advance(601)  # cross the 10-minute ceiling mid-transmission
    session.feed(b"\x03\x04")  # periodic re-ID + content
    assert [f.samples for f in radio.tx_log] == [_ID, b"\x01\x02", _ID, b"\x03\x04"]


def test_txsession_signs_off_when_overdue_on_close():
    radio = MockRadio()
    clock = FakeClock()
    session = TxSession(radio, idle_timeout=2.0, clock=clock, station_id=_streaming_id(clock))
    session.feed(b"\x01\x02")  # ID + content
    clock.advance(700)  # keyed long enough to be overdue at key-down
    session.close()
    assert [f.samples for f in radio.tx_log] == [_ID, b"\x01\x02", _ID]


def test_txsession_no_signoff_id_on_a_short_over():
    radio = MockRadio()
    clock = FakeClock()
    session = TxSession(radio, idle_timeout=2.0, clock=clock, station_id=_streaming_id(clock))
    session.feed(b"\x01\x02")  # ID at key-up
    clock.advance(3)  # short over: still well within the interval at key-down
    session.close()  # no closing ID (the key-up ID already identified within the window)
    assert [f.samples for f in radio.tx_log] == [_ID, b"\x01\x02"]


class _SignoffRaisingRadio(_PttSpyRadio):
    """A spy radio that transmits normally until armed, then raises on the next `transmit()` —
    models a backend that fails on the key-down sign-off ID (a wedged stream / torn-down audio
    device), so we can prove the failing ID does NOT skip the unkey (ADR 0092)."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.raise_next = False

    def transmit(self, frame: object) -> None:
        if self.raise_next:
            raise RuntimeError("backend wedged on sign-off transmit")
        super().transmit(frame)  # type: ignore[arg-type]


def test_txsession_close_drops_ptt_even_if_signoff_id_transmit_raises():
    # The unlink-didn't-drop-PTT field bug (ADR 0092): close() transmits a sign-off ID *before*
    # ptt(False). On a wedged backend that transmit raises — and it must NEVER skip the unkey. The
    # sign-off ID is best-effort; dropping PTT is the load-bearing safety work.
    radio = _SignoffRaisingRadio()
    clock = FakeClock()
    session = TxSession(radio, idle_timeout=2.0, clock=clock, station_id=_streaming_id(clock))
    session.feed(b"\x01\x02")  # keys (ptt True); key-up ID + content already out
    clock.advance(700)  # overdue → a sign-off ID is due at key-down
    radio.raise_next = True  # the sign-off transmit will raise
    session.close()  # must not propagate, must still unkey
    assert radio.ptt_log == [True, False]  # unkeyed despite the failing sign-off ID
    assert not radio.status().transmitting


def test_txsession_without_station_id_is_unidentified():
    # The default (no `station_id`) is byte-identical to the historical un-ID'd streaming behaviour.
    radio = MockRadio()
    session = TxSession(radio, idle_timeout=2.0, clock=FakeClock())
    session.feed(b"\x01\x02")
    session.close()
    assert [f.samples for f in radio.tx_log] == [b"\x01\x02"]


def test_audio_tx_ws_is_identified_when_a_station_id_is_wired():
    # The create_app-level wiring (ADR 0041): a shared `StreamingId` passed to `create_app` is fed to
    # the `/audio/tx` TxSession, so the browser talker's first over carries the station ID — closing
    # the pre-existing gap where streaming TX went out un-ID'd.
    radio = MockRadio()
    sid = StreamingId(StubId(), "AE9S", interval=600.0)
    with TestClient(create_app(radio, api_token=TOKEN, station_id=sid)) as client:
        with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws:
            _handshake(ws)
            ws.send_bytes(b"\x01\x02")
    assert [f.samples for f in radio.tx_log] == [_ID, b"\x01\x02"]


def test_txsession_touch_refreshes_idle_without_transmitting():
    # ADR 0106: touch() keeps a keyed session alive when its upstream STREAM is alive but the
    # current content is below the gate (a talker's pause) — no transmit, no key, just the stamp.
    clock = FakeClock()
    radio = _PttSpyRadio()
    session = TxSession(radio, idle_timeout=2.0, clock=clock)
    session.feed(b"\x01\x02")
    tx_count = len(radio.tx_log)
    clock.advance(1.5)
    session.touch()  # frames still arriving upstream, nothing worth transmitting
    clock.advance(1.5)  # 3.0 s since feed, 1.5 s since touch
    assert session.idle_elapsed() is False  # the touch moved the deadline
    assert len(radio.tx_log) == tx_count  # touch transmitted nothing
    clock.advance(0.5)
    assert session.idle_elapsed() is True  # and expires normally afterwards
    session.close()


def test_txsession_touch_on_an_unkeyed_session_is_a_noop():
    # touch() must never key up or arm an idle deadline on a session that never transmitted.
    clock = FakeClock()
    radio = _PttSpyRadio()
    session = TxSession(radio, idle_timeout=2.0, clock=clock)
    session.touch()
    assert session.keyed is False and radio.ptt_log == []
    assert session.idle_elapsed() is False
    clock.advance(10.0)
    assert session.idle_elapsed() is False


# --- ADR 0151: a raising key-up must not strand the arbiter ---------------------------------
#
# ADR 0150 gave `AiocBaofeng._key_on` a refusal: in AM the radio will not key its own PTT path, so
# `ptt(True)` now RAISES rather than asserting the line into silence. That is the first time a
# key-up on the deployed station could routinely fail, and it exposed the bug below: `feed` claimed
# the arbiter and only then keyed, so a raising `ptt(True)` left the arbiter latched TRANSMITTING
# with `_keyed` still False — and `close()` guards its release on `_keyed`, so nothing ever gave the
# radio back. The RX pump and the scan runner consult that latch and stand down, permanently.


class _RefusingRadio(_PttSpyRadio):
    """A spy radio whose `ptt(True)` raises — the shape ADR 0150's AM refusal put on the key path.

    `RadioUnavailable` specifically, because that is what `AiocBaofeng._refuse_if_tx_disabled`
    raises; the unwind must not depend on the exception type, but testing the real one keeps the
    proof honest. `ptt(False)` still works (the unkey path is unconditional, ADR 0093), so a
    session that DID key can always be torn down.
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.refuse = True

    def ptt(self, on: bool) -> None:
        if on and self.refuse:
            # The message AiocBaofeng actually produces, so a test failure reads like the field.
            raise RadioUnavailable(
                "the radio is demodulating AM and refuses its own PTT path "
                "(VFO_STATE_TX_DISABLE). Set modulation FM to transmit."
            )
        super().ptt(on)


def test_txsession_releases_the_arbiter_when_key_up_raises():
    # THE BUG. `feed` acquires the arbiter, then keys. A raising key-up must give the radio back
    # before propagating, or the latch is held for the life of the process.
    arbiter = RadioArbiter()
    radio = _RefusingRadio()
    session = TxSession(radio, idle_timeout=2.0, clock=FakeClock(), arbiter=arbiter)
    with pytest.raises(RadioUnavailable):
        session.feed(b"\x01\x02")
    assert arbiter.transmitting is False  # <-- the whole cycle
    assert arbiter.mode is RadioMode.IDLE
    assert session.keyed is False
    assert radio.tx_log == []  # nothing went out on a key-up that never happened


def test_txsession_gives_the_radio_back_when_the_station_cannot_hear_itself():
    # ADR 0158, and it reuses this whole harness rather than adding to it. The refusal is the
    # SERVER'S interlock reading its own status block — no overridden `ptt` — so unlike the class
    # above this is red on master, where the mock keys a deaf station happily.
    #
    # What is proved is that ADR 0151's unwind is cause-agnostic: a second standing refusal arrives
    # in the same window and the arbiter still comes back. That is the payoff for routing through
    # the existing path instead of building a second refusal mechanism beside it.
    arbiter = RadioArbiter()
    radio = _PttSpyRadio(left_in_broadcast_fm=True)
    session = TxSession(radio, idle_timeout=2.0, clock=FakeClock(), arbiter=arbiter)
    with pytest.raises(RadioUnavailable, match="second receiver"):
        session.feed(b"\x01\x02")
    assert arbiter.transmitting is False
    assert arbiter.mode is RadioMode.IDLE
    assert session.keyed is False
    assert radio.tx_log == []
    # The spy records the CALL, which did happen — the refusal is raised from inside `ptt()`, which
    # is the point: the gate lives in the radio, not in the session, so `TxSession` needed no change.
    assert radio.ptt_log == [True]
    assert radio.status().transmitting is False   # and the line never went high


def test_txsession_close_after_a_refused_key_up_stays_a_noop():
    # The unwind must not leave `close()` with work to do: a session that never keyed must still
    # emit no spurious `ptt(False)`, and a second release must not fire a bogus mode transition.
    seen: list[RadioMode] = []
    arbiter = RadioArbiter(on_change=seen.append)
    radio = _RefusingRadio()
    session = TxSession(radio, idle_timeout=2.0, clock=FakeClock(), arbiter=arbiter)
    with pytest.raises(RadioUnavailable):
        session.feed(b"\x01\x02")
    session.close()
    assert radio.ptt_log == []  # the line was never asserted, so it is never dropped
    # transmitting -> idle exactly once: the unwind's release, not the close's.
    assert seen == [RadioMode.TRANSMITTING, RadioMode.IDLE]


def test_arbiter_is_reusable_after_a_refused_key_up():
    # Released, not merely reset: the very next key-up on the same arbiter must succeed. Under the
    # bug this raises ArbiterStateError ("already transmitting") — the radio is unusable until
    # restart even after the fault clears.
    arbiter = RadioArbiter()
    radio = _RefusingRadio()
    session = TxSession(radio, idle_timeout=2.0, clock=FakeClock(), arbiter=arbiter)
    with pytest.raises(RadioUnavailable):
        session.feed(b"\x01\x02")
    radio.refuse = False  # the operator sets the radio back to FM
    session.feed(b"\x03\x04")
    assert session.keyed is True
    assert arbiter.transmitting is True
    assert [f.samples for f in radio.tx_log] == [b"\x03\x04"]
    session.close()
    assert arbiter.transmitting is False


def test_txsession_unwinds_a_keyed_over_when_the_key_up_id_raises():
    # The second window: `ptt(True)` SUCCEEDED, so the line is up — and then the key-up station ID
    # transmit fails. `key_up_id` only returns audio when an ID is DUE, so carrying on would put an
    # un-ID'd transmission on the air (guardrail 5). Undo the whole key-up instead.
    clock = FakeClock()
    radio = _SignoffRaisingRadio()
    arbiter = RadioArbiter()
    keys: list[bool] = []
    session = TxSession(
        radio,
        idle_timeout=2.0,
        clock=clock,
        arbiter=arbiter,
        on_key=keys.append,
        station_id=_streaming_id(clock),
    )
    radio.raise_next = True  # the key-up ID transmit will fail
    with pytest.raises(RuntimeError):
        session.feed(b"\x01\x02")
    assert radio.ptt_log == [True, False]  # keyed, then unwound — not left keyed
    assert arbiter.transmitting is False
    assert session.keyed is False
    # The ledger computes TX duration from the key-up/key-down pair (eventlog/log.py), so an
    # unwound key-up must still emit its key-down or the record is unclosable.
    assert keys == [True, False]


def test_audio_tx_ws_releases_the_arbiter_when_key_up_raises():
    # Call site 1 of 3: the browser talker (`/audio/tx`). A refused key-up must find nothing left to
    # unwind in the `finally`, because `feed` already gave the radio back.
    radio = _RefusingRadio()
    with TestClient(create_app(radio, api_token=TOKEN)) as client:
        with pytest.raises(Exception):
            with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws:
                _handshake(ws)
                ws.send_bytes(b"\x01\x02")
                ws.receive_bytes()  # park until the server tears the socket down
        assert client.app.state.arbiter.transmitting is False
        assert client.app.state.arbiter.mode is RadioMode.IDLE
    assert radio.tx_log == []


def test_a_refused_key_up_tells_the_browser_why_before_it_closes():
    """**The consumer the reasons were written for, and the one that never saw them** (ADR 0161).

    Every refusal in this arc reaches the 503 body, the `tx_failed` event and both bridges' logs.
    None of it reached the browser: `session.feed` raising `RadioUnavailable` was caught by nothing
    here — the handler's `except` names only `WebSocketDisconnect` and `CancelledError` — so the
    socket simply died and `useTxAudio` classified it as "Transmit connection dropped." An operator
    holding Talk on a station that cannot hear itself was told the network had a problem.

    The mechanism is not new: this endpoint already sends an explicit `{"status": "busy"}` text
    message before closing, precisely because a browser cannot read a close code. A refusal gets the
    same treatment, and carries the reason verbatim so the sentence the backend wrote is the sentence
    the operator reads.
    """
    radio = _RefusingRadio()
    with TestClient(create_app(radio, api_token=TOKEN)) as client:
        with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws:
            _handshake(ws)
            ws.send_bytes(b"\x01\x02")
            msg = ws.receive_json()
        assert msg["status"] == "refused"
        assert "demodulating AM" in msg["reason"]
        # The `finally` still ran: PTT dropped (idempotent) and the slot freed for the next talker.
        assert client.app.state.arbiter.transmitting is False
        assert client.app.state.arbiter.mode is RadioMode.IDLE
    assert radio.tx_log == []


def test_a_deaf_station_gets_the_same_treatment_on_the_socket():
    """Not a second mechanism — the same one, reached by the other cause. That is the point of
    catching `RadioUnavailable` rather than any particular refusal: the AM gate, the broadcast-FM
    gate and a clear that could not be checked all arrive here, and all three are diagnosable to the
    operator instead of collapsing into one dropped connection."""
    radio = _PttSpyRadio(left_in_broadcast_fm=True)
    with TestClient(create_app(radio, api_token=TOKEN)) as client:
        with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws:
            _handshake(ws)
            ws.send_bytes(b"\x01\x02")
            msg = ws.receive_json()
        assert msg["status"] == "refused"
        assert "second receiver" in msg["reason"]
    assert radio.tx_log == []
