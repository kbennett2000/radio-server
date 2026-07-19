"""TotRadio — a hard transmitter time-out timer (TOT) wrapping any Radio (ADR 0090).

Every existing keying safeguard (`tx.idle_timeout`, the D-STAR/Mumble `tx_hang`) is a *silence*
timeout: it resets on every non-silent frame, so a **continuous** transmission — a held mic, a
crossband over that never closes, a decode loop that wedged with PTT asserted — is uncapped. The
D-STAR reflector→RF stuck-key incident (a decode parked in the DV Dongle executor while the radio
stayed keyed, transmitting dead air, that neither `unlink` nor `bridge.stop()` could drop) is exactly
that failure mode.

`TotRadio` is the last-resort net. It wraps the active :class:`Radio` at the composition root
(`build_radio`), so **every** keying path funnels through it — browser TX, the D-STAR/Mumble bridges,
DTMF services, station ID, the REST ``/ptt`` and ``/transmit`` routes, and every backend. It watches
the two keying methods (`ptt`, `transmit`) and force-drops PTT after ``tot`` seconds of continuous key.

Two design points make it bulletproof against the incident:

- **It fires on its own timer thread**, not an asyncio task or the caller. When the reflector→RF loop
  is parked in ``run_in_executor(vocoder.decode, …)`` the caller cannot drop PTT — but an independent
  ``threading.Timer`` still runs and calls ``ptt(False)`` on the wrapped radio.
- **A streaming time-out latches TOT-locked**: after force-unkey, further ``transmit()`` calls are
  dropped until an explicit ``ptt(False)``/``ptt(True)`` re-key. This stops a *self-keying* source
  (the audio-triggered SignaLink keys off the audio itself, so dropping the line alone won't silence
  it) from re-keying frame after frame. A one-shot ``transmit()`` that overran does **not** latch — it
  is independent and short, so a single stuck clip must not wedge station ID forever.

The watchdog is armed once on key-up and never reset per frame — a hard cap on continuous key, distinct
from the per-frame idle timeouts above. ``tot <= 0`` disables it (pure pass-through).
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Callable, Protocol

if TYPE_CHECKING:
    from ..backends import AudioFrame, Radio

log = logging.getLogger(__name__)


class _Timer(Protocol):
    """The slice of ``threading.Timer`` the watchdog uses — injectable so tests fire it synchronously."""

    def start(self) -> None: ...
    def cancel(self) -> None: ...


def _default_timer_factory(delay: float, fn: Callable[[], None]) -> _Timer:
    """Real watchdog: a daemon ``threading.Timer`` that runs ``fn`` ``delay`` seconds after ``start()``."""
    timer = threading.Timer(delay, fn)
    timer.daemon = True  # never keep the process alive for a pending unkey
    return timer


class TotRadio:
    """Wrap ``radio`` so no keying path can hold PTT longer than ``tot`` seconds (ADR 0090).

    Delegates the whole :class:`Radio` surface (and off-protocol ``close``/CAT methods) to the wrapped
    backend; only ``ptt`` and ``transmit`` are intercepted to drive the watchdog. ``on_timeout`` (if
    given) is fired after a forced unkey, for the operating log / UI. ``timer_factory`` is injectable so
    tests drive expiry deterministically without real waits.
    """

    def __init__(
        self,
        radio: "Radio",
        *,
        tot: float,
        on_timeout: Callable[[], None] | None = None,
        timer_factory: Callable[[float, Callable[[], None]], _Timer] = _default_timer_factory,
    ) -> None:
        self._radio = radio
        self._tot = float(tot)
        self._on_timeout = on_timeout
        self._timer_factory = timer_factory
        self._lock = threading.RLock()
        self._timer: _Timer | None = None
        self._token: object | None = None  # identifies the live timer; a superseded fire is ignored
        self._keyed = False  # an explicit ptt(True) is held (streaming), vs a one-shot transmit()
        self._locked = False  # a streaming TOT fired; drop transmit() until an explicit re-key/unkey

    # -- transparent delegation for everything we don't intercept ------------------------------
    def __getattr__(self, name: str) -> object:
        # Only reached for attributes TotRadio doesn't define (receive, status, capabilities, CAT
        # tuning, …). `_radio` is a real instance attribute, so this never recurses.
        return getattr(self._radio, name)

    # -- watchdog (all callers hold self._lock) ------------------------------------------------
    def _arm(self) -> None:
        self._disarm()
        if self._tot > 0:
            token = object()
            self._token = token
            self._timer = self._timer_factory(self._tot, lambda: self._fire(token))
            self._timer.start()

    def _disarm(self) -> None:
        self._token = None  # invalidate any fire already in flight
        if self._timer is not None:
            try:
                self._timer.cancel()
            except Exception:  # a cancel fault must never leave a caller stuck
                pass
            self._timer = None

    def _fire(self, token: object) -> None:
        """Runs on the timer thread ``tot`` seconds after key-up — force-drop PTT independent of the caller."""
        with self._lock:
            if token is not self._token:  # a newer arm/disarm superseded this timer; ignore
                return
            self._timer = None
            self._token = None
            was_streaming = self._keyed
            self._keyed = False
            if was_streaming:
                # Latch off a runaway *held* key so a self-keying source can't re-key each frame; a
                # one-shot transmit() that merely overran is independent and must not latch.
                self._locked = True
        # Drop PTT OUTSIDE the lock: the wrapped call may block, and this must run while a stuck caller
        # is parked in transmit()/decode — that is the whole point of the independent timer thread.
        try:
            self._radio.ptt(False)
        except Exception:
            log.exception("TOT: forced ptt(False) failed")
        log.error("transmitter time-out timer fired: PTT force-dropped after %.0fs continuous key", self._tot)
        if self._on_timeout is not None:
            try:
                self._on_timeout()
            except Exception:
                pass

    # -- intercepted keying surface ------------------------------------------------------------
    def ptt(self, on: bool) -> None:
        with self._lock:
            self._locked = False  # an explicit key command is always a fresh start
            self._keyed = bool(on)
            if on:
                self._arm()
            else:
                self._disarm()
        self._radio.ptt(on)  # outside the lock — the wrapped call may block

    def transmit(self, audio: "AudioFrame") -> None:
        with self._lock:
            if self._locked:
                return  # a streaming TOT tripped; drop audio until an explicit re-key/unkey
            one_shot = not self._keyed  # a self-keying transmit() with no ptt(True) held
            if one_shot:
                self._arm()  # cap this clip's duration; streaming was already armed at ptt(True)
        try:
            self._radio.transmit(audio)  # outside the lock — blocks during playback
        finally:
            if one_shot:
                with self._lock:
                    # Skip if a TOT fired during the call (it cleared the timer already); else disarm.
                    if not self._locked:
                        self._disarm()

    def close(self) -> None:
        """Disarm the watchdog and close the wrapped device (if it has a ``close``); safe if not."""
        with self._lock:
            self._disarm()
            self._keyed = False
            self._locked = False
        inner = getattr(self._radio, "close", None)
        if inner is not None:
            inner()
