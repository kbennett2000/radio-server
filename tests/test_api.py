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

import asyncio

from starlette.websockets import WebSocketDisconnect

from fastapi.testclient import TestClient

import pytest

from radio_server.api import create_app
from radio_server.api.events import DEFAULT_EVENT_QUEUE_MAXSIZE, Event
from radio_server.backends import CAT_CAPS, FULL_CAPS, SHARED_CAPS, MockRadio, RadioUnavailable

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


def test_status_carries_the_pa_block_and_it_is_null_where_there_is_nothing_to_report():
    """`pa` is a nested dataclass, so this pins that `asdict` really flattens it onto the wire and
    that a backend which cannot see its PA reports null rather than omitting the key (ADR 0134)."""
    body = _client(MockRadio(supports_cat=True)).get("/status", headers=AUTH).json()
    assert "pa" in body and body["pa"] is None


def test_status_flattens_a_populated_pa_block_onto_the_wire():
    """MockRadio has no power amplifier to report, and giving it a fake one would be inventing
    hardware. Override the snapshot instead — what is under test is the serialisation."""
    from dataclasses import replace

    from radio_server.backends.base import PaState

    radio = MockRadio(supports_cat=True)
    pa = PaState(bias=12, gain=0x88, band_matched=False, tx_frequency=147_555_000)
    plain = radio.status
    radio.status = lambda: replace(plain(), pa=pa)
    body = _client(radio).get("/status", headers=AUTH).json()
    assert body["pa"] == {
        "bias": 12, "gain": 0x88, "band_matched": False, "tx_frequency": 147_555_000,
    }


def test_status_reports_no_cadence_block_rather_than_a_confident_zero():
    """`rssi_cadence` is `null` on a backend with nothing polling it (ADR 0179).

    The same tri-state discipline `wire` beside it keeps: a block of zeroes on a station where no
    cadence exists would claim "polled 0 times, 0 failures" — a clean bill of health issued by an
    instrument that was never installed.
    """
    body = _client(MockRadio(supports_cat=True)).get("/status", headers=AUTH).json()
    assert "rssi_cadence" in body and body["rssi_cadence"] is None
    assert "wire" in body and body["wire"] is None


def test_status_flattens_a_populated_cadence_block_onto_the_wire():
    """`asdict` is the only thing that serialises it, so pin that it reaches HTTP intact."""
    from dataclasses import replace

    from radio_server.backends.base import RssiCadence

    radio = MockRadio(supports_cat=True)
    cadence = RssiCadence(polls=900, unknown=4, skipped=11, pause_errors=0,
                          age_s=0.25, stale_after_s=1.5)
    plain = radio.status
    radio.status = lambda: replace(plain(), rssi_cadence=cadence)
    body = _client(radio).get("/status", headers=AUTH).json()
    assert body["rssi_cadence"] == {
        "polls": 900, "unknown": 4, "skipped": 11, "pause_errors": 0,
        "age_s": 0.25, "stale_after_s": 1.5,
    }


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
        ("/split", {"tx_hz": 144_890_000}, "set_split"),
        ("/tone", {"tone": 100.0}, "set_tone"),
        ("/mode", {"mode": "FM"}, "set_mode"),
        ("/modulation", {"modulation": "FM"}, "set_modulation"),
    ]
    for path, payload, cap in cases:
        resp = client.post(path, json=payload, headers=AUTH)
        assert resp.status_code == 501, path
        assert resp.json()["detail"]["capability"] == cap, path


def test_tone_zero_means_off_rather_than_a_500():
    """`0` is the intuitive way to say "no tone", and the UI's parseFloat("0") sends it.

    It used to skip the `is None` branch, fail the backend's CTCSS range check, and escape as an
    unhandled ValueError — a real HTTP 500 on the bench. It must clear the tone instead.
    """
    radio = MockRadio(supports_cat=True)
    radio.set_tone(100.0)
    resp = _client(radio).post("/tone", json={"tone": 0}, headers=AUTH)
    assert resp.status_code == 200
    assert radio.status().tone is None


def test_tone_out_of_range_is_a_client_error_not_a_server_fault():
    """A genuinely bad tone is the caller's mistake: 422, not an unhandled 500."""

    class _Picky(MockRadio):
        def set_tone(self, tone):
            raise ValueError(f"CTCSS tone {tone} Hz is out of range [67.0, 254.1]")

    resp = _client(_Picky(supports_cat=True)).post("/tone", json={"tone": 9000.0}, headers=AUTH)
    assert resp.status_code == 422
    assert "out of range" in resp.json()["detail"]


def test_split_arms_the_tx_leg_and_shows_it_in_status():
    radio = MockRadio(supports_cat=True)
    radio.set_frequency(145_460_000)
    body = _client(radio).post("/split", json={"tx_hz": 144_860_000}, headers=AUTH).json()
    assert body["frequency"] == 145_460_000
    assert body["tx_frequency"] == 144_860_000


def test_split_null_and_zero_both_mean_simplex():
    radio = MockRadio(supports_cat=True)
    client = _client(radio)
    for payload in ({"tx_hz": None}, {"tx_hz": 0}):
        radio.set_frequency(145_460_000)
        radio.set_split(144_860_000)
        resp = client.post("/split", json=payload, headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["tx_frequency"] is None


def test_tuning_reports_the_split_it_cleared():
    """A cleared split must be visible in the response, not something the caller has to infer."""
    radio = MockRadio(supports_cat=True)
    client = _client(radio)
    client.post("/frequency", json={"hz": 145_460_000}, headers=AUTH)
    client.post("/split", json={"tx_hz": 144_860_000}, headers=AUTH)
    body = client.post("/frequency", json={"hz": 146_520_000}, headers=AUTH).json()
    assert body["tx_frequency"] is None


def test_a_split_the_backend_refuses_is_a_client_error_not_a_server_fault():
    class _Picky(MockRadio):
        def set_split(self, tx_hz):
            raise ValueError("crossband split is not supported")

    resp = _client(_Picky(supports_cat=True)).post(
        "/split", json={"tx_hz": 445_800_000}, headers=AUTH
    )
    assert resp.status_code == 422
    assert "crossband" in resp.json()["detail"]


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


# --- ADR 0158: the interlock reaches the REST key paths for free --------------------------
#
# No route changes: the app-wide `RadioUnavailable` handler already renders any backend refusal as
# a 503 with the reason intact. These pin that the interlock arrives there, and that the reason an
# operator reads is the one that tells them what to go and do.


def test_ptt_is_a_503_naming_broadcast_fm_on_a_station_that_cannot_hear_itself():
    radio = MockRadio(left_in_broadcast_fm=True)
    resp = _client(radio).post("/ptt", json={"on": True}, headers=AUTH)
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert "second receiver" in detail
    assert "demodulating" not in detail       # NOT the AM refusal wearing a different hat
    assert radio.status().transmitting is False


def test_transmit_is_a_503_and_puts_nothing_on_the_air():
    radio = MockRadio(left_in_broadcast_fm=True)
    resp = _client(radio).post("/transmit", content=b"\x01\x02\x03\x04", headers=AUTH)
    assert resp.status_code == 503
    assert radio.tx_log == []


def test_the_status_route_reports_the_block_that_gated_the_key_up():
    # The 503 and the status block must agree — an operator who reads "on: true" and then gets a
    # refusal has a diagnosis; one who reads a null block and gets a refusal has a bug report.
    #
    # `blocks_tx: null` on the mock, and it is the right answer rather than a gap (ADR 0161): the
    # field is `0x087A` flags bit 1, the FIRMWARE's own report that it is refusing to key, and this
    # backend has no firmware to ask. `false` here would be an affirmative claim that the radio will
    # transmit while deaf, which is the wrong wrong-answer to give.
    #
    # `rescues: 0` and it is an int rather than a third tri-state (ADR 0162), because "how many
    # key-ups did this server rescue" has no unknown: a backend that cannot probe has rescued
    # nobody. The mock has no second receiver to probe, so 0 is a fact and not a gap.
    radio = MockRadio(left_in_broadcast_fm=True)
    body = _client(radio).get("/status", headers=AUTH).json()
    # `band` since ADR 0164: the ON route takes the band byte explicitly, and 76.5 MHz is one
    # station under band 1 and a different one under band 2 — so half the answer to "where is this
    # receiver" would be missing without it.
    assert body["broadcast_fm"] == {
        "on": True, "hz": 103_200_000, "blocks_tx": None, "rescues": 0, "band": 0,
    }


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


class _CancelWS:
    """A minimal server-side WebSocket double for driving a handler coroutine off the TestClient path:
    just enough surface for the `/events` handler — token check → accept → send the snapshot → park."""

    def __init__(self, token: str) -> None:
        self.query_params = {"token": token}
        self.accepted = False
        self.sent: list = []
        self.close_code = None

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, data) -> None:
        self.sent.append(data)

    async def close(self, code=None) -> None:
        self.close_code = code


def test_ws_events_swallows_shutdown_cancellation_and_cleans_up():
    # Regression (ADR 0122): on Ctrl-C, uvicorn cancels each parked WS task — the handler must exit
    # quietly (no CancelledError traceback) AND still run its `finally` cleanup. All the `?token=` WS
    # handlers share one `except (WebSocketDisconnect, asyncio.CancelledError)` clause; `/events` (the
    # simplest, no rx pump) proves it. Driven as a bare coroutine because the sync TestClient sends a
    # *disconnect* on block-exit, never the *cancellation* a real shutdown raises.
    async def _run():
        radio = MockRadio(supports_cat=False)
        app = create_app(radio, api_token=TOKEN)
        endpoint = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/events")
        ws = _CancelWS(TOKEN)
        task = asyncio.ensure_future(endpoint(ws))
        # Let it accept, send the initial snapshot, and park on queue.get() (subscribed, no traffic).
        for _ in range(1000):
            await asyncio.sleep(0)
            if app.state.hub.subscriber_count == 1:
                break
        assert app.state.hub.subscriber_count == 1
        assert ws.accepted and ws.sent and ws.sent[0]["type"] == "status"

        task.cancel()  # the shutdown signal, delivered into the parked `await queue.get()`
        await task  # must NOT raise CancelledError — the handler swallows it
        assert not task.cancelled()
        assert app.state.hub.subscriber_count == 0  # `finally` ran: the subscriber was released

    asyncio.run(_run())


class _SlowWS(_CancelWS):
    """A `/events` peer that connects and then stops reading — the slow consumer, deterministically.

    The snapshot goes straight through (a real client reads that much); every send after it parks on
    `release` until the test lets go. That is what real TCP backpressure does to this handler, with
    none of the timing luck a socket-level rig would need.
    """

    def __init__(self, token: str, release: asyncio.Event) -> None:
        super().__init__(token)
        self._release = release

    async def send_json(self, data) -> None:
        if self.sent:
            await self._release.wait()
        self.sent.append(data)

    async def receive(self):
        await asyncio.Event().wait()  # a live peer that simply never sends anything


def test_ws_events_drops_a_subscriber_that_falls_behind_and_tells_it_why():
    """ADR 0180: the bound engages, the client is told in a frame it can read, then closed 1013.

    Drives the handler coroutine directly, like the shutdown test above: the TestClient drains
    eagerly on its own thread, so a queue behind it never fills and the branch would never run.
    """

    async def _run():
        release = asyncio.Event()
        radio = MockRadio(supports_cat=False)
        app = create_app(radio, api_token=TOKEN)
        endpoint = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/events")
        ws = _SlowWS(TOKEN, release)
        task = asyncio.ensure_future(endpoint(ws))
        for _ in range(1000):
            await asyncio.sleep(0)
            if app.state.hub.subscriber_count == 1:
                break
        assert app.state.hub.subscriber_count == 1

        hub = app.state.hub
        published = 0
        # Publish until the hub gives up on it, rather than counting to the bound: the drain holds
        # one event in flight, so the exact number is an implementation detail and pinning it here
        # would make this test about arithmetic instead of about the drop.
        while hub.stats()["dropped_subscribers"] == 0 and published < 10_000:
            hub.publish(Event(type="ptt", data={"on": False}))
            published += 1
            await asyncio.sleep(0)
        assert hub.stats()["dropped_subscribers"] == 1
        assert published > hub.stats()["queue_maxsize"], "it was dropped before its queue was full"

        release.set()
        await task  # the handler unwinds through `_Overflowed`, not through an escaping exception

        assert ws.sent[0]["type"] == "status", "the connect snapshot still leads"
        assert ws.sent[-1]["type"] == "overflow"
        assert ws.sent[-1]["data"]["missed"] > 0
        assert ws.sent[-1]["data"]["queue_maxsize"] == hub.stats()["queue_maxsize"]
        assert ws.close_code == 1013  # try again later — and the web client does
        # The backlog was DISCARDED, not delivered: hundreds published, a handful sent. Delivering it
        # to a consumer this slow takes longer than reconnecting, and the snapshot supersedes it.
        assert len(ws.sent) < 10, f"the backlog was drained to the slow peer: {len(ws.sent)} frames"
        assert app.state.hub.subscriber_count == 0  # `finally` still ran

    asyncio.run(_run())


def test_status_carries_the_event_fan_outs_own_counters():
    """The block ADR 0180 adds, beside `slots`/`rx_demand` — the ADR 0179 scattered shape.

    A healthy station reports all-zero drops beside a NONZERO `published`, and that pairing is the
    point: zero drops with nothing published says only that nothing has happened yet.
    """
    client = _client(MockRadio())
    client.post("/ptt", json={"on": True}, headers=AUTH)  # something to have published

    events = client.get("/status", headers=AUTH).json()["events"]

    assert events["published"] > 0
    assert events["queue_maxsize"] == DEFAULT_EVENT_QUEUE_MAXSIZE
    assert events["dropped_subscribers"] == 0
    assert events["dropped_deliveries"] == 0
    assert events["subscribers"] == 0  # no event_log wired in this app, and no socket open
    assert events["deepest_queue"] == 0


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


# --- GET /diagnostics/ptt-line (ADR 0140) -------------------------------------------------
#
# Every other endpoint reports intent. This one reports the pin, and it is three-valued on purpose:
# `null` means "cannot be asked", which must never collapse into "low". A day of bench measurements
# was uninterpretable because "the server keyed and the radio ignored it" and "the server never
# keyed" produced identical evidence; an unavailable readback reported as a failure would rebuild
# that ambiguity inside the very endpoint meant to end it.


def test_ptt_line_is_null_not_false_on_a_backend_that_cannot_be_asked():
    body = _client(MockRadio(supports_cat=False)).get("/diagnostics/ptt-line", headers=AUTH).json()
    assert body["asserted"] is None
    assert body["readable"] is False
    assert body["backend"] == "mock"


def test_ptt_line_reports_the_probe_when_the_backend_has_one():
    radio = MockRadio(supports_cat=False)
    radio.ptt_line_asserted = lambda: True
    body = _client(radio).get("/diagnostics/ptt-line", headers=AUTH).json()
    assert body["asserted"] is True
    assert body["readable"] is True


# --- POST /diagnostics/reboot-radio (ADR 0144) --------------------------------------------
#
# The bench affordance behind the persistence claim. No unattended test can otherwise reach the one
# state that matters — a radio that has been switched off and on — and a claim that a channel
# survives, checked only by reading back the bytes we just wrote, proves storage rather than radio.


def test_reboot_radio_is_501_on_a_backend_with_nothing_to_reboot():
    resp = _client(MockRadio()).post("/diagnostics/reboot-radio", json={}, headers=AUTH)
    assert resp.status_code == 501
    assert "reboot" in resp.json()["detail"]


def test_reboot_radio_calls_the_backend_and_reports_status():
    radio = MockRadio(supports_cat=True)
    calls: list[int] = []
    radio.reboot_radio = lambda: calls.append(1)
    resp = _client(radio).post("/diagnostics/reboot-radio", json={}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["rebooted"] is True
    assert calls == [1]


def test_reboot_radio_is_refused_mid_transmission():
    """Rebooting a keyed radio is how a transmitter gets stranded."""
    radio = MockRadio(supports_cat=True)
    radio.reboot_radio = lambda: pytest.fail("must not reboot while transmitting")
    client = _client(radio)
    client.app.state.arbiter.acquire_tx()
    resp = client.post("/diagnostics/reboot-radio", json={}, headers=AUTH)
    assert resp.status_code == 409


# --- POST /power (ADR 0146) ---------------------------------------------------------------


def test_power_sets_the_level_and_reports_it_back():
    resp = _client(MockRadio(supports_cat=True)).post(
        "/power", json={"level": "low"}, headers=AUTH
    )
    assert resp.status_code == 200
    assert resp.json()["power"] == "low"


def test_power_is_501_on_a_backend_that_cannot_set_it():
    """A UV-5R holds its power on its own front panel; the UI greys the control rather than
    offering a dead one (guardrail 3)."""
    resp = _client(MockRadio(supports_cat=False)).post(
        "/power", json={"level": "low"}, headers=AUTH
    )
    assert resp.status_code == 501
    assert resp.json()["detail"]["capability"] == "set_power"


def test_a_bad_power_level_is_a_422_naming_the_allowed_words():
    """A client error, not a server fault — and the operator has to be told which words work."""
    resp = _client(MockRadio(supports_cat=True)).post(
        "/power", json={"level": "turbo"}, headers=AUTH
    )
    assert resp.status_code == 422
    assert "low" in resp.json()["detail"]


# --- POST /mode: the bandwidth vocabulary (ADR 0172) ----------------------------------------
#
# Wide/narrow BANDWIDTH — not `/modulation`, the demodulator, which follows directly below. These
# two sections sit adjacent on purpose: both spell one of their values "FM", and `/mode` quietly
# accepting "AM" for seventeen ADRs is what that confusion actually cost.


def test_mode_sets_the_bandwidth_and_reports_it_back():
    resp = _client(MockRadio(supports_cat=True)).post("/mode", json={"mode": "NFM"}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["mode"] == "NFM"


def test_a_bad_mode_is_a_422_naming_the_allowed_words():
    """A client error, not a server fault — and the operator has to be told which words work.

    `"AM"` rather than `"banana"` because AM is the value a human actually sends: it is the
    demodulator's word, it sat in this control's own select until ADR 0154, and it is what a ham's
    muscle memory reaches for. It used to answer 200 here (the mock swallowed it) and 500 on the
    bench (every real backend raises) — ADR 0160 finding 13, measured on the station.
    """
    radio = MockRadio(supports_cat=True)
    resp = _client(radio).post("/mode", json={"mode": "AM"}, headers=AUTH)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    # Not `"FM" in detail` — "FM" is a substring of "NFM" and would pass on half a message.
    assert "FM or NFM" in detail
    assert "'AM'" in detail                 # the refused value is echoed, like /power and /modulation
    assert radio.status().mode is None      # nothing changed on the way to refusing


def test_a_bad_mode_on_an_audio_only_backend_is_still_a_501_and_not_a_422():
    """The capability answer outranks the value answer, all the way out to HTTP. A radio that
    cannot set bandwidth at all must say so; telling the operator to fix the word would send them
    to a control this backend does not have.
    """
    resp = _client(MockRadio(supports_cat=False)).post("/mode", json={"mode": "AM"}, headers=AUTH)
    assert resp.status_code == 501
    assert resp.json()["detail"]["capability"] == "set_mode"


# --- POST /modulation (ADR 0150) ----------------------------------------------------------
#
# The demodulator, FM or AM — not `/mode`, which is wide/narrow bandwidth. The two are different
# radio settings that both spell one of their values "FM", so these pin that they stay separate all
# the way out to the HTTP surface.


def test_modulation_sets_the_demodulator_and_reports_it_back():
    resp = _client(MockRadio(supports_cat=True)).post(
        "/modulation", json={"modulation": "AM"}, headers=AUTH
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["modulation"] == "AM"
    # And the consequence a client must not have to infer: in AM the radio will not key.
    assert body["tx_ok"] is False


def test_modulation_and_mode_are_different_settings_on_the_wire():
    """`/mode` is bandwidth and `/modulation` is the demodulator. Setting one must not move the
    other, or a client reading either would be reading a lie."""
    client = _client(MockRadio(supports_cat=True))
    client.post("/mode", json={"mode": "NFM"}, headers=AUTH)
    body = client.post("/modulation", json={"modulation": "AM"}, headers=AUTH).json()
    assert (body["mode"], body["modulation"]) == ("NFM", "AM")


def test_modulation_is_501_on_a_backend_that_cannot_set_it():
    """Everything but the mock and a UV-K5 on F7 firmware — a stock-firmware `eeprom` tuner has no
    such command at all, and the UI greys the control rather than offering a dead one."""
    resp = _client(MockRadio(supports_cat=False)).post(
        "/modulation", json={"modulation": "AM"}, headers=AUTH
    )
    assert resp.status_code == 501
    assert resp.json()["detail"]["capability"] == "set_modulation"


def test_a_bad_modulation_is_a_422_naming_the_allowed_words():
    resp = _client(MockRadio(supports_cat=True)).post(
        "/modulation", json={"modulation": "USB"}, headers=AUTH
    )
    assert resp.status_code == 422
    assert "FM" in resp.json()["detail"]


def test_modulation_is_refused_mid_transmission():
    """Not politeness: switching to AM disables the radio's own transmit path, so doing it under a
    live carrier would end the over from underneath the operator."""
    radio = MockRadio(supports_cat=True)
    client = _client(radio)
    client.app.state.arbiter.acquire_tx()
    resp = client.post("/modulation", json={"modulation": "AM"}, headers=AUTH)
    assert resp.status_code == 409
    assert radio.status().modulation is None      # nothing changed on the way to refusing


def test_a_radio_that_will_not_confirm_the_modulation_is_a_503_not_a_500():
    """A UV-K5 that is switched off, or running pre-F7 firmware that drops the command in silence.
    The operator standing next to it needs the sentence, not a stack trace (ADR 0143)."""
    radio = MockRadio(supports_cat=True)
    radio.set_modulation = lambda m: (_ for _ in ()).throw(
        RadioUnavailable("no 0x0878 reply — pre-F7 firmware, or the radio is off")
    )
    resp = _client(radio).post("/modulation", json={"modulation": "AM"}, headers=AUTH)
    assert resp.status_code == 503
    assert "pre-F7" in resp.json()["detail"]


def test_modulation_pushes_a_status_event():
    radio = MockRadio(supports_cat=True)
    client = _client(radio)
    with client.websocket_connect(f"/events?token={TOKEN}") as ws:
        ws.receive_json()  # initial status snapshot
        client.post("/modulation", json={"modulation": "AM"}, headers=AUTH)
        event = ws.receive_json()
    assert event["type"] == "status"
    assert event["data"]["modulation"] == "AM"


# --- POST /tuning/persist (ADR 0145) ------------------------------------------------------
#
# Store tuned channels on the radio, or tune instantly and let it forget. A live switch rather than
# a config setting, because a setting returns apply:"restart" and this is a choice the operator
# changes with what they are doing.


def _switchable(persist=False):
    """A backend that has the choice — the shape `AiocBaofeng` presents on the hybrid tuner."""
    radio = MockRadio(supports_cat=True)
    state = {"persist": persist}

    def setter(on):
        state["persist"] = bool(on)
        return state["persist"]

    type(radio).tune_persist = property(lambda _self: state["persist"])
    radio.set_tune_persist = setter
    return radio, state


def test_tune_persist_is_501_where_the_backend_has_no_such_choice():
    """Every backend but a UV-K5 on the hybrid tuner. The UI hides the control instead of offering
    one that does nothing (guardrail 3)."""
    resp = _client(MockRadio()).post("/tuning/persist", json={"on": True}, headers=AUTH)
    assert resp.status_code == 501
    assert "stored" in resp.json()["detail"]


def test_tune_persist_switches_and_reports_the_resulting_state():
    radio, state = _switchable()
    try:
        client = _client(radio)
        resp = client.post("/tuning/persist", json={"on": True}, headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["persist"] is True
        assert state["persist"] is True

        assert client.post("/tuning/persist", json={"on": False}, headers=AUTH).json()["persist"] \
            is False
    finally:
        del type(radio).tune_persist


def test_tune_persist_is_refused_mid_transmission():
    """Storing arms the radio's serial lockout, and the firmware cuts an over in progress when it
    does — flipping this switch must never be able to end a transmission."""
    radio, state = _switchable()
    try:
        client = _client(radio)
        client.app.state.arbiter.acquire_tx()
        resp = client.post("/tuning/persist", json={"on": True}, headers=AUTH)
        assert resp.status_code == 409
        assert state["persist"] is False          # and nothing was written on the way to refusing
    finally:
        del type(radio).tune_persist


def test_ptt_line_distinguishes_a_low_line_from_an_unreadable_one():
    """The whole point: False means the kernel says the pin is low (radio-server's fault), None
    means nobody could ask (nobody's fault yet). Collapsing them re-creates the ambiguity."""
    radio = MockRadio(supports_cat=False)
    radio.ptt_line_asserted = lambda: False
    body = _client(radio).get("/diagnostics/ptt-line", headers=AUTH).json()
    assert body["asserted"] is False
    assert body["readable"] is True
