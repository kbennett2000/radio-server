"""The DVAP control manager (ADR 0095, PR 2): link/unlink/refresh over a fake remote-control client.

No gateway and no socket — ``DvapManager`` is driven against ``MockRemoteControlClient`` (which models a
tiny gateway), so the confirmed-state cache, the link/unlink round-trips and the error mapping are all
exercised with no I/O. The mock-first discipline.
"""

from __future__ import annotations

import pytest

from radio_server.dstar.dvap_manager import (
    DvapManager,
    DvapModule,
    DvapUnavailable,
    DvapUnknownModule,
    resolve_dvap_modules,
)
from radio_server.dstar.remote_client import MockRemoteControlClient, RemoteTimeout
from radio_server.dstar.remote_codec import (
    Direction,
    Protocol,
    RemoteKind,
    RemoteMessage,
    RepeaterLink,
)

MODULES = [
    DvapModule("B", "DVAP 70cm #1", 441_600_000),
    DvapModule("C", "DVAP 70cm #2", 441_000_000),
]


def _manager(client=None, **kw):
    client = client if client is not None else MockRemoteControlClient()
    return DvapManager(
        client, MODULES, station_callsign="AE9S", remote_host="127.0.0.1", remote_port=10022, **kw
    ), client


def test_status_is_uncached_until_refreshed():
    manager, _ = _manager()
    status = manager.status()
    assert status["configured"] is True
    assert status["remote"] == {"host": "127.0.0.1", "port": 10022}
    assert [m["module"] for m in status["modules"]] == ["B", "C"]
    assert all(m["reachable"] is False and m["linked"] is False for m in status["modules"])
    assert status["modules"][0]["frequency_hz"] == 441_600_000


def test_link_then_refresh_shows_confirmed_state():
    manager, client = _manager()
    manager.link("B", "REF001 C")
    block = manager.refresh()
    b, c = block["modules"]
    assert b["module"] == "B" and b["reachable"] and b["linked"] and b["reflector"] == "REF001 C"
    assert c["module"] == "C" and c["reachable"] and not c["linked"] and c["reflector"] == ""
    # the gateway callsign field carries the module letter in slot 8.
    assert client.sent[0] == ("link", "AE9S   B", "REF001 C", client.sent[0][3])


def test_unlink_clears_the_confirmed_link():
    manager, _ = _manager()
    manager.link("C", "REF030 C")
    assert manager.refresh()["modules"][1]["linked"] is True
    manager.unlink("C")
    assert manager.refresh()["modules"][1]["linked"] is False


def test_module_letter_is_normalised():
    manager, client = _manager()
    manager.link("b", "REF001 C")  # lowercase resolves to module B
    assert client.linked == {"AE9S   B": "REF001 C"}


def test_unknown_module_raises():
    manager, _ = _manager()
    with pytest.raises(DvapUnknownModule):
        manager.link("Z", "REF001 C")
    with pytest.raises(DvapUnknownModule):
        manager.unlink("A")


def test_bad_reflector_name_raises_value_error():
    manager, _ = _manager()
    with pytest.raises(ValueError):
        manager.link("B", "REF")  # too short to carry a module letter


def test_gateway_rejection_surfaces_as_unavailable():
    manager, _ = _manager(MockRemoteControlClient(fail_auth=True))
    with pytest.raises(DvapUnavailable):
        manager.link("B", "REF001 C")


class _StatusRaises:
    """A remote client whose queries always time out — to prove refresh degrades, not raises."""

    def link(self, *a, **k):
        pass

    def unlink(self, *a, **k):
        pass

    def status(self, *a, **k):
        raise RemoteTimeout("gateway down")

    def callsigns(self):
        raise RemoteTimeout("gateway down")

    def close(self):
        pass


def test_refresh_marks_unreachable_module_without_raising():
    manager, _ = _manager(_StatusRaises())
    block = manager.refresh()  # must not raise
    assert all(m["reachable"] is False for m in block["modules"])


class _CannedStatusClient:
    """A remote client that returns a fixed ``RemoteMessage`` per module — to model gateway quirks the
    well-behaved ``MockRemoteControlClient`` never produces (e.g. a remembered reflector with no live link).
    """

    def __init__(self, by_module: dict[str, RemoteMessage]):
        self._by_module = by_module

    def link(self, *a, **k):
        pass

    def unlink(self, *a, **k):
        pass

    def status(self, repeater: str) -> RemoteMessage:
        module = repeater.strip()[-1:]  # "AE9S   B" -> "B"
        return self._by_module.get(
            module, RemoteMessage(RemoteKind.REPEATER, repeater=repeater)
        )

    def callsigns(self):
        return RemoteMessage(RemoteKind.CALLSIGNS)

    def close(self):
        pass


def test_remembered_reflector_without_a_live_link_reads_unlinked():
    # The real gateway keeps the *reconnect target* in RPT's top-level reflector field after an unlink,
    # while the links list is empty. That must read as "not linked" — not "Linked · <last reflector>".
    # (Regression for the sticky-badge bug where Disconnect appeared to do nothing.)
    client = _CannedStatusClient(
        {"B": RemoteMessage(RemoteKind.REPEATER, repeater="AE9S   B", reflector="REF001 C", links=())}
    )
    manager, _ = _manager(client)
    b = manager.refresh()["modules"][0]
    assert b["reachable"] is True
    assert b["linked"] is False
    assert b["reflector"] == ""


def test_link_record_present_but_not_linked_reads_unlinked():
    # A link record in a mid-link ("Linking…") state has linked=False — still not a live link.
    client = _CannedStatusClient(
        {
            "B": RemoteMessage(
                RemoteKind.REPEATER,
                repeater="AE9S   B",
                reflector="REF001 C",
                links=(RepeaterLink("REF001 C", Protocol.DPLUS, False, Direction.OUTGOING, True),),
            )
        }
    )
    manager, _ = _manager(client)
    b = manager.refresh()["modules"][0]
    assert b["linked"] is False and b["reflector"] == ""


def test_active_link_reads_linked_with_its_reflector():
    # The positive control: a link record whose linked flag is set is the one true "linked" signal.
    client = _CannedStatusClient(
        {
            "B": RemoteMessage(
                RemoteKind.REPEATER,
                repeater="AE9S   B",
                reflector="REF001 C",
                links=(RepeaterLink("REF001 C", Protocol.DPLUS, True, Direction.OUTGOING, True),),
            )
        }
    )
    manager, _ = _manager(client)
    b = manager.refresh()["modules"][0]
    assert b["linked"] is True and b["reflector"] == "REF001 C"


def test_on_change_fires_per_transition():
    events: list[tuple[str, str]] = []
    manager, _ = _manager(on_change=lambda module, state: events.append((module, state)))
    manager.link("B", "REF001 C")
    manager.unlink("B")
    assert events == [("B", "linked"), ("B", "unlinked")]


# --------------------------------------------------------------------------------------
# resolve_dvap_modules — [[dvap.modules]] validation
# --------------------------------------------------------------------------------------


def test_resolve_modules_happy_path():
    mods = resolve_dvap_modules(
        [
            {"module": "b", "label": "70cm #1", "frequency_hz": 441_600_000},
            {"module": "C", "frequency_hz": 441_000_000},  # label optional
        ]
    )
    assert [(m.module, m.label, m.frequency_hz) for m in mods] == [
        ("B", "70cm #1", 441_600_000),
        ("C", "module C", 441_000_000),  # default label
    ]


def test_resolve_modules_empty_or_none():
    assert resolve_dvap_modules(None) == []
    assert resolve_dvap_modules([]) == []


@pytest.mark.parametrize(
    "bad",
    [
        [{"module": "BB", "frequency_hz": 1}],  # not a single letter
        [{"module": "1", "frequency_hz": 1}],  # not a letter
        [{"module": "B"}],  # missing frequency
        [{"module": "B", "frequency_hz": 0}],  # non-positive
        [{"module": "B", "frequency_hz": "x"}],  # not an int
        [{"module": "B", "frequency_hz": 1}, {"module": "b", "frequency_hz": 2}],  # duplicate
        [{"module": "B", "frequency_hz": 1, "freq": 2}],  # unknown field
    ],
)
def test_resolve_modules_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        resolve_dvap_modules(bad)


def test_unlink_sends_a_blank_link_command_never_unl():
    # ADR 0109 (bench-proven): every link is Reconnect.FIXED and the gateway REFUSES to UNL a
    # fixed link ("Cannot unlink … because it is fixed") — the old blank UNL didn't even match,
    # so the panel's Disconnect was a silent no-op until the next Connect drop-and-switched the
    # link. The verb that drops a fixed link is LNK to a BLANK reflector; unlink() must send
    # exactly that and never UNL.
    manager, client = _manager()
    manager.link("C", "REF030 C")
    manager.unlink("C")
    from radio_server.dstar.remote_codec import Reconnect

    assert ("link", "AE9S   C", "", Reconnect.NEVER) in client.sent
    assert [s for s in client.sent if s[0] == "unlink"] == []


def test_unlink_with_no_live_link_sends_no_command_and_is_idempotent():
    # Nothing to drop: no wire command, but the transition still notifies so the UI settles.
    events = []
    manager, client = _manager(on_change=lambda module, state: events.append((module, state)))
    manager.unlink("C")
    assert [s for s in client.sent if s[0] in ("unlink", "link")] == []
    assert ("C", "unlinked") in events


def test_unlink_maps_a_dead_gateway_during_the_status_read_to_unavailable():
    class _DeadStatusClient(MockRemoteControlClient):
        def status(self, repeater):
            raise RemoteTimeout("no answer")

    manager, _ = _manager(client=_DeadStatusClient())
    with pytest.raises(DvapUnavailable):
        manager.unlink("C")
