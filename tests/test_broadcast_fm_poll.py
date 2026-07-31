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
