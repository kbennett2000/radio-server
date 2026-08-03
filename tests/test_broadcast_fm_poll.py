"""The broadcast-FM cadence (ADR 0163).

ADR 0162 shipped a relay mute that reads a block measured at key-up, and on F8/F9 that block can
never say ``on=True`` — so the mute was armed and blind. The poller is what makes it fire, and the
whole cycle turns on one bench result: the ``0x0879`` probe answers ``ERR_BAND`` for the entire time
broadcast FM is selected, including while the radio is passing a real over (M3), and it caught a
front-panel ``F+0`` on the very next poll (M4).

Two invariants run through every test here:

* **The poller never writes ``tuner.broadcast_fm``.** ``clear_broadcast_fm`` stays the single writer,
  so nothing the poller learns can reach ``refuse_if_deafened`` and refuse a key-up.
* **A non-answer is not a state transition.** ``ERR_TX``, a timeout, a busy wire and an exception all
  leave the previous reading standing, because unknowns are routine here — the dock refuses
  ``0x0879`` on every key-up — and a link that mutes on every over is a dead link.
"""

from __future__ import annotations

import threading
import time

from radio_server.activity.broadcast_fm_poll import BroadcastFmPoller
from radio_server.backends.base import BroadcastFm

FULL = BroadcastFm(on=False, hz=104_300_000, blocks_tx=True, rescues=3)


class Probe:
    """A scripted ``probe_broadcast_fm``: pops answers, then repeats the last one for ever."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.calls = 0
        self.raises: BaseException | None = None
        self._lock = threading.Lock()

    def __call__(self, **kwargs):
        with self._lock:
            self.calls += 1
            if self.raises is not None:
                raise self.raises
            if not self.answers:
                return None
            return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def poller(*answers, fallback=FULL, **kwargs) -> tuple[BroadcastFmPoller, Probe]:
    probe = Probe(*answers)
    return BroadcastFmPoller(probe, lambda: fallback, **kwargs), probe


# --- what the bridges read ----------------------------------------------------------------


def test_before_any_definite_reading_it_is_the_key_up_snapshot_unchanged():
    """No poll has answered, so the poller has nothing better than what ADR 0162 already had.

    Not ``None`` and not a guess: falling back to the key-up snapshot is strictly more evidence
    than either. This is the state every backend without a probe stays in for ever.
    """
    p, probe = poller(None)
    assert p() is FULL
    assert probe.calls == 0, "reading the block must not itself poke the radio"


def test_a_probe_that_says_fm_is_selected_mutes():
    p, _ = poller(True)
    p.poll_once()
    block = p()
    assert block.on is True


def test_a_probe_that_says_fm_is_off_does_not_mute():
    p, _ = poller(False)
    p.poll_once()
    assert p().on is False


def test_the_probe_owns_on_and_carries_everything_else_from_the_last_full_block():
    """``hz``/``blocks_tx``/``rescues`` are not measurable from a refusal, so they are carried.

    ``dock.c`` blanks state, band, frequency and flags on every non-``APPLIED`` reply, and the
    probe is a refusal by construction. Inventing values here would report a frequency nothing
    measured; dropping them would lose the F9 interlock bit the status block already carries.
    """
    p, _ = poller(True)
    p.poll_once()
    block = p()
    assert (block.on, block.hz, block.blocks_tx, block.rescues) == (
        True, FULL.hz, FULL.blocks_tx, FULL.rescues,
    )


def test_with_no_snapshot_at_all_a_positive_probe_still_mutes():
    """A backend that never completed a key-up clear still gets the Part 97 gate."""
    p, _ = poller(True, fallback=None)
    p.poll_once()
    block = p()
    assert block.on is True
    assert block.hz is None and block.rescues == 0


# --- a non-answer is not a state transition -----------------------------------------------


def test_an_unknown_holds_the_previous_reading():
    p, probe = poller(True, None)
    p.poll_once()
    assert p().on is True
    p.poll_once()  # ERR_TX / timeout / busy wire — all arrive here as None
    assert p().on is True, "an unknown flipped a measured reading"
    assert p.stats()["unknown"] == 1


def test_an_unknown_before_any_reading_is_still_the_snapshot():
    p, _ = poller(None)
    p.poll_once()
    assert p() is FULL
    assert p.stats()["reading"] is None


def test_a_probe_that_raises_is_an_unknown_not_a_crash():
    """`probe_broadcast_fm` promises never to raise; the poller does not take that on trust.

    It runs on a daemon thread whose death would be silent, and a silent dead poller is a mute
    that stops firing with every counter still reading zero.
    """
    p, probe = poller(True)
    p.poll_once()
    probe.raises = OSError("port went away")
    p.poll_once()
    assert p().on is True
    assert p.stats()["unknown"] == 1


def test_age_tracks_the_last_definite_reading_only():
    clock = iter([100.0, 100.0, 110.0, 130.0, 140.0])
    ticks = [100.0]

    def fake_clock() -> float:
        return ticks[-1]

    p, _ = poller(True, None, clock=fake_clock)
    assert p.stats()["age_s"] is None, "nothing measured yet is not an age of zero"
    p.poll_once()
    ticks.append(115.0)
    assert p.stats()["age_s"] == 15.0
    p.poll_once()  # unknown — must not refresh the age
    ticks.append(140.0)
    assert p.stats()["age_s"] == 40.0
    del clock


# --- lifecycle: no bridge relaying, no serial traffic --------------------------------------


def test_it_does_not_poll_until_a_bridge_asks():
    p, probe = poller(True, interval=0.01)
    time.sleep(0.05)
    assert probe.calls == 0
    assert p.running is False


def test_start_and_stop_are_refcounted_so_two_bridges_do_not_fight():
    """Mumble and D-STAR both relay; the first to start opens the cadence, the last closes it."""
    p, probe = poller(True, interval=0.01)
    p.start()
    p.start()
    assert p.running is True
    p.stop()
    assert p.running is True, "one bridge stopping silenced the other bridge's gate"
    p.stop()
    assert p.running is False


def test_stopping_more_times_than_starting_does_not_go_negative():
    p, _ = poller(True, interval=0.01)
    p.stop()
    p.stop()
    p.start()
    assert p.running is True
    p.stop()
    assert p.running is False


def test_a_started_poller_actually_polls_and_a_stopped_one_stops():
    p, probe = poller(True, interval=0.01)
    p.start()
    deadline = time.monotonic() + 2.0
    while probe.calls < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert probe.calls >= 3, "the cadence never ran"
    p.stop()
    settled = probe.calls
    time.sleep(0.1)
    assert probe.calls == settled, "the thread kept polling after the last bridge left"


def test_the_poll_asks_for_a_short_timeout_and_never_queues_on_the_wire():
    """The two knobs that bound what a poll can cost a key-up.

    ``wire_timeout=0`` means a poll competing with a tune or a clear skips this round instead of
    queueing; the short ``timeout`` bounds how long a poll that already holds the wire can make a
    key-up wait. Neither is cosmetic: the key-up path is where ADR 0161 put a frame that took the
    station ID off the air.
    """
    seen: dict = {}

    def probe(**kwargs):
        seen.update(kwargs)
        return True

    p = BroadcastFmPoller(probe, lambda: FULL)
    p.poll_once()
    assert seen.get("wire_timeout") == 0.0
    assert 0 < seen.get("timeout", 99) <= 1.5


# --- readings taken by somebody other than the cadence (ADR 0164) -------------------------


def test_observe_records_a_definite_reading_and_returns_the_one_it_replaced():
    """The ON route holds a full ``0x087A`` read-back, which is better evidence than any poll — the
    probe only ever receives a refusal. Handing it to the cadence means the mute acts on it
    immediately instead of up to one interval later."""
    p, _probe = poller(None)

    assert p.observe(True) is None            # nothing was known before
    assert p().on is True
    assert p.observe(False) is True           # and it reports what it displaced
    assert p().on is False


def test_assume_on_arms_the_mute_before_the_frame_goes_out():
    """The measured ON exchange takes ~0.4 s on the bench (ADR 0160) and the cadence would not
    notice for up to 2.0 s more. That is up to 2.4 s of commercial broadcast relayed onto somebody
    else's repeater — on the one path where the ordering is entirely ours to choose.

    So the relay mute moves toward silence **before** the wire, and away from it only on proof.
    """
    p, _probe = poller(False)
    p.poll_once()
    assert p().on is False

    with p.assume_on():
        assert p().on is True                 # armed, with nothing measured yet
    assert p().on is False                    # released, and the reading is untouched


def test_a_refused_on_leaves_the_mute_exactly_where_it_was():
    """The hold is a hold, not a write. A refusal releases it and the cadence's own reading — which
    may be ``True`` because somebody pressed ``F+0`` — decides, rather than the route's optimism."""
    p, _probe = poller(True)
    p.poll_once()
    with p.assume_on():
        pass
    assert p().on is True                     # still what the radio said, not what the route hoped


def test_a_poll_landing_inside_the_hold_cannot_disarm_it():
    """The race the hold exists to close: the probe reads ``ERR_OFF`` in the millisecond between
    arming and the firmware applying the ON. A plain write-then-restore would be clobbered here."""
    p, _probe = poller(False)
    with p.assume_on():
        p.poll_once()                         # a definite "off" lands mid-flight
        assert p().on is True
    assert p().on is False


def test_the_hold_nests_so_two_requests_do_not_disarm_each_other():
    p, _probe = poller(False)
    p.poll_once()
    with p.assume_on():
        with p.assume_on():
            assert p().on is True
        assert p().on is True
    assert p().on is False


def test_the_seams_are_inert_on_a_bare_callable():
    """Every bridge test in the suite injects a plain lambda for ``broadcast_fm`` (ADR 0162), and
    the lifecycle is reached by ``getattr`` for exactly that reason. The two ADR 0164 seams follow
    the same idiom, so none of those tests changes."""
    from radio_server.activity.broadcast_fm_poll import (
        assume_broadcast_fm_on,
        observe_broadcast_fm,
    )

    plain = lambda: FULL                                            # noqa: E731
    assert observe_broadcast_fm(plain, True) is None
    with assume_broadcast_fm_on(plain):
        assert plain() is FULL
    assert observe_broadcast_fm(None, True) is None


def test_observe_carries_the_key_up_snapshots_other_fields():
    """The probe cannot measure ``hz``/``blocks_tx``/``rescues`` and neither can an observation of a
    bare boolean, so they are carried from the last full block rather than invented — the same rule
    ADR 0163 applied to a polled reading."""
    p, _probe = poller(None, fallback=FULL)
    p.observe(True)
    block = p()
    assert (block.on, block.hz, block.blocks_tx, block.rescues) == (
        True, FULL.hz, FULL.blocks_tx, FULL.rescues,
    )


# --- nothing on the wire while the station transmits (ADR 0176) ---------------------------


def test_a_transmitting_station_gets_no_probe_at_all():
    """FAIL-FIRST. ADR 0175 measured what a cadence costs a transmission on this cable, and this
    one is *larger* per exchange (38 bytes against 32) and starts on any bridge connect — so it runs
    straight through every relayed over and every unattended station ID.

    The assertion is on ``probe.calls``, not on a counter: the requirement is that no frame is
    built, not merely that its answer is discarded.
    """
    keyed = True
    p, probe = poller(True, paused=lambda: keyed)
    for _ in range(5):
        p.poll_once()
    assert probe.calls == 0, "a probe went out while the station was keyed"


def test_skipped_rounds_are_counted_apart_from_failed_ones():
    """A deliberate skip and a refusal mean different things to whoever reads the counters — and
    the ratio is how an operator sees that a quiet cadence means a busy transmitter."""
    keyed = True
    p, _probe = poller(True, paused=lambda: keyed)
    p.poll_once()
    assert p.stats()["skipped"] == 1
    assert p.stats()["unknown"] == 0
    assert p.stats()["polls"] == 0, "a round that never reached the wire is not a poll"


def test_the_reading_is_held_across_a_pause_not_blanked():
    """A pause is not a transition. The block the bridges act on must not move because the station
    happened to key — that would mute (or unmute) a link on every over."""
    keyed = False
    p, _probe = poller(True, paused=lambda: keyed)
    p.poll_once()
    before = p()
    keyed = True
    for _ in range(3):
        p.poll_once()
    assert p() == before
    assert p().on is True


def test_the_mute_still_fires_from_a_held_reading_while_paused():
    """The load-bearing one. Pausing the cadence must not disarm the guard it feeds: the reading is
    what the RF->network loops consult, and it stands until something definite replaces it."""
    keyed = False
    p, _probe = poller(True, paused=lambda: keyed)
    p.poll_once()
    keyed = True
    p.poll_once()
    assert p().on is True, "the mute went blind the moment the station keyed"


def test_the_cadence_resumes_the_moment_the_carrier_drops():
    keyed = True
    p, probe = poller(True, paused=lambda: keyed)
    p.poll_once()
    assert probe.calls == 0
    keyed = False
    p.poll_once()
    assert probe.calls == 1
    assert p().on is True


def test_a_poller_with_no_pause_hook_behaves_exactly_as_before():
    """Every existing caller, and every test above, injects no hook. That path must not move."""
    p, probe = poller(True)
    p.poll_once()
    assert probe.calls == 1
    assert p.stats()["skipped"] == 0


def test_a_pause_hook_that_raises_does_not_stop_the_cadence_for_ever():
    """A broken hook must fail toward polling. The alternative is a mute that silently stops
    updating — the fault class this repo keeps closing."""
    def boom():
        raise RuntimeError("no idea")

    p, probe = poller(True, paused=boom)
    p.poll_once()
    assert probe.calls == 1


def test_the_pause_check_never_touches_the_radio():
    """Pinned, because the two obvious spellings both do I/O and one of them is a serial read.

    A pause check that puts a frame on the wire to decide whether to put a frame on the wire would
    rebuild the fault one layer up — on the `uvk5` backend `status()` reads a register.
    """
    touched: list[str] = []

    class Radio:
        def status(self):
            touched.append("status")
            raise AssertionError("the cadence must not call status() to decide whether to poll")

        def ptt_line_asserted(self):
            touched.append("ptt_line_asserted")
            raise AssertionError("the cadence must not read the serial line to decide")

        transmitting = True

    radio = Radio()
    p, probe = poller(True, paused=lambda: bool(getattr(radio, "transmitting", False)))
    p.poll_once()
    assert touched == []
    assert probe.calls == 0
