"""A cadence for the signal-strength read, off the audio thread (ADR 0175).

``status.rssi`` was ``null`` on the deployed `baofeng` backend, so nothing above the radio layer
could see how strong a signal was and ADR 0160's airband sweep measured audio RMS across 760
channels rather than ask the radio. The reading was one ``0x0851`` away the whole time. This is what
takes it.

**Why a background thread and not a read inside** ``status()``. ADR 0125 measured the alternative:
`CatBusyGate` did a ~100 ms blocking serial round-trip once per 20 ms audio frame, and the RX pump
fell to **14.3 % duty with zero bytes reaching the browser**. Caching inline only slowed the
failure (84.4 %, still overflowing the 60 ms ring). Its conclusion is a rule — *you cannot do a
100 ms blocking serial read on the thread that must drain a 60 ms ring, ever* — and while
``status()`` is not that thread today, `CatBusyGate` calls ``radio.status()`` and is one config key
away from being on it. A cadence is the shape that cannot regress into the measured fault.

It is a `PolledGate` in spirit and `BroadcastFmPoller` in detail: a poll that learns nothing changes
nothing, and a non-answer is never a transition. What it adds over both is **an expiry**. Those two
hold a verdict about a state that persists — a channel is busy, a receiver is selected — so a stale
answer is merely late. This holds a *measurement of a moment*, and a measurement rendered as current
long after it was taken is not late, it is wrong. So a reading nobody has refreshed within
:data:`STALE_AFTER` intervals reports ``None``, which the API already defines as *no reading* rather
than *no signal*.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

#: Seconds between reads. **VERIFY AGAINST HARDWARE (guardrail 1.)** A module default and not a
#: config key, following `DEFAULT_CAT_POLL_INTERVAL` and `DEFAULT_BROADCAST_FM_POLL_INTERVAL`.
#:
#: Sized from the measurement rather than by analogy (ADR 0175). On the bench the reading tracks the
#: channel inside ~0.3 s: it rises on the first sample after a key-up (0.15 s) and is back at the
#: floor two samples after an un-key, across four key/un-key cycles. So the channel is never more
#: than a couple of polls ahead of this. 0.2 s is `PolledGate`'s number because its verdict gates
#: *audio* and lag is audible; 2.0 s is the broadcast-FM poll's because it watches a human pressing
#: a key. This gates nothing and follows an over, so it sits between the two — and it matches the
#: station's own ``audio.vad_hang = 0.5``, so the number is never staler than the gate beside it.
#: Costs two 32-byte exchanges a second on an otherwise idle 38400 wire, abandoned the instant
#: anything else wants it.
DEFAULT_RSSI_POLL_INTERVAL = 0.5

#: How many intervals a reading may go unrefreshed before it stops being reported. Three, so a
#: single skipped round — a poll that lost the wire to a tune, which is routine — does not blank a
#: perfectly good number, but a link that has stopped answering goes quiet in about a second and a
#: half rather than showing the last thing it ever saw for the life of the process.
STALE_AFTER = 3.0

#: Seconds a poll waits for its own reply. Deliberately short: a poll holding the wire is time a
#: key-up spends waiting for it, and the wire is also this station's PTT line.
POLL_REQUEST_TIMEOUT = 1.0


class RssiPoller:
    """Reads the tuner's signal strength on a cadence and answers ``status.rssi``.

    Args:
        probe: ``(*, timeout, wire_timeout) -> int | None``. An ``int`` is a measurement; ``None``
            is "nothing was learned" and is what every failure collapses to — a busy wire, a silent
            radio, a yanked cable, and a raw ``0`` (which `SetVfoTuner.read_rssi` reports as no
            reading because ADR 0132 measured 0 as the receiver being switched off, not as quiet).
        paused: ``() -> bool``. While it answers ``True`` **nothing touches the wire at all** — not
            a request, not a non-blocking lock acquire. See :meth:`poll_once`.
        interval: seconds between polls.
        clock: monotonic seconds, injectable for tests.
    """

    def __init__(self, probe, *, paused=None, interval: float = DEFAULT_RSSI_POLL_INTERVAL,
                 clock=None) -> None:
        self._probe = probe
        self._paused = paused
        self._interval = interval
        self._clock = clock or time.monotonic

        #: The last definite reading, or ``None`` for "no poll has ever answered". A statement about
        #: this poller's knowledge, not about the channel.
        self._reading: int | None = None
        self._reading_at: float | None = None
        self._unknown = 0
        self._skipped = 0
        self._polls = 0

        self._lock = threading.Lock()
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None

    # -- the reading -----------------------------------------------------------------------

    def poll_once(self) -> None:
        """One read. Every failure holds the previous reading rather than inventing a transition.

        **Nothing happens here while the station is transmitting, and that is not an optimisation —
        it is the whole reason this class has a `paused` hook.** Measured on the bench (ADR 0175):
        with this cadence running through an over, the kv4p witness recovered the 1000 Hz test tone
        at **0.026** against 0.989 on the same station minutes earlier, and a 4.4 s transmission
        reached it as 0.70 s of audio. The station ID and the time announcement were mangled the
        same way. The AIOC is ONE USB composite device — the CDC serial and the audio interface
        share a cable, a controller, and the radio's K1 jack contacts — so a 32-byte register exchange
        every half second is enough to wreck the isochronous audio-out stream feeding the
        transmitter. `uart_while_streaming.py` had measured dock frames surviving a running sound
        card, 18/18, which is a different claim: it proved the FRAMES got through, never that the
        AUDIO did.

        Skipping costs nothing, because `AiocBaofeng.status()` reports ``None`` while keyed anyway:
        a receiver cannot measure a channel through its own carrier, so a poll taken here was
        always going to be thrown away. It was pure risk on a wire that is also the PTT line.
        """
        if self._paused is not None:
            try:
                paused = bool(self._paused())
            except Exception:  # noqa: BLE001 - a broken hook must not silence the meter for ever
                paused = False
            if paused:
                with self._lock:
                    self._skipped += 1
                return
        try:
            answer = self._probe(timeout=POLL_REQUEST_TIMEOUT, wire_timeout=0.0)
        except Exception:  # noqa: BLE001
            answer = None
        with self._lock:
            self._polls += 1
            if answer is None:
                # Routine, not exceptional: the wire is shared with tuning and with the PTT line,
                # so a poll that skips its round because a key-up has the wire is the design
                # working. Holding the previous reading is what keeps the number steady across it;
                # the expiry below is what stops that from becoming a lie.
                self._unknown += 1
                return
            self._reading = int(answer)
            self._reading_at = self._clock()

    def reading(self) -> int | None:
        """What `status()` reports: the last measurement, or ``None`` once it has gone stale."""
        with self._lock:
            if self._reading is None or self._reading_at is None:
                return None
            if self._clock() - self._reading_at > STALE_AFTER * self._interval:
                return None
            return self._reading

    def stats(self) -> dict:
        with self._lock:
            age = None if self._reading_at is None else self._clock() - self._reading_at
            return {
                "reading": self._reading,
                # `null`, not 0: "nobody has polled" must never render as "polled just now".
                "age_s": age,
                "unknown": self._unknown,
                # Rounds deliberately not taken (the station was transmitting), kept apart from
                # `unknown` because they are not failures — and because the ratio is how an
                # operator sees that a quiet meter means a busy transmitter.
                "skipped": self._skipped,
                "polls": self._polls,
            }

    # -- lifecycle -------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        """Begin polling. Idempotent and restartable, like `PolledGate`'s."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            stop = threading.Event()
            self._stop = stop
            self._thread = threading.Thread(
                target=self._run, args=(stop,), name="rssi-poll", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        """Signal the poller to exit and join it (bounded; idempotent)."""
        with self._lock:
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
