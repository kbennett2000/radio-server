"""AiocBaofeng — audio-only UV-5R backend (audio/PTT via NA6D AIOC cable, no CAT).

Stub only. Hardware is in transit; this class exists so the factory/registry wiring
is complete, but no hardware path (sounddevice/ALSA, pyserial) is touched this cycle.
Brought up in a dedicated hardware phase (ADR 0001, guardrail 6).

Wiring reminders for the bring-up cycle:
  * PTT is explicit: assert the serial RTS line, play audio, drop RTS. The AIOC's
    default PTT line (RTS vs DTR) is empirical — verify on hardware (guardrail 1).
  * No CAT: frequency is set by hand on the radio. capabilities() must omit the CAT
    operations, and the API rejects them rather than no-op'ing (guardrail 3).
"""

from __future__ import annotations

_NOT_READY = "AiocBaofeng is a hardware backend — implemented in a later bring-up cycle"


class AiocBaofeng:
    """Placeholder for the UV-5R backend. Every path raises until hardware bring-up."""

    backend_name = "baofeng"

    def __init__(self, **kwargs):
        raise NotImplementedError(_NOT_READY)
