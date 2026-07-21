"""The UV-K5's mandatory transmitter time-out — the docked-UV-K6 stuck-key gate (ADR 0117).

The UV-K5 in full-control (XVFO) mode has NO device-side runaway-TX cutoff (unlike the kv4p's
firmware ~200 s cap or the UV-5R's TOT menu), so the server `TotRadio` is the ONLY protection. Two
things must hold and are proven here without hardware:

- **Expiry force-unkeys with the FULL restore.** `TotRadio._fire()` calls `ptt(False)`, which on the
  UV-K5 routes to `_key_off` — RX registers restored on the wire FIRST, then the audio teardown — not
  a bare PTT drop. The register-file assertion is against the firmware-accurate `FirmwareFakeSerial`.
- **The cap is mandatory.** `uvk5.tot` may be shortened but never disabled (0) nor lengthened past its
  180 s default, and `build_radio` resolves it for the uvk5 backend even when the global `tx.tot=0`.

The cross-thread alarm (`TotRadio` fires on a `threading.Timer` thread; the hub is async) is proven
end-to-end over the `/events` WebSocket.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from radio_server.api.app import create_app
from radio_server.api.holder import resolve_tot
from radio_server.backends.mock import MockRadio
from radio_server.backends.uvk5.radio import DEFAULT_TOT, _REG30_TX_ENABLED
from radio_server.tx.tot import TotRadio

from .conftest import make_settings
from .test_tx_tot import FakeTimerFactory
from .test_uvk5_radio import make_radio, reg_writes
from .test_uvk5_transport import FirmwareFakeSerial


# --- the crux: expiry performs the full RX-register restore, not a bare PTT drop --------------

def test_tot_expiry_restores_the_rx_register_file():
    fake = FirmwareFakeSerial()
    radio = make_radio(fake, frequency=442_000_000)
    factory = FakeTimerFactory()
    tot = TotRadio(radio, tot=180.0, timer_factory=factory)

    tot.ptt(True)
    assert fake.registers[0x30] == _REG30_TX_ENABLED  # keyed on the wire

    factory.fire_latest()  # tot seconds elapse mid-over — fires on the (fake) timer thread
    assert radio._keyed is False
    # The register FILE (not just the write log) is back in RX — the transmitter is truly un-keyed.
    assert fake.registers[0x30] == radio._reg30
    # And it got there via the full _key_off pair (RX restore), not a lone PTT-line drop.
    assert reg_writes(fake)[-2:] == [(0x30, 0), (0x30, radio._reg30)]


def test_normal_key_unkey_under_the_cap_restores_rx_and_never_fires():
    fake = FirmwareFakeSerial()
    radio = make_radio(fake, frequency=442_000_000)
    factory = FakeTimerFactory()
    tot = TotRadio(radio, tot=180.0, timer_factory=factory)

    tot.ptt(True)
    tot.ptt(False)  # an ordinary unkey, well under the cap
    assert factory.live is None  # the watchdog was cancelled, so a stale fire can't unkey a later over
    assert radio._keyed is False
    assert fake.registers[0x30] == radio._reg30  # RX restored by the normal path


# --- the cap is mandatory: config bounds + per-backend resolution -----------------------------

def test_uvk5_tot_defaults_to_the_backend_declared_value():
    assert make_settings({}).get("uvk5.tot") == DEFAULT_TOT


def test_uvk5_tot_may_be_shortened():
    assert make_settings({"uvk5.tot": "90"}).get("uvk5.tot") == 90.0


@pytest.mark.parametrize("bad", ["0", "-1", "9999"])
def test_uvk5_tot_rejects_disable_and_over_ceiling(bad):
    # 0/negative (the "disable" the global tx.tot allows) and any value above the 180 s default are
    # rejected at load — the UV-K6 has no device backstop, so its server cap can only be shortened.
    with pytest.raises(RuntimeError, match="uvk5.tot"):
        make_settings({"uvk5.tot": bad})


def test_resolve_tot_uvk5_uses_its_mandatory_key_even_when_global_is_disabled():
    settings = make_settings({
        "server.backend": "uvk5",
        "uvk5.serial_port": "/dev/ttyACM0",
        "uvk5.frequency": "442000000",
        "uvk5.tot": "120",
        "tx.tot": "0",  # the operator disabled the global server cap...
    })
    assert resolve_tot(settings) == 120.0  # ...but uvk5 is protected by its own mandatory key


def test_resolve_tot_other_backends_use_the_global_tx_tot():
    assert resolve_tot(make_settings({"server.backend": "mock", "tx.tot": "90"})) == 90.0
    # The global disable still applies to non-uvk5 backends (their firmware/radio-side TOTs cover them).
    assert resolve_tot(make_settings({"server.backend": "mock", "tx.tot": "0"})) == 0.0


# --- the non-silent alarm reaches /events across the timer-thread boundary --------------------

def test_tot_expiry_publishes_an_alarm_event_over_the_events_ws():
    # Backend-agnostic (a MockRadio suffices): the point is create_app wiring the initial radio's hook
    # and the threading.Timer → asyncio hop. Firing on the (fake) timer thread must still surface an
    # "alarm" event on the reactive path — no polling, no new UI machinery.
    factory = FakeTimerFactory()
    tot_radio = TotRadio(MockRadio(supports_cat=True), tot=180.0, timer_factory=factory)
    client = TestClient(create_app(tot_radio, api_token="tot-token"))
    with client:  # enter the lifespan so the app loop is captured for call_soon_threadsafe
        with client.websocket_connect("/events?token=tot-token") as ws:
            assert ws.receive_json()["type"] == "status"  # initial snapshot
            tot_radio.ptt(True)  # arm the watchdog
            factory.fire_latest()  # TOT fires on the timer thread
            event = ws.receive_json()
    assert event["type"] == "alarm"
    assert event["data"] == {"kind": "tx_timeout", "tot": 180.0}
