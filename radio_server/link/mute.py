"""DTMF mute for the Mumble feed (ADR 0045).

DTMF is the control plane, not program audio: callers dialing codes should not be broadcast to
Mumble listeners. But the decoder (multimon-ng) reports a digit only *after* the tone has already
been published to the audio hub, so the bridge cannot gate in real time. Instead it holds its
Mumble feed in a short delay line and, when a digit fires here, retroactively condemns the
buffered audio — the tone onset never leaves the delay line — then stays muted for a hold window
(refreshed per digit) that covers the rest of the sequence.

This module is just the shared mute state; the delay line lives in
:class:`~radio_server.link.bridge.MumbleBridge`. One gate instance is created per app and outlives
individual bridges (they are rebuilt per connect, ADR 0042).
"""

from __future__ import annotations

from collections.abc import Callable

#: A clock returns seconds as a float (``time.monotonic`` by default) — injectable so the hold
#: window is exactly testable with a fake clock (the ``AudioLevelGate`` convention).
Clock = Callable[[], float]

#: Marked default: mute DTMF out of the Mumble feed. ``mumble.dtmf_mute = false`` restores the
#: raw relay (tones audible to Mumble listeners).
DEFAULT_DTMF_MUTE = True

#: Marked default: seconds the feed stays muted after each decoded digit. Long enough to bridge
#: the inter-digit gap of a hand-dialed sequence (each new tone re-arms the hold before it
#: expires); audio between slower digits passes — it is legitimate RF audio, and each tone's
#: onset is still swallowed by the bridge's delay line.
DEFAULT_DTMF_MUTE_HOLD = 1.0


class DtmfMuteGate:
    """Shared "DTMF recently decoded" state between the controller and the Mumble bridge.

    ``note_digit`` is called by the controller's decode path for every digit (on the event loop);
    ``muted`` is polled by the bridge's RF→Mumble task (same loop), so a plain float is safe.
    """

    def __init__(self, *, hold: float = DEFAULT_DTMF_MUTE_HOLD, clock: Clock | None = None) -> None:
        if clock is None:
            import time

            clock = time.monotonic
        self._hold = hold
        self._clock = clock
        self._muted_until: float | None = None

    def note_digit(self) -> None:
        """Record a decoded DTMF digit: mute now, refreshed to ``hold`` seconds from now."""
        self._muted_until = self._clock() + self._hold

    def muted(self) -> bool:
        """Whether the Mumble feed should currently be suppressed."""
        return self._muted_until is not None and self._clock() < self._muted_until
