"""The guard that catches an inert cadence pause hook — and the tests it never had (ADR 0178).

ADR 0177 found `getattr(radio, "transmitting", False)` answering `False` for ever against
`Uvk5Radio`, which has no such attribute, and added a WARNING at `create_app` for the class rather
than the instance. That warning had **no test**: a grep of `tests/` for its message text returned
nothing. So the guard for "a check that reports safe because it never ran" was itself a check that
had never run — ADR 0167's shape, one level out.

It also ran exactly once, in the `create_app` body, against the radio it was passed. `POST
/radio/select` rebinds the composition root's `radio` and never re-asked. The *behaviour* followed
the swap correctly, because `_station_wants_a_quiet_wire` is a late-binding closure; only the
diagnostic was frozen. That asymmetry is worth pinning from both sides, because the obvious repair —
resolve the hook once at startup and cache it — would fix the warning by breaking the cadence.

Nothing else in this suite asserts on a WARNING from `create_app`, so the `caplog` shape here is the
first of its kind. It follows the house form from `tests/test_aioc_baofeng_tuning.py`:
`caplog.at_level(..., logger=<dotted name>)`, then assert on records filtered by level and logger.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from radio_server.api import create_app
from radio_server.api.app import (
    _pause_source,
    _warn_if_the_cadence_cannot_tell_the_station_is_keyed,
)
from radio_server.backends import MockRadio, available_backends
from tests.test_backend_select import AUTH, TOKEN, _client, _write_config

APP_LOGGER = "radio_server.api.app"


class ProbeableButDeafToItsOwnCarrier(MockRadio):
    """ADR 0177's exact shape: it can be polled for broadcast FM and cannot say when it is keyed.

    `MockRadio` carries a private `_transmitting` and no public `transmitting`, and no
    `cadence_paused` — so this double trips both halves of the check without any further help. That
    it is so easy to build is the point: one method is all that separates a shipped backend from
    this.
    """

    def probe_broadcast_fm(self, **kwargs):
        return None


class Pausable(MockRadio):
    """The other side: a probe *and* a predicate, which is what `AiocBaofeng` actually has."""

    def probe_broadcast_fm(self, **kwargs):
        return None

    def cadence_paused(self) -> bool:
        return False


# --- the resolver -----------------------------------------------------------------------------


def test_the_pause_source_is_named_rather_than_inferred():
    """One resolver, so the warning and the cadence cannot disagree about which hook is live.

    Before this cycle they were two separate `getattr` chains computing the same thing a few lines
    apart — itself a latent instance of the class this ADR is about.
    """
    assert _pause_source(Pausable()) == "cadence_paused"
    assert _pause_source(ProbeableButDeafToItsOwnCarrier()) is None
    assert _pause_source(MockRadio()) is None


def test_a_backend_that_can_be_probed_but_cannot_say_when_it_is_keyed_is_warned_about(caplog):
    with caplog.at_level(logging.WARNING, logger=APP_LOGGER):
        message = _warn_if_the_cadence_cannot_tell_the_station_is_keyed(
            ProbeableButDeafToItsOwnCarrier()
        )
    assert message is not None
    warnings = [r for r in caplog.records if r.levelname == "WARNING" and r.name == APP_LOGGER]
    assert len(warnings) == 1
    assert "ProbeableButDeafToItsOwnCarrier" in warnings[0].getMessage(), (
        "the warning must name the type, or an operator cannot act on it"
    )


def test_a_backend_with_a_predicate_is_not_warned_about(caplog):
    """The paired negative. Without it, a resolver that warns unconditionally passes the test
    above."""
    with caplog.at_level(logging.WARNING, logger=APP_LOGGER):
        assert _warn_if_the_cadence_cannot_tell_the_station_is_keyed(Pausable()) is None
    assert [r for r in caplog.records if r.name == APP_LOGGER] == []


def test_a_backend_with_no_probe_at_all_is_not_warned_about(caplog):
    """A cadence that never polls cannot collide with a key-up, so there is nothing to say. This is
    why the warning is unreachable in production today, and the reason is a *coincidence between two
    backends' attribute sets*, not a design — which is what the next test exists to re-check."""
    with caplog.at_level(logging.WARNING, logger=APP_LOGGER):
        assert _warn_if_the_cadence_cannot_tell_the_station_is_keyed(MockRadio()) is None
    assert [r for r in caplog.records if r.name == APP_LOGGER] == []


# --- the instrument: every backend this project ships ------------------------------------------

#: Why the ADR 0177 cadence hazard does not reach each shipped backend. The value is a **reason**,
#: not a flag: a row you cannot write a sentence for is a row nobody has thought about. Adding a
#: backend fails the registry assertion below with "you have not written a reason" rather than with
#: a diff somebody accepts.
EXPECTED = {
    "mock": "no probe_broadcast_fm — the cadence never polls, so it cannot collide with a key-up",
    "v71": "not constructible; SignaLinkV71.__init__ raises NotImplementedError",
    "baofeng": "has a probe AND cadence_paused() (aioc_baofeng.py) — the deployed station, guarded "
               "by ADR 0177's wire reservation",
    "kv4p": "no probe, and no shared PTT/control wire — audio and keying are separate USB paths",
    "uvk5": "no probe_broadcast_fm ON THE RADIO (only on the tuner). ADR 0177's open finding: the "
            "physical hazard is real and there is no reservation anywhere. The day this backend "
            "gains a probe, this row is a FIX, not an update.",
}


def test_no_backend_this_project_ships_can_reach_the_inert_hook_warning():
    """Green today, and that is the point: it is what re-checks the coincidence.

    ADR 0177 recorded "harmless *today* only because that backend has no `probe_broadcast_fm`" in a
    HANDOFF paragraph. A paragraph is true on the day it is written and nothing re-checks it; this
    goes red the day it stops being true. It asserts the **consequence** — does the composition root
    warn about this radio — rather than an attribute list, so a rename follows the resolver instead
    of blinding the guard (the residual ADR 0167 recorded against source scans).
    """
    assert set(EXPECTED) == set(available_backends()), (
        "a backend was added or removed. Write the sentence saying why the ADR 0177 cadence hazard "
        "does not reach it — or fix it."
    )
    for name, radio in _shipped_backends():
        assert _warn_if_the_cadence_cannot_tell_the_station_is_keyed(radio) is None, (
            f"{name}: {EXPECTED[name]}\n\n"
            "This backend can now be polled for broadcast FM and cannot say when it is keyed. ADR "
            "0175 measured what that costs: the witness recovered the 1000 Hz tone at 0.026 against "
            "0.989. ADR 0177 measured worse — one control exchange across the DTR assert and the "
            "station did not radiate at all (0 of 81 carrier polls). Choose one:\n"
            "  (a) give it a cadence_paused() — a plain flag read, NO I/O (see aioc_baofeng.py);\n"
            "  (b) give it the wire reservation _key_on has (ADR 0177); or\n"
            "  (c) do not build the cadence over it, and say so in EXPECTED with the measurement."
        )


def _shipped_backends():
    """Every constructible backend in the registry, built the way its own test module builds it.

    `v71` is absent deliberately and is covered by the registry assertion instead: its `__init__`
    raises `NotImplementedError`, so there is no instance for any check to run against. That is an
    enforcer ("harmless by construction") rather than an omission.
    """
    from tests.test_aioc_baofeng_tuning import make_tuned
    from tests.test_kv4p_radio import FakeTransport
    from tests.test_kv4p_radio import make_radio as make_kv4p
    from tests.test_uvk5_radio import make_radio as make_uvk5
    from tests.test_uvk5_transport import FirmwareFakeSerial

    yield "mock", MockRadio()
    yield "baofeng", make_tuned()[0]
    yield "kv4p", make_kv4p(FakeTransport())
    yield "uvk5", make_uvk5(FirmwareFakeSerial())


# --- the composition root, and the swap it never re-asked --------------------------------------


def test_create_app_warns_about_the_radio_it_was_handed(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger=APP_LOGGER):
        create_app(ProbeableButDeafToItsOwnCarrier(), api_token=TOKEN)
    assert "ProbeableButDeafToItsOwnCarrier" in caplog.text


def test_selecting_a_new_backend_re_asks_whether_the_cadence_can_tell_it_is_keyed(
    tmp_path, caplog
):
    """The gap: `radio` is rebound on select and the check was never re-run.

    Three guards, none optional. The initial radio is proven clean, so a warning from `create_app`
    cannot satisfy the final assertion. `caplog.clear()` separates the two scopes. And the swap is
    proven to have *happened* — 200 plus the active name actually changing — because without that
    this test goes green the day `POST /radio/select` starts refusing.
    """
    def factory(settings):
        name = settings.get("server.backend")
        return ProbeableButDeafToItsOwnCarrier() if name == "kv4p" else MockRadio()

    with caplog.at_level(logging.WARNING, logger=APP_LOGGER):
        client = _client(tmp_path, radio_factory=factory)
    assert "ProbeableButDeafToItsOwnCarrier" not in caplog.text, "the initial radio must be clean"
    caplog.clear()

    with client, caplog.at_level(logging.WARNING, logger=APP_LOGGER):
        resp = client.post("/radio/select", headers=AUTH, json={"backend": "kv4p"})
        assert resp.status_code == 200, resp.text
        assert client.get("/radio/backends", headers=AUTH).json()["active"] == "kv4p"
        assert "ProbeableButDeafToItsOwnCarrier" in caplog.text


def test_the_cadence_pause_check_follows_the_newly_selected_radio(tmp_path):
    """Green on master, and pinned so it stays that way.

    The *behaviour* is already correct — `_station_wants_a_quiet_wire` closes over the composition
    root's `radio` and reads it per call. The tempting way to fix the warning above is to resolve the
    hook once and cache it, which would fix the diagnostic by freezing the cadence against the
    previous radio. This is the test that refuses that repair.
    """
    keyed = Pausable()
    keyed.cadence_paused = lambda: True  # type: ignore[method-assign]

    def factory(settings):
        return keyed if settings.get("server.backend") == "kv4p" else MockRadio()

    with _client(tmp_path, radio_factory=factory) as client:
        cadence = client.app.state.broadcast_fm_cadence
        assert cadence.stats()["skipped"] == 0
        assert client.post(
            "/radio/select", headers=AUTH, json={"backend": "kv4p"}
        ).status_code == 200
        cadence.poll_once()
        assert cadence.stats()["skipped"] == 1, (
            "the cadence is still asking the radio it was built with, not the one that is live"
        )
