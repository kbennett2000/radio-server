"""The software scan engine: plan model, resume modes, priority/lockout, events, API gate.

Everything runs against a fresh CAT ``MockRadio`` whose per-frequency busy is scripted with
``busy_frequencies`` (the mock hook added this cycle), and the clock-driven paths run on the
``FakeClock`` from conftest — no hardware and no real sleeps. Progress is captured through the
engine's injected ``on_event`` callback (a list of ``ScanEvent``), the same seam the API adapts
to ``"scan"`` events on the WebSocket ``EventHub``.

The load-bearing proofs: a scripted-busy channel holds the scan; carrier-operated resumes when
the carrier drops and time-operated moves on after the dwell; lockout skips a channel entirely;
the priority channel is re-checked between steps; scan events reach the hub in order; and scan
is capability-gated — ``POST /scan`` is a 501 naming ``"scan"`` (never a silent no-op) on an
audio-only backend, where it is not even advertised.
"""

import pytest

from fastapi.testclient import TestClient

from radio_server.api import create_app
from radio_server.backends import Capability, MockRadio, UnsupportedCapability
from radio_server.scan import (
    DEFAULT_SCAN_DWELL,
    DEFAULT_SCAN_MODE,
    DEFAULT_SCAN_SETTLE,
    ResumeMode,
    ScanEngine,
    ScanEvent,
    ScanPlan,
    load_scan_dwell,
    load_scan_mode,
    load_scan_settle,
)

from .conftest import FakeClock

# Three 15 kHz-spaced 2 m channels; the middle one is the usual "busy" target.
FREQS = [146_500_000, 146_520_000, 146_540_000]
BUSY = FREQS[1]
SETTLE = 0.05

TOKEN = "test-lan-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def build(clock=None, *, radio=None, plan=None, mode=ResumeMode.CARRIER, dwell=1.0):
    """A fresh engine over a CAT MockRadio with an event recorder — the scan analog of `build`."""
    radio = radio if radio is not None else MockRadio(supports_cat=True)
    plan = plan if plan is not None else ScanPlan.from_frequencies(FREQS)
    events: list[ScanEvent] = []
    engine = ScanEngine(
        radio,
        plan,
        on_event=events.append,
        mode=mode,
        dwell=dwell,
        settle=SETTLE,
        clock=clock,
    )
    return radio, engine, events


def phases(events):
    return [(e.phase, e.frequency) for e in events]


def poll(engine, clock):
    """Let the current channel settle, then tick once so its busy read is trusted."""
    clock.advance(SETTLE)
    return engine.tick()


# --- plan model ----------------------------------------------------------------------------

def test_plan_from_frequencies_preserves_order_and_lockout():
    plan = ScanPlan.from_frequencies(FREQS, lockout={BUSY})
    assert plan.channels == tuple(FREQS)
    assert plan.active_channels() == [FREQS[0], FREQS[2]]  # BUSY removed


def test_plan_from_range_is_inclusive_and_stepped():
    plan = ScanPlan.from_range(146_500_000, 146_540_000, 20_000)
    assert plan.channels == (146_500_000, 146_520_000, 146_540_000)


def test_plan_from_range_rejects_bad_bounds():
    with pytest.raises(ValueError):
        ScanPlan.from_range(146_540_000, 146_500_000, 20_000)  # stop < start
    with pytest.raises(ValueError):
        ScanPlan.from_range(146_500_000, 146_540_000, 0)  # non-positive step


# --- config loaders (marked defaults, fail loud) -------------------------------------------

def test_scan_config_defaults_when_unset():
    assert load_scan_settle({}) == DEFAULT_SCAN_SETTLE
    assert load_scan_dwell({}) == DEFAULT_SCAN_DWELL
    assert load_scan_mode({}) == ResumeMode(DEFAULT_SCAN_MODE) == ResumeMode.CARRIER


def test_scan_config_reads_env():
    assert load_scan_dwell({"RADIO_SCAN_DWELL": "8"}) == 8.0
    assert load_scan_mode({"RADIO_SCAN_MODE": "timed"}) is ResumeMode.TIMED


def test_scan_config_fails_loud_on_bad_values():
    with pytest.raises(RuntimeError):
        load_scan_dwell({"RADIO_SCAN_DWELL": "-1"})
    with pytest.raises(RuntimeError):
        load_scan_settle({"RADIO_SCAN_SETTLE": "nope"})
    with pytest.raises(RuntimeError):
        load_scan_mode({"RADIO_SCAN_MODE": "sideways"})


# --- capability gate -----------------------------------------------------------------------

def test_engine_refuses_audio_only_backend():
    with pytest.raises(UnsupportedCapability) as excinfo:
        ScanEngine(MockRadio(supports_cat=False), ScanPlan.from_frequencies(FREQS))
    assert excinfo.value.capability is Capability.SCAN


# --- sweep: stop-and-hold at first activity ------------------------------------------------

def test_sweep_holds_first_active_channel():
    radio = MockRadio(supports_cat=True, busy_frequencies={BUSY})
    radio, engine, events = build(radio=radio)
    held = engine.sweep()
    assert held == BUSY
    # Clear channel before it was scanned; the busy one ends active -> dwelling.
    assert phases(events) == [
        ("scanning", FREQS[0]),
        ("scanning", BUSY),
        ("active", BUSY),
        ("dwelling", BUSY),
    ]


def test_sweep_returns_none_when_all_clear():
    radio, engine, events = build()
    assert engine.sweep() is None
    assert [e.phase for e in events] == ["scanning", "scanning", "scanning"]


def test_sweep_skips_locked_out_channel():
    # The busy channel is locked out, so it is never tuned and the sweep finds nothing.
    radio = MockRadio(supports_cat=True, busy_frequencies={BUSY})
    tuned: list[int] = []
    original = radio.set_frequency
    radio.set_frequency = lambda hz: (tuned.append(hz), original(hz))[1]
    plan = ScanPlan.from_frequencies(FREQS, lockout={BUSY})
    radio, engine, events = build(radio=radio, plan=plan)

    assert engine.sweep() is None
    assert BUSY not in tuned  # never even tuned to the locked channel
    assert tuned == [FREQS[0], FREQS[2]]
    assert all(e.frequency != BUSY for e in events)


def test_sweep_checks_priority_between_steps_and_holds_it():
    priority = 146_900_000
    radio = MockRadio(supports_cat=True, busy_frequencies={priority})
    tuned: list[int] = []
    original = radio.set_frequency
    radio.set_frequency = lambda hz: (tuned.append(hz), original(hz))[1]
    plan = ScanPlan.from_frequencies([FREQS[0], FREQS[2]], priority=priority)
    radio, engine, events = build(radio=radio, plan=plan)

    held = engine.sweep()
    assert held == priority
    # Priority is peeked between step 0 and step 1: tuned first channel, then priority.
    assert tuned[:2] == [FREQS[0], priority]
    assert ("active", priority) in phases(events)


# --- tick: the clock-driven resume modes ---------------------------------------------------

def test_carrier_dwells_while_busy_then_resumes_on_drop(clock):
    radio = MockRadio(supports_cat=True, busy_frequencies={BUSY})
    radio, engine, events = build(clock, radio=radio, mode=ResumeMode.CARRIER)

    engine.tick()                 # start: tune FREQS[0] (settling)
    assert poll(engine, clock) == "listening"   # FREQS[0] clear -> advance to BUSY
    assert poll(engine, clock) == "dwelling"    # BUSY -> dwell
    # It keeps holding while the carrier is up.
    assert poll(engine, clock) == "dwelling"

    radio.busy_frequencies.discard(BUSY)        # carrier drops
    assert engine.tick() == "listening"         # resume -> advance to FREQS[2]
    assert ("resumed", BUSY) in phases(events)
    assert engine.current_frequency == FREQS[2]


def test_timed_moves_on_after_dwell_even_if_still_busy(clock):
    radio = MockRadio(supports_cat=True, busy_frequencies={BUSY})
    radio, engine, events = build(clock, radio=radio, mode=ResumeMode.TIMED, dwell=5.0)

    engine.tick()
    poll(engine, clock)           # FREQS[0] clear -> BUSY
    assert poll(engine, clock) == "dwelling"    # BUSY -> dwell, deadline = now + 5

    clock.advance(4.9)
    assert engine.tick() == "dwelling"          # still within the dwell
    clock.advance(0.1)                          # now exactly at the 5s boundary (>=)
    assert engine.tick() == "listening"         # moves on though still busy
    assert ("resumed", BUSY) in phases(events)


def test_hold_mode_stops_on_first_activity(clock):
    radio = MockRadio(supports_cat=True, busy_frequencies={BUSY})
    radio, engine, events = build(clock, radio=radio, mode=ResumeMode.HOLD)

    engine.tick()
    poll(engine, clock)           # FREQS[0] clear -> BUSY
    assert poll(engine, clock) == "held"        # stop-and-hold
    # Further ticks stay held even after the carrier clears.
    radio.busy_frequencies.discard(BUSY)
    assert engine.tick() == "held"


def test_busy_read_waits_for_settle(clock):
    # Before the settle window elapses, a busy channel is not yet acted on.
    radio = MockRadio(supports_cat=True, busy=True)
    radio, engine, events = build(clock, radio=radio, mode=ResumeMode.CARRIER)

    engine.tick()                 # tune FREQS[0] (busy, but still settling)
    assert engine.tick() == "listening"         # no time passed -> not yet trusted
    assert events[-1].phase == "scanning"       # nothing acted on yet
    assert poll(engine, clock) == "dwelling"    # after settle, the busy read lands


# --- events reach the hub in order (recorder) ----------------------------------------------

def test_events_are_emitted_in_phase_order():
    radio = MockRadio(supports_cat=True, busy_frequencies={FREQS[0]})
    radio, engine, events = build(radio=radio)
    engine.sweep()
    # First channel is busy: scanning -> active -> dwelling, in that order, no gaps.
    assert phases(events) == [
        ("scanning", FREQS[0]),
        ("active", FREQS[0]),
        ("dwelling", FREQS[0]),
    ]


# --- the API: capability-gated /scan -------------------------------------------------------

def _client(radio: MockRadio) -> TestClient:
    return TestClient(create_app(radio, api_token=TOKEN))


def test_scan_endpoint_sweeps_and_holds_on_cat_backend():
    radio = MockRadio(supports_cat=True, busy_frequencies={BUSY})
    resp = _client(radio).post("/scan", json={"frequencies": FREQS}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["held"] == BUSY


def test_scan_endpoint_publishes_scan_events_over_ws_in_order():
    radio = MockRadio(supports_cat=True, busy_frequencies={BUSY})
    client = _client(radio)
    with client.websocket_connect(f"/events?token={TOKEN}") as ws:
        ws.receive_json()  # initial status snapshot
        client.post("/scan", json={"frequencies": FREQS}, headers=AUTH)
        seen = [ws.receive_json() for _ in range(4)]
    assert [(e["type"], e["data"]["phase"]) for e in seen] == [
        ("scan", "scanning"),
        ("scan", "scanning"),
        ("scan", "active"),
        ("scan", "dwelling"),
    ]
    assert seen[2]["data"]["frequency"] == BUSY


def test_scan_endpoint_gated_501_names_capability_on_audio_only():
    radio = MockRadio(supports_cat=False)
    resp = _client(radio).post("/scan", json={"frequencies": FREQS}, headers=AUTH)
    assert resp.status_code == 501
    assert resp.json()["detail"]["capability"] == "scan"


def test_scan_not_advertised_on_audio_only_backend():
    caps = _client(MockRadio(supports_cat=False)).get("/capabilities", headers=AUTH).json()
    assert "scan" not in caps


def test_scan_endpoint_rejects_ambiguous_body():
    radio = MockRadio(supports_cat=True)
    # Neither addressing form provided.
    assert _client(radio).post("/scan", json={}, headers=AUTH).status_code == 422


def test_scan_endpoint_requires_token():
    radio = MockRadio(supports_cat=True)
    assert _client(radio).post("/scan", json={"frequencies": FREQS}).status_code == 401
