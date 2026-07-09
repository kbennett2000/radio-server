"""Tests for the station event log / QSO ledger (ADR 0018).

All model-free and deterministic: a `FakeClock` drives every timestamp/duration and `tmp_path`
gives a real file to append to and read back. The suite proves each event type's record, the
no-secrets rule, TX key-down duration, JSONL validity, fail-loud construction, and — the safety
guarantee — that a sink write error never propagates into the caller.
"""

import json

import pytest
from fastapi.testclient import TestClient

from radio_server.api import create_app
from radio_server.api.events import Event
from radio_server.backends.mock import MockRadio
from radio_server.eventlog import EventLog, JsonlSink

from .conftest import FakeClock

# --- helpers -------------------------------------------------------------------------------------


class RecordingSink:
    """In-memory `LogSink` double — captures records for assertion."""

    def __init__(self) -> None:
        self.records: list[dict] = []
        self.closed = False

    def write(self, record: dict) -> None:
        self.records.append(record)

    def close(self) -> None:
        self.closed = True


class ExplodingSink:
    """A `LogSink` whose every write raises — to prove failure isolation."""

    def write(self, record: dict) -> None:
        raise OSError("disk full")

    def close(self) -> None:
        pass


def _log(clock: FakeClock) -> tuple[EventLog, RecordingSink]:
    sink = RecordingSink()
    return EventLog(sink, clock=clock), sink


# --- record taxonomy: one test per event type ----------------------------------------------------


def test_ptt_key_up_records_tx_key_up(clock: FakeClock) -> None:
    log, sink = _log(clock)
    log.handle(Event(type="ptt", data={"on": True}))
    assert sink.records == [{"ts": clock.now, "type": "tx_key_up"}]


def test_ptt_key_down_records_duration_from_fakeclock(clock: FakeClock) -> None:
    log, sink = _log(clock)
    log.handle(Event(type="ptt", data={"on": True}))
    clock.advance(3.0)
    log.handle(Event(type="ptt", data={"on": False}))
    key_down = sink.records[-1]
    assert key_down["type"] == "tx_key_down"
    assert key_down["duration"] == 3.0
    assert key_down["ts"] == clock.now


def test_key_down_without_prior_key_up_has_null_duration(clock: FakeClock) -> None:
    log, sink = _log(clock)
    log.handle(Event(type="ptt", data={"on": False}))
    assert sink.records == [{"ts": clock.now, "type": "tx_key_down", "duration": None}]


def test_scan_active_records_frequency(clock: FakeClock) -> None:
    log, sink = _log(clock)
    log.handle(Event(type="scan", data={"phase": "active", "frequency": 146_520_000, "channel": None}))
    assert sink.records == [
        {"ts": clock.now, "type": "scan", "phase": "active", "frequency": 146_520_000}
    ]


def test_scan_scanning_phase_omits_absent_fields(clock: FakeClock) -> None:
    log, sink = _log(clock)
    log.handle(Event(type="scan", data={"phase": "scanning", "frequency": None, "channel": None}))
    assert sink.records == [{"ts": clock.now, "type": "scan", "phase": "scanning"}]


def test_session_open_records_session_open(clock: FakeClock) -> None:
    log, sink = _log(clock)
    log.handle(Event(type="session", data={"phase": "session_open"}))
    assert sink.records == [{"ts": clock.now, "type": "session_open"}]


def test_session_close_records_reason_and_signed_off(clock: FakeClock) -> None:
    log, sink = _log(clock)
    log.handle(Event(type="session", data={"phase": "session_close", "signed_off": True}))
    assert sink.records == [
        {"ts": clock.now, "type": "session_close", "signed_off": True}
    ]


def test_session_id_records_station_id(clock: FakeClock) -> None:
    log, sink = _log(clock)
    log.handle(Event(type="session", data={"phase": "id", "callsign": "N0CALL", "mode": "cw"}))
    assert sink.records == [
        {"ts": clock.now, "type": "station_id", "callsign": "N0CALL", "mode": "cw"}
    ]


def test_auth_accepted_records_auth_accepted(clock: FakeClock) -> None:
    log, sink = _log(clock)
    log.handle(Event(type="auth", data={"result": "accepted"}))
    assert sink.records == [{"ts": clock.now, "type": "auth_accepted"}]


def test_command_records_dispatched_service(clock: FakeClock) -> None:
    log, sink = _log(clock)
    log.handle(Event(type="command", data={"service": "time"}))
    assert sink.records == [{"ts": clock.now, "type": "command_dispatched", "service": "time"}]


def test_arbiter_records_mode(clock: FakeClock) -> None:
    log, sink = _log(clock)
    log.handle(Event(type="arbiter", data={"mode": "transmitting"}))
    assert sink.records == [{"ts": clock.now, "type": "arbiter_mode", "mode": "transmitting"}]


def test_status_events_are_not_logged(clock: FakeClock) -> None:
    log, sink = _log(clock)
    log.handle(Event(type="status", data={"transmitting": True, "busy": False}))
    assert sink.records == []


def test_unknown_event_type_is_not_logged(clock: FakeClock) -> None:
    log, sink = _log(clock)
    log.handle(Event(type="busy", data={"foo": "bar"}))
    assert sink.records == []


# --- SECURITY: no code material ever reaches the ledger ------------------------------------------


def test_rejected_auth_record_carries_no_code_material(clock: FakeClock) -> None:
    log, sink = _log(clock)
    # Even if a rejected-auth event were to carry the code/secret upstream, the whitelist mapper
    # must not copy it into the record.
    log.handle(
        Event(
            type="auth",
            data={"result": "rejected", "code": "123456", "secret": "JBSWY3DPEHPK3PXP"},
        )
    )
    (record,) = sink.records
    assert record == {"ts": clock.now, "type": "auth_rejected"}
    # Belt-and-suspenders: no digit/secret material anywhere in the serialized record.
    serialized = json.dumps(record)
    assert "123456" not in serialized
    assert "JBSWY3DPEHPK3PXP" not in serialized
    assert "code" not in record
    assert "secret" not in record


# --- JsonlSink: durable JSONL, fail-loud construction --------------------------------------------


def test_jsonl_sink_writes_one_valid_object_per_line(tmp_path, clock: FakeClock) -> None:
    path = tmp_path / "ledger.jsonl"
    log = EventLog(JsonlSink(path), clock=clock)
    log.handle(Event(type="ptt", data={"on": True}))
    clock.advance(2.0)
    log.handle(Event(type="ptt", data={"on": False}))
    log.handle(Event(type="session", data={"phase": "session_open"}))
    log.close()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    parsed = [json.loads(line) for line in lines]  # each line is a valid JSON object
    assert [r["type"] for r in parsed] == ["tx_key_up", "tx_key_down", "session_open"]
    assert parsed[1]["duration"] == 2.0


def test_jsonl_sink_fails_loud_on_unwritable_path_at_construction(tmp_path) -> None:
    # Parent directory does not exist → open("a") raises immediately, not one dropped record later.
    with pytest.raises(OSError):
        JsonlSink(tmp_path / "nope-dir" / "ledger.jsonl")


# --- failure isolation: a sink error never propagates --------------------------------------------


def test_sink_write_error_does_not_propagate(clock: FakeClock) -> None:
    log = EventLog(ExplodingSink(), clock=clock)
    # Must return normally — a logging fault can never reach the event pump or a transmission.
    log.handle(Event(type="ptt", data={"on": True}))
    log.handle(Event(type="session", data={"phase": "session_open"}))


# --- wiring: EventLog is a passive subscriber of the live hub ------------------------------------


def test_app_wiring_logs_ptt_events_to_file(tmp_path) -> None:
    """End-to-end: a REST /ptt round-trip lands as tx_key_up/tx_key_down in the ledger file."""
    log_path = tmp_path / "app-ledger.jsonl"
    event_log = EventLog(JsonlSink(log_path))
    app = create_app(MockRadio(), api_token="secret", event_log=event_log)
    auth = {"Authorization": "Bearer secret"}

    with TestClient(app) as client:
        # The drain task subscribed during lifespan startup.
        assert app.state.hub.subscriber_count == 1
        client.post("/ptt", json={"on": True}, headers=auth)
        client.post("/ptt", json={"on": False}, headers=auth)
    # Context exit runs lifespan shutdown: drain task cancelled, sink closed/flushed.
    assert app.state.hub.subscriber_count == 0

    types = [json.loads(line)["type"] for line in log_path.read_text().splitlines()]
    assert "tx_key_up" in types
    assert "tx_key_down" in types
