"""End-to-end: the deferred log events now reach the ledger file (ADR 0019).

Cycle 17 built the full ledger taxonomy but left ~half of it dead — nothing published `auth`,
`command`, or `arbiter` events, and the `station_id` record carried no callsign/mode. This is the
proof that the log is **no longer half-blind**: a real login → command → forced-ID → streaming-TX
round-trip, driven through `create_app` with a live `EventLog(JsonlSink(...))`, and the resulting
JSONL file must contain **every** taxonomy type — and, as ever, no code/secret/token material.

Everything is mock + `FakeClock`: a scripted `FakeDtmfDecoder` supplies the DTMF overs, the
controller is stepped by hand for deterministic timing, and the streaming TX socket keys the shared
arbiter (transmitting → idle) and fires the ptt edges.
"""

import json

from fastapi.testclient import TestClient

from radio_server.api import create_app
from radio_server.api.events import Event
from radio_server.audio import synth_dtmf
from radio_server.backends import MockRadio, RadioUnavailable
from radio_server.controller import (
    ControllerRunner,
    build_controller,
)
from radio_server.eventlog import EventLog, JsonlSink
from radio_server.services import DEFAULT_ID_INTERVAL, StubTts

from .conftest import TEST_SECRET, FakeClock, make_settings
from .test_dtmf import FakeDtmfDecoder

CALLSIGN = "AE9S"
TOKEN = "test-lan-secret"
RX = synth_dtmf("1")  # a received "over"; the fake decoder ignores its content
CANONICAL_HEADER = {"rate": 48000, "width": 2, "channels": 1}


def test_full_taxonomy_reaches_the_ledger_and_leaks_no_secrets(tmp_path, clock, code_for):
    good = code_for(clock.now)
    bad = "000000" if good != "000000" else "111111"
    settings = make_settings(
        {
            "station.callsign": CALLSIGN,
            # ID interval (600) < session timeout (700), so a +600 advance forces an ID *before* the
            # inactivity close — the periodic-ID window the lifecycle test uses.
            "controller.session_timeout": 700,
            # A tiny DTMF window so each `RX` frame decodes on its own step (ADR 0030); dedup off
            # because the fake decoder returns whole pre-formed entries per call.
            "dtmf.buffer_seconds": 0.02,
        }
    )
    radio = MockRadio()
    decoder = FakeDtmfDecoder([bad + "#", good + "#", "02#"])
    ctrl = build_controller(
        settings,
        radio=radio,
        totp_secret=TEST_SECRET,
        decoder=decoder,
        tts=StubTts(),
        clock=clock,
        dedup=False,
    )
    runner = ControllerRunner(radio, ctrl, clock=clock, poll=0.01)  # present, never started

    log_path = tmp_path / "qso.jsonl"
    event_log = EventLog(JsonlSink(log_path))
    app = create_app(
        radio, api_token=TOKEN, controller=ctrl, runner=runner, event_log=event_log
    )

    with TestClient(app) as client:
        assert app.state.hub.subscriber_count == 1  # the ledger drain task subscribed
        # Controller-driven: a bad code, then a good login, then an authed service command.
        ctrl.step(clock.now, RX)              # bad  -> auth_rejected
        ctrl.step(clock.now, RX)              # good -> auth_accepted + session_open
        ctrl.step(clock.now, RX)              # "02" -> command_dispatched (time)
        clock.advance(DEFAULT_ID_INTERVAL)    # +600: periodic ID overdue, still within 700s
        ctrl.step(clock.now)                  # forced -> station_id (callsign + mode)
        # Streaming TX keys the shared arbiter (idle -> transmitting -> idle) and fires the ptt
        # edges -> tx_key_up / tx_key_down / arbiter_mode.
        with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws:
            ws.send_json(CANONICAL_HEADER)
            assert ws.receive_json()["status"] == "ready"
            ws.send_bytes(b"\x01\x02")
    # Context exit runs lifespan shutdown: the drain flushes every queued record, then closes.
    assert app.state.hub.subscriber_count == 0

    text = log_path.read_text(encoding="utf-8")
    records = [json.loads(line) for line in text.splitlines()]
    types = [r["type"] for r in records]

    # Every taxonomy type the cycle set out to light up is present — the log is not half-blind.
    for expected in (
        "auth_rejected",
        "auth_accepted",
        "session_open",
        "command_dispatched",
        "station_id",
        "tx_key_up",
        "tx_key_down",
        "arbiter_mode",
    ):
        assert expected in types, f"{expected} missing from ledger: {types}"

    # The records carry their identifying fields.
    command = next(r for r in records if r["type"] == "command_dispatched")
    assert command["service"] == "time"
    station_id = next(r for r in records if r["type"] == "station_id")
    assert station_id["callsign"] == CALLSIGN and station_id["mode"] == "cw"
    assert {r["mode"] for r in records if r["type"] == "arbiter_mode"} >= {"transmitting"}
    # Ordering invariants that are deterministic regardless of drain interleaving.
    assert types.index("auth_rejected") < types.index("auth_accepted")
    assert types.index("tx_key_up") < types.index("tx_key_down")

    # SECURITY (guardrail 4): now that auth is a *live* producer, prove no code/secret/token
    # material ever reaches the file.
    assert good not in text
    assert bad not in text
    assert TEST_SECRET not in text
    assert TOKEN not in text


class _RefusingRadio(MockRadio):
    """A radio whose `transmit()` raises — ADR 0150's AM refusal, reached through `StationId`."""

    REASON = "the radio is demodulating AM and refuses its own PTT path"

    def transmit(self, frame):
        raise RadioUnavailable(self.REASON)


def test_a_station_that_cannot_identify_lands_in_the_ledger(tmp_path, clock, code_for):
    """ADR 0151, end to end: controller -> hub adapter -> ledger file, with no app.py change.

    The claim being pinned is that `_publish_controller`'s catch-all branch already carries a new
    controller phase onto the hub as `Event(type="session", ...)`, so `tx_failed` needed no API
    wiring of its own. If someone later replaces that branch with an explicit whitelist, this goes
    red rather than the record quietly vanishing — which is the exact failure mode the record
    exists to prevent.
    """
    good = code_for(clock.now)
    settings = make_settings(
        {
            "station.callsign": CALLSIGN,
            "dtmf.buffer_seconds": 0.02,
            "controller.login_announcement": "",
        }
    )
    radio = _RefusingRadio()
    path = tmp_path / "events.jsonl"
    log = EventLog(JsonlSink(path))
    ctrl = build_controller(
        settings,
        radio=radio,
        totp_secret=TEST_SECRET,
        decoder=FakeDtmfDecoder([good + "#", "01#"]),
        tts=StubTts(),
        clock=clock,
        dedup=False,
    )
    with TestClient(
        create_app(radio, api_token=TOKEN, controller=ctrl, event_log=log)
    ):
        ctrl.step(clock.now, RX)   # login
        ctrl.step(clock.now, RX)   # 01# — the station ID refuses to key

    records = [json.loads(line) for line in path.read_text().splitlines()]
    failures = [r for r in records if r["type"] == "tx_failed"]
    assert [r["what"] for r in failures] == ["station_id"]
    assert failures[0]["reason"] == _RefusingRadio.REASON
    # And crucially: no `station_id` record was written for an ID that never went on the air.
    assert [r for r in records if r["type"] == "station_id"] == []


def test_the_ledger_drain_is_never_the_subscriber_the_hub_gives_up_on(tmp_path):
    """ADR 0180: the ledger keeps DROP_OLDEST, and this proves the WIRING and not just the source.

    `tests/test_event_hub_bound.py` pins the text of the call; this pins what `create_app` actually
    built. The distinction matters because the failure it guards against is silent: a ledger drain
    given the `/events` policy would be unsubscribed the first time it fell behind, and the Part 97
    operating log would simply stop — no error, no gap marker, no way back, because this subscriber
    has no snapshot to reconnect to.
    """
    log = EventLog(JsonlSink(tmp_path / "qso.jsonl"))
    app = create_app(MockRadio(), api_token=TOKEN, event_log=log)

    with TestClient(app):
        hub = app.state.hub
        assert hub.subscriber_count == 1  # the drain task
        # Far past the bound, in one synchronous burst the drain task never gets to run inside.
        for _ in range(hub.stats()["queue_maxsize"] * 3):
            hub.publish(Event(type="ptt", data={"on": False}))

        assert hub.subscriber_count == 1, "the ledger's drain was dropped for falling behind"
        assert hub.stats()["dropped_subscribers"] == 0
        assert hub.stats()["dropped_deliveries"] > 0  # it lost events, and says how many
