"""M17 control packets and stream frame (ADR 0050): build/parse round-trips, talker, fail-loud.

Byte-exact assertions (the wire format is exact). The malformed-input rule is exercised in both
directions: parse returns ``None`` on hostile/truncated input; build raises by name on local
misuse. A purity guard AST-checks that the subpackage is stdlib-only with no socket import.
"""

import ast
from pathlib import Path

import pytest

import radio_server.link.m17 as m17pkg
from radio_server.link.m17 import callsign as cs
from radio_server.link.m17.packet import (
    ACKN,
    CONN,
    DISC,
    LSTN,
    NACK,
    PING,
    PONG,
    STREAM_MAGIC,
    ControlPacket,
    StreamFrame,
    build_ackn,
    build_conn,
    build_disc,
    build_lstn,
    build_nack,
    build_ping,
    build_pong,
    build_stream,
    parse_control,
    parse_stream,
)


# --- control packets: magic + exact length -----------------------------------


def test_control_builders_emit_right_magic_and_length():
    assert build_conn("W1AW", "A")[:4] == CONN and len(build_conn("W1AW", "A")) == 11
    assert build_lstn("W1AW", "B")[:4] == LSTN and len(build_lstn("W1AW", "B")) == 11
    assert build_disc("W1AW")[:4] == DISC and len(build_disc("W1AW")) == 10
    assert build_ping("W1AW")[:4] == PING and len(build_ping("W1AW")) == 10
    assert build_pong("W1AW")[:4] == PONG and len(build_pong("W1AW")) == 10
    assert build_ackn() == ACKN and len(build_ackn()) == 4
    assert build_nack() == NACK and len(build_nack()) == 4


# --- control packets: build -> parse round-trip ------------------------------


def test_conn_round_trips_with_module():
    parsed = parse_control(build_conn("KE0ABC", "C"))
    assert parsed == ControlPacket("CONN", callsign="KE0ABC", module="C")


def test_lstn_round_trips_the_listen_only_tier():
    # LSTN is the zero-credential listening request; it must be first-class, not left for later.
    parsed = parse_control(build_lstn("N0CALL", "D"))
    assert parsed == ControlPacket("LSTN", callsign="N0CALL", module="D")


@pytest.mark.parametrize("build,kind", [(build_ping, "PING"), (build_pong, "PONG")])
def test_ping_pong_round_trip(build, kind):
    assert parse_control(build("W1AW")) == ControlPacket(kind, callsign="W1AW")


def test_disc_request_and_bare_ack_both_parse():
    assert parse_control(build_disc("W1AW")) == ControlPacket("DISC", callsign="W1AW")
    assert parse_control(bytes(DISC)) == ControlPacket("DISC")  # 4-byte reflector ack


@pytest.mark.parametrize(
    "build,kind", [(build_ackn, "ACKN"), (build_nack, "NACK")]
)
def test_bare_ackn_nack_round_trip(build, kind):
    assert parse_control(build()) == ControlPacket(kind)


# --- control parse: malformed -> None ----------------------------------------


def test_parse_control_rejects_unknown_magic():
    assert parse_control(b"XXXX") is None
    assert parse_control(b"M17 " + b"\x00" * 6) is None  # stream magic is not a control magic


def test_parse_control_rejects_wrong_length():
    assert parse_control(build_conn("W1AW", "A")[:-1]) is None  # truncated CONN
    assert parse_control(build_conn("W1AW", "A") + b"\x00") is None  # overlong CONN
    assert parse_control(build_ping("W1AW") + b"\x00") is None  # overlong PING
    assert parse_control(bytes(ACKN) + b"\x00") is None  # overlong ACKN


def test_parse_control_rejects_short_and_empty():
    assert parse_control(b"") is None
    assert parse_control(b"CO") is None


# --- control build: fail loud ------------------------------------------------


def test_build_conn_rejects_bad_module():
    with pytest.raises(ValueError):
        build_conn("W1AW", "AA")  # not a single letter
    with pytest.raises(ValueError):
        build_conn("W1AW", "1")  # not A-Z


def test_build_conn_rejects_bad_callsign():
    with pytest.raises(cs.CallsignError):
        build_conn("w1aw", "A")  # lowercase, fail loud


# --- stream frame ------------------------------------------------------------

_META = bytes(range(14))
_PAYLOAD = bytes(range(16))


def test_build_stream_shape_magic_and_crc():
    frame = build_stream(0x1234, "M17-M17", "W1AW", 0x0005, _META, 7, _PAYLOAD)
    assert len(frame) == 54
    assert frame[:4] == STREAM_MAGIC
    assert parse_stream(frame) is not None  # CRC self-consistent


def test_stream_round_trip_yields_source_callsign_the_talker():
    frame = build_stream(0xABCD, "M17-M17", "KE0ABC", 0x0005, _META, 42, _PAYLOAD, last=True)
    parsed = parse_stream(frame)
    assert parsed == StreamFrame(
        stream_id=0xABCD,
        dst="M17-M17",
        src="KE0ABC",  # <-- the talker, read out of the LSF
        frame_type=0x0005,
        meta=_META,
        frame_number=42,
        payload=_PAYLOAD,
        last=True,
    )


def test_stream_last_flag_and_index_split():
    not_last = parse_stream(build_stream(1, "M17", "W1AW", 0, _META, 100, _PAYLOAD, last=False))
    assert not_last.frame_number == 100 and not_last.last is False
    last = parse_stream(build_stream(1, "M17", "W1AW", 0, _META, 100, _PAYLOAD, last=True))
    assert last.frame_number == 100 and last.last is True


def test_stream_accepts_broadcast_destination():
    frame = build_stream(1, cs.BROADCAST, "W1AW", 0, _META, 0, _PAYLOAD)
    parsed = parse_stream(frame)
    assert parsed.dst is None  # BROADCAST is not a decodable callsign
    assert parsed.src == "W1AW"
    assert frame[6:12] == cs.BROADCAST


def test_parse_stream_rejects_bad_crc():
    frame = bytearray(build_stream(1, "M17", "W1AW", 0, _META, 0, _PAYLOAD))
    frame[36] ^= 0xFF  # flip a payload byte -> CRC no longer matches
    assert parse_stream(bytes(frame)) is None


def test_parse_stream_rejects_wrong_length_and_magic():
    good = build_stream(1, "M17", "W1AW", 0, _META, 0, _PAYLOAD)
    assert parse_stream(good[:-1]) is None  # 53 bytes
    assert parse_stream(good + b"\x00") is None  # 55 bytes
    bad_magic = b"XXXX" + good[4:]
    assert parse_stream(bad_magic) is None


def test_build_stream_rejects_bad_field_sizes():
    with pytest.raises(ValueError):
        build_stream(1, "M17", "W1AW", 0, _META[:-1], 0, _PAYLOAD)  # meta too short
    with pytest.raises(ValueError):
        build_stream(1, "M17", "W1AW", 0, _META, 0, _PAYLOAD + b"\x00")  # payload too long
    with pytest.raises(ValueError):
        build_stream(1, "M17", "W1AW", 0, _META, 0x8000, _PAYLOAD)  # index doesn't fit 15 bits
    with pytest.raises(ValueError):
        build_stream(0x1FFFF, "M17", "W1AW", 0, _META, 0, _PAYLOAD)  # stream_id > 16 bits


# --- purity guard: stdlib-only, no socket (ADR 0050) -------------------------


def test_m17_subpackage_is_stdlib_only_and_imports_no_socket():
    """AST-check every m17 module: no ``socket`` import, no absolute ``radio_server`` import."""
    m17_dir = Path(m17pkg.__file__).parent
    modules = sorted(m17_dir.glob("*.py"))
    assert modules, "expected m17 modules to scan"

    for path in modules:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root != "socket", f"{path.name} imports socket"
                    assert root != "radio_server", f"{path.name} imports radio_server"
            elif isinstance(node, ast.ImportFrom):
                # level > 0 is an intra-m17 relative import (allowed); only check absolute ones.
                if node.level == 0 and node.module:
                    root = node.module.split(".")[0]
                    assert root != "socket", f"{path.name} imports from socket"
                    assert root != "radio_server", f"{path.name} imports from radio_server"
