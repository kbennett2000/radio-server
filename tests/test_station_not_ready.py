"""A station that has not been tuned yet says so with a 409, not a 422 and not a crash (ADR 0173).

Two conditions look alike from a distance and are not alike at all:

- **the value is wrong** — `POST /mode {"mode":"AM"}`. The client can fix it by changing the body.
  That is a **422**, and ADR 0172 is the cycle that made it one.
- **the value is fine and the station is not ready** — `POST /mode {"mode":"FM"}` before any
  frequency has been set, which is the state every service restart leaves behind. There is no body
  the client could send that would make an untuned station tuned. The remedy is a *different call*
  (`POST /frequency`) and then this one again, unchanged. That is a **409**.

Until this cycle the second answered the first's code: both raised `ValueError` on `AiocBaofeng`, so
ADR 0172's arm caught both and gave the readiness refusal the bad-value code. `Uvk5Radio` expressed
the same condition as a `Uvk5KeyingError` that no route catches — a latent 500 rather than a live
one, because its constructor seeds `_frequency` from the radio's own VFO and the guard can never
fire. Measured, not assumed, and it is a finding in its own right.

Before this file existed, exactly one test in the suite touched any of it: `test_aioc_baofeng_
tuning.py`'s *a setter before any frequency is refused not guessed*, whose real subject is the line
after the `raises` — that nothing reached the radio. **Nothing asserted what a client sees**, on any
backend, at any layer, which is how a readiness refusal wore a value error's status code across
seventeen ADRs without anyone noticing.

**The instrument is the point.** ADR 0172 finding 1 recorded that `MockRadio` could not have found
this: a double with no ordering constraint has no ordering constraint to violate, and only the bench
saw it. So these tests mount a **real backend over a fake serial** behind the real `create_app` —
`SpyTuner`/`FakeSerial` for baofeng, `FirmwareFakeSerial` for the UV-K5 — and drive real HTTP at it.
No hardware, and no double standing in for the state model that is the whole subject.

Note on the red runs these were written against: `TestClient` defaults to
``raise_server_exceptions=True``, so on master the uvk5 cases surfaced as the backend exception
escaping the client rather than as a `500` response object. That is the same 500 a real server
returns; it is only spelled differently in-process (ADR 0172 recorded the same thing). The committed
assertion is the 409, which is true under either client.
"""

from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from radio_server.api import create_app
from radio_server.backends import MockRadio, RadioBusy, RadioNotReady, RadioUnavailable
from tests.test_aioc_baofeng_tuning import make_tuned
from tests.test_uvk5_radio import make_radio
from tests.test_uvk5_transport import FirmwareFakeSerial

TOKEN = "test-lan-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _client(radio) -> TestClient:
    return TestClient(create_app(radio, api_token=TOKEN))


def _untuned_baofeng():
    """A real `AiocBaofeng` with a tuner attached and no frequency ever set.

    `make_tuned` builds the radio; it does not tune it. That is exactly the post-restart state —
    the radio in the room is still on whatever frequency it was left on, but the host has no
    channel staged and cannot build one.
    """
    radio, _tuner = make_tuned()
    return radio


def _uvk5_with_no_frequency():
    """A `Uvk5Radio` forced into a state its own constructor makes unreachable — see the finding.

    `Uvk5Radio.__init__` seeds `self._frequency` from registers 0x38/0x39 at connect, so it is
    `None` only for the handful of lines between the attribute's declaration and the register read.
    Its two "before a frequency is set" guards are therefore **dead code**: measured, not assumed —
    a freshly built radio over the firmware fake reports `_frequency == 0`, and `POST /ptt` against
    it returns **200** rather than the 500 this cycle went looking for.

    They are still converted to `RadioNotReady`, and this test still pins the type. A guard that
    cannot fire today is exactly the kind that fires in three years after someone makes the boot
    probe lazy or optional, and on that day it should already be saying the same thing the rest of
    the fleet says. `tx_allowed` is explicit because `_key_on` checks the RF gate *before* the
    frequency, and a receive-only node refusing for that reason would prove nothing here.
    """
    radio = make_radio(FirmwareFakeSerial(), tx_allowed=True)
    radio._frequency = None
    return radio


# --- the tuning routes: a conflict, not a bad request ------------------------------------------

def test_mode_on_an_untuned_station_is_a_conflict_not_a_bad_value():
    """`FM` is the most valid value there is. 422 would send the operator to fix a correct word."""
    resp = _client(_untuned_baofeng()).post("/mode", json={"mode": "FM"}, headers=AUTH)
    assert resp.status_code == 409


def test_tone_on_an_untuned_station_is_a_conflict_not_a_bad_value():
    resp = _client(_untuned_baofeng()).post("/tone", json={"tone": 100.0}, headers=AUTH)
    assert resp.status_code == 409


def test_split_on_an_untuned_station_is_a_conflict_not_a_bad_value():
    resp = _client(_untuned_baofeng()).post("/split", json={"tx_hz": 147_555_000}, headers=AUTH)
    assert resp.status_code == 409


def test_a_route_with_no_exception_handling_at_all_still_answers_the_conflict():
    """The omission proof, and the reason the fix is a handler rather than more `except` arms.

    `POST /ptt` is three lines with no `try` in them (`app.py`: `radio.ptt(body.on)`). It has never
    caught anything, and nobody adding it thought about a radio that was not ready. It answers 409
    anyway, because the status code follows from the exception's *type* and not from what the route
    remembered. A route added tomorrow gets this by writing no code at all.

    A double rather than a real backend here on purpose: the subject is the **handler**, not any
    backend's state model, and no shipping backend refuses a key-up for readiness today (see
    `_uvk5_with_no_frequency`). The real-state evidence is the baofeng cases above.
    """

    class _NotReady(MockRadio):
        def ptt(self, on):
            raise RadioNotReady("set a frequency before keying")

    resp = _client(_NotReady(supports_cat=True)).post("/ptt", json={"on": True}, headers=AUTH)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "set a frequency before keying"


# --- what the refusal carries ------------------------------------------------------------------

def test_the_refusal_names_the_remedy():
    """A 409 whose body did not say what to do first would be worse than the 422 it replaces."""
    body = _client(_untuned_baofeng()).post("/mode", json={"mode": "FM"}, headers=AUTH).json()
    assert body["detail"] == "set a frequency before a split, tone or mode"


def test_a_refused_call_moves_nothing():
    """The refusal happens before anything is staged, so the station is exactly as it was."""
    radio = _untuned_baofeng()
    client = _client(radio)
    before = client.get("/status", headers=AUTH).json()
    assert client.post("/mode", json={"mode": "NFM"}, headers=AUTH).status_code == 409
    assert client.get("/status", headers=AUTH).json() == before


# --- pins: the 422 that ADR 0172 built is still a 422 ------------------------------------------

def test_a_bad_value_on_a_tuned_station_is_still_a_422():
    """ADR 0172's arm, unchanged. If this cycle had swallowed it, every bad word would be a 409."""
    radio = _untuned_baofeng()
    client = _client(radio)
    assert client.post("/frequency", json={"hz": 147_555_000}, headers=AUTH).status_code == 200
    resp = client.post("/mode", json={"mode": "AM"}, headers=AUTH)
    assert resp.status_code == 422
    assert "FM or NFM" in resp.json()["detail"]


def test_the_value_check_runs_before_the_readiness_check():
    """An untuned station sent a bad word answers **422**, not 409 — and that is deliberate.

    Both faults are true at once, so the order decides what the operator is told. Naming the value
    is the more useful answer: fixing the frequency first would only surface the bad word on the
    next call. Same shape as ADR 0172's rule that `_require_cat` runs before the value check, and
    only a request that is wrong *both* ways can tell the two orders apart.

    Confirmed on the station before this cycle deployed: untuned + `AM` → 422 `mode must be FM or
    NFM, got 'AM'`, while untuned + `FM` → the readiness refusal.
    """
    resp = _client(_untuned_baofeng()).post("/mode", json={"mode": "AM"}, headers=AUTH)
    assert resp.status_code == 422
    assert "FM or NFM" in resp.json()["detail"]


# --- the hierarchy the two status codes rest on ------------------------------------------------

def test_not_ready_is_neither_a_value_error_nor_an_unavailability():
    """Both status codes depend on this, and both break silently if someone "tidies" the tree.

    A `ValueError` subclass would be caught by the per-route 422 arms ADR 0172 added, *before* the
    app-wide handler ever ran — the sequencing error would go straight back to wearing the value
    error's code. A `RadioUnavailable` subclass would be a 503 the moment a route did not catch it,
    which is the accident of inheritance ADR 0164 introduced `RadioBusy` to escape.
    """
    assert not issubclass(RadioNotReady, ValueError)
    assert not issubclass(RadioNotReady, RadioUnavailable)


def test_an_uncaught_busy_is_a_conflict_and_not_an_unavailability():
    """`RadioBusy` is a `RadioUnavailable`, so without its own handler it is a 503 by inheritance.

    Only `/broadcast-fm` catches it locally. Every other route — including this one — got the
    hardware-fault code for a radio that answered promptly and said *not right now*, which is the
    exact trap `RadioBusy`'s own docstring names.
    """

    class _Busy(MockRadio):
        def set_mode(self, mode):
            raise RadioBusy("the radio is mid-over and declined")

    resp = _client(_Busy(supports_cat=True)).post("/mode", json={"mode": "FM"}, headers=AUTH)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "the radio is mid-over and declined"


def test_the_backends_agree_on_the_type_they_raise():
    """The fleet said this three ways. It says it one way now, which is what the handler needs.

    Asserted at the backend rather than through HTTP because that is where the knowledge lives: no
    route could check "has this radio been tuned" without reimplementing the backend's state model,
    which is precisely why the fix belongs to the exception type and not to a route.
    """
    baofeng = _untuned_baofeng()
    with pytest.raises(RadioNotReady):
        baofeng.set_mode("FM")
    with pytest.raises(RadioNotReady):
        baofeng.set_tone(100.0)
    with pytest.raises(RadioNotReady):
        baofeng.set_split(147_555_000)

    # Unreachable through the constructor, pinned anyway — see `_uvk5_with_no_frequency`.
    with pytest.raises(RadioNotReady):
        _uvk5_with_no_frequency().set_split(145_000_000)
    with pytest.raises(RadioNotReady):
        _uvk5_with_no_frequency().ptt(True)
