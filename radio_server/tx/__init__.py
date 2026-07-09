"""TX audio ingest package: keying/ingest state machine + single-talker guard (ADR 0016).

The mirror of ``radio_server.rx`` in the opposite direction — a LAN client streams audio *in* and
the server feeds it to ``radio.transmit()``. Public surface: :class:`TxSession` (per-connection
keying + ingest + idle logic), :class:`TxSlot` (single-talker occupancy guard),
:func:`parse_tx_format` (the format-declaration handshake), and the env-driven idle-timeout config.
"""

from .session import (
    DEFAULT_TX_IDLE_TIMEOUT,
    RADIO_TX_IDLE_TIMEOUT_ENV_VAR,
    Clock,
    TxRecorder,
    TxSession,
    TxSlot,
    load_tx_idle_timeout,
    null_recorder,
    parse_tx_format,
)

__all__ = [
    "TxSession",
    "TxSlot",
    "TxRecorder",
    "null_recorder",
    "parse_tx_format",
    "load_tx_idle_timeout",
    "DEFAULT_TX_IDLE_TIMEOUT",
    "RADIO_TX_IDLE_TIMEOUT_ENV_VAR",
    "Clock",
]
