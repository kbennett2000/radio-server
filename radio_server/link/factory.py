"""Link backend factory (ADR 0041) — mirrors ``backends/factory.py``.

Maps a backend name to a :class:`~radio_server.link.base.Link` implementation. Only ``mock`` is
registered this cycle; the real transports (M17 via mrefd, then AllStar via chan_usrp + AMI) are
later cycles that add a class and one registry entry — no consumer changes.
"""

from __future__ import annotations

from .base import Link
from .mock import MockLink

#: name -> Link class, keyed on each backend's `backend_name` class attribute.
REGISTRY: dict[str, type] = {
    MockLink.backend_name: MockLink,
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
