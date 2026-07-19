"""Reflector link control for the one D-STAR endpoint (ADR 0088).

A thin command/state layer over the *single* :class:`~radio_server.dstar.bridge.DStarBridge` — the
D-STAR analogue of :class:`~radio_server.link.manager.LinkManager`, but deliberately *not* a
per-entry factory manager: there is one gateway endpoint and one bridge (owned by the app lifespan),
so this holds no bridge of its own. It resolves the live bridge lazily through a provider and turns a
reflector name (``"REF001 C"``) into the standard D-STAR link **URCALL command** (``"REF001CL"``),
which the bridge injects into the gateway as a one-shot stream.

**Believed state, not confirmed.** The ircDDBGateway remote-control interface is off (and stays off),
so there is no readback of the real link. :meth:`status` reports what we *sent* — a silently dropped
command or a gateway-side timeout will make ``active`` diverge from reality. The eventual fix is
surfacing the gateway→repeater DSRP ``TEXT``/``STATUS`` packets (the parser already decodes them) as
a confirmation channel; that is a follow-on, noted in ADR 0088, not built here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .bridge import DStarBridge
from .header import LONG_CALLSIGN_LEN

__all__ = [
    "DStarLinkManager",
    "ReflectorTarget",
    "parse_reflector",
    "link_urcall",
    "DStarLinkError",
    "DStarUnavailable",
    "DStarBusy",
    "BridgeProvider",
    "OnChange",
]

#: Resolves the live bridge (or ``None`` when D-STAR is unconfigured / not yet started).
BridgeProvider = Callable[[], "DStarBridge | None"]

#: Fired after every transition with the reflector label ("REF001 C") and the new state.
OnChange = Callable[[str, str], None]

#: Reflector names are a fixed width; the module letter follows, then the "L"/"U" command letter.
_NAME_LEN = LONG_CALLSIGN_LEN - 2  # 6


class DStarLinkError(Exception):
    """Base for reflector-link failures the API surfaces."""


class DStarUnavailable(DStarLinkError):
    """The D-STAR bridge is not configured or not running (API → 503)."""


class DStarBusy(DStarLinkError):
    """The bridge is mid-over (RX or TX), so a command can't be injected now (API → 409)."""


@dataclass(frozen=True)
class ReflectorTarget:
    """A parsed reflector: the 3-char family (REF/XRF/DCS/XLX), full name, and module letter."""

    family: str
    name: str
    module: str

    @property
    def label(self) -> str:
        return f"{self.name} {self.module}"


def parse_reflector(text: str) -> ReflectorTarget:
    """Parse ``"REF001 C"`` / ``"ref001c"`` / ``"XRF012 A"`` into a :class:`ReflectorTarget`.

    Accepts a space-separated ``NAME MODULE`` or a run-together ``NAMEMODULE``. ``ValueError`` on
    anything without a resolvable module letter.
    """
    s = text.strip().upper()
    if not s:
        raise ValueError("empty reflector")
    parts = s.split()
    if len(parts) >= 2:
        name, module = parts[0], parts[1][:1]
    elif len(s) >= _NAME_LEN + 1:
        name, module = s[:_NAME_LEN], s[_NAME_LEN : _NAME_LEN + 1]
    else:
        raise ValueError(f"reflector {text!r} is missing a module letter (e.g. 'REF001 C')")
    if not module.isalpha():
        raise ValueError(f"reflector module {module!r} must be a letter")
    return ReflectorTarget(family=name[:3], name=name, module=module)


def link_urcall(target: ReflectorTarget) -> str:
    """The 8-char link URCALL for a reflector: ``NAME`` (6) + module + ``L`` (e.g. ``REF001CL``).

    REF/XRF/DCS/XLX differ only by the name prefix; the gateway routes to the right protocol handler
    by that prefix, so no family-specific formatting is needed.
    """
    return f"{target.name[:_NAME_LEN]:<{_NAME_LEN}}{target.module}L"


#: The module-wide unlink command letter (right-justified to ``"       U"`` by ``format_urcall``).
UNLINK_URCALL = "U"


class DStarLinkManager:
    """Link/unlink one reflector for the single D-STAR module. Pure DI, asyncio-side.

    ``connect``/``disconnect`` are ``async`` to mirror :class:`LinkManager` and keep the API uniform,
    though the bridge's command injection is a synchronous burst.
    """

    def __init__(self, bridge_provider: BridgeProvider, *, on_change: OnChange | None = None) -> None:
        self._bridge_provider = bridge_provider
        self._on_change = on_change
        self._active: ReflectorTarget | None = None

    @property
    def active(self) -> ReflectorTarget | None:
        """The reflector we believe we're linked to, or ``None``."""
        return self._active

    async def connect(self, reflector: str) -> ReflectorTarget:
        """Link ``reflector`` (e.g. ``"REF001 C"``). ``ValueError`` on a bad name; ``DStarUnavailable``
        with no bridge; ``DStarBusy`` if the bridge is mid-over."""
        bridge = self._bridge_provider()
        if bridge is None:
            raise DStarUnavailable("D-STAR is not configured")
        target = parse_reflector(reflector)
        if not bridge.send_link_command(link_urcall(target)):
            raise DStarBusy("D-STAR link is busy (an over is in progress)")
        self._active = target
        self._notify(target.label, "linked")
        return target

    async def disconnect(self) -> None:
        """Unlink the current reflector (module-wide). Idempotent on the believed state."""
        bridge = self._bridge_provider()
        if bridge is None:
            raise DStarUnavailable("D-STAR is not configured")
        prev = self._active
        if not bridge.send_link_command(UNLINK_URCALL):
            raise DStarBusy("D-STAR link is busy (an over is in progress)")
        self._active = None
        self._notify(prev.label if prev is not None else "", "unlinked")

    def status(self) -> dict:
        """The snapshot for ``GET /dstar/status`` and the ``/status`` ``"dstar"`` block.

        ``active`` is **believed** state (what we last sent), not gateway-confirmed — see the module
        docstring. Includes the live half-duplex ``mode``, the gateway registration, and tx counters.
        """
        active = None
        if self._active is not None:
            active = {
                "family": self._active.family,
                "name": self._active.name,
                "module": self._active.module,
                "reflector": self._active.label,
                "urcall": link_urcall(self._active),
            }
        bridge = self._bridge_provider()
        if bridge is None:
            return {"configured": False, "active": active, "mode": None, "gateway": None, "tx": None}
        gw = bridge.status()
        return {
            "configured": True,
            "active": active,
            "mode": bridge.mode,
            "gateway": {
                "running": gw.running,
                "registered": gw.registered,
                "host": gw.host,
                "port": gw.port,
                "module": gw.module,
            },
            "tx": bridge.tx_stats(),
        }

    def _notify(self, reflector: str, state: str) -> None:
        if self._on_change is not None:
            self._on_change(reflector, state)
