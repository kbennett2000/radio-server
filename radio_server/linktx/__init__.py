"""Inbound-link transmit bridge (ADR 0048): a network peer keys the local transmitter.

Direction three — ``link.receive()`` → ``radio.transmit()``. :class:`LinkTxBridge` is the single reader of
``link.receive()``: it keys the radio off the stream edges (ADR 0047), transmits each frame, tees frames to
the browser ``link_hub`` (ADR 0043), and bounds a keyed stream with ``tx.idle_timeout`` (silence) and the
:class:`~radio_server.txlimit.TxLimiter` (continuous audio). Contention with the local operator is mediated
by the shared :class:`~radio_server.tx.session.TxSlot`: the local operator owns the station.
"""

from .bridge import LinkTxBridge

__all__ = [
    "LinkTxBridge",
]
