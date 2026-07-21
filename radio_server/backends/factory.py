"""Backend factory/registry — map a backend name to a Radio implementation.

Keeps backend selection in one place so the rest of the stack never imports a
concrete backend directly. The hardware backends are registered here (wiring exists);
each is brought up empirically in its own cycle (guardrail 6).
"""

from __future__ import annotations

from .aioc_baofeng import AiocBaofeng
from .base import Radio
from .kv4p.radio import Kv4pHt
from .mock import MockRadio
from .signalink_v71 import SignaLinkV71
from .uvk5.radio import Uvk5Radio

#: Backend name -> class. Names are what config/the API select on.
REGISTRY: dict[str, type] = {
    MockRadio.backend_name: MockRadio,
    SignaLinkV71.backend_name: SignaLinkV71,
    AiocBaofeng.backend_name: AiocBaofeng,
    Kv4pHt.backend_name: Kv4pHt,
    Uvk5Radio.backend_name: Uvk5Radio,
}


def available_backends() -> tuple[str, ...]:
    """Return the registered backend names."""
    return tuple(REGISTRY)


def create_radio(backend: str, **kwargs) -> Radio:
    """Construct the radio backend named ``backend``.

    Args:
        backend: One of :func:`available_backends` (e.g. ``"mock"``, ``"v71"``,
            ``"baofeng"``).
        **kwargs: Passed through to the backend constructor.

    Raises:
        ValueError: If ``backend`` is not registered.
        NotImplementedError: If a hardware backend is selected before its bring-up.
    """
    try:
        cls = REGISTRY[backend]
    except KeyError:
        known = ", ".join(available_backends())
        raise ValueError(f"unknown backend {backend!r}; known backends: {known}")
    return cls(**kwargs)
