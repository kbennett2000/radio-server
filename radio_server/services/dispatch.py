"""Command dispatch: digit → service handler → transmit.

This is the layer that makes the cycle-2 `AuthGate` command hook real. An authenticated
session's digits arrive here (via `Dispatcher.__call__`, which matches the auth layer's
`Dispatch = Callable[[str, Session], object]` contract). A registered service *produces*
audio; the dispatcher — not the handler — owns the radio and transmits it. Keeping I/O
out of handlers makes them pure/testable and makes "unknown digit → no transmit" correct
by construction: there is simply nothing to send.

No station ID is added here — un-ID'd transmissions are intentional this cycle; automatic
ID (guardrail 5) is a separate scheduler concern (cycle 4). Nothing reaches real hardware
until that exists.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..auth import Clock, Session
from ..backends import AudioFrame, Radio
from .tts import TtsEngine


@dataclass(frozen=True)
class ServiceContext:
    """The capabilities a service handler needs to build audio.

    Deliberately minimal and radio-free: a handler reads the clock and renders speech,
    but cannot do arbitrary radio I/O — the dispatcher transmits what the handler
    returns. Extend this (by ADR) when a service genuinely needs more.
    """

    clock: Clock
    tts: TtsEngine


# A service takes the calling session and the shared context and returns the audio to
# transmit. It performs no radio I/O itself.
Service = Callable[[Session, ServiceContext], AudioFrame]


@dataclass(frozen=True)
class DispatchResult:
    """Outcome of routing one digit string, surfaced as `Outcome.detail`.

    `service` is the matched service's name, or `None` when no service is registered for
    the digits. `transmitted` records whether audio was actually sent — `False` for an
    unknown digit (a graceful no-op, not an error).
    """

    digits: str
    service: str | None
    transmitted: bool


class ServiceRegistry:
    """Maps a digit string to a named service. Services self-register into an instance."""

    def __init__(self) -> None:
        self._services: dict[str, tuple[str, Service]] = {}

    def register(self, digit: str, name: str, service: Service) -> None:
        self._services[digit] = (name, service)

    def get(self, digit: str) -> tuple[str, Service] | None:
        return self._services.get(digit)


class Dispatcher:
    """Callable that routes authenticated digits to a service and transmits its audio.

    Matches the auth layer's `Dispatch` contract, so it drops straight into
    `AuthGate(verifier, ..., dispatch=dispatcher)`. Holds the radio the auth layer does
    not: on a hit it renders via the service and calls `radio.transmit`; on a miss it
    returns a graceful `DispatchResult(transmitted=False)` and sends nothing.
    """

    def __init__(
        self, radio: Radio, ctx: ServiceContext, registry: ServiceRegistry
    ) -> None:
        self._radio = radio
        self._ctx = ctx
        self._registry = registry

    def __call__(self, digits: str, session: Session) -> DispatchResult:
        match = self._registry.get(digits)
        if match is None:
            return DispatchResult(digits=digits, service=None, transmitted=False)
        name, service = match
        audio = service(session, self._ctx)
        self._radio.transmit(audio)
        return DispatchResult(digits=digits, service=name, transmitted=True)
