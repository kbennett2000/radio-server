"""LinkManager — one active link across N configured entries, switch semantics (ADR 0042).

Driven through injected factories: a `FakeBridge` (records start/stop ordering) for the state
machine, plus one integration scenario over the real `MumbleBridge` + `MockMumbleClient` to prove
the factories compose with the ADR 0041 bridge unchanged. Async scenarios use ``asyncio.run``
(the `test_link_bridge.py` convention).
"""

from __future__ import annotations

import asyncio

import pytest

from radio_server.arbiter import RadioArbiter
from radio_server.backends import MockRadio
from radio_server.link import (
    LinkManager,
    MockMumbleClient,
    MumbleBridge,
    MumbleEntry,
    resolve_mumble_entries,
)
from radio_server.rx import AudioHub
from radio_server.tx import TxSlot

ENTRIES = resolve_mumble_entries(
    [
        {"name": "home", "host": "h1", "dtmf": "13"},
        {"name": "club_net", "host": "h2", "channel": "Club Net", "dtmf": "1234"},
        {"name": "quiet", "host": "h3"},
    ]
)


class FakeBridge:
    """Just enough of `MumbleBridge` for the manager: async start/stop + status via the client."""

    log: list[str] = []  # shared start/stop ordering across instances, reset per scenario

    def __init__(self, client, entry):
        self._client = client
        self._entry = entry
        self.running = False

    async def start(self):
        self._client.connect()
        self.running = True
        FakeBridge.log.append(f"start:{self._entry.name}")

    async def stop(self):
        self.running = False
        self._client.disconnect()
        FakeBridge.log.append(f"stop:{self._entry.name}")

    def status(self):
        return self._client.status()


def _manager(entries=ENTRIES, on_change=None, clients=None):
    FakeBridge.log = []
    clients = clients if clients is not None else {}

    def client_factory(entry):
        client = MockMumbleClient(host=entry.host, channel=entry.channel, peers=2)
        clients.setdefault(entry.name, []).append(client)
        return client

    return LinkManager(
        entries,
        client_factory=client_factory,
        bridge_factory=FakeBridge,
        on_change=on_change,
    )


# --- connect / switch / disconnect -----------------------------------------------------------


def test_connect_and_disconnect_lifecycle():
    async def scenario():
        manager = _manager()
        assert manager.active is None
        await manager.connect("home")
        assert manager.active == "home"
        await manager.disconnect()
        assert manager.active is None
        assert FakeBridge.log == ["start:home", "stop:home"]

    asyncio.run(scenario())


def test_connect_switches_stopping_the_old_bridge_first():
    async def scenario():
        manager = _manager()
        await manager.connect("home")
        await manager.connect("club_net")
        assert manager.active == "club_net"
        # Strict ordering: the old link is fully stopped before the new one starts.
        assert FakeBridge.log == ["start:home", "stop:home", "start:club_net"]

    asyncio.run(scenario())


def test_reconnecting_the_active_entry_restarts_it_fresh():
    async def scenario():
        clients: dict[str, list] = {}
        manager = _manager(clients=clients)
        await manager.connect("home")
        await manager.connect("home")
        assert manager.active == "home"
        assert FakeBridge.log == ["start:home", "stop:home", "start:home"]
        assert len(clients["home"]) == 2  # a fresh client per connect, never reused

    asyncio.run(scenario())


def test_disconnect_is_idempotent():
    async def scenario():
        manager = _manager()
        await manager.disconnect()  # nothing up: a no-op, not an error
        await manager.connect("home")
        await manager.disconnect()
        await manager.disconnect()
        assert FakeBridge.log == ["start:home", "stop:home"]

    asyncio.run(scenario())


def test_unknown_entry_raises_keyerror_and_keeps_the_current_link():
    async def scenario():
        manager = _manager()
        await manager.connect("home")
        with pytest.raises(KeyError):
            await manager.connect("nope")
        # KeyError is raised before the switch — the active link is untouched.
        assert manager.active == "home"
        assert FakeBridge.log == ["start:home"]

    asyncio.run(scenario())


def test_failed_start_leaves_no_active_link():
    async def scenario():
        class ExplodingBridge(FakeBridge):
            async def start(self):
                raise RuntimeError("mumble extra missing")

        clients: dict[str, list] = {}

        def client_factory(entry):
            client = MockMumbleClient(host=entry.host)
            clients.setdefault(entry.name, []).append(client)
            return client

        manager = LinkManager(
            ENTRIES, client_factory=client_factory, bridge_factory=ExplodingBridge
        )
        with pytest.raises(RuntimeError, match="mumble extra"):
            await manager.connect("home")
        assert manager.active is None
        assert not clients["home"][0].status().connected  # half-open client torn down

    asyncio.run(scenario())


# --- status / dtmf / on_change ---------------------------------------------------------------


def test_status_lists_every_entry_with_live_state_on_the_active_one():
    async def scenario():
        manager = _manager()
        snap = manager.status()
        assert snap["active"] is None
        assert [e["name"] for e in snap["entries"]] == ["home", "club_net", "quiet"]
        assert all(not e["running"] and not e["connected"] for e in snap["entries"])

        await manager.connect("club_net")
        snap = manager.status()
        assert snap["active"] == "club_net"
        by_name = {e["name"]: e for e in snap["entries"]}
        assert by_name["club_net"]["running"] and by_name["club_net"]["connected"]
        assert by_name["club_net"]["peers"] == 2
        assert by_name["club_net"]["channel"] == "Club Net"
        assert not by_name["home"]["running"] and by_name["home"]["peers"] is None

    asyncio.run(scenario())


def test_entry_for_dtmf_is_exact_string():
    manager = _manager()
    assert manager.entry_for_dtmf("13") == "home"
    assert manager.entry_for_dtmf("1234") == "club_net"
    assert manager.entry_for_dtmf("1") is None  # no prefix matching — the framer submits whole strings
    assert manager.entry_for_dtmf("") is None  # entries without a combo are not reachable by DTMF


def test_on_change_fires_per_transition():
    async def scenario():
        seen: list[tuple[str, str]] = []
        manager = _manager(on_change=lambda name, state: seen.append((name, state)))
        await manager.connect("home")
        await manager.connect("club_net")  # the switch reports both edges
        await manager.disconnect()
        assert seen == [
            ("home", "connected"),
            ("home", "disconnected"),
            ("club_net", "connected"),
            ("club_net", "disconnected"),
        ]

    asyncio.run(scenario())


# --- integration: the real MumbleBridge composes through the factories -----------------------


def test_manager_drives_a_real_bridge_end_to_end():
    async def scenario():
        radio = MockRadio()
        arbiter, tx_slot, hub = RadioArbiter(), TxSlot(), AudioHub()
        clients: dict[str, MockMumbleClient] = {}

        def client_factory(entry: MumbleEntry):
            client = MockMumbleClient(host=entry.host, channel=entry.channel)
            clients[entry.name] = client
            return client

        def bridge_factory(client, entry: MumbleEntry):
            return MumbleBridge(
                client,
                radio,
                arbiter=arbiter,
                tx_slot=tx_slot,
                audio_hub=hub,
                tx_to_rf=entry.tx_to_rf,
                tx_hang=0.05,
            )

        manager = LinkManager(
            ENTRIES, client_factory=client_factory, bridge_factory=bridge_factory
        )
        await manager.connect("home")
        hub.publish(b"\x01\x00" * 960)
        await asyncio.sleep(0.02)
        assert clients["home"].sent_audio  # RF frame reached Mumble via the real bridge
        await manager.connect("club_net")
        assert not clients["home"].status().connected  # switch fully stopped the old bridge
        assert clients["club_net"].status().connected
        await manager.disconnect()

    asyncio.run(scenario())
