"""The controller loop: the whole software tower running live on the mock (ADR 0013).

This is the first cycle where `DtmfInput` → `AuthGate` → `Dispatcher` → `StationId` and an
attached `ScanEngine` are pumped by one driver off real session transitions, and where
`StationId.begin_session`/`check`/`sign_off` finally connect to something other than a test
fixture. Everything runs against `MockRadio` with the `FakeClock` and scripted receive audio —
no hardware, no multimon/piper (a `FakeDtmfDecoder` + `StubTts` stand in), and no real sleeps.

The load-bearing proofs: a DTMF-TOTP login over `step()` opens a session and arms the ID; an
authed ``"1"`` lands a genuinely CW-ID'd time announcement in `tx_log`; the periodic-ID safety
net forces an ID when the clock passes the interval mid-session; an inactivity timeout closes
the session and signs off (the transition `AuthGate` only demotes lazily — surfaced here via
`expire_if_idle`); an attached scan ticks each step and holds on a scripted-busy channel;
lifecycle events reach the `EventHub` in order; and `POST /controller` flips the live state in
`/status`.
"""

import asyncio

from fastapi.testclient import TestClient

from radio_server.api import create_app
from radio_server.audio import AudioFrame, synth_dtmf
from radio_server.auth import SECRET_ENV_VAR, OutcomeKind
from radio_server.backends import MockRadio
from radio_server.controller import (
    ControllerRunner,
    DEFAULT_SESSION_TIMEOUT,
    RADIO_SESSION_TIMEOUT_ENV_VAR,
    build_controller,
)
from radio_server.scan import ResumeMode, ScanEngine, ScanPlan
from radio_server.services import (
    DEFAULT_ID_INTERVAL,
    RADIO_CALLSIGN_ENV_VAR,
    CwId,
    StubTts,
)

from .conftest import TEST_SECRET, FakeClock
from .test_dtmf import FakeDtmfDecoder

CALLSIGN = "AE9S"
BASE_ENV = {SECRET_ENV_VAR: TEST_SECRET, RADIO_CALLSIGN_ENV_VAR: CALLSIGN}

TOKEN = "test-lan-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

# A canonical DTMF frame stands in for a received "over"; the fake decoder ignores its content.
RX = synth_dtmf("1")

# Three 20 kHz-spaced 2 m channels for the scan; the middle one is the scripted-busy target.
FREQS = [146_500_000, 146_520_000, 146_540_000]
BUSY = FREQS[1]

ID_AUDIO = CwId().encode(CALLSIGN)  # what `build_id_encoder` (CW default) prepends


class SilentDecoder:
    """A decoder that hears no DTMF — every frame yields an empty digit string.

    For the live-loop tests, where `receive()` delivers a real (empty) frame every iteration and
    a scripted finite decoder would run dry; here the loop simply pumps silence.
    """

    def decode(self, frame: AudioFrame) -> str:
        return ""


def build_ctrl(clock, scripts, *, radio=None, env_extra=None, decoder=None):
    """A controller over the real stack, wired via `build_controller` with test doubles.

    ``scripts`` feeds a `FakeDtmfDecoder` (one entry per received over) unless ``decoder`` is
    given. ``on_event`` is left unset so the caller attaches a recorder or hands the controller to
    `create_app` (which rebinds it to the hub adapter).
    """
    radio = radio if radio is not None else MockRadio()
    env = dict(BASE_ENV)
    if env_extra:
        env.update(env_extra)
    dec = decoder if decoder is not None else FakeDtmfDecoder(list(scripts))
    ctrl = build_controller(env, radio=radio, decoder=dec, tts=StubTts(), clock=clock)
    return radio, ctrl


# --- login → session open, ID armed --------------------------------------------------------

def test_login_over_the_loop_opens_session_and_arms_id(clock, code_for):
    code = code_for(clock.now)
    radio, ctrl = build_ctrl(clock, [code + "#"])
    events = []
    ctrl.on_event = events.append

    result = ctrl.step(clock.now, RX)

    assert result.outcomes[0].kind is OutcomeKind.ACCEPTED
    assert ctrl.session.authenticated
    assert result.session_open is True
    assert [e.phase for e in events] == ["session_open"]
    assert radio.tx_log == []  # authenticating never transmits


# --- authed "1" → a CW-ID'd time announcement in tx_log ------------------------------------

def test_authed_one_lands_cw_id_time_announcement(clock, code_for):
    code = code_for(clock.now)
    radio, ctrl = build_ctrl(clock, [code + "#", "1#"])

    ctrl.step(clock.now, RX)              # over 1: login
    result = ctrl.step(clock.now, RX)     # over 2: authed "1"

    assert result.outcomes[0].kind is OutcomeKind.COMMAND
    assert result.outcomes[0].detail.service == "time"
    assert result.outcomes[0].detail.transmitted is True
    # One over, carrying the CW station ID prepended to the time announcement.
    assert len(radio.tx_log) == 1
    assert radio.tx_log[0].samples.startswith(ID_AUDIO.samples)
    assert len(radio.tx_log[0].samples) > len(ID_AUDIO.samples)  # ... then the announcement


# --- forced periodic ID mid-session (Part 97 safety net) -----------------------------------

def test_forced_periodic_id_fires_when_interval_passes_mid_session(clock, code_for):
    code = code_for(clock.now)
    # Session timeout well past the ID interval so the session stays open across it.
    radio, ctrl = build_ctrl(
        clock, [code + "#", "1#"], env_extra={RADIO_SESSION_TIMEOUT_ENV_VAR: "100000"}
    )
    events = []
    ctrl.on_event = events.append

    ctrl.step(clock.now, RX)  # login
    ctrl.step(clock.now, RX)  # command -> transmits (ID + time), last_id = now
    assert len(radio.tx_log) == 1

    clock.advance(DEFAULT_ID_INTERVAL)      # now overdue for a re-ID
    result = ctrl.step(clock.now)           # no audio this iteration

    assert result.id_sent is True
    assert len(radio.tx_log) == 2           # an ID-only over was forced
    assert radio.tx_log[1] == ID_AUDIO
    assert events[-1].phase == "id"


# --- inactivity timeout closes the session and signs off -----------------------------------

def test_inactivity_timeout_closes_session_and_signs_off(clock, code_for):
    code = code_for(clock.now)
    radio, ctrl = build_ctrl(clock, [code + "#", "1#"])  # default 300s session timeout
    events = []
    ctrl.on_event = events.append

    ctrl.step(clock.now, RX)  # login
    ctrl.step(clock.now, RX)  # command -> station has transmitted, so sign-off will ID
    assert len(radio.tx_log) == 1

    clock.advance(DEFAULT_SESSION_TIMEOUT + 1.0)  # idle past the timeout, no further digits
    result = ctrl.step(clock.now)

    assert result.signed_off is True
    assert ctrl.session.authenticated is False
    assert result.session_open is False
    assert len(radio.tx_log) == 2                 # closing ID
    assert radio.tx_log[1] == ID_AUDIO
    assert events[-1].phase == "session_close"


# --- an attached scan ticks each step and holds on scripted busy ---------------------------

def test_attached_scan_ticks_each_step_and_holds_on_busy(clock):
    radio = MockRadio(supports_cat=True, busy_frequencies={BUSY})
    _, ctrl = build_ctrl(clock, [], radio=radio, decoder=SilentDecoder())
    engine = ScanEngine(
        radio, ScanPlan.from_frequencies(FREQS), mode=ResumeMode.CARRIER, settle=0.0, clock=clock
    )
    ctrl.scan = engine

    last = None
    for _ in range(8):
        last = ctrl.step(clock.now)  # no DTMF; the scan advances on the injected clock
        clock.advance(0.01)

    assert last.scanning is True
    # Carrier-operated scan dwells on the busy channel — it holds there.
    assert engine.current_frequency == BUSY


# --- lifecycle events reach the hub in order -----------------------------------------------

def test_lifecycle_events_are_emitted_in_order(clock, code_for):
    code = code_for(clock.now)
    # Timeout between the ID interval (600) and the moment we force a close, so the order is
    # session_open (login) -> id (periodic) -> session_close (timeout).
    radio, ctrl = build_ctrl(
        clock, [code + "#", "1#"], env_extra={RADIO_SESSION_TIMEOUT_ENV_VAR: "700"}
    )
    events = []
    ctrl.on_event = events.append

    ctrl.step(clock.now, RX)              # login  -> session_open
    ctrl.step(clock.now, RX)              # command (transmits; no event)
    clock.advance(DEFAULT_ID_INTERVAL)    # +600: overdue, still within the 700s timeout
    ctrl.step(clock.now)                  # periodic -> id
    clock.advance(101.0)                  # now 701s idle since last activity
    ctrl.step(clock.now)                  # idle    -> session_close

    assert [e.phase for e in events] == ["session_open", "id", "session_close"]


# --- the thin async driver pumps step() each iteration -------------------------------------

def test_runner_pumps_step_each_iteration(clock):
    box: dict = {}

    class CountingRadio(MockRadio):
        def __init__(self) -> None:
            super().__init__()
            self.receives = 0

        def receive(self) -> AudioFrame:
            self.receives += 1
            if self.receives >= 3:
                box["runner"].stop()
            return super().receive()

    radio = CountingRadio()
    _, ctrl = build_ctrl(clock, [], radio=radio, decoder=SilentDecoder())
    runner = ControllerRunner(radio, ctrl, clock=clock, poll=0.0)
    box["runner"] = runner

    asyncio.run(runner.run())

    assert radio.receives == 3       # looped receive() -> step() until stop()
    assert runner.running is False


# --- the API: start/stop the controller, live state in /status -----------------------------

def _api(radio, clock, **kwargs):
    _, ctrl = build_ctrl(clock, [], radio=radio, decoder=SilentDecoder(), **kwargs)
    runner = ControllerRunner(radio, ctrl, clock=clock, poll=0.01)
    app = create_app(radio, api_token=TOKEN, controller=ctrl, runner=runner)
    return ctrl, runner, app


def test_controller_endpoint_flips_running_in_status(clock):
    radio = MockRadio()
    _, _, app = _api(radio, clock)
    with TestClient(app) as client:
        assert client.get("/status", headers=AUTH).json()["controller"]["running"] is False

        r = client.post("/controller", json={"on": True}, headers=AUTH)
        assert r.status_code == 200
        assert client.get("/status", headers=AUTH).json()["controller"]["running"] is True

        client.post("/controller", json={"on": False}, headers=AUTH)
        assert client.get("/status", headers=AUTH).json()["controller"]["running"] is False


def test_controller_endpoint_requires_token(clock):
    radio = MockRadio()
    _, _, app = _api(radio, clock)
    assert TestClient(app).post("/controller", json={"on": True}).status_code == 401


def test_status_controller_block_is_null_when_unconfigured():
    radio = MockRadio()
    client = TestClient(create_app(radio, api_token=TOKEN))
    assert client.get("/status", headers=AUTH).json()["controller"] is None


def test_controller_endpoint_503_when_unconfigured():
    radio = MockRadio()
    client = TestClient(create_app(radio, api_token=TOKEN))
    resp = client.post("/controller", json={"on": True}, headers=AUTH)
    assert resp.status_code == 503


# --- session lifecycle streamed over the WebSocket -----------------------------------------

def test_session_events_reach_the_ws_in_order(clock, code_for):
    code = code_for(clock.now)
    radio = MockRadio()
    # A controller scripted for one login; handed to the app so its on_event publishes to the hub.
    _, ctrl = build_ctrl(clock, [code + "#"], radio=radio)
    runner = ControllerRunner(radio, ctrl, clock=clock, poll=0.01)
    app = create_app(radio, api_token=TOKEN, controller=ctrl, runner=runner)

    with TestClient(app) as client:
        with client.websocket_connect(f"/events?token={TOKEN}") as ws:
            ws.receive_json()  # initial status snapshot
            ctrl.step(clock.now, RX)                       # login -> session_open
            clock.advance(DEFAULT_SESSION_TIMEOUT + 1.0)
            ctrl.step(clock.now)                           # idle  -> session_close
            seen = [ws.receive_json() for _ in range(2)]

    assert [(e["type"], e["data"]["phase"]) for e in seen] == [
        ("session", "session_open"),
        ("session", "session_close"),
    ]
