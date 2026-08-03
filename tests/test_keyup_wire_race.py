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

import radio_server.backends.aioc_baofeng as aioc
from radio_server.backends.base import AudioFrame
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


def test_a_poll_that_lands_inside_a_key_up_is_counted(monkeypatch):
    """FAIL-FIRST: nothing on master counts this, so nothing could ever show it happening."""
    radio, _fake = make_station(rssi=CARRIER)
    try:
        radio._rssi.stop()
        _poll_inside_the_key_up(monkeypatch, radio)

        radio.ptt(True)
        radio.ptt(False)

        wire = radio.status().wire
        assert wire.key_ups == 1
        assert wire.key_ups_with_wire_traffic == 1
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
