"""TX time-limiter package (ADR 0045).

The policy that bounds how long the transmitter may stay keyed on a single link transmission: expiry at
``max_seconds`` and a ``cooloff_seconds`` re-key refusal after a forced unkey. A pure leaf — imports
nothing from the rest of ``radio_server`` (and no ``time``: the caller passes ``now``) — so
``tx``/``api`` can depend on it with no cycles. It bounds the runaway ``tx.idle_timeout`` cannot catch:
*continuous* audio (a stuck VOX, a looped bridge) that never goes silent. Public surface:
:class:`TxLimiter`, :class:`TxLimitState`.
"""

from .limiter import TxLimiter, TxLimitState

__all__ = [
    "TxLimiter",
    "TxLimitState",
]
