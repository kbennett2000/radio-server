"""RX audio streaming: a bounded fan-out hub and the pump that feeds it (ADR 0014).

The first half of the voice relay — received audio streamed out to LAN listeners over a binary
WebSocket. :class:`AudioHub` fans raw PCM frames out to every ``/audio/rx`` subscriber with a
bounded, drop-oldest backpressure policy; :class:`RxPump` reads ``radio.receive()`` and publishes
each live frame, gated by an injectable :data:`RxActivityGate` (default pass-through — real
squelch/VAD is a later cycle).
"""

from __future__ import annotations

from .hub import DEFAULT_AUDIO_QUEUE_MAXSIZE, AudioHub
from .link_feeder import LinkFeeder
from .link_pump import DEFAULT_LINK_POLL, LinkPump
from .pump import (
    DEFAULT_RX_POLL,
    RxActivityGate,
    RxPump,
    RxRecorder,
    null_recorder,
    pass_through_gate,
)

__all__ = [
    "AudioHub",
    "DEFAULT_AUDIO_QUEUE_MAXSIZE",
    "RxPump",
    "RxActivityGate",
    "RxRecorder",
    "null_recorder",
    "pass_through_gate",
    "DEFAULT_RX_POLL",
    "LinkPump",
    "DEFAULT_LINK_POLL",
    "LinkFeeder",
]
