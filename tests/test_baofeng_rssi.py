"""Signal strength on the deployed backend (ADR 0175).

``status.rssi`` was ``null`` on `AiocBaofeng` — the mode the station actually runs — so nothing
above the radio layer could see how strong a signal was, and ADR 0160's airband sweep had to measure
audio RMS across 760 channels instead of asking the radio. The reading was one `0x0851` away the
whole time: the backend hands its own open AIOC handle to a `Uvk5Transport` for tuning, and a
register read dispatches at top level in the firmware's ordinary non-blocking path.

**This module is also the harness that did not exist.** Until now nothing wired the
firmware-accurate `FirmwareFakeSerial` into `create_radio("baofeng", ...)`:
`test_aioc_baofeng_tuning.py` injects a `SpyTuner` over a serial fake with no ``read``/``write`` at
all, so every baofeng tuning test asserts against a stand-in rather than against the wire. Here the
backend, the `HybridTuner`, the `Uvk5Transport` and the frame codec are all real, and the only fake
is the radio — which is what lets a test say "the host sent 0x0851 and this is what came back".
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from radio_server.api import create_app
from radio_server.backends import create_radio
from radio_server.backends.base import AudioFrame
from tests.test_aioc_baofeng import FakeAudio
from tests.test_uvk5_transport import FirmwareFakeSerial

TOKEN = "test-lan-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

#: A quiet channel on this radio, measured on the deployed station at 445.800: 103-114 raw counts
#: across 72 samples. Not a round number, so a test that passes on it is reading the register
#: rather than a default.
FLOOR = 107
#: A carrier from the kv4p witness, inches away: 310-311 across 72 samples, and the same ~311 ADR
#: 0122 measured under full dock control on the same frequency.
CARRIER = 310


def make_station(*, rssi: int | None = FLOOR, **kwargs):
    """A real `AiocBaofeng` + real `HybridTuner` + real `Uvk5Transport` over the firmware fake.

    No ``tuner=`` kwarg, deliberately: this builds the composition the deployed station runs
    (``baofeng.uvk5_tuner = "hybrid"``), so the dock frames go through the real codec and land in
    the fake's register store. ``FirmwareFakeSerial`` already carries ``baudrate``/``timeout``/
    ``dtr``/``rts``, which is everything `apply_port_settings` and the PTT line touch.
    """
    fake = FirmwareFakeSerial()
    if rssi is None:
        fake.registers.pop(0x67, None)
    else:
        fake.registers[0x67] = rssi
    kwargs.setdefault("ptt_line", "dtr")
    kwargs.setdefault("tx_lead_seconds", 0.0)
    kwargs.setdefault("uvk5_tuner", "hybrid")
    radio = create_radio(
        "baofeng",
        _serial_factory=lambda port: fake,
        _audio=FakeAudio(),
        **kwargs,
    )
    return radio, fake


def _client(radio) -> TestClient:
    return TestClient(create_app(radio, api_token=TOKEN))


def settle(radio, timeout: float = 2.0):
    """Wait for the cadence to have taken at least one reading."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if radio.status().rssi is not None:
            return
        time.sleep(0.01)


# --- the reading ------------------------------------------------------------------------------


def test_status_reports_the_rssi_the_radio_returned():
    """FAIL-FIRST: `null` on master, because nothing on this path ever read a register."""
    radio, _fake = make_station(rssi=CARRIER)
    try:
        settle(radio)
        with _client(radio) as client:
            body = client.get("/status", headers=AUTH).json()
        assert body["rssi"] == CARRIER
    finally:
        radio.close()


def test_the_reading_is_masked_to_the_nine_bits_that_are_the_rssi():
    """`0x67` carries other bits above the reading; the firmware's own accessor masks `0x01FF`."""
    radio, _fake = make_station(rssi=0xFE67)
    try:
        settle(radio)
        assert radio.status().rssi == 0x067
    finally:
        radio.close()


def test_a_quiet_channel_and_a_carrier_are_different_numbers():
    """The whole point: the value tracks the channel, not the radio's mere presence."""
    quiet, _ = make_station(rssi=FLOOR)
    loud, _ = make_station(rssi=CARRIER)
    try:
        settle(quiet)
        settle(loud)
        assert quiet.status().rssi < loud.status().rssi
    finally:
        quiet.close()
        loud.close()


def test_a_zero_reading_is_reported_as_no_reading():
    """`0` is not a signal level — ADR 0132 measured it as the receiver being switched off.

    The floor on this hardware is ~107 counts at 445.800 and ~155 at 147.555, so 0 is fifty-odd dB
    below anything a working receiver reports. Rendering it as an RSSI would be the ADR 0132 fault
    exactly: a station reporting `rssi 0 / busy false` looks identical to a quiet one.
    """
    radio, _fake = make_station(rssi=0)
    try:
        time.sleep(0.05)
        assert radio.status().rssi is None
    finally:
        radio.close()


def test_a_radio_that_does_not_answer_reports_no_reading():
    radio, fake = make_station(rssi=FLOOR)
    try:
        settle(radio)
        fake.dock = False  # a stock radio ignores every 0x08xx frame
        time.sleep(0.05)
        radio._rssi.poll_once()
        assert radio._rssi.stats()["unknown"] >= 1
    finally:
        radio.close()


# --- what it must not do ----------------------------------------------------------------------


def test_no_reading_while_transmitting():
    """`null`, not a stale number: nothing measures a received signal through its own carrier."""
    radio, _fake = make_station(rssi=CARRIER)
    try:
        settle(radio)
        radio.ptt(True)
        assert radio.status().rssi is None
    finally:
        radio.ptt(False)
        radio.close()


def test_busy_is_still_false_and_the_squelch_is_untouched():
    """A pin. This surfaces a diagnostic number; it does not become a carrier-detect (ADR 0175)."""
    radio, _fake = make_station(rssi=CARRIER)
    try:
        settle(radio)
        assert radio.status().busy is False
    finally:
        radio.close()


def test_receiving_audio_puts_nothing_on_the_wire():
    """ADR 0125's rule: no serial read may happen on the thread that drains the sound card."""
    radio, fake = make_station(rssi=FLOOR)
    try:
        settle(radio)
        radio._rssi.stop()          # silence the cadence so only the audio path is measured
        before = len(fake.writes)
        for _ in range(20):
            radio.receive()
        assert len(fake.writes) == before
    finally:
        radio.close()


def test_a_failed_poll_holds_the_previous_reading_rather_than_inventing_a_transition():
    radio, fake = make_station(rssi=CARRIER)
    try:
        settle(radio)
        assert radio.status().rssi == CARRIER
        fake.dock = False
        radio._rssi.poll_once()
        assert radio.status().rssi == CARRIER
    finally:
        radio.close()


def test_a_reading_nobody_has_refreshed_ages_out_to_none():
    """A number from thirty seconds ago rendered as current is the lie this repo keeps closing."""
    radio, fake = make_station(rssi=CARRIER)
    try:
        settle(radio)
        radio._rssi.stop()
        fake.dock = False
        time.sleep(0.05)
        radio._rssi._reading_at -= 60.0   # pretend the last good reading is a minute old
        assert radio.status().rssi is None
    finally:
        radio.close()


def test_the_cadence_yields_the_wire_instead_of_queueing_behind_a_tune():
    """A poll that cannot have the wire skips its round; it never delays a key-up (ADR 0163)."""
    radio, _fake = make_station(rssi=FLOOR)
    try:
        settle(radio)
        radio._rssi.stop()
        with radio._transport._wire:      # a tune is holding the wire
            started = time.monotonic()
            radio._rssi.poll_once()
            assert time.monotonic() - started < 0.5
        assert radio._rssi.stats()["unknown"] >= 1
    finally:
        radio.close()


def test_a_backend_with_no_tuner_reports_no_reading_and_starts_no_thread():
    """A plain UV-5R has no UART on that jack. Nothing to poll, and nothing polling."""
    radio, _fake = make_station(rssi=CARRIER, uvk5_tuner="off")
    try:
        assert radio.status().rssi is None
        assert getattr(radio, "_rssi", None) is None
    finally:
        radio.close()


@pytest.mark.parametrize("frame_bytes", [b"", b"\x01\x02" * 8])
def test_the_reading_survives_audio_frames_of_any_size(frame_bytes):
    """The cadence and the audio path share a backend but not a thread."""
    radio, _fake = make_station(rssi=FLOOR)
    try:
        settle(radio)
        radio.transmit(AudioFrame(frame_bytes))
        settle(radio)
        assert radio.status().rssi == FLOOR
    finally:
        radio.close()
