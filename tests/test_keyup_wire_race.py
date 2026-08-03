"""Is a poll still on the wire when the station keys? Count it, do not argue about it (ADR 0177).

ADR 0176's audit found that both cadences guard the shared AIOC wire with a **check-then-act**
pause: `poll_once` reads ``paused()`` and then, holding nothing, issues a dock exchange that owns
`Uvk5Transport._wire`. A poll that passes the check can still be on the wire when the transmitter
comes up — and ADR 0175 measured what a frame on this cable costs a transmission.

Reading `AiocBaofeng._key_on` moved the window. It is not "the gap before the DTR assert": the audio
stream is opened **and started** two steps before the line goes high, so the exposure runs from the
last key-up frame releasing the wire, through a PortAudio device open and a thread start, to the
flag being set *after* the assert.

**Two instruments, because one of them under-reports.** `wire_busy()` samples the lock at an
instant, so it misses a poll that starts just after the sample and finishes across the assert. The
exchange-count delta catches exactly that case. A `0` from the first is not evidence; a `0` from the
second across a real sample is.

These tests pin the **instrument**, not a fix. The counters say the race fired; only a witness's
received audio can say an over was clipped, and keeping those two claims apart is the whole point of
naming the counters after what they count.
"""

from __future__ import annotations

import threading
import time

import pytest

import radio_server.backends.aioc_baofeng as aioc
from radio_server.backends.base import AudioFrame
from radio_server.backends.uvk5.tuner import TuneError
from tests.test_baofeng_rssi import CARRIER, make_station


def _poll_inside_the_key_up(monkeypatch, radio):
    """Plant one cadence poll in the widest part of the window — inside `open_playout_stream`.

    That is after the key-up's own dock frames have released the wire and after the instrument has
    snapshotted it, and before the DTR assert. A poll landing there is the race, reproduced
    deterministically instead of waited for.
    """
    original = aioc.open_playout_stream

    def opening_the_stream(*args, **kwargs):
        radio._rssi.poll_once()
        return original(*args, **kwargs)

    monkeypatch.setattr(aioc, "open_playout_stream", opening_the_stream)


def test_an_exchange_that_lands_inside_a_key_up_is_counted(monkeypatch):
    """FAIL-FIRST: nothing on master counts this, so nothing could ever show it happening.

    Driven straight through the transport rather than through the cadence, because the cadence can
    no longer get there — that is the next test. The counter has to catch traffic from *any*
    source, since the whole reason it ships is to notice a future one nobody has thought of.
    """
    radio, _fake = make_station(rssi=CARRIER)
    try:
        radio._rssi.stop()
        original = aioc.open_playout_stream

        def opening_the_stream(*args, **kwargs):
            radio._transport.read_register(0x67, timeout=1.0, wire_timeout=0.0)
            return original(*args, **kwargs)

        monkeypatch.setattr(aioc, "open_playout_stream", opening_the_stream)
        radio.ptt(True)
        radio.ptt(False)

        wire = radio.status().wire
        assert wire.key_ups == 1
        assert wire.key_ups_with_wire_traffic == 1
    finally:
        radio.close()


def test_an_exchange_still_running_when_the_line_drops_is_counted(monkeypatch):
    """The interval must reach un-key, not the end of the key-up.

    Measured, not reasoned: the first version closed it when the lead-in was queued, and a forced
    collision timestamped from 2.5 ms *before* the DTR assert to 96 ms after was recorded as zero
    on all three bench trials. An instrument that misses a collision somebody caused on purpose
    would have missed every real one.
    """
    radio, _fake = make_station(rssi=CARRIER)
    try:
        radio._rssi.stop()
        original = aioc.open_playout_stream
        started = threading.Event()
        release = threading.Event()

        def slow_exchange():
            with radio._transport._wire:
                started.set()
                release.wait(5.0)
            # The exchange "completes" only now — after the key-up has finished and un-keyed.
            radio._transport._exchanges += 1

        def opening_the_stream(*args, **kwargs):
            threading.Thread(target=slow_exchange, daemon=True).start()
            started.wait(5.0)
            return original(*args, **kwargs)

        monkeypatch.setattr(aioc, "open_playout_stream", opening_the_stream)
        radio.ptt(True)
        release.set()
        time.sleep(0.05)
        radio.ptt(False)

        assert radio.status().wire.key_ups_with_wire_traffic == 1
    finally:
        release.set()
        radio.close()


def test_a_cadence_poll_cannot_start_inside_a_key_up(monkeypatch):
    """The fix. A poll landing in the widest part of the key-up must put NOTHING on the wire.

    Bench-measured stakes: one register exchange in flight across the DTR assert and this station
    radiated nothing at all — the witness's hardware carrier detect saw RF in 0 of 81 polls, where
    the same script without it saw 48 of 83. Not damaged audio; **no carrier**.

    Asserted on `fake.writes` rather than on a counter, because the requirement is that no frame is
    ever built — the convention `test_broadcast_fm_poll.py` already uses.
    """
    radio, fake = make_station(rssi=CARRIER)
    try:
        radio._rssi.stop()
        seen = {}
        original = aioc.open_playout_stream

        def opening_the_stream(*args, **kwargs):
            seen["before"] = len(fake.writes)
            radio._rssi.poll_once()
            seen["after"] = len(fake.writes)
            return original(*args, **kwargs)

        monkeypatch.setattr(aioc, "open_playout_stream", opening_the_stream)
        radio.ptt(True)
        radio.ptt(False)

        assert seen["after"] == seen["before"]
        assert radio._rssi.stats()["skipped"] >= 1
        assert radio.status().wire.key_ups_with_wire_traffic == 0
    finally:
        radio.close()


def test_a_key_up_notices_a_poll_still_holding_the_wire():
    """The instant sample, injected where a poll would actually be holding it.

    The wire is grabbed from `_refuse_if_tx_disabled`, which runs after the key-up's own frames
    have released it and before the stream opens — so this is a window that exists, not one
    invented for the test.
    """
    radio, _fake = make_station(rssi=CARRIER)
    try:
        radio._rssi.stop()
        wire_lock = radio._transport._wire
        original = radio._refuse_if_tx_disabled

        def grab_the_wire():
            assert wire_lock.acquire(blocking=False)
            original()

        radio._refuse_if_tx_disabled = grab_the_wire
        try:
            radio.ptt(True)
        finally:
            wire_lock.release()
            radio.ptt(False)

        wire = radio.status().wire
        assert wire.key_ups == 1
        assert wire.wire_busy_at_key_up == 1
    finally:
        radio.close()


def test_a_quiet_wire_at_key_up_counts_the_key_up_and_nothing_else():
    """The ordinary case, and the control for both tests above."""
    radio, _fake = make_station(rssi=CARRIER)
    try:
        radio._rssi.stop()
        radio.ptt(True)
        radio.ptt(False)

        wire = radio.status().wire
        assert wire.key_ups == 1
        assert wire.wire_busy_at_key_up == 0
        assert wire.key_ups_with_wire_traffic == 0
    finally:
        radio.close()


def test_the_key_ups_own_frames_are_not_counted_as_contention():
    """`_clear_if_deafened` puts an exchange on the wire on every key-up, by design.

    Counting it would report the race firing on 100 % of key-ups and make the instrument useless.
    The snapshot is therefore taken *after* the key-up's own frames, at the point it commits to
    opening the audio stream.
    """
    radio, fake = make_station(rssi=CARRIER)
    try:
        radio._rssi.stop()
        before = len(fake.writes)
        radio.ptt(True)
        radio.ptt(False)
        # The key-up really did use the wire...
        assert len(fake.writes) > before
        # ...and none of it was counted against it.
        assert radio.status().wire.key_ups_with_wire_traffic == 0
    finally:
        radio.close()


def test_wire_busy_answers_the_lock_and_touches_nothing_else():
    """`wire_busy()` must be readable on the keying path, so it may not take a lock or do I/O.

    A pause check that puts a frame on the wire to decide whether to put a frame on the wire is the
    fault ADR 0176 named one layer up. The same rule applies to a *measurement* of that wire.
    """
    radio, fake = make_station(rssi=CARRIER)
    try:
        radio._rssi.stop()
        transport = radio._transport
        before = len(fake.writes)

        assert transport.wire_busy() is False
        with transport._wire:
            assert transport.wire_busy() is True
        assert transport.wire_busy() is False

        # It observed the lock without acquiring it, and put nothing on the wire.
        assert len(fake.writes) == before
        assert transport._wire.acquire(blocking=False)
        transport._wire.release()
    finally:
        radio.close()


def test_the_exchange_counter_counts_completed_round_trips():
    """It has to be monotonic and it has to move, or the delta measures nothing."""
    radio, _fake = make_station(rssi=CARRIER)
    try:
        radio._rssi.stop()
        transport = radio._transport
        before = transport.exchanges
        radio._rssi.poll_once()
        assert transport.exchanges == before + 1
    finally:
        radio.close()


def test_both_keying_routes_reach_the_counter():
    """One-shot `transmit()` and streaming `ptt(True)` are separate key-ups and both must count.

    A denominator that misses a route reports a rate that is too high, which is the direction that
    invents a defect rather than hiding one.
    """
    radio, _fake = make_station(rssi=CARRIER)
    try:
        radio._rssi.stop()
        radio.transmit(AudioFrame(b"\x01\x02" * 8))
        radio.ptt(True)
        radio.ptt(False)
        assert radio.status().wire.key_ups == 2
    finally:
        radio.close()


def test_a_station_that_has_never_keyed_is_not_a_station_that_has_never_raced():
    """The tri-state rule this repo keeps re-learning, one level down.

    ``key_ups_with_wire_traffic: 0`` beside ``key_ups: 0`` means *nobody has transmitted yet*;
    beside ``key_ups: 200`` it is a measurement. Reporting the counter without its denominator
    renders those two identically, which is the trap `deafened_unknown` and `broadcast_fm` are both
    shaped to avoid.
    """
    radio, _fake = make_station(rssi=CARRIER)
    try:
        radio._rssi.stop()
        wire = radio.status().wire
        assert wire is not None
        assert wire.key_ups == 0
        assert wire.wire_busy_at_key_up == 0
        assert wire.key_ups_with_wire_traffic == 0
    finally:
        radio.close()


def test_a_backend_with_no_dock_wire_reports_no_block_at_all():
    """A plain UV-5R has no UART on that jack: there is no wire to be busy, so the answer is `null`
    rather than a confident zero."""
    radio, _fake = make_station(rssi=CARRIER, uvk5_tuner="off")
    try:
        assert radio.status().wire is None
    finally:
        radio.close()


def test_a_refused_key_up_does_not_leave_the_cadences_muted():
    """A stuck reservation would silence both cadences for the life of the process.

    That failure is worse than the one being fixed and completely silent: the meter simply stops
    updating and every counter still reads zero. So the release is in a `finally` and every refusal
    path is expected to unwind through it.
    """
    radio, _fake = make_station(rssi=CARRIER)
    try:
        radio._rssi.stop()

        def refuse():
            raise TuneError("the radio did not confirm its channel")

        radio._reassert_channel = refuse
        with pytest.raises(TuneError):
            radio.ptt(True)

        assert radio.cadence_paused() is False
        assert radio._keying == 0
        # And a poll really can reach the wire again.
        before = radio._transport.exchanges
        radio._rssi.poll_once()
        assert radio._transport.exchanges == before + 1
    finally:
        radio.close()


def test_two_concurrent_key_ups_do_not_disarm_each_other():
    """Depth, not a flag. The station ID, `POST /transmit` and a bridge can all key from different
    threads; with a bool the first to finish unguards the second one mid-key-up."""
    radio, _fake = make_station(rssi=CARRIER)
    try:
        radio._rssi.stop()
        radio._reserve_the_wire()
        radio._reserve_the_wire()
        assert radio.cadence_paused() is True
        radio._release_the_wire()
        # One key-up finishing must NOT unguard the other.
        assert radio.cadence_paused() is True
        radio._release_the_wire()
        assert radio.cadence_paused() is False
    finally:
        radio.close()


def test_the_barrier_gives_up_and_keys_rather_than_waiting_on_a_busy_wire():
    """**The guardrail-5 test.** A diagnostic must never be able to stop the station ID going out.

    The wire is held across the key-up, so the barrier cannot succeed. The requirement is that it
    gives up on schedule, keys **anyway**, and records that it did — a torn diagnostic read is
    cheaper than a transmission that did not happen. It must NOT refuse: guardrail 5 makes the
    station ID required controller behaviour, and a refused key-up is silence.
    """
    radio, fake = make_station(rssi=CARRIER)
    try:
        radio._rssi.stop()
        held = threading.Event()
        release = threading.Event()

        def hog():
            with radio._transport._wire:
                held.set()
                release.wait(10.0)

        threading.Thread(target=hog, daemon=True).start()
        assert held.wait(5.0)

        started = time.monotonic()
        barrier_gave_up_after = radio._transport.wait_for_quiet(aioc.KEY_UP_WIRE_DRAIN_S)
        elapsed = time.monotonic() - started

        assert barrier_gave_up_after is None, "it must not claim the wire went quiet"
        assert aioc.KEY_UP_WIRE_DRAIN_S <= elapsed < aioc.KEY_UP_WIRE_DRAIN_S + 0.5
        release.set()

        radio.ptt(True)
        assert fake.dtr is True, "the line must come up: keying anyway is the whole point"
    finally:
        release.set()
        radio.ptt(False)
        radio.close()


def test_a_key_up_that_waited_records_how_long():
    """The standing measure of the barrier doing its job, and of what it costs a key-up.

    Bench-measured on the deployed hardware: 0.01 ms when the wire was free (two trials of three)
    and 96.80 ms when a cadence poll was genuinely in flight — one register exchange, which is
    exactly what the barrier exists to wait out.
    """
    radio, _fake = make_station(rssi=CARRIER)
    try:
        radio._rssi.stop()
        assert radio.status().wire.longest_wire_wait_ms is None, "never waited is not 'waited 0'"

        held = threading.Event()

        def hog():
            with radio._transport._wire:
                held.set()
                time.sleep(0.1)

        threading.Thread(target=hog, daemon=True).start()
        assert held.wait(5.0)
        radio.ptt(True)
        radio.ptt(False)

        wire = radio.status().wire
        assert wire.key_ups_that_waited_for_the_wire == 1
        assert 50 < wire.longest_wire_wait_ms < aioc.KEY_UP_WIRE_DRAIN_S * 1000
    finally:
        radio.close()


def test_a_key_up_behind_a_busy_wire_still_keys_and_says_so():
    """A poll at its full budget delays a key-up; it must never turn one into a refusal.

    One second of hold stands in for a cadence poll running to its own `POLL_REQUEST_TIMEOUT`,
    which is the worst a poller can impose now that the reservation stops new ones starting.
    """
    radio, fake = make_station(rssi=CARRIER)
    try:
        radio._rssi.stop()
        held = threading.Event()

        def hog():
            with radio._transport._wire:
                held.set()
                time.sleep(1.0)

        threading.Thread(target=hog, daemon=True).start()
        assert held.wait(5.0)

        radio.ptt(True)
        assert fake.dtr is True
        assert radio.status().wire.keyed_with_wire_busy == 1
    finally:
        radio.ptt(False)
        radio.close()


def test_the_drain_bound_covers_a_real_exchange_with_margin():
    """Pins the derivation, so a later edit cannot quietly shrink it below what it must cover.

    0.3 s is three times the 96.3-97.7 ms a register exchange measured on this hardware, sized for
    the two cadences' exchanges back to back — and a twentieth of the six seconds the key-up path
    already sits through for the firmware's own serial TX lockout.
    """
    assert aioc.KEY_UP_WIRE_DRAIN_S >= 3 * 0.098
    assert aioc.KEY_UP_WIRE_DRAIN_S <= aioc.SERIAL_TX_LOCKOUT_S / 10


def test_status_transmitting_stays_false_until_the_line_is_actually_up(monkeypatch):
    """The pin that stops the two predicates being merged back into one.

    `cadence_paused()` is True for the whole key-up; `status().transmitting` is a user-visible field
    that answers a narrower question and must not start claiming the radio is on the air early.
    """
    radio, _fake = make_station(rssi=CARRIER)
    try:
        radio._rssi.stop()
        seen = {}
        original = aioc.open_playout_stream

        def opening_the_stream(*args, **kwargs):
            seen["transmitting"] = radio.status().transmitting
            seen["paused"] = radio.cadence_paused()
            return original(*args, **kwargs)

        monkeypatch.setattr(aioc, "open_playout_stream", opening_the_stream)
        radio.ptt(True)
        radio.ptt(False)

        assert seen["transmitting"] is False
        assert seen["paused"] is True
    finally:
        radio.close()


def test_the_pause_predicate_does_no_io():
    """It is consulted on the keying path and on every cadence tick; a read here would rebuild the
    fault it exists to prevent (ADR 0176's rule, applied to the new name)."""
    radio, fake = make_station(rssi=CARRIER)
    try:
        radio._rssi.stop()
        before = len(fake.writes)
        for _ in range(50):
            radio.cadence_paused()
        assert len(fake.writes) == before
    finally:
        radio.close()


def test_a_backend_with_no_transport_keys_without_a_barrier():
    """A plain UV-5R has no wire to reserve, and must key exactly as it always did."""
    radio, fake = make_station(rssi=CARRIER, uvk5_tuner="off")
    try:
        radio.transmit(AudioFrame(b"\x01\x02" * 8))
        assert fake.dtr is False  # one-shot transmit keys and un-keys
        assert radio.status().wire is None
    finally:
        radio.close()


def test_the_counters_survive_concurrent_key_ups():
    """`_key_on` is reachable from the event loop, the station ID, and a bridge's thread, so the
    counters are mutated under a lock of their own — one that is never held across I/O."""
    radio, _fake = make_station(rssi=CARRIER)
    try:
        radio._rssi.stop()

        def key():
            radio.transmit(AudioFrame(b"\x01\x02" * 8))

        threads = [threading.Thread(target=key) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10.0)
        assert radio.status().wire.key_ups == 8
    finally:
        radio.close()
