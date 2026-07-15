"""Tests for the Link protocol and MockLink (ADR 0041).

Mirrors ``test_mock_radio.py`` + ``test_capabilities.py`` for the new network port: protocol
conformance, tx_log round-trip, scripted RX (with the ``None`` idle return), the toggleable
capability set, capability gating that raises *by name*, the enable safety lifecycle, connect/status,
and the factory. A final test enforces the acceptance property that ``link/`` imports nothing from
``radio_server`` outside ``..audio``.
"""

import ast
import pathlib

import pytest

from radio_server.audio import CANONICAL_FORMAT, AudioFormat, AudioFormatMismatch, AudioFrame
from radio_server.link import (
    FULL_CAPS,
    OPTIONAL_CAPS,
    SHARED_CAPS,
    Link,
    LinkCapability,
    LinkStatus,
    MockLink,
    Station,
    StreamEdge,
    UnsupportedLinkCapability,
    available_links,
    create_link,
)

OTHER = AudioFormat(rate=8000, width=2, channels=1)  # a non-canonical format for the TX guard test


# --- protocol conformance ------------------------------------------------------------------------


def test_mock_is_a_link():
    assert isinstance(MockLink(), Link)


# --- tx_log round-trip ---------------------------------------------------------------------------


def test_transmit_records_audio_in_order():
    link = MockLink()
    assert link.tx_log == []
    link.transmit(AudioFrame(b"one"))
    link.transmit(AudioFrame(b"two"))
    assert link.tx_log == [AudioFrame(b"one"), AudioFrame(b"two")]


def test_transmit_of_wrong_format_raises_and_records_nothing():
    link = MockLink()  # accepts canonical
    with pytest.raises(AudioFormatMismatch):
        link.transmit(AudioFrame(b"bad", OTHER))
    assert link.tx_log == []


def test_link_can_be_built_for_a_non_canonical_format():
    link = MockLink(format=OTHER)
    link.transmit(AudioFrame(b"ok", OTHER))
    assert link.tx_log == [AudioFrame(b"ok", OTHER)]


# --- stream boundaries (ADR 0044 amendment: the ptt(on) mirror) -----------------------------------


def test_stream_records_boundaries_in_tx_log():
    link = MockLink()
    link.stream(True)
    link.stream(False)
    assert link.tx_log == [StreamEdge.START, StreamEdge.END]


def test_stream_brackets_the_transmitted_frames():
    # The acceptance shape: a transmission is one stream open, its frames, one stream close.
    link = MockLink()
    link.stream(True)
    link.transmit(AudioFrame(b"aa"))
    link.transmit(AudioFrame(b"bb"))
    link.stream(False)
    assert link.tx_log == [
        StreamEdge.START,
        AudioFrame(b"aa"),
        AudioFrame(b"bb"),
        StreamEdge.END,
    ]


def test_stream_is_part_of_the_link_protocol():
    # Adding stream() keeps MockLink a Link, and stream is callable on the protocol surface.
    link: Link = MockLink()
    assert isinstance(link, Link)
    link.stream(True)  # no capability gate — part of the TRANSMIT surface


# --- receive: scripted FIFO then idle None -------------------------------------------------------


def test_receive_serves_scripted_frames_then_none_when_idle():
    link = MockLink(rx_frames=[AudioFrame(b"\x01\x02")])
    link.script_rx(AudioFrame(b"\x03\x04"))
    assert link.receive().samples == b"\x01\x02"  # scripted, FIFO
    assert link.receive().samples == b"\x03\x04"  # scripted, FIFO
    assert link.receive() is None  # drained -> network idle


def test_receive_falls_back_to_canned_rx_when_set():
    canned = AudioFrame(b"\x00\x00")
    link = MockLink(canned_rx=canned)
    assert link.receive() is canned
    assert link.receive() is canned  # canned is served repeatedly, unlike the FIFO


# --- toggleable capability set -------------------------------------------------------------------


def test_full_mock_advertises_every_capability():
    assert MockLink().capabilities() == FULL_CAPS


def test_directory_off_drops_only_directory():
    assert MockLink(directory=False).capabilities() == SHARED_CAPS | {LinkCapability.LISTEN_ONLY}


def test_listen_only_off_drops_only_listen_only():
    assert MockLink(listen_only=False).capabilities() == SHARED_CAPS | {LinkCapability.DIRECTORY}


def test_both_off_advertises_shared_only():
    caps = MockLink(directory=False, listen_only=False).capabilities()
    assert caps == SHARED_CAPS
    assert not (caps & OPTIONAL_CAPS)


def test_capability_sets_partition_cleanly():
    assert SHARED_CAPS | OPTIONAL_CAPS == FULL_CAPS
    assert SHARED_CAPS.isdisjoint(OPTIONAL_CAPS)
    assert len(FULL_CAPS) == len(LinkCapability)


# --- capability gating raises by name ------------------------------------------------------------


def test_directory_raises_by_name_when_unsupported():
    link = MockLink(directory=False)
    with pytest.raises(UnsupportedLinkCapability) as excinfo:
        link.directory()
    assert excinfo.value.capability is LinkCapability.DIRECTORY


def test_set_listen_only_raises_by_name_when_unsupported():
    link = MockLink(listen_only=False)
    with pytest.raises(UnsupportedLinkCapability) as excinfo:
        link.set_listen_only(True)
    assert excinfo.value.capability is LinkCapability.LISTEN_ONLY


def test_directory_returns_entries_when_supported():
    entries = (Station("AE9S"), Station("K1ABC"))
    link = MockLink(directory_entries=entries)
    assert link.directory() == entries


def test_set_listen_only_toggles_when_supported():
    link = MockLink()
    assert link.listening_only is False
    link.set_listen_only(True)
    assert link.listening_only is True


# --- the enable safety lifecycle (ADR 0041) ------------------------------------------------------


def test_link_comes_up_disabled():
    # The safety property: a Link is never born enabled. Autostart must not put it on the air.
    assert MockLink().status().enabled is False


def test_enable_is_a_deliberate_act_and_reversible():
    link = MockLink()
    link.enable(True)
    assert link.status().enabled is True
    link.enable(False)
    assert link.status().enabled is False


def test_enabled_is_not_sticky_across_instances():
    first = MockLink()
    first.enable(True)
    # A fresh instance is independent and disabled — nothing is persisted or restored.
    assert MockLink().status().enabled is False


def test_no_constructor_argument_can_start_a_link_enabled():
    # There is no born-enabled path: `enabled` is not a constructor parameter.
    with pytest.raises(TypeError):
        MockLink(enabled=True)


# --- connect / disconnect / who's on / who's talking ---------------------------------------------


def test_connect_and_disconnect_update_status():
    link = MockLink()
    assert link.status().connected is False and link.status().target is None
    link.connect("M17-USA C")
    status = link.status()
    assert status.connected is True and status.target == "M17-USA C"
    link.disconnect()
    assert link.status().connected is False and link.status().target is None


def test_status_reports_stations_and_talker():
    on = [Station("AE9S"), Station("K1ABC")]
    talking = Station("AE9S")
    link = MockLink(stations=on, talker=talking)
    status = link.status()
    assert status.backend == "mock"
    assert status.stations == (Station("AE9S"), Station("K1ABC"))
    assert status.talker == talking
    assert isinstance(status, LinkStatus)


# --- factory ---------------------------------------------------------------------------------------


def test_create_link_builds_a_mock():
    link = create_link("mock")
    assert isinstance(link, MockLink)
    assert isinstance(link, Link)


def test_create_link_forwards_kwargs():
    link = create_link("mock", directory=False)
    assert LinkCapability.DIRECTORY not in link.capabilities()


def test_create_link_unknown_name_raises_valueerror():
    with pytest.raises(ValueError, match="unknown link backend"):
        create_link("m17")


def test_available_links_lists_mock():
    assert "mock" in available_links()


# --- acceptance: link/ imports nothing from radio_server outside ..audio --------------------------


def _absolute_import_targets(path: pathlib.Path, package: str) -> set[str]:
    """Resolve every `import`/`from` in `path` to absolute module names.

    `package` is the dotted package the file lives in (e.g. "radio_server.link"), used to resolve
    relative (`from ..x`) imports.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    targets.add(node.module)
            else:
                # Resolve `level` dots relative to `package`: level 1 = this package, 2 = parent, ...
                base = package.split(".")
                trimmed = base[: len(base) - (node.level - 1)]
                prefix = ".".join(trimmed)
                targets.add(f"{prefix}.{node.module}" if node.module else prefix)
    return targets


def test_link_package_imports_only_audio_from_radio_server():
    link_dir = pathlib.Path(__file__).resolve().parent.parent / "radio_server" / "link"
    offenders: dict[str, set[str]] = {}
    for py in sorted(link_dir.glob("*.py")):
        targets = _absolute_import_targets(py, "radio_server.link")
        bad = {
            t
            for t in targets
            if t.startswith("radio_server")
            and not t.startswith("radio_server.audio")
            and not t.startswith("radio_server.link")
        }
        if bad:
            offenders[py.name] = bad
    assert offenders == {}, f"link/ may only import radio_server.audio; found: {offenders}"
