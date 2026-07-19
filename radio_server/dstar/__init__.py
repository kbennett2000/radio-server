"""D-STAR link: radio-server as a homebrew-repeater endpoint on an ircDDBGateway (ADR 0087).

A sibling to the Mumble link (:mod:`radio_server.link`) — a network peer, not a
:class:`~radio_server.backends.base.Radio` backend. It speaks the DSRP repeater<->gateway protocol
(:mod:`.dsrp`, :mod:`.header`) over a UDP client (:mod:`.client`) and bridges reflector audio to and
from the RF stack through the ADR 0086 vocoder (:mod:`.bridge`). Off by default; heavy work (the
serial vocoder) is constructed only when a live link is configured or the doctor self-test runs.
"""

from __future__ import annotations

from .bridge import DEFAULT_DSTAR_TX_HANG, ECHO_URCALL, DStarBridge
from .client import (
    DEFAULT_GATEWAY_HOST,
    DEFAULT_GATEWAY_PORT,
    DEFAULT_LOCAL_PORT,
    DEFAULT_MODULE,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_REGISTER_INTERVAL,
    GatewayClient,
    GatewayStatus,
    MockGatewayClient,
    UdpGatewayClient,
)
from .header import RadioHeader, build_header, build_voice_header, crc16_x25, format_callsign, parse_header

__all__ = [
    "DStarBridge",
    "DEFAULT_DSTAR_TX_HANG",
    "ECHO_URCALL",
    "GatewayClient",
    "GatewayStatus",
    "MockGatewayClient",
    "UdpGatewayClient",
    "DEFAULT_GATEWAY_HOST",
    "DEFAULT_GATEWAY_PORT",
    "DEFAULT_LOCAL_PORT",
    "DEFAULT_MODULE",
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_REGISTER_INTERVAL",
    "RadioHeader",
    "build_header",
    "build_voice_header",
    "crc16_x25",
    "format_callsign",
    "parse_header",
]
