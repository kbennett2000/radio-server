"""Link — radio-server's second port: a network peer on the audio bus, not the antenna (ADR 0041).

Parallel to :mod:`radio_server.backends`: one :class:`Link` protocol, a hardware-free
:class:`MockLink` the stack is built against, and a :func:`create_link` factory. Real transports
(M17/mrefd first, AllStar/USRP later) are brought up last. This package is a leaf — its only
``radio_server`` dependency is :mod:`radio_server.audio`.
"""

from .base import (
    FULL_CAPS,
    OPTIONAL_CAPS,
    SHARED_CAPS,
    Link,
    LinkCapability,
    LinkStatus,
    Station,
    StreamEdge,
    UnsupportedLinkCapability,
)
from .factory import available_links, create_link
from .mock import MockLink

__all__ = [
    "Link",
    "LinkCapability",
    "LinkStatus",
    "Station",
    "StreamEdge",
    "UnsupportedLinkCapability",
    "SHARED_CAPS",
    "OPTIONAL_CAPS",
    "FULL_CAPS",
    "MockLink",
    "create_link",
    "available_links",
]
