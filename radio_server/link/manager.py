"""One active Mumble link across N configured destinations (ADR 0042).

`LinkManager` owns the boot-time :class:`~radio_server.link.entries.MumbleEntry` tuple and at most
one live :class:`~radio_server.link.bridge.MumbleBridge`. ``connect(name)`` has **switch
semantics** — it disconnects the current entry first — because one half-duplex radio and one talker
slot can only serve one conference at a time (ADR 0042 §3).

A **fresh client + fresh bridge is built per connect** through the injected factories: pymumble's
connection thread is not designed for reuse, and per-connect construction isolates a wedged old
thread. The factories are the seam that keeps this module pure DI — tests inject
:class:`~radio_server.link.client.MockMumbleClient`; the composition root injects
`PyMumbleClient` + a `MumbleBridge` closure over the radio/arbiter/hub wiring.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict

from .bridge import MumbleBridge
from .client import MumbleClient
from .entries import MumbleEntry

__all__ = ["LinkManager", "ClientFactory", "BridgeFactory"]

#: Builds a fresh (unconnected) client for an entry — the composition root closes over secrets here.
ClientFactory = Callable[[MumbleEntry], MumbleClient]

#: Wires a fresh client into a bridge — the composition root closes over radio/arbiter/hub here.
BridgeFactory = Callable[[MumbleClient, MumbleEntry], MumbleBridge]

#: Fired after every transition with the entry name and the new state ("connected"/"disconnected").
OnChange = Callable[[str, str], None]


class LinkManager:
    """The single-active-link state machine over the configured entries. Pure DI, asyncio-side."""

    def __init__(
        self,
        entries: tuple[MumbleEntry, ...],
        *,
        client_factory: ClientFactory,
        bridge_factory: BridgeFactory,
        on_change: OnChange | None = None,
    ) -> None:
        self._entries = entries
        self._by_name = {entry.name: entry for entry in entries}
        self._by_dtmf = {entry.dtmf: entry.name for entry in entries if entry.dtmf}
        self._client_factory = client_factory
        self._bridge_factory = bridge_factory
        self._on_change = on_change
        self._active: str | None = None
        self._bridge: MumbleBridge | None = None

    @property
    def entries(self) -> tuple[MumbleEntry, ...]:
        return self._entries

    @property
    def active(self) -> str | None:
        """The connected entry's name, or ``None`` when no link is up."""
        return self._active

    def entry_for_dtmf(self, digits: str) -> str | None:
        """The entry name a submitted DTMF combo selects, or ``None`` (exact-string match)."""
        return self._by_dtmf.get(digits)

    async def connect(self, name: str) -> None:
        """Connect ``name``, switching away from any current link first. Reconnecting the active
        entry restarts it (a fresh client — the operator's retry path). ``KeyError`` on an unknown
        name (the API maps it to 404)."""
        entry = self._by_name.get(name)
        if entry is None:
            raise KeyError(name)
        await self.disconnect()
        client = self._client_factory(entry)
        bridge = self._bridge_factory(client, entry)
        try:
            await bridge.start()
        except BaseException:
            # A failed start (e.g. the mumble extra missing) must not leave a half-open client
            # behind or the manager pointing at a dead bridge.
            client.disconnect()
            raise
        self._bridge = bridge
        self._active = entry.name
        self._notify(entry.name, "connected")

    async def disconnect(self) -> None:
        """Drop the active link, if any. Idempotent."""
        bridge, name = self._bridge, self._active
        self._bridge = None
        self._active = None
        if bridge is not None:
            await bridge.stop()
            if name is not None:
                self._notify(name, "disconnected")

    def status(self) -> dict:
        """The per-entry snapshot for ``GET /link/status`` / the ``/status`` link block.

        Every configured entry appears (the UI renders the whole list); the active one carries the
        live Mumble connection state (``connected``/``peers``) from the bridge.
        """
        entries = []
        for entry in self._entries:
            row = asdict(entry)
            row["running"] = entry.name == self._active
            if entry.name == self._active and self._bridge is not None:
                mumble = self._bridge.status()
                row["connected"] = mumble.connected
                row["peers"] = mumble.peers
                # Mumble→RF counters (ADR 0045) — getattr-guarded so a minimal bridge stand-in
                # (tests) without `tx_stats` still renders.
                stats = getattr(self._bridge, "tx_stats", None)
                row["tx"] = stats() if callable(stats) else None
            else:
                row["connected"] = False
                row["peers"] = None
                row["tx"] = None
            entries.append(row)
        return {"active": self._active, "entries": entries}

    def _notify(self, name: str, state: str) -> None:
        if self._on_change is not None:
            self._on_change(name, state)
