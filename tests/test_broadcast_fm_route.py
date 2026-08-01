"""`POST /broadcast-fm` — the ON path, and the first host-side way out (ADR 0164).

Four cycles of findings say the same thing from four sides: ADR 0158 **R4** (a capability with no
`Radio`-level method), ADR 0160 **finding 3** (the documented remedy is "press EXIT on the radio"),
ADR 0161 **finding 2** (*"pressing Talk is the operator's only way out of broadcast FM from the host
side"*), ADR 0162 finding 8 (unmoved). This route closes all of them, in both directions.

The load-bearing proofs here are about **refusals**, not about the happy path:

- **A refusal that arrives is an answer; only silence is unavailability.** `TuneBusy` subclasses
  `RadioUnavailable`, so without a route-level catch every courtesy refusal from the radio would
  reach the app-wide handler and be reported as a **503** — "this radio is unavailable" — for a radio
  that answered promptly and named a condition it will be out of in a second. Those are **409**s, and
  the tests assert the number rather than merely that it failed.
- **Refuse, never round** (ADR 0156). 100 kHz off the raster is a whole adjacent station, so an
  off-raster frequency is a 422 **and no frame reaches the radio** — the assertion is on the radio's
  state, because a 422 raised after the wire write would pass a test that only checked the code.
- **The 501 names which capability is missing**, and the two are separately earned: an image with no
  BK1080 (`ERR_NO_HAL`) can answer `0x0879` — earning `clear_broadcast_fm` — while having nothing to
  switch on. That combination is reachable, and `docs/api.md` tells a client what to conclude from it.
"""

from dataclasses import replace

import pytest

from fastapi.testclient import TestClient

from radio_server.api import create_app
from radio_server.backends import Capability, MockRadio, RadioUnavailable
from radio_server.backends.base import BroadcastFm
from radio_server.backends.uvk5.tuner import TuneBusy, TuneError

TOKEN = "test-lan-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

#: 104.3 MHz — the station the bench radio has been left on since ADR 0160, on the raster.
ON_AIR_HZ = 104_300_000


def _client(radio: MockRadio) -> TestClient:
    return TestClient(create_app(radio, api_token=TOKEN))


def _raiser(exc: Exception):
    def _go(*_args, **_kwargs):
        raise exc

    return _go


# --- the happy path, both directions ------------------------------------------------------


def test_on_starts_the_second_receiver_and_reports_the_read_back():
    """The reply reports the frequency the radio is **actually** on (ADR 0156), not what was asked.

    The mock echoes here because it has no raster to fall foul of, but the field being read out of
    `status()` rather than out of the request body is the contract this pins.
    """
    radio = MockRadio(supports_cat=True)
    resp = _client(radio).post(
        "/broadcast-fm", json={"action": "on", "hz": ON_AIR_HZ, "band": 0}, headers=AUTH
    )
    assert resp.status_code == 200
    block = resp.json()["broadcast_fm"]
    assert block["on"] is True
    assert block["hz"] == ON_AIR_HZ
    assert block["band"] == 0


def test_off_is_the_first_host_side_way_out_of_broadcast_fm():
    """ADR 0158 R4 / 0160 finding 3 / 0161 finding 2, closed.

    Until this route existed the only way out from the host side was to press Talk and let the
    key-up path's rescue do it as a side effect — which meant an unattended LAN station's remedy was
    a hand on a keypad in another room.
    """
    radio = MockRadio(supports_cat=True, left_in_broadcast_fm=True)
    assert radio.status().broadcast_fm.on is True
    resp = _client(radio).post("/broadcast-fm", json={"action": "off"}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["broadcast_fm"]["on"] is False


def test_tune_moves_a_running_receiver():
    radio = MockRadio(supports_cat=True, left_in_broadcast_fm=True)
    resp = _client(radio).post(
        "/broadcast-fm", json={"action": "tune", "hz": 90_100_000, "band": 0}, headers=AUTH
    )
    assert resp.status_code == 200
    assert resp.json()["broadcast_fm"]["hz"] == 90_100_000


def test_the_route_publishes_a_status_event_so_the_ui_sees_it():
    """The web UI never polls `/status` — every live field arrives on the events socket
    (`useEvents.js`). A mutating route that did not publish would leave the card showing the
    opposite of what the radio is doing until something else happened to publish."""
    radio = MockRadio(supports_cat=True)
    with _client(radio) as client:
        with client.websocket_connect(f"/events?token={TOKEN}") as ws:
            ws.receive_json()  # the connect snapshot
            client.post(
                "/broadcast-fm", json={"action": "on", "hz": ON_AIR_HZ, "band": 0}, headers=AUTH
            )
            event = ws.receive_json()
    assert event["type"] == "status"
    assert event["data"]["broadcast_fm"]["on"] is True


# --- refuse, never round ------------------------------------------------------------------


def test_an_off_raster_frequency_is_refused_and_no_frame_is_sent():
    """ADR 0156: the BK1080 tunes on a 100 kHz raster and the next step is a **whole adjacent
    station**, so this refuses rather than rounding.

    The second assertion is the one that matters. A 422 raised *after* the write would satisfy a
    test that only read the status code, and would have moved a receiver nobody asked to move.
    """
    radio = MockRadio(supports_cat=True)
    resp = _client(radio).post(
        "/broadcast-fm", json={"action": "on", "hz": 104_350_000, "band": 0}, headers=AUTH
    )
    assert resp.status_code == 422
    assert "raster" in resp.json()["detail"] or "100" in resp.json()["detail"]
    assert radio.status().broadcast_fm is None      # nothing reached the radio


def test_the_refusal_names_the_frequency_that_was_asked_for():
    """Not "invalid frequency". The operator typed a number and has to be told which number."""
    radio = MockRadio(supports_cat=True)
    resp = _client(radio).post(
        "/broadcast-fm", json={"action": "on", "hz": 104_350_000, "band": 0}, headers=AUTH
    )
    assert "104350000" in resp.json()["detail"]


def test_a_band_above_three_is_refused_rather_than_clamped():
    """`gEeprom.FM_Band` is a **two-bit field**, so assigning 4 yields 0 — a clamp performed by the
    assignment operator itself, with no diagnostic anywhere, leaving the radio on 87.5-108 while the
    host believes it asked for something else (ADR 0156). Refused here, on the wire's own scale."""
    radio = MockRadio(supports_cat=True)
    resp = _client(radio).post(
        "/broadcast-fm", json={"action": "on", "hz": ON_AIR_HZ, "band": 4}, headers=AUTH
    )
    assert resp.status_code == 422
    assert radio.status().broadcast_fm is None


def test_an_unknown_action_is_a_422_naming_the_three_that_exist():
    resp = _client(MockRadio(supports_cat=True)).post(
        "/broadcast-fm", json={"action": "scan", "hz": ON_AIR_HZ}, headers=AUTH
    )
    assert resp.status_code == 422
    for word in ("off", "on", "tune"):
        assert word in resp.json()["detail"]


def test_on_without_a_frequency_is_a_422():
    """There is no default station. Picking one would be this server choosing what the operator
    listens to, and `0` is not a frequency."""
    resp = _client(MockRadio(supports_cat=True)).post(
        "/broadcast-fm", json={"action": "on"}, headers=AUTH
    )
    assert resp.status_code == 422


def test_off_ignores_a_stale_frequency_and_band_rather_than_refusing_on_them():
    """`Dock_SetFm` branches to `Dock_FmOff()` **before** the raster and band checks, so the OFF leg
    never reads either field — the fork proves it by sending deliberate junk in both and having it
    accepted (`test_dock.c:1272`). Refusing here would be this server inventing a rule the firmware
    does not have (ADR 0156)."""
    radio = MockRadio(supports_cat=True, left_in_broadcast_fm=True)
    resp = _client(radio).post(
        "/broadcast-fm",
        json={"action": "off", "hz": 104_350_000, "band": 9},   # junk in both
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["broadcast_fm"]["on"] is False


# --- a refusal that arrives is an answer --------------------------------------------------


def test_err_tx_is_a_409_not_a_503():
    """The deliverable of this cycle's status-code work.

    `TuneBusy` is a `TuneError` is a `RadioUnavailable`, and `create_app` installs an app-wide
    handler that turns any `RadioUnavailable` into a 503. So **503 is what this returns by accident
    of inheritance** unless the route catches it, and 503 is the wrong answer: the radio answered,
    promptly, and named a condition — it is keyed or somebody is holding MONITOR — that it will be
    out of in a second. That is a conflict, not an unavailability, and a client should retry rather
    than go looking for an unplugged cable.
    """
    radio = MockRadio(supports_cat=True)
    radio.set_broadcast_fm = _raiser(
        TuneBusy("the radio is transmitting or monitoring, so it declined (ERR_TX)")
    )
    resp = _client(radio).post(
        "/broadcast-fm", json={"action": "on", "hz": ON_AIR_HZ, "band": 0}, headers=AUTH
    )
    assert resp.status_code == 409
    assert "ERR_TX" in resp.json()["detail"]


def test_a_radio_that_never_answers_is_a_503():
    """The other half of the same rule: **only silence is unavailability.** No reply means the radio
    is switched off, unplugged, or running firmware that has no such command — a real fault with a
    real remedy, and the one case where 503 is the honest answer."""
    radio = MockRadio(supports_cat=True)
    radio.set_broadcast_fm = _raiser(TuneError("no 0x087A reply to the set-broadcast-FM frame"))
    resp = _client(radio).post(
        "/broadcast-fm", json={"action": "on", "hz": ON_AIR_HZ, "band": 0}, headers=AUTH
    )
    assert resp.status_code == 503


def test_the_radios_own_band_verdict_comes_back_as_a_422():
    """The host does **not** keep a copy of `BK1080_GetFreqLoLimit`'s `{875, 760, 760, 640}`.

    `dock.h` refuses to keep that table in `dock.c` because "a second copy of a hardware table … is a
    drift hazard", and the same argument reaches one layer further. So the band's own limits are the
    radio's verdict, asked for and reported — and it is still a 422, because it is still the
    operator's number that was wrong.
    """
    radio = MockRadio(supports_cat=True)
    radio.set_broadcast_fm = _raiser(
        ValueError("the radio refused: 64.0 MHz is outside band 0 (ERR_BAND)")
    )
    resp = _client(radio).post(
        "/broadcast-fm", json={"action": "on", "hz": 64_000_000, "band": 0}, headers=AUTH
    )
    assert resp.status_code == 422
    assert "ERR_BAND" in resp.json()["detail"]


def test_on_is_refused_mid_transmission_with_409():
    """Not politeness, and not the firmware's `ERR_TX` either — this one never reaches the wire.

    An over in progress is this station talking to somebody. Taking the speaker out from under it
    would end the over from the operator's own web UI, which is the shape `POST /modulation`'s 409
    already refuses for the same reason.
    """
    radio = MockRadio(supports_cat=True)
    client = _client(radio)
    client.app.state.arbiter.acquire_tx()
    resp = client.post(
        "/broadcast-fm", json={"action": "on", "hz": ON_AIR_HZ, "band": 0}, headers=AUTH
    )
    assert resp.status_code == 409
    assert radio.status().broadcast_fm is None      # nothing changed on the way to refusing


def test_off_is_allowed_mid_transmission():
    """The asymmetry is the whole point. Turning the second receiver **off** during an over gives
    the station its ears back — it is the direction the key-up path already takes on its own, and
    refusing it would leave the one safe action blocked by the unsafe one's guard."""
    radio = MockRadio(supports_cat=True, left_in_broadcast_fm=True)
    client = _client(radio)
    client.app.state.arbiter.acquire_tx()
    resp = client.post("/broadcast-fm", json={"action": "off"}, headers=AUTH)
    assert resp.status_code == 200


# --- the capability gate, and the pair -----------------------------------------------------


def test_on_is_501_naming_set_broadcast_fm_where_it_is_not_earned():
    radio = MockRadio(supports_cat=False)
    resp = _client(radio).post(
        "/broadcast-fm", json={"action": "on", "hz": ON_AIR_HZ, "band": 0}, headers=AUTH
    )
    assert resp.status_code == 501
    assert resp.json()["detail"]["capability"] == "set_broadcast_fm"


def test_off_is_501_naming_clear_broadcast_fm_where_it_is_not_earned():
    """The two are named separately in the 501 body because they are separately earned, and a UI
    that greys the wrong control is a UI that hides the remedy while showing the symptom."""
    radio = MockRadio(supports_cat=False)
    resp = _client(radio).post("/broadcast-fm", json={"action": "off"}, headers=AUTH)
    assert resp.status_code == 501
    assert resp.json()["detail"]["capability"] == "clear_broadcast_fm"


def test_a_radio_that_can_clear_but_not_set_still_gets_its_way_out():
    """The reachable one-and-not-the-other combination, and it is not hypothetical.

    An image built without `ENABLE_FMRADIO` answers `0x0879` with `ERR_NO_HAL`. That reply **earns**
    `clear_broadcast_fm` — it is a definitive negative, the station can never be deafened by a
    receiver that is not compiled in — and must **not** earn `set_broadcast_fm`, because there is
    nothing there to switch on. `docs/api.md` says exactly this, and this pins it.
    """
    radio = MockRadio(supports_cat=True)
    radio.capabilities = lambda: frozenset(
        c for c in MockRadio(supports_cat=True).capabilities()
        if c is not Capability.SET_BROADCAST_FM
    )
    client = _client(radio)
    assert client.post(
        "/broadcast-fm", json={"action": "on", "hz": ON_AIR_HZ, "band": 0}, headers=AUTH
    ).status_code == 501
    assert client.post("/broadcast-fm", json={"action": "off"}, headers=AUTH).status_code == 200


def test_capabilities_lists_both_on_a_cat_backend():
    caps = set(_client(MockRadio(supports_cat=True)).get("/capabilities", headers=AUTH).json())
    assert {"clear_broadcast_fm", "set_broadcast_fm"} <= caps


# --- the status block ----------------------------------------------------------------------


def test_the_status_block_carries_the_band_it_is_actually_on():
    """The route takes the band byte explicitly, so the read-back has to report it — otherwise half
    the answer to "where is this receiver" is missing and a host has to guess it back."""
    radio = MockRadio(supports_cat=True)
    _client(radio).post(
        "/broadcast-fm", json={"action": "on", "hz": 76_500_000, "band": 2}, headers=AUTH
    )
    body = _client(radio).get("/status", headers=AUTH).json()
    assert body["broadcast_fm"]["band"] == 2


def test_the_block_is_still_tri_state_and_null_is_not_off():
    """ADR 0157's rule, unchanged by anything this cycle adds: `null` is "nobody asked" and
    `{"on": false}` is "asked, and the answer is no". Rendering them the same is how a deaf station
    gets trusted."""
    body = _client(MockRadio(supports_cat=True)).get("/status", headers=AUTH).json()
    assert body["broadcast_fm"] is None


def test_a_deafened_station_still_refuses_to_key_after_the_route_turned_it_on():
    """The operator turning it on deliberately does not make the transmitter safe.

    `refuse_if_deafened` reads the same block this route now writes, so a station the operator has
    just put into broadcast FM refuses a key-up on exactly the evidence ADR 0158 built the interlock
    on. Nothing about the deliberateness of the choice makes the station able to hear.
    """
    radio = MockRadio(supports_cat=True)
    _client(radio).post(
        "/broadcast-fm", json={"action": "on", "hz": ON_AIR_HZ, "band": 0}, headers=AUTH
    )
    with pytest.raises(RadioUnavailable):
        radio.ptt(True)


def test_the_block_the_route_writes_is_a_full_reading_not_a_probes_worth():
    """ADR 0163 made `clear_broadcast_fm` the single writer of the block because the **probe** only
    ever receives a refusal, and a refusal blanks every field but the status byte — so a
    probe-written `on=True` would be a claim its evidence did not support.

    This route receives an `APPLIED` reply carrying state, frequency, band and flags. The invariant
    was never about arity; it was about writing from evidence that supports the claim.
    """
    radio = MockRadio(supports_cat=True)
    _client(radio).post(
        "/broadcast-fm", json={"action": "on", "hz": ON_AIR_HZ, "band": 0}, headers=AUTH
    )
    block = radio.status().broadcast_fm
    assert isinstance(block, BroadcastFm)
    assert (block.on, block.hz, block.band) == (True, ON_AIR_HZ, 0)
    # `replace` proves it is still the frozen dataclass rather than a dict that happens to serialise.
    assert replace(block, on=False).on is False


# --- the relay mute arms before the wire ---------------------------------------------------


def test_the_relay_mute_arms_before_the_frame_reaches_the_radio():
    """ADR 0164 decision 5, pinned at the composition root where the ordering actually lives.

    The bench measured the ON exchange at ~0.4 s (ADR 0160) and ADR 0163's cadence polls every
    2.0 s, so a mute that armed off the *reply* would relay up to 2.4 s of a commercial station onto
    whatever is at the far end of the link — which may be somebody else's repeater (97.113(b)). On
    the front-panel path that window is unavoidable and the ADR prices it; on this path the ordering
    is entirely ours, so the leak is zero.
    """
    radio = MockRadio(supports_cat=True)
    client = _client(radio)
    cadence = client.app.state.broadcast_fm_cadence
    cadence.observe(False)                       # the links are relaying normally

    seen: list = []
    real = radio.set_broadcast_fm

    def watched(*args, **kwargs):
        # What the relay mute would see at the instant the frame goes out.
        seen.append(cadence().on)
        return real(*args, **kwargs)

    radio.set_broadcast_fm = watched
    client.post(
        "/broadcast-fm", json={"action": "on", "hz": ON_AIR_HZ, "band": 0}, headers=AUTH
    )

    assert seen == [True], "the mute must arm BEFORE the frame, not off the reply"
    assert cadence().on is True                  # and stay armed once the reply confirms it


def test_a_refused_on_does_not_leave_the_links_muted():
    """The hold releases and the cadence's own reading decides. Leaving it armed after a refusal
    would silence both links for a receiver that was never switched on — and nothing would clear it
    until the next poll, or for ever if no bridge is running one."""
    radio = MockRadio(supports_cat=True)
    client = _client(radio)
    cadence = client.app.state.broadcast_fm_cadence
    cadence.observe(False)
    radio.set_broadcast_fm = _raiser(TuneBusy("declined (ERR_TX)"))

    assert client.post(
        "/broadcast-fm", json={"action": "on", "hz": ON_AIR_HZ, "band": 0}, headers=AUTH
    ).status_code == 409
    assert cadence().on is False


def test_turning_it_off_hands_the_cadence_the_read_back_too():
    """Symmetrical, and the direction that matters less: arming late costs a leak, disarming late
    costs a couple of seconds of the operator's own traffic. OFF is therefore never pre-armed —
    arming is only ever the pessimistic direction."""
    radio = MockRadio(supports_cat=True, left_in_broadcast_fm=True)
    client = _client(radio)
    cadence = client.app.state.broadcast_fm_cadence
    cadence.observe(True)

    client.post("/broadcast-fm", json={"action": "off"}, headers=AUTH)
    assert cadence().on is False
