"""DTMF activity gate for the Mumble bridge (ADR 0045, reworked ADR 0049).

DTMF is the control plane, not program audio. This shared gate marks "DTMF is happening right
now" for a hold window, and the bridge uses it two ways (ADR 0049):

- **RF→Mumble**: drop tone frames from the Mumble feed so control tones are not broadcast.
- **Mumble→RF**: yield keying while DTMF is active so inbound Mumble voice does not transmit over
  (and blind) the operator's command.

Originally (ADR 0045) the only trigger was multimon-ng's *decoded* digit (`note_digit`), gated by a
delay line to cover decode latency — which lost the race under a continuous ``squelch="off"``
stream. ADR 0049 makes the primary trigger the real-time
:class:`~radio_server.link.tone_detect.DtmfToneDetector` (via `mute_for`), so the gate arms the
instant tone energy appears; `note_digit` remains as a secondary hold-extender when a controller is
present.

This module is just the shared state; the per-frame decision lives in
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

#: Marked default: seconds the gate stays armed after each detected tone / decoded digit. Long
#: enough to span a whole hand-dialed command (each new tone re-arms the hold before it expires),
#: so the RF→Mumble mute and the Mumble→RF yield both cover the full sequence, not just one tone.
DEFAULT_DTMF_MUTE_HOLD = 2.0


class DtmfMuteGate:
    """Shared "DTMF is happening now" state between the tone detector, controller, and bridge.

    ``mute_for`` (real-time tone detector, per frame) and ``note_digit`` (controller's decoded
    digit) both arm the gate; ``muted`` is polled by the bridge's RF→Mumble and Mumble→RF tasks.
    All run on the same event loop, so a plain float needs no locking.
    """

    def __init__(self, *, hold: float = DEFAULT_DTMF_MUTE_HOLD, clock: Clock | None = None) -> None:
        if clock is None:
            import time

            clock = time.monotonic
        self._hold = hold
        self._clock = clock
        self._muted_until: float | None = None

    def mute_for(self, hold: float) -> None:
        """Arm the gate for ``hold`` seconds from now, never *shortening* an in-flight window.

        Max semantics so a short per-frame tone arm can't cut a longer command hold short (and
        vice-versa) when both the detector and `note_digit` are firing.
        """
        until = self._clock() + hold
        if self._muted_until is None or until > self._muted_until:
            self._muted_until = until

    def note_tone(self) -> None:
        """Real-time tone detected (ADR 0049): arm the gate for the default hold — the primary
        trigger, fired per frame by the bridge's :class:`DtmfToneDetector`."""
        self.mute_for(self._hold)

    def note_digit(self) -> None:
        """Record a decoded DTMF digit: arm the gate for the default hold (secondary trigger)."""
        self.mute_for(self._hold)

    def muted(self) -> bool:
        """Whether DTMF is currently active — suppress the Mumble feed and yield RF keying."""
        return self._muted_until is not None and self._clock() < self._muted_until
