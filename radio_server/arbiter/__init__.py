"""Half-duplex radio arbiter package (ADR 0017).

The shared radio-ownership seam that keeps a half-duplex radio from receiving and transmitting at
once: TX claims it on key-up, RX and scan stand down while it holds. A pure leaf — imports nothing
from the rest of ``radio_server`` — so ``tx``/``rx``/``scan``/``api`` can all depend on it with no
cycles. Public surface: :class:`RadioArbiter`, :class:`RadioMode`, :class:`ArbiterStateError`.
"""

from .state import ArbiterStateError, RadioArbiter, RadioMode

__all__ = [
    "RadioArbiter",
    "RadioMode",
    "ArbiterStateError",
]
