"""FastAPI API layer: REST + WebSocket over MockRadio, and the capability split (ADR 0011).

Everything is driven through Starlette's `TestClient` against `create_app(MockRadio(...))` —
no server binds and no hardware is touched, the same software-first posture as the rest of the
suite. Two axes are exercised: `supports_cat=True` (a full-control V71-like backend) and
`supports_cat=False` (an audio-only Baofeng-like backend), so the capability split is proven
from both sides.

The load-bearing proofs:
- `/status` mirrors the mock's `RadioStatus`; `/capabilities` tracks `supports_cat`.
- A CAT endpoint *works* on a CAT backend and returns a **501 whose body names the missing
  capability** (never a silent no-op) on an audio-only backend — guardrail 3 at the HTTP edge.
- The WebSocket emits a `status` event on connect and pushes further events live.
- The LAN bearer-token plane rejects missing/bad tokens and accepts a good one; the token
  loader fails loud when unset.
"""

from starlette.websockets import WebSocketDisconnect

from fastapi.testclient import TestClient

import pytest

from radio_server.api import create_app
from radio_server.backends import CAT_CAPS, FULL_CAPS, SHARED_CAPS, MockRadio

TOKEN = "test-lan-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _client(radio: MockRadio) -> TestClient:
    """A TestClient over the API wrapping `radio` — the API analog of `_build_gate`."""
    return TestClient(create_app(radio, api_token=TOKEN))


def _caps(supports_cat: bool) -> set[str]:
    return {str(c) for c in (FULL_CAPS if supports_cat else SHARED_CAPS)}


# --- GET /status reflects mock state -----------------------------------------------------

def test_status_reflects_mock_state():
    radio = MockRadio(supports_cat=True, busy=True)
    radio.ptt(True)
    body = _client(radio).get("/status", headers=AUTH).json()
    # Note the field is `transmitting`, not `ptt`.
    assert body["backend"] == "mock"
    assert body["transmitting"] is True
    assert body["busy"] is True


def test_status_omits_cat_fields_stay_none_on_audio_only():
    body = _client(MockRadio(supports_cat=False)).get("/status", headers=AUTH).json()
    assert body["frequency"] is None
    assert body["mode"] is None


# --- GET /capabilities matches supports_cat ----------------------------------------------

def test_capabilities_full_on_cat_backend():
    body = _client(MockRadio(supports_cat=True)).get("/capabilities", headers=AUTH).json()
    assert set(body) == _caps(True)


def test_capabilities_shared_only_on_audio_only_backend():
    body = _client(MockRadio(supports_cat=False)).get("/capabilities", headers=AUTH).json()
    assert set(body) == _caps(False)
    # The CAT ops are genuinely absent, not merely flagged.
    assert not set(body) & {str(c) for c in CAT_CAPS}


# --- The capability split at the HTTP boundary (guardrail 3) ------------------------------

def test_cat_endpoint_works_on_cat_backend():
    radio = MockRadio(supports_cat=True)
    resp = _client(radio).post("/frequency", json={"hz": 146_520_000}, headers=AUTH)
    assert resp.status_code == 200
    # The call actually reached the backend.
    assert radio.status().frequency == 146_520_000


def test_cat_endpoint_gated_501_names_capability_on_audio_only():
    radio = MockRadio(supports_cat=False)
    resp = _client(radio).post("/frequency", json={"hz": 146_520_000}, headers=AUTH)
    assert resp.status_code == 501
    # The body names the exact missing capability — what the UI greys the right control on.
    assert resp.json()["detail"]["capability"] == "set_frequency"
    # And it was NOT a silent no-op: the backend state is untouched.
    assert radio.status().frequency is None


def test_every_cat_endpoint_is_gated_on_audio_only():
    radio = MockRadio(supports_cat=False)
    client = _client(radio)
    cases = [
        ("/frequency", {"hz": 146_520_000}, "set_frequency"),
        ("/channel", {"n": 5}, "set_channel"),
        ("/tone", {"tone": 100.0}, "set_tone"),
        ("/mode", {"mode": "FM"}, "set_mode"),
    ]
    for path, payload, cap in cases:
        resp = client.post(path, json=payload, headers=AUTH)
        assert resp.status_code == 501, path
        assert resp.json()["detail"]["capability"] == cap, path


# --- Shared control endpoints: ptt / transmit --------------------------------------------

def test_ptt_endpoint_keys_the_mock():
    radio = MockRadio(supports_cat=False)
    resp = _client(radio).post("/ptt", json={"on": True}, headers=AUTH)
    assert resp.status_code == 200
    assert radio.status().transmitting is True


def test_transmit_endpoint_appends_to_tx_log():
    radio = MockRadio(supports_cat=False)
    pcm = b"\x01\x02\x03\x04"
    resp = _client(radio).post("/transmit", content=pcm, headers=AUTH)
    assert resp.status_code == 200
    assert [f.samples for f in radio.tx_log] == [pcm]


# --- WebSocket event stream --------------------------------------------------------------

def test_ws_emits_status_event_on_connect():
    radio = MockRadio(supports_cat=True, busy=True)
    client = _client(radio)
    with client.websocket_connect(f"/events?token={TOKEN}") as ws:
        event = ws.receive_json()
    assert event["type"] == "status"
    assert event["data"]["busy"] is True
    assert event["data"]["backend"] == "mock"


def test_ws_pushes_ptt_event_after_control_call():
    radio = MockRadio(supports_cat=False)
    client = _client(radio)
    with client.websocket_connect(f"/events?token={TOKEN}") as ws:
        ws.receive_json()  # initial status snapshot
        client.post("/ptt", json={"on": True}, headers=AUTH)
        event = ws.receive_json()
    assert event["type"] == "ptt"
    assert event["data"] == {"on": True}


def test_ws_rejects_bad_token():
    client = _client(MockRadio())
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/events?token=nope") as ws:
            ws.receive_json()
    assert excinfo.value.code == 1008  # policy violation


def test_ws_rejects_missing_token():
    client = _client(MockRadio())
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/events") as ws:
            ws.receive_json()


# --- LAN bearer-token auth plane ---------------------------------------------------------

def test_rest_rejects_missing_token():
    assert _client(MockRadio()).get("/status").status_code == 401


def test_rest_rejects_bad_token():
    resp = _client(MockRadio()).get("/status", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_rest_accepts_good_token():
    assert _client(MockRadio()).get("/status", headers=AUTH).status_code == 200


# API-token secret loading moved to the secrets channel (ADR 0025) — see tests/test_config.py.
