"""Reflector control for the DVAP gateway modules over the remote-control interface (ADR 0095, PR 2).

Unlike :class:`~radio_server.dstar.manager.DStarLinkManager` — which drives radio-server's *own* module
(module A) by injecting an in-band URCALL through the audio bridge and can only ever report *believed*
state — this manages the **DVAP modules** (B = 441.600, C = 441.000). Those are separate
``dstarrepeater`` endpoints radio-server carries no audio for; it links/unlinks and reads their
**confirmed** state purely over the gateway's remote-control channel
(:class:`~radio_server.dstar.remote_client.RemoteControlClient`). No vocoder, no bridge, no PTT.

Confirmed state is **cached**. Each module's link is a bounded UDP round-trip, so a live query on every
``GET /status`` would add latency (and hang on a down gateway). Instead :meth:`status` returns the last
cached snapshot with no I/O, and :meth:`refresh` does the round-trips and updates it — driven by the DVAP
card's poll and after each link/unlink, exactly where a fresh read is wanted.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from .header import format_callsign
from .manager import parse_reflector
from .remote_client import RemoteControlClient, RemoteControlError
from .remote_codec import Reconnect

__all__ = [
    "DvapModule",
    "DvapManager",
    "resolve_dvap_modules",
    "DvapError",
    "DvapUnavailable",
    "DvapUnknownModule",
    "OnDvapChange",
]

#: The fields a ``[[dvap.modules]]`` table may carry; anything else is a typo and fails loud.
_KNOWN_MODULE_FIELDS = frozenset({"module", "label", "frequency_hz"})

#: Fired after a link/unlink transition with the module letter and the new state ("linked"/"unlinked").
OnDvapChange = Callable[[str, str], None]


class DvapError(Exception):
    """Base for DVAP control failures the API surfaces."""


class DvapUnavailable(DvapError):
    """The gateway remote-control interface could not be reached / rejected us (API → 503)."""


class DvapUnknownModule(DvapError):
    """The requested module letter is not a configured DVAP (API → 404)."""


@dataclass(frozen=True)
class DvapModule:
    """One configured DVAP: its gateway module letter, a display label, and its RF frequency (Hz)."""

    module: str
    label: str
    frequency_hz: int


def resolve_dvap_modules(raw: Sequence[Mapping] | None) -> list[DvapModule]:
    """Validate the raw ``[[dvap.modules]]`` list into `DvapModule` values. Fails loud.

    Every entry needs a single-letter ``module`` (unique, A-Z) and a positive integer ``frequency_hz``;
    ``label`` defaults to a ``"module <L>"`` placeholder. Unknown fields are typos.
    """
    if not raw:
        return []
    modules: list[DvapModule] = []
    seen: set[str] = set()
    for i, table in enumerate(raw):
        unknown = set(table) - _KNOWN_MODULE_FIELDS
        if unknown:
            raise ValueError(f"dvap.modules[{i}]: unknown field(s) {sorted(unknown)}")
        module = str(table.get("module", "")).strip().upper()
        if len(module) != 1 or not module.isalpha():
            raise ValueError(f"dvap.modules[{i}]: 'module' must be a single letter, got {module!r}")
        if module in seen:
            raise ValueError(f"dvap.modules[{i}]: duplicate module {module!r}")
        seen.add(module)
        try:
            frequency_hz = int(table.get("frequency_hz", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"dvap.modules[{i}]: 'frequency_hz' must be an integer") from exc
        if frequency_hz <= 0:
            raise ValueError(f"dvap.modules[{i}]: 'frequency_hz' must be a positive integer (Hz)")
        label = str(table.get("label") or f"module {module}").strip()
        modules.append(DvapModule(module=module, label=label, frequency_hz=frequency_hz))
    return modules


class DvapManager:
    """Link/unlink/monitor the configured DVAP modules via the gateway remote-control client.

    All client calls are synchronous (bounded UDP round-trips); the API routes invoke the manager in a
    threadpool so the event loop never blocks. State is cached — :meth:`status` is I/O-free.
    """

    def __init__(
        self,
        client: RemoteControlClient,
        modules: list[DvapModule],
        *,
        station_callsign: str,
        remote_host: str = "",
        remote_port: int = 0,
        on_change: OnDvapChange | None = None,
    ) -> None:
        self._client = client
        self._modules = list(modules)
        self._station = station_callsign
        self._remote_host = remote_host
        self._remote_port = remote_port
        self._on_change = on_change
        # The 8-char gateway callsign field per module (e.g. "AE9S   B"), sent as the repeater id.
        self._callsign: dict[str, str] = {
            m.module: format_callsign(station_callsign, m.module).decode("ascii", "replace")
            for m in self._modules
        }
        # Cached confirmed state per module letter (no I/O to read); refreshed by :meth:`refresh`.
        self._cache: dict[str, dict] = {
            m.module: {
                "module": m.module,
                "label": m.label,
                "frequency_hz": m.frequency_hz,
                "reachable": False,
                "linked": False,
                "reflector": "",
            }
            for m in self._modules
        }

    def _module(self, module: str) -> DvapModule:
        letter = module.strip().upper()[:1]
        for m in self._modules:
            if m.module == letter:
                return m
        raise DvapUnknownModule(f"no DVAP configured on module {module!r}")

    # -- commands (blocking; call via a threadpool) -----------------------------------------

    def link(self, module: str, reflector: str) -> None:
        """Link a DVAP module to ``reflector`` (e.g. ``"REF001 C"``). ``ValueError`` on a bad name;
        ``DvapUnknownModule`` for an unconfigured module; ``DvapUnavailable`` if the gateway won't answer.
        """
        m = self._module(module)
        target = parse_reflector(reflector)  # a bad name fails before we touch the gateway
        try:
            self._client.link(self._callsign[m.module], target.label, Reconnect.FIXED)
        except RemoteControlError as exc:
            raise DvapUnavailable(f"gateway remote-control unavailable: {exc}") from exc
        self._notify(m.module, "linked")

    def unlink(self, module: str) -> None:
        """Unlink a DVAP module's current reflector. ``DvapUnavailable`` if the gateway won't answer."""
        m = self._module(module)
        try:
            self._client.unlink(self._callsign[m.module])
        except RemoteControlError as exc:
            raise DvapUnavailable(f"gateway remote-control unavailable: {exc}") from exc
        self._notify(m.module, "unlinked")

    def refresh(self) -> dict:
        """Query each module's confirmed link over the gateway and update the cache; returns :meth:`status`.

        A module the gateway won't answer for is marked ``reachable: false`` (not an error) so one dead
        module never fails the whole snapshot.
        """
        for m in self._modules:
            entry = self._cache[m.module]
            try:
                msg = self._client.status(self._callsign[m.module])
            except RemoteControlError:
                entry["reachable"] = False
                continue
            # A module is linked ONLY if the RPT reply carries a link record whose `linked` flag is set.
            # The top-level `msg.reflector` is the repeater's *reconnect target* (the name it's configured
            # to relink to) — the gateway keeps it after an unlink, so it is NOT proof of a live link. An
            # earlier version OR'd `bool(msg.reflector)` in here, which made an unlinked module read as
            # "Linked · <last reflector>" forever and made the Disconnect button look like a no-op.
            linked = next((lk for lk in msg.links if lk.linked), None)
            entry["reachable"] = True
            entry["linked"] = linked is not None
            entry["reflector"] = linked.reflector.strip() if linked is not None else ""
        return self.status()

    # -- state (I/O-free) -------------------------------------------------------------------

    def status(self) -> dict:
        """The cached snapshot for ``GET /dvap/status`` and the ``/status`` ``"dvap"`` block. No I/O."""
        return {
            "configured": True,
            "remote": {"host": self._remote_host, "port": self._remote_port},
            "modules": [dict(self._cache[m.module]) for m in self._modules],
        }

    def close(self) -> None:
        """Release the remote-control client. Idempotent."""
        self._client.close()

    def _notify(self, module: str, state: str) -> None:
        if self._on_change is not None:
            self._on_change(module, state)
