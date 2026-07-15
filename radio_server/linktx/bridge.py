"""The inbound-link transmit bridge: a network peer keys the local transmitter (ADR 0048).

Direction three — ``link.receive()`` → ``radio.transmit()`` — the highest-risk path in the project. A
remote peer's stream is put on the air under the licensee's callsign. This mirrors
:class:`~radio_server.tx.session.TxSession` (the ``/audio/tx`` keying state machine) in the opposite
source: instead of a LAN client streaming binary PCM *in*, the bridge polls the network :class:`Link` and
keys off the **stream edges** it returns — not off inferred frame gaps (ADR 0047). Two shapes reused
verbatim: a clock-injected **synchronous keying core** (unit-testable with a fake clock, no asyncio) and an
async **poll loop** (the :class:`~radio_server.rx.link_pump.LinkPump` shape), gated on ``status().enabled``.

The keying lifecycle is driven by the protocol:

- ``StreamEdge.START`` → acquire the shared :class:`~radio_server.tx.session.TxSlot`, ``arbiter.acquire_tx``,
  ``radio.ptt(True)``, ``limiter.key_down`` (arbiter *before* PTT, exactly as ``TxSession.feed``).
- ``AudioFrame`` → ``radio.transmit(frame)`` while keyed.
- ``StreamEdge.END`` → ``radio.ptt(False)``, release the slot + arbiter, ``limiter.key_up``.
- ``None`` → hold PTT (it says nothing about stream state).

Two backstops cover the two runaways, which are different failures:

- **Silence** — an unpaired ``START`` (the peer vanished, no ``END`` comes; ADR 0047). ``tx.idle_timeout``
  (ADR 0016) drops PTT after the inbound stream goes quiet.
- **Continuous audio** — a stuck-on peer that never goes silent, so ``idle_timeout`` never fires. The
  :class:`~radio_server.txlimit.TxLimiter` force-unkeys at ``max_seconds`` *mid-stream*, then refuses to
  re-key for ``cooloff_seconds`` (a ``START`` refused by cooloff is dropped, not queued).

**Contention: the local operator owns the station.** The bridge and ``/audio/tx`` share one ``TxSlot``. A
link ``START`` while the slot is held is **dropped** (not queued, not preempting) — its frames still tee to
the browser monitor, but nothing reaches the antenna. A browser Talk while the link holds is refused by the
same slot. Both refusals are surfaced by name (guardrail 3), never a silent no-op.

PTT is keyed via the audio/serial path only (guardrail 2 — never a CAT ``TX``). The bridge's runtime
dependency arrow is ``linktx -> {audio, backends, arbiter, link, rx.hub}``; ``TxSlot`` and ``TxLimiter`` are
injected shared instances (annotated under ``TYPE_CHECKING``), so there is no ``linktx -> tx``/``txlimit``
import cycle.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

from ..arbiter import RadioArbiter
from ..audio import AudioFrame
from ..backends import Radio
from ..link import Link, StreamEdge
from ..rx.hub import AudioHub
from ..rx.link_pump import DEFAULT_LINK_POLL
from ..tx.session import Clock, TxRecorder, null_recorder

if TYPE_CHECKING:
    from ..tx.session import TxSlot
    from ..txlimit import TxLimiter


class LinkTxBridge:
    """Poll a :class:`Link`, key the radio off the stream edges, tee frames to the browser hub.

    Feed is implicit: :meth:`run` polls ``link.receive()`` and dispatches each item to the synchronous
    keying core (:meth:`on_start` / :meth:`on_frame` / :meth:`on_end` / :meth:`tick`). The core takes an
    explicit ``now`` so the idle timeout and the limiter are exactly testable with a fake clock. Call
    :meth:`hard_unkey` (or :meth:`stop`, which calls it) to drop PTT immediately on disable/teardown.
    """

    def __init__(
        self,
        link: Link,
        radio: Radio,
        link_hub: AudioHub,
        *,
        tx_slot: TxSlot,
        limiter: TxLimiter,
        idle_timeout: float,
        clock: Clock | None = None,
        arbiter: RadioArbiter | None = None,
        on_key: Callable[[bool], None] | None = None,
        on_event: Callable[..., None] | None = None,
        recorder: TxRecorder = null_recorder,
        poll: float = DEFAULT_LINK_POLL,
    ) -> None:
        if clock is None:
            import time

            clock = time.monotonic
        self._link = link
        self._radio = radio
        self._hub = link_hub
        self._tx_slot = tx_slot
        self._limiter = limiter
        self._idle_timeout = idle_timeout
        self._clock = clock
        # A standalone bridge gets a private arbiter (the `TxSession` default-shape), so isolated
        # construction is behaviorally unchanged; the app injects the one shared instance.
        self._arbiter = arbiter if arbiter is not None else RadioArbiter()
        self._on_key = on_key
        self._on_event = on_event
        self._recorder = recorder
        self._poll = poll
        # Keying state. `_keyed` = PTT asserted for a live link stream. A refused START (contention or
        # cooloff) sets nothing: its frames simply never transmit (they gate on `_keyed`), and a later
        # START is re-evaluated fresh — so an unpaired refused START can never wedge future keying.
        self._keyed = False
        self._keyed_at: float | None = None  # key-down time, for the forced-unkey keyed duration
        self._last_active: float | None = None
        self._running = False
        self._task: asyncio.Task[None] | None = None

    @property
    def keyed(self) -> bool:
        """Whether PTT is currently asserted for an inbound link stream."""
        return self._keyed

    @property
    def running(self) -> bool:
        """Whether the poll loop is active."""
        return self._running

    # --- synchronous keying core (clock-injected; no asyncio) ------------------------------------

    def _fire_key(self, on: bool) -> None:
        if self._on_key is not None:
            self._on_key(on)

    def _fire_event(self, phase: str, **fields: object) -> None:
        if self._on_event is not None:
            self._on_event(phase, **fields)

    def _release(self) -> None:
        """Drop the half-duplex claim and the single-talker slot (idempotent both)."""
        self._arbiter.release_tx()
        self._tx_slot.release()

    def on_start(self, now: float) -> None:
        """A peer began transmitting (LSF). Key the radio, or drop the stream on contention/cooloff."""
        if self._keyed:
            # A second START while a stream is in flight is a backend bug (ADR 0047: START..END brackets);
            # ignore it rather than double-key.
            return
        if not self._limiter.may_key(now):
            # Cooloff after a forced unkey: a START refused here is DROPPED, not queued (ADR 0048). Its
            # frames gate on `_keyed` (still False), so nothing reaches the antenna.
            self._fire_event("refused_cooloff")
            return
        if not self._tx_slot.try_acquire():
            # The local operator owns the station (ADR 0048): the slot is held by a browser Talk, a
            # voice service, or the ID. Drop the link stream — do not preempt, do not queue. We hold
            # nothing to release (try_acquire returned False).
            self._fire_event("dropped")
            return
        # Claim the radio for TX *before* asserting PTT (ADR 0017), exactly as TxSession.feed.
        self._arbiter.acquire_tx()
        self._radio.ptt(True)
        self._limiter.key_down(now)
        self._keyed = True
        self._keyed_at = now
        self._last_active = now
        self._fire_key(True)

    def on_frame(self, data: bytes, now: float) -> None:
        """A frame of stream audio. Transmit only while keyed; a dropped stream's frames go nowhere."""
        if not self._keyed:
            return
        self._radio.transmit(AudioFrame(data))
        self._last_active = now
        # Guarded (ADR 0021): a disk fault must never break keying — the transmit already went out.
        try:
            self._recorder.write(data)
        except Exception:
            pass

    def on_end(self, now: float) -> None:
        """The peer stopped (EOT). Normal unkey — no cooloff. Idempotent; a no-op on a dropped stream."""
        if self._keyed:
            self._unkey(now, forced=False)

    def idle_elapsed(self, now: float) -> bool:
        """True iff keyed and the inbound stream has been silent for at least ``idle_timeout``."""
        if not self._keyed or self._last_active is None:
            return False
        return now - self._last_active >= self._idle_timeout

    def tick(self, now: float) -> None:
        """Enforce the two backstops each poll: the limiter (continuous) and idle-timeout (silence)."""
        if not self._keyed:
            return
        if self._limiter.expired(now):
            # Continuous-audio runaway: force unkey mid-stream, do NOT wait for END. Enters cooloff.
            self._unkey(now, forced=True)
        elif self.idle_elapsed(now):
            # Unpaired-START backstop (ADR 0047): the stream went silent and no END came. A normal
            # unkey — an idle-out is not a limit violation, so no cooloff.
            self._unkey(now, forced=False)

    def _unkey(self, now: float, *, forced: bool) -> None:
        """Drop PTT and release the radio. ``forced`` = the limiter fired (cooloff + distinct record)."""
        keyed_since = self._keyed_at  # key-down time — the operator-visible forced-unkey duration
        self._radio.ptt(False)
        if forced:
            self._limiter.force_unkey(now)
        else:
            self._limiter.key_up(now)
        self._release()
        self._keyed = False
        self._fire_key(False)
        try:
            self._recorder.end_segment()
        except Exception:
            pass
        if forced:
            # DISTINCT from a normal END (ADR 0048): the operator must be able to see the limiter fired.
            duration = None if keyed_since is None else now - keyed_since
            self._fire_event("forced_unkey", duration=duration)

    def hard_unkey(self, now: float | None = None) -> None:
        """Panic drop for ``POST /link/disable``: drop PTT NOW, mid-frame, and release the slot.

        Synchronous and idempotent. Called before the loop is stopped and the gate flipped, so disable
        works while a stranger is keying the rig. An operator abort is a normal key-up (no cooloff).
        """
        if now is None:
            now = self._clock()
        if self._keyed:
            self._unkey(now, forced=False)

    # --- async poll loop (the LinkPump shape) ----------------------------------------------------

    async def run(self) -> None:
        """Poll ``link.receive()`` and key/transmit/unkey off its edges, gated on ``enabled``."""
        self._running = True
        try:
            while self._running:
                # The enable gate (ADR 0041/0042): while disabled, read nothing and key nothing.
                if not self._link.status().enabled:
                    await asyncio.sleep(self._poll)
                    continue
                now = self._clock()
                self.tick(now)  # enforce backstops before processing this poll's item
                item = self._link.receive()  # AudioFrame | StreamEdge | None (ADR 0047)
                if isinstance(item, AudioFrame):
                    # Tee to the browser monitor ALWAYS (ADR 0043) — even a dropped stream is audible —
                    # then transmit only if keyed.
                    if item.samples:
                        self._hub.publish(item.samples)
                    self.on_frame(item.samples, now)
                elif item is StreamEdge.START:
                    self.on_start(now)
                elif item is StreamEdge.END:
                    self.on_end(now)
                # item is None: nothing more — _tick above already handled idle/expiry.
                await asyncio.sleep(self._poll)
        finally:
            self._running = False

    def start(self) -> None:
        """Start the poll loop if not already running (idempotent). Sets ``running`` synchronously."""
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        """Hard-unkey, then stop the loop and join its task. Idempotent — safe when already stopped.

        The hard-unkey guarantees a stop/teardown never leaves PTT asserted, whatever the loop was doing.
        """
        self.hard_unkey()
        task = self._task
        if task is None:
            return
        self._task = None
        self._running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
