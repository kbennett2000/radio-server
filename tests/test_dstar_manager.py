"""DStarLinkManager — reflector link/unlink over the single bridge (ADR 0088).

Pure: the "bridge" is a tiny stub recording the URCALL commands it's handed and reporting a settable
mode, so the manager's parsing, URCALL formatting, believed-state tracking, on_change, and the
busy/unavailable error mapping are all exercised with no gateway and no vocoder.
"""

from __future__ import annotations

import asyncio

import pytest

from radio_server.dstar import GatewayStatus
from radio_server.dstar.manager import (
    DStarBusy,
    DStarLinkManager,
    DStarUnavailable,
    ReflectorTarget,
    link_urcall,
    parse_reflector,
)


class StubBridge:
    """Records link commands; ``busy`` forces send_link_command to refuse (mid-over). ``start_error``
    simulates the shared DV Dongle being held by the other instance (ADR 0089)."""

    def __init__(self, *, busy: bool = False, start_error: Exception | None = None) -> None:
        self.commands: list[str] = []
        self.busy = busy
        self.start_error = start_error
        self.mode = "idle"
        self.started = False
        self.starts = 0
        self.stops = 0

    async def start(self) -> None:
        if self.start_error is not None:
            raise self.start_error
        self.started = True
        self.starts += 1

    async def stop(self) -> None:
        self.started = False
        self.stops += 1

    def send_link_command(self, urcall: str) -> bool:
        if self.busy:
            return False
        self.commands.append(urcall)
        return True

    def status(self) -> GatewayStatus:
        return GatewayStatus(running=True, registered=True, host="127.0.0.1", port=20010, module="A")

    def tx_stats(self) -> dict:
        return {"mode": self.mode}


def test_parse_reflector_space_and_runtogether_and_case():
    assert parse_reflector("REF001 C") == ReflectorTarget("REF", "REF001", "C")
    assert parse_reflector("ref001c") == ReflectorTarget("REF", "REF001", "C")
    assert parse_reflector("  XRF012 a ") == ReflectorTarget("XRF", "XRF012", "A")
    assert parse_reflector("DCS002 C") == ReflectorTarget("DCS", "DCS002", "C")
    assert parse_reflector("XLX123 C") == ReflectorTarget("XLX", "XLX123", "C")


def test_parse_reflector_rejects_missing_module():
    with pytest.raises(ValueError):
        parse_reflector("REF001")
    with pytest.raises(ValueError):
        parse_reflector("REF001 9")  # module must be a letter
    with pytest.raises(ValueError):
        parse_reflector("")


def test_link_urcall_is_name_module_L_across_families():
    assert link_urcall(ReflectorTarget("REF", "REF001", "C")) == "REF001CL"
    assert link_urcall(ReflectorTarget("XRF", "XRF012", "A")) == "XRF012AL"
    assert link_urcall(ReflectorTarget("DCS", "DCS002", "C")) == "DCS002CL"
    assert link_urcall(ReflectorTarget("XLX", "XLX123", "C")) == "XLX123CL"
    assert len(link_urcall(ReflectorTarget("REF", "REF1", "C"))) == 8  # short names pad to 6


def test_connect_sends_link_urcall_and_tracks_believed_state():
    bridge = StubBridge()
    events: list[tuple[str, str]] = []
    mgr = DStarLinkManager(lambda: bridge, on_change=lambda r, s: events.append((r, s)))
    target = asyncio.run(mgr.connect("REF001 C"))
    assert target.label == "REF001 C"
    assert bridge.commands == ["REF001CL"]
    assert mgr.active == ReflectorTarget("REF", "REF001", "C")
    assert events == [("REF001 C", "linked")]
    st = mgr.status()
    assert st["configured"] and st["active"]["reflector"] == "REF001 C"
    assert st["active"]["urcall"] == "REF001CL"


def test_disconnect_sends_unlink_and_clears_state():
    bridge = StubBridge()
    events: list[tuple[str, str]] = []
    mgr = DStarLinkManager(lambda: bridge, on_change=lambda r, s: events.append((r, s)))
    asyncio.run(mgr.connect("REF030 C"))
    asyncio.run(mgr.disconnect())
    assert bridge.commands == ["REF030CL", "U"]  # link then module-wide unlink
    assert mgr.active is None
    assert events[-1] == ("REF030 C", "unlinked")
    assert mgr.status()["active"] is None


def test_busy_bridge_raises_and_leaves_state_untouched():
    bridge = StubBridge(busy=True)
    mgr = DStarLinkManager(lambda: bridge)
    with pytest.raises(DStarBusy):
        asyncio.run(mgr.connect("REF001 C"))
    assert mgr.active is None


def test_connect_starts_the_bridge_and_disconnect_stops_it():
    # ADR 0089: linking acquires the shared DV Dongle (start), unlinking releases it (stop).
    bridge = StubBridge()
    mgr = DStarLinkManager(lambda: bridge)
    asyncio.run(mgr.connect("REF001 C"))
    assert bridge.started and bridge.starts == 1
    asyncio.run(mgr.disconnect())
    assert not bridge.started and bridge.stops == 1


def test_busy_dongle_on_start_raises_unavailable():
    # The exclusive DV Dongle open fails (other radio holds it) → start() raises VocoderUnavailable,
    # surfaced as DStarUnavailable; believed state untouched.
    from radio_server.vocoder.base import VocoderUnavailable

    bridge = StubBridge(start_error=VocoderUnavailable("in use by the other radio"))
    mgr = DStarLinkManager(lambda: bridge)
    with pytest.raises(DStarUnavailable):
        asyncio.run(mgr.connect("REF001 C"))
    assert mgr.active is None
    assert bridge.commands == []  # never got to the link command


def test_no_bridge_raises_unavailable():
    mgr = DStarLinkManager(lambda: None)
    with pytest.raises(DStarUnavailable):
        asyncio.run(mgr.connect("REF001 C"))
    st = mgr.status()
    assert st["configured"] is False and st["active"] is None


def test_status_active_survives_when_bridge_absent():
    # Believed state is manager-held, so it's still reported even if the bridge vanished mid-life.
    box = {"bridge": StubBridge()}
    mgr = DStarLinkManager(lambda: box["bridge"])
    asyncio.run(mgr.connect("REF001 C"))
    box["bridge"] = None
    st = mgr.status()
    assert st["configured"] is False
    assert st["active"]["reflector"] == "REF001 C"  # what we sent, still shown
