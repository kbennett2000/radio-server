"""SignaLinkV71 — full-control TM-V71A backend (audio/PTT via SignaLink, CAT via Hamlib).

Stub only. Hardware is in transit; this class exists so the factory/registry wiring
is complete, but no hardware path (sounddevice/ALSA, Hamlib rigctld) is touched this
cycle. Brought up in a dedicated hardware phase (ADR 0001, guardrail 6).

Wiring reminders for the bring-up cycle:
  * PTT is audio-triggered by the SignaLink off the DATA port — transmit() plays
    audio and the box self-keys. NEVER key via a CAT TX command (guardrail 2).
  * CAT (frequency/channel/tone/mode/scan) goes over the PC/COM jack via rigctld.
    The exact Hamlib rig model and serial speed are empirical — verify on hardware.
"""

from __future__ import annotations

_NOT_READY = "SignaLinkV71 is a hardware backend — implemented in a later bring-up cycle"


class SignaLinkV71:
    """Placeholder for the TM-V71A backend. Every path raises until hardware bring-up."""

    backend_name = "v71"

    def __init__(self, **kwargs):
        raise NotImplementedError(_NOT_READY)
