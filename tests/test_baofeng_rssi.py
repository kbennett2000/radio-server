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

from radio_server.activity.rssi_poll import DEFAULT_RSSI_POLL_INTERVAL, STALE_AFTER
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


def test_the_cadence_stops_dead_while_the_station_transmits():
    """The one that matters most, and the one the bench found the hard way (ADR 0175).

    A register read on this cable while the sound card plays OUT destroys the transmission: the
    witness recovered the 1000 Hz tone at **0.026** against 0.989, and a 4.4 s over reached it as
    0.70 s of audio. So while keyed the poller must put **nothing** on the wire — not a request,
    not a non-blocking lock acquire.
    """
    radio, fake = make_station(rssi=CARRIER)
    try:
        settle(radio)
        radio._rssi.stop()
        radio.ptt(True)
        before = len(fake.writes)
        for _ in range(5):
            radio._rssi.poll_once()
        assert len(fake.writes) == before
        assert radio._rssi.stats()["skipped"] >= 5
        # And a skipped round is not counted as a failed one — they mean different things.
        assert radio._rssi.stats()["unknown"] == 0
    finally:
        radio.ptt(False)
        radio.close()


def test_the_cadence_resumes_once_the_carrier_drops():
    radio, _fake = make_station(rssi=CARRIER)
    try:
        settle(radio)
        radio._rssi.stop()
        radio.ptt(True)
        radio._rssi.poll_once()
        radio.ptt(False)
        radio._rssi.poll_once()
        assert radio.status().rssi == CARRIER
    finally:
        radio.close()


# --- the numbers behind the reading, where somebody can read them (ADR 0179) --------------------
#
# ADR 0175 built the cadence and ADR 0178 recorded that nothing outside the process could see any of
# it: `RssiPoller.stats()` had **zero** production callers, so `polls`, `unknown`, `skipped` and
# `pause_errors` were computed on every tick and dropped. `status.rssi` alone cannot answer the
# question an operator actually has, which is *why is it null* — never measured, measured and aged
# out, or deliberately not measured because the station is keyed all read as one `null`.


def test_the_cadence_numbers_reach_status():
    radio, _fake = make_station(rssi=FLOOR)
    try:
        settle(radio)
        cadence = radio.status().rssi_cadence
        assert cadence is not None
        # Prove the event before asserting the number: a poll actually happened, so `polls` is a
        # measurement and not a field that happens to be initialised to something plausible.
        assert radio.status().rssi == FLOOR
        assert cadence.polls >= 1
        assert cadence.age_s is not None and cadence.age_s >= 0
        # `null` where no hook is wired, `0` where the hook is fine (ADR 0178). This station wires
        # one, so the honest answer here is 0 and NOT `null`.
        assert cadence.pause_errors == 0
    finally:
        radio.close()


def test_a_backend_with_no_cadence_reports_no_block_rather_than_zeroes():
    """The tri-state rule `wire` and `broadcast_fm` keep: "nothing polls here" is not "0 polls"."""
    radio, _fake = make_station(rssi=CARRIER, uvk5_tuner="off")
    try:
        assert radio.status().rssi_cadence is None
    finally:
        radio.close()


def test_a_round_skipped_for_a_key_up_is_visible_from_outside_the_process():
    """The inverse of `test_the_cadence_stops_dead_while_the_station_transmits`, one layer out.

    That test proves the guard works by watching the wire. This proves an operator can SEE it work
    — which is a different claim, and the one ADR 0178 found unmet: a quiet meter during an over
    and a broken meter rendered identically to anybody outside this process.
    """
    radio, fake = make_station(rssi=CARRIER)
    try:
        settle(radio)
        radio._rssi.stop()
        before = radio.status().rssi_cadence.skipped
        radio.ptt(True)
        writes = len(fake.writes)
        for _ in range(3):
            radio._rssi.poll_once()
        # The event first: nothing reached the wire. Then the number that reports it.
        assert len(fake.writes) == writes
        radio.ptt(False)
        cadence = radio.status().rssi_cadence
        assert cadence.skipped == before + 3
        # And a deliberate skip is still not a failure — they are different facts and stay apart.
        assert cadence.unknown == 0
    finally:
        radio.ptt(False)
        radio.close()


def test_the_expiry_threshold_travels_with_the_age():
    """`age_s` alone cannot say whether a reading has expired; the threshold has to travel with it.

    The shape `slots.tx.stale_after_s` already ships for the same reason (ADR 0170) — a UI that
    hardcodes the number drifts the day the interval changes.
    """
    radio, _fake = make_station(rssi=FLOOR)
    try:
        settle(radio)
        cadence = radio.status().rssi_cadence
        assert cadence.stale_after_s == pytest.approx(STALE_AFTER * DEFAULT_RSSI_POLL_INTERVAL)
        assert cadence.age_s < cadence.stale_after_s
    finally:
        radio.close()


def test_an_expired_reading_is_null_beside_an_age_that_says_why():
    """The whole point of surfacing `age_s`: `rssi: null` stops being one undifferentiated answer.

    Never measured is `age_s: null`; expired is `age_s` past the threshold; keyed is a fresh age
    with `transmitting: true`. Before this block all three were the same `null`.
    """
    radio, _fake = make_station(rssi=CARRIER)
    try:
        settle(radio)
        radio._rssi.stop()
        radio._rssi._reading_at -= 60.0
        status = radio.status()
        assert status.rssi is None
        assert status.rssi_cadence.age_s > status.rssi_cadence.stale_after_s
    finally:
        radio.close()


def test_the_block_survives_the_json_round_trip_to_a_client():
    """It is only a reader if it reaches HTTP. `asdict` flattens the dataclass; nothing else does."""
    radio, _fake = make_station(rssi=FLOOR)
    try:
        settle(radio)
        with _client(radio) as client:
            body = client.get("/status", headers=AUTH).json()
        assert body["rssi_cadence"]["polls"] >= 1
        assert body["rssi_cadence"]["pause_errors"] == 0
        assert body["rssi_cadence"]["stale_after_s"] == pytest.approx(1.5)
    finally:
        radio.close()
