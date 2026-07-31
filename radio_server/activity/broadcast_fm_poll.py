"""A cadence for the broadcast-FM probe, so the relay mute can actually fire (ADR 0163).

ADR 0162 put a mute in each bridge's relay loop and a non-mutating probe on the key-up path, and
was straight about the result: on F8/F9 ``Dock_FmOff()`` clears ``gFmRadioMode`` as its first
statement, so a block measured at key-up can never report ``on=True`` and **the mute shipped armed
and blind**. The missing half was a reading taken while nothing was keying. This is that reading.

It is a `PolledGate` (ADR 0125) in shape and for the same reason — a serial read must never happen
on the thread that drains the sound card — but it is a different animal in one respect worth
stating: `PolledGate` caches a verdict that is recomputed from scratch every tick, and **this holds
a reading**. A tick that learns nothing changes nothing.

**What the probe actually answers.** ``True`` is *broadcast FM is selected*, not *the speaker is on
broadcast right now*. Measured on the bench (ADR 0163 M3): while a real signal was on the station's
own channel, the AIOC recovered the witness's 1000 Hz tone at power 0.995 — the firmware had torn
the BK1080 down and was passing channel audio — and this frame still answered ``ERR_BAND``, because
`APP_StartListening` never clears the flag. So the mute this drives also withholds real overs for as
long as an operator listens to broadcast FM. That is the deliberate trade of ADR 0163: the hazard is
a commercial station relayed onto somebody else's repeater (97.113(b)) and lands on third parties;
the cost is traffic missed on a channel the operator has already left, and it is visible in
``deafened_reason`` and the counters rather than silent.
"""

from __future__ import annotations

import logging
import threading
import time

from radio_server.backends.base import BroadcastFm

logger = logging.getLogger(__name__)

#: Seconds between probes while a bridge is relaying. **VERIFY AGAINST HARDWARE (guardrail 1.)**
#: Ten times `PolledGate`'s 0.2 s because this watches a state that only changes when a human
#: presses ``F+0``, and it is not in the audio path. It is also the **leak window**: up to this
#: long of broadcast programming reaches the far end of a link before the mute arms.
DEFAULT_BROADCAST_FM_POLL_INTERVAL = 2.0

#: Seconds a poll waits for its own reply. Deliberately shorter than `SetVfoTuner`'s 3.0 s: a poll
#: holding the wire is time a key-up spends waiting for it, so this is the bound on what the
#: cadence can cost a transmission.
POLL_REQUEST_TIMEOUT = 1.0


class BroadcastFmPoller:
    """Polls the second receiver while a bridge relays, and answers the bridges' ``broadcast_fm``.

    Drops into ADR 0162's existing ``Callable[[], BroadcastFm | None]`` seam — it *is* that
    callable — so no bridge signature changes and every test that injects a bare lambda is
    unaffected. The lifecycle and stats are reached by `getattr`, the idiom `RxPump` already uses
    for `PolledGate`.

    Args:
        probe: ``() -> bool | None``. ``True``/``False`` are measurements; ``None`` is "nothing was
            learned" and is the answer every failure collapses to.
        fallback: ``() -> BroadcastFm | None``, the key-up snapshot ADR 0162 already reads.
        interval: seconds between polls.
        clock: monotonic seconds, injectable for tests.
    """

    def __init__(self, probe, fallback, *,
                 interval: float = DEFAULT_BROADCAST_FM_POLL_INTERVAL, clock=None) -> None:
        self._probe = probe
        self._fallback = fallback
        self._interval = interval
        self._clock = clock or time.monotonic

        #: The last **definite** answer, or ``None`` for "no poll has ever answered". Not a
        #: tri-state about the radio — a tri-state about this poller's knowledge.
        self._reading: bool | None = None
        self._reading_at: float | None = None
        self._unknown = 0
        self._polls = 0

        self._lock = threading.Lock()
        self._users = 0          # how many relay loops are running; the cadence follows it
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None

    # -- the reading -----------------------------------------------------------------------

    def poll_once(self) -> None:
        """One probe. Every failure holds the previous reading rather than inventing a transition.

        Guarded even though `probe_broadcast_fm` promises never to raise, because this runs on a
        daemon thread whose death would be silent — and a silently dead poller is a mute that stops
        firing with every counter still reading zero, which is the fault class this repo keeps
        closing.
        """
        try:
            answer = self._probe(timeout=POLL_REQUEST_TIMEOUT, wire_timeout=0.0)
        except Exception:
            answer = None
        with self._lock:
            self._polls += 1
            if answer is None:
                # ERR_TX (we are keying), a busy wire, a timeout, an unplugged radio. Unknowns are
                # ROUTINE here — the dock refuses `0x0879` on every key-up — so treating one as a
                # state change would mute the link on every over. A non-answer is not a transition.
                self._unknown += 1
                return
            changed = answer != self._reading
            self._reading = bool(answer)
            self._reading_at = self._clock()
        if changed:
            logger.info(
                "uvk5: the second receiver is now %s (relay %s)",
                "SELECTED — this station is on broadcast FM" if answer else "off",
                "muted" if answer else "resumed",
            )

    def __call__(self) -> BroadcastFm | None:
        """The block the relay mute acts on: this poller's reading over the key-up snapshot."""
        block = self._fallback() if self._fallback is not None else None
        with self._lock:
            reading = self._reading
        if reading is None:
            # Nothing polled yet — a backend with no probe stays here for ever. The key-up snapshot
            # is strictly more evidence than a guess, and it is exactly what ADR 0162 shipped.
            return block
        if block is None:
            return BroadcastFm(on=reading)
        # `on` is measured; `hz`, `blocks_tx` and `rescues` are CARRIED, because a refusal blanks
        # every field but the status byte (`dock.c`) and the probe is a refusal by construction.
        # Inventing them would report a frequency nothing measured; dropping them would lose the F9
        # interlock bit the status block already carries.
        return BroadcastFm(on=reading, hz=block.hz, blocks_tx=block.blocks_tx,
                           rescues=block.rescues)

    def stats(self) -> dict:
        """What the bridges surface in ``tx_stats()`` beside the counters ADR 0162 added."""
        with self._lock:
            age = None if self._reading_at is None else self._clock() - self._reading_at
            return {
                "reading": self._reading,
                # `null`, not 0: "nobody is polling" must never render as "polled just now".
                "age_s": age,
                "unknown": self._unknown,
                "polls": self._polls,
            }

    # -- lifecycle: no bridge relaying, no serial traffic ----------------------------------

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        """A relay loop started. Refcounted: Mumble and D-STAR both relay and must not fight."""
        with self._lock:
            self._users += 1
            if self._thread is not None and self._thread.is_alive():
                return
            stop = threading.Event()
            self._stop = stop
            self._thread = threading.Thread(
                target=self._run, args=(stop,), name="broadcast-fm-poll", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        """A relay loop stopped. The cadence ends when the last one does, never before."""
        with self._lock:
            if self._users > 0:
                self._users -= 1
            if self._users > 0:
                return
            stop, thread = self._stop, self._thread
            self._stop = None
            self._thread = None
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.poll_once()
            stop.wait(self._interval)


# --- the three seams the bridges reach through ------------------------------------------------
#
# `getattr`, not `isinstance`: the injected `broadcast_fm` is typed as a plain callable (ADR 0162)
# and most of the suite injects a lambda. Duck-typing the lifecycle is what keeps those unchanged,
# and it is the idiom `RxPump` already uses to drive `PolledGate`'s start/stop.

#: What `tx_stats()` reports where there is no cadence: never measured, nothing held.
NO_CADENCE = {"reading": None, "age_s": None, "unknown": 0, "polls": 0}


def cadence_stats(broadcast_fm) -> dict:
    """The cadence's own numbers, or :data:`NO_CADENCE` when the block is a bare callable."""
    stats = getattr(broadcast_fm, "stats", None)
    if not callable(stats):
        return NO_CADENCE
    try:
        return {**NO_CADENCE, **stats()}
    except Exception:  # a broken poller must not take a status endpoint down with it
        logger.exception("broadcast-fm cadence: stats() failed")
        return NO_CADENCE


def start_cadence(broadcast_fm) -> None:
    """A relay loop is running: begin polling if there is anything to poll."""
    starter = getattr(broadcast_fm, "start", None)
    if callable(starter):
        starter()


def stop_cadence(broadcast_fm) -> None:
    """A relay loop has stopped. Refcounted inside the poller, so the other bridge is unaffected."""
    stopper = getattr(broadcast_fm, "stop", None)
    if callable(stopper):
        stopper()
