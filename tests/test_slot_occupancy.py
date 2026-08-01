"""A held talk slot is visible, and a leaked one is diagnosable (ADR 0170).

ADR 0167 closed the strand and **carried its own finding**: nothing anywhere reported slot state, so
a leak and a busy station rendered identically — no holder, no timestamp, no counter, no event, and
`"busy"` reserved in `EVENT_TYPES` but never published. This file is that finding turned into
assertions.

The load-bearing field is the **timestamp**, not the flag. "The transmitter is held" is ambiguous
between an operator mid-over and a socket that died in March; "held by `browser` for 4 h 20 m" is a
diagnosis, and it is the only form of the fact an operator can act on.

Two kinds of number live in `/status` and this file keeps them apart on purpose — see
`test_rx_demand_is_named_for_what_it_measures` for the measurement that forces the split.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from radio_server.api import create_app
from radio_server.audio import AudioFrame
from radio_server.backends import MockRadio
from radio_server.dstar import MockGatewayClient
from radio_server.link import MockMumbleClient, resolve_mumble_entries
from radio_server.tx import TxSlot
from radio_server.vocoder.base import AMBE_BYTES_PER_FRAME, PCM_FORMAT

TOKEN = "test-lan-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
HEADER = {"rate": 48000, "width": 2, "channels": 1}


class _FakeVocoder:
    def encode(self, frame: AudioFrame) -> bytes:
        return bytes(AMBE_BYTES_PER_FRAME)

    def decode(self, ambe: bytes) -> AudioFrame:
        return AudioFrame(b"\x00\x00" * 160, PCM_FORMAT)

    def close(self) -> None:
        pass


def _mumble_app(**kwargs):
    entries = resolve_mumble_entries([{"name": "home", "host": "mumble.example", "dtmf": "13"}])
    return create_app(
        MockRadio(),
        api_token=TOKEN,
        mumble_entries=entries,
        mumble_client_factory=lambda entry: MockMumbleClient(host=entry.host, peers=3),
        **kwargs,
    )


def _dstar_app(**kwargs):
    return create_app(
        MockRadio(),
        api_token=TOKEN,
        dstar_gateway_factory=MockGatewayClient,
        dstar_vocoder_factory=_FakeVocoder,
        dstar_callsign="AE9S",
        dstar_module="A",
        **kwargs,
    )


def _slots(client) -> dict:
    return client.get("/status", headers=AUTH).json()["slots"]


# --- the surface itself ----------------------------------------------------------------------


def test_status_reports_every_talk_slot_free_at_rest():
    # The three slots are named individually, not collapsed into one "somebody is talking" flag: the
    # RF slot, the Mumble slot and the D-STAR slot block different things and a stuck one is a
    # different repair each time. Unconfigured subsystems are `null`, the `_link_state` convention.
    with TestClient(create_app(MockRadio(), api_token=TOKEN)) as client:
        slots = _slots(client)
        assert slots["tx"] == {
            "held": False,
            "holder": None,
            "since": None,
            "held_s": None,
            "stale_after_s": 180.0,
            "refused": {},
        }
        # Neither Mumble nor D-STAR is configured here, so there is no slot to report. `null` and
        # "free" are different facts and must not render alike (the ADR 0162 tri-state discipline).
        assert slots["mumble"] is None
        assert slots["dstar"] is None


def test_status_names_the_holder_and_when_it_was_claimed():
    # The whole cycle in one assertion: who holds it, and since when.
    radio = MockRadio()
    with TestClient(create_app(radio, api_token=TOKEN)) as client:
        before = time.time()
        with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws:
            ws.send_json(HEADER)
            assert ws.receive_json()["status"] == "ready"
            ws.send_bytes(b"\x01\x02")
            tx = _slots(client)["tx"]
            assert tx["held"] is True
            assert tx["holder"] == "browser"
            assert tx["held_s"] >= 0.0
            # `since` is wall-clock so it renders as a time of day; it is DERIVED from the monotonic
            # age rather than stored, so an NTP step moves the displayed clock time and never the
            # age the diagnosis rests on.
            assert before - 1.0 <= tx["since"] <= time.time() + 1.0
        # Released: back to the at-rest shape, and `since`/`held_s` clear with it — a stale timestamp
        # beside `held: false` is exactly the "reads like a measurement, is a leftover" trap.
        assert _slots(client)["tx"] == {
            "held": False,
            "holder": None,
            "since": None,
            "held_s": None,
            "stale_after_s": 180.0,
            "refused": {},
        }


def test_mumble_talk_slot_is_reported_separately_from_the_rf_slot():
    # A Mumble talker blocks Mumble talk and nothing else (it keys no RF). One "busy" flag covering
    # both would send an operator to the radio for a fault that is in the link.
    with TestClient(_mumble_app()) as client:
        client.post("/link", headers=AUTH, json={"entry": "home", "on": True})
        with client.websocket_connect(f"/audio/mumble/tx?token={TOKEN}") as ws:
            ws.send_json(HEADER)
            assert ws.receive_json()["status"] == "ready"
            slots = _slots(client)
            assert slots["mumble"]["held"] is True
            assert slots["mumble"]["holder"] == "browser"
            assert slots["tx"]["held"] is False  # the RF transmitter is untouched
        assert _slots(client)["mumble"]["held"] is False


def test_dstar_talk_slot_is_reported_separately_too():
    with TestClient(_dstar_app()) as client:
        client.post("/dstar/link", headers=AUTH, json={"reflector": "REF001 C"})
        with client.websocket_connect(f"/audio/dstar/tx?token={TOKEN}") as ws:
            ws.send_json(HEADER)
            assert ws.receive_json()["status"] == "ready"
            slots = _slots(client)
            assert slots["dstar"]["held"] is True
            assert slots["dstar"]["holder"] == "browser"
            assert slots["tx"]["held"] is False
        assert _slots(client)["dstar"]["held"] is False


# --- the counters ----------------------------------------------------------------------------


def test_a_refused_talker_is_counted_under_its_own_name():
    # ADR 0085's counter shape, with ADR 0153's rule about WHICH counter. The RF slot has three
    # claimants and one of them (the Mumble relay) refuses at frame rate, so a single flat integer
    # would let a relay storm bury the one browser refusal an operator is trying to explain. The
    # count is therefore keyed by who was refused.
    with TestClient(create_app(MockRadio(), api_token=TOKEN)) as client:
        with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws1:
            ws1.send_json(HEADER)
            ws1.receive_json()
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws2:
                    assert ws2.receive_json()["status"] == "busy"
                    ws2.receive_bytes()
        assert _slots(client)["tx"]["refused"] == {"browser": 1}


def test_relay_refusals_do_not_land_in_the_browser_talkers_count():
    # The unit form of the same rule, without needing a live bridge: two claimants, two keys.
    slot = TxSlot()
    assert slot.try_acquire("mumble-relay") is True
    assert slot.try_acquire("browser") is False
    for _ in range(40):
        assert slot.try_acquire("mumble-relay") is False
    assert slot.refused == {"browser": 1, "mumble-relay": 40}
    # And the holder is the one that actually got it, not the last one to ask.
    assert slot.holder == "mumble-relay"


def test_releasing_clears_the_holder_but_keeps_the_counts():
    # Occupancy is live state; refusals are a ledger. Clearing the ledger on release would erase the
    # evidence of the contention an operator came to /status to understand.
    slot = TxSlot()
    slot.try_acquire("browser")
    slot.try_acquire("dstar-relay")
    slot.release()
    assert slot.holder is None and slot.held_s is None and slot.occupied is False
    assert slot.refused == {"dstar-relay": 1}


# --- the reserved event, published at last -----------------------------------------------------


def test_a_refused_talker_publishes_the_busy_event():
    # `"busy"` has sat in EVENT_TYPES as a reserved-but-never-published name since ADR 0011. A
    # refusal is the edge it was reserved for: this is finishing a surface, not inventing one.
    with TestClient(create_app(MockRadio(), api_token=TOKEN)) as client:
        with client.websocket_connect(f"/events?token={TOKEN}") as events:
            events.receive_json()  # the initial status snapshot
            with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws1:
                ws1.send_json(HEADER)
                ws1.receive_json()
                with pytest.raises(WebSocketDisconnect):
                    with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws2:
                        ws2.receive_json()
                        ws2.receive_bytes()
            # A sentinel the server is CERTAIN to publish, read until it arrives. `receive_json` on
            # the sync TestClient blocks forever on an empty stream, so a test that just reads N
            # frames hoping for one hangs instead of failing when the event is missing — and a red
            # run you have to kill is not a red run.
            client.post("/ptt", headers=AUTH, json={"on": False})
            seen = []
            while True:
                frame = events.receive_json()
                seen.append(frame)
                if frame["type"] == "ptt":
                    break
            busy = next((f for f in seen if f["type"] == "busy"), None)
            assert busy is not None, f"the refusal published no `busy` event; saw {[f['type'] for f in seen]}"
            # Carrying the holder and the age is the point: an event that says only "refused" leaves
            # the operator exactly where ADR 0167 left them.
            assert busy["data"]["slot"] == "tx"
            assert busy["data"]["holder"] == "browser"
            assert busy["data"]["held_s"] >= 0.0


def test_the_refused_talker_is_told_who_holds_it():
    # The browser cannot read a close code, so anything it needs comes in the text message before the
    # close (the ADR 0161 mechanism). "Another operator is transmitting" is FALSE during a leak; the
    # message now carries the facts that let the client say something true instead.
    with TestClient(create_app(MockRadio(), api_token=TOKEN)) as client:
        with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws1:
            ws1.send_json(HEADER)
            ws1.receive_json()
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws2:
                    msg = ws2.receive_json()
                    assert msg["status"] == "busy"
                    assert msg["holder"] == "browser"
                    assert msg["held_s"] >= 0.0
                    assert msg["stale_after_s"] == 180.0
                    ws2.receive_bytes()


# --- the number that is NOT liveness -----------------------------------------------------------


def test_rx_demand_is_named_for_what_it_measures():
    """`requested`, never `listeners` or `active` — and the name is the fix, not a doc note.

    MEASURED (ADR 0170), real uvicorn + the deployed `squelch = "audio"` gate: `/audio/rx` parks on
    `queue.get()` and only ever *sends*, so it learns a client is gone from the next `send_bytes`.
    With a VAD gate a quiet channel publishes nothing, so a cleanly-closed listener stays counted
    until somebody next transmits — and an RST'd one (yanked wifi) was still counted 50 s later,
    past uvicorn's 20 s keepalive, and stayed counted through a signal that woke every other reader.

    So this number counts **requests for received audio**, not proven-live listeners, and no note in
    `api.md` reaches the UI that renders it. The label is the only thing that travels with the value.
    """
    with TestClient(create_app(MockRadio(), api_token=TOKEN)) as client:
        body = client.get("/status", headers=AUTH).json()
        assert body["rx_demand"] == {"requested": 0, "reader_running": False}
        # Deliberately NOT inside `slots`: talk slots are self-reaping and this is not, and one block
        # holding both would lend this number the trustworthiness of the ones beside it.
        assert "rx_demand" not in body["slots"]


# --- regression pins (these pass on master; they are pins, not part of the red run) -------------


def test_a_talk_slot_is_reaped_when_its_socket_drops():
    # The property that makes the talk-slot half of the surface truthful, pinned so a later cycle
    # cannot quietly remove it. MEASURED against real uvicorn: a clean close frees the slot in 43 ms
    # (the disconnect surfaces out of `receive_bytes`), and an RST — which the app never sees at all
    # — frees it in 1.85 s, because that await carries `wait_for(..., tx.idle_timeout)`.
    #
    # **The timeout is the whole difference.** The same RST leaves an `/audio/rx` listener counted
    # indefinitely (ADR 0170's measurement), because its `queue.get()` has no timeout on it. Two
    # loops, one bounded await and one unbounded, and that is why one number in `/status` is
    # liveness and the other is only intent.
    with TestClient(create_app(MockRadio(), api_token=TOKEN)) as client:
        with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws:
            ws.send_json(HEADER)
            ws.receive_json()
        # Socket closed → handler unwound → the next talker gets the slot immediately.
        with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws2:
            ws2.send_json(HEADER)
            assert ws2.receive_json()["status"] == "ready"


def test_holder_label_does_not_change_refusal_behaviour():
    # This cycle observes; it does not gate. A label on the claim must not alter who gets refused.
    slot = TxSlot()
    assert slot.try_acquire() is True  # no label at all still works
    assert slot.try_acquire("browser") is False
    slot.release()
    assert slot.try_acquire("browser") is True
