"""`POST /auth/session` — open the OTA session from the web UI's code chip (ADR 0046).

Posture proofs: the LAN token is the credential (401 without it), the open has the same on-air
effect as a DTMF login (welcome over, `/status` session_open flips), it NEVER burns a TOTP code
(an RF caller's same-window code still verifies after a UI open), it 503s with no controller (the
hide/inert signal), and a repeat click refreshes rather than re-announcing.

`POST /server/restart` (ADR 0047) lives here too — the other operator-plane POST added in the
same cycle: it hands the configured command to an injectable runner, 503s when unconfigured, and
is token-gated like everything else.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from radio_server.api import create_app
from radio_server.auth import TotpVerifier
from radio_server.backends import MockRadio

from .conftest import TEST_SECRET, make_secrets
from .test_controller import AUTH, TOKEN, build_ctrl

# --- POST /auth/session -----------------------------------------------------------------------


def _session_client(clock, **build_kwargs):
    radio, ctrl = build_ctrl(clock, [], **build_kwargs)
    app = create_app(
        radio,
        api_token=TOKEN,
        controller=ctrl,
        secrets=make_secrets(api_token=TOKEN, totp_secret=TEST_SECRET),
    )
    return radio, ctrl, TestClient(app)


def test_open_session_opens_and_flips_status(clock):
    radio, ctrl, client = _session_client(clock)
    with client:
        assert client.get("/status", headers=AUTH).json()["controller"]["session_open"] is False
        body = client.post("/auth/session", headers=AUTH).json()
        assert body == {"opened": True, "session_open": True}
        assert client.get("/status", headers=AUTH).json()["controller"]["session_open"] is True


def test_open_session_repeat_reports_already_open(clock):
    radio, ctrl, client = _session_client(clock)
    with client:
        client.post("/auth/session", headers=AUTH)
        body = client.post("/auth/session", headers=AUTH).json()
        assert body == {"opened": False, "session_open": True}


def test_open_session_transmits_the_welcome_over(clock):
    radio, ctrl, client = _session_client(
        clock, settings_extra={"controller.login_announcement": "Welcome."}
    )
    with client:
        client.post("/auth/session", headers=AUTH)
        assert len(radio.tx_log) == 1  # one over: the armed ID + spoken welcome


def test_open_session_burns_no_totp_code(clock):
    # After a UI open, the code displayed in that same window must still work over RF (ADR 0046):
    # the endpoint never touches the verifier, so `verify_and_burn` still gets its single use.
    radio, ctrl, client = _session_client(clock)
    with client:
        code = client.get("/auth/totp", headers=AUTH).json()["code"]
        client.post("/auth/session", headers=AUTH)
        assert TotpVerifier(TEST_SECRET).verify_and_burn(code) is True


def test_open_session_503_when_no_controller():
    client = TestClient(create_app(MockRadio(), api_token=TOKEN))
    resp = client.post("/auth/session", headers=AUTH)
    assert resp.status_code == 503
    assert "controller" in resp.json()["detail"]


def test_open_session_requires_the_token(clock):
    _, _, client = _session_client(clock)
    with client:
        assert client.post("/auth/session").status_code == 401


# --- POST /server/restart (ADR 0047) ----------------------------------------------------------


def _restart_client(*, command, runner):
    return TestClient(
        create_app(
            MockRadio(), api_token=TOKEN, restart_command=command, restart_runner=runner
        )
    )


def test_restart_hands_the_configured_command_to_the_runner():
    ran: list[str] = []
    client = _restart_client(command="systemctl --user restart radio-server", runner=ran.append)
    with client:
        resp = client.post("/server/restart", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json() == {"restarting": True}
        # The spawn is deliberately delayed (~0.3 s) so the reply beats the stop signal; run the
        # loop's pending timers by making another request after the delay elapses.
        import time

        time.sleep(0.35)
        client.get("/status", headers=AUTH)  # a loop turn fires the call_later callback
    assert ran == ["systemctl --user restart radio-server"]


def test_restart_503_when_unconfigured():
    ran: list[str] = []
    client = _restart_client(command="", runner=ran.append)
    with client:
        resp = client.post("/server/restart", headers=AUTH)
    assert resp.status_code == 503
    assert "restart_command" in resp.json()["detail"]
    assert ran == []


def test_restart_requires_the_token():
    client = _restart_client(command="true", runner=lambda cmd: None)
    with client:
        assert client.post("/server/restart").status_code == 401


def test_settings_surface_restart_availability():
    on = _restart_client(command="true", runner=lambda cmd: None)
    off = _restart_client(command="", runner=lambda cmd: None)
    with on, off:
        assert on.get("/settings", headers=AUTH).json()["restart_available"] is True
        assert off.get("/settings", headers=AUTH).json()["restart_available"] is False
