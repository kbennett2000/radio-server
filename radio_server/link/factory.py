"""Link backend factory (ADR 0041) — mirrors ``backends/factory.py``.

Maps a backend name to a :class:`~radio_server.link.base.Link` implementation. ``mock`` (software
only) and ``m17`` (a real mrefd reflector, ADR 0052) are registered; AllStar (via chan_usrp + AMI)
is a later cycle that adds a class and one registry entry — no consumer changes. Importing
``M17Link`` here is at-rest safe: it constructs its Codec2 seam lazily, so ``libcodec2`` is only
touched when an M17 backend is actually built.
"""

from __future__ import annotations

from .base import Link
from .m17_link import M17Link
from .mock import MockLink

#: name -> Link class, keyed on each backend's `backend_name` class attribute.
REGISTRY: dict[str, type] = {
    MockLink.backend_name: MockLink,
    M17Link.backend_name: M17Link,
}


def available_links() -> tuple[str, ...]:
    """The registered Link backend names."""
    return tuple(REGISTRY)


def create_link(backend: str, **kwargs) -> Link:
    """Construct a Link backend by name, forwarding ``kwargs`` to its constructor.

    Raises ``ValueError`` (naming the known backends) on an unknown name.
    """
    try:
        cls = REGISTRY[backend]
    except KeyError:
        known = ", ".join(available_links())
        raise ValueError(f"unknown link backend {backend!r}; known link backends: {known}")
    return cls(**kwargs)
