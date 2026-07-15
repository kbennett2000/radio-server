"""``doctor --link-listen`` / ``--link-decode`` — the M17 reflector bring-up instrument (ADR 0053).

The tool is a *raw* read-only observer, not a wrapper around ``M17Client`` (which swallows control
packets and discards raw stream bytes — the two things a bench listen must see). So the tests split
in two: the pure :class:`~radio_server.doctor.LinkObserver` accounting is driven directly with a
synthetic clock (drop count, talker, **raw LSF hex**, measured inter-frame interval, PING cadence,
handshake callback), and the socket driver :func:`~radio_server.doctor._observe_link` is exercised
against the localhost :class:`FakeReflector` reused from ``test_m17_client`` (handshake, a scripted
stream, a PING answered with PONG, a wrong-source drop, and the NACK/timeout fail-loud paths). The
config fail-loud paths and CLI dispatch are unit-tested; the real-Codec2 WAV write is skip-gated on
``libcodec2``. Nothing here touches a real reflector or the network beyond loopback.
"""

from __future__ import annotations

import asyncio
import socket
import wave
from ctypes.util import find_library

import pytest

from radio_server.doctor import (
    LinkObserver,
    _link_decode,
    _link_listen,
    _LinkConfigError,
    _LinkHandshakeError,
    _observe_link,
    _resolve_link_cfg,
    main,
)
from radio_server.link.m17.packet import (
    build_ackn,
    build_nack,
    build_ping,
    build_pong,
    build_stream,
    parse_control,
)

from .test_m17_client import FakeReflector, _ackn_on_conn, _nack_on_conn

_META = bytes(14)
_PAYLOAD = bytes(16)
REFLECTOR_ADDR = ("127.0.0.1", 17000)

_CODEC2_SKIP = pytest.mark.skipif(
    find_library("codec2") is None,
    reason="libcodec2 not installed; real Codec2 decode is a build check",
)


def _stream(stream_id, src, fn, last, *, dst="A", frame_type=0x0005, payload=_PAYLOAD):
    """A 54-byte M17 stream frame for a scripted talker (dst defaults to the module letter)."""
    return build_stream(stream_id, dst, src, frame_type, _META, fn, payload, last)


# --- pure LinkObserver: the accounting math, with a synthetic clock -------------------------------


def test_observer_drops_wrong_source_before_parsing():
    obs = LinkObserver(REFLECTOR_ADDR, "KE0ABC")
    reply = obs.ingest(_stream(1, "W1AW", 0, True), ("10.0.0.9", 40000), now=1.0)
    assert reply is None
    assert obs.dropped_source == 1
    assert obs.streams() == []  # never reached the parser


def test_observer_records_talker_raw_lsf_and_measured_interval():
    obs = LinkObserver(REFLECTOR_ADDR, "KE0ABC")
    f0 = _stream(7, "W1AW", 0, False)
    obs.ingest(f0, REFLECTOR_ADDR, now=0.0)
    obs.ingest(_stream(7, "W1AW", 1, False), REFLECTOR_ADDR, now=0.04)  # +40 ms
    obs.ingest(_stream(7, "W1AW", 2, True), REFLECTOR_ADDR, now=0.08)  # +40 ms
    (st,) = obs.streams()
    assert st.talker == "W1AW"
    assert st.frames == 3
    assert st.ended is True
    # RAW LSF bytes of the first frame — the actual wire bytes, never a re-encode (ADR 0053).
    assert st.lsf_hex == f0[6:34].hex()
    assert st.intervals_ms == pytest.approx([40.0, 40.0])


def test_observer_ping_replies_pong_and_measures_cadence():
    obs = LinkObserver(REFLECTOR_ADDR, "KE0ABC")
    reply = obs.ingest(build_ping("W1AW"), REFLECTOR_ADDR, now=10.0)
    assert reply == build_pong("KE0ABC")  # keepalive back to the reflector
    obs.ingest(build_ping("W1AW"), REFLECTOR_ADDR, now=13.0)
    obs.ingest(build_ping("W1AW"), REFLECTOR_ADDR, now=16.0)
    assert obs.ping_count() == 3
    assert obs.ping_intervals_ms() == pytest.approx([3000.0, 3000.0])


def test_observer_handshake_callback_fires_on_ackn_and_nack():
    ackn_seen: list[bool] = []
    LinkObserver(REFLECTOR_ADDR, "KE0ABC", on_handshake=ackn_seen.append).ingest(
        build_ackn(), REFLECTOR_ADDR, now=0.0
    )
    assert ackn_seen == [True]

    nack_seen: list[bool] = []
    LinkObserver(REFLECTOR_ADDR, "KE0ABC", on_handshake=nack_seen.append).ingest(
        build_nack(), REFLECTOR_ADDR, now=0.0
    )
    assert nack_seen == [False]


def test_observer_single_frame_stream_has_no_interval():
    obs = LinkObserver(REFLECTOR_ADDR, "KE0ABC")
    obs.ingest(_stream(2, "W1AW", 0, True), REFLECTOR_ADDR, now=0.0)
    (st,) = obs.streams()
    assert st.frames == 1 and st.ended and st.intervals_ms == []


# --- socket driver: _observe_link against a localhost FakeReflector -------------------------------


def _cfg(fake, **over):
    c = dict(
        reflector_host="127.0.0.1",
        reflector_port=fake.addr[1],
        reflector_module="A",
        bind_host="127.0.0.1",
        bind_port=0,
        callsign="KE0ABC",
    )
    c.update(over)
    return c


def _script_on_lstn(datagrams):
    """A FakeReflector handler that ACKs the LSTN, then bursts the scripted datagrams back."""

    def handler(fake, data, addr):
        ctrl = parse_control(data)
        if ctrl is not None and ctrl.kind == "LSTN":
            fake.send(build_ackn())
            for dg in datagrams:
                fake.send(dg)

    return handler


def test_observe_link_reports_handshake_stream_and_cadence():
    async def scenario():
        script = [
            _stream(9, "W1AW", 0, False),
            _stream(9, "W1AW", 1, True),
            build_ping("W1AW"),
            build_ping("W1AW"),
        ]
        fake = await FakeReflector(_script_on_lstn(script)).start()
        try:
            report = await _observe_link(_cfg(fake), 0.15)
        finally:
            fake.close()
        return report, fake

    report, fake = asyncio.run(scenario())
    assert report.handshake_ms >= 0.0
    (st,) = report.streams
    assert st.talker == "W1AW" and st.frames == 2 and st.ended
    assert st.lsf_hex  # raw LSF captured
    assert report.ping_count == 2
    # The observer answered each PING with a PONG (keepalive), which the fake received.
    assert any((c := parse_control(r)) is not None and c.kind == "PONG" for r in fake.received)


def test_observe_link_counts_a_wrong_source_datagram_as_a_drop():
    async def scenario():
        fake = await FakeReflector(_ackn_on_conn).start()  # ACKs the LSTN, then stays quiet
        task = asyncio.ensure_future(_observe_link(_cfg(fake), 0.2))
        loop = asyncio.get_running_loop()
        for _ in range(100):  # wait until the observer has bound + sent LSTN (fake learns its addr)
            if fake.client_addr is not None:
                break
            await asyncio.sleep(0.005)
        spoof, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0)
        )
        spoof.sendto(_stream(3, "W1AW", 0, True), fake.client_addr)  # from a DIFFERENT source port
        try:
            return await task
        finally:
            spoof.close()
            fake.close()

    report = asyncio.run(scenario())
    assert report.dropped_source >= 1
    assert report.streams == []  # the spoof never became a stream


def test_observe_link_raises_link_handshake_error_on_nack():
    async def scenario():
        fake = await FakeReflector(_nack_on_conn).start()
        try:
            with pytest.raises(_LinkHandshakeError, match="NACK"):
                await _observe_link(_cfg(fake), 0.1)
        finally:
            fake.close()

    asyncio.run(scenario())


def test_observe_link_raises_link_handshake_error_on_timeout(monkeypatch):
    monkeypatch.setattr("radio_server.doctor._LINK_CONNECT_TIMEOUT", 0.1)

    async def scenario():
        fake = await FakeReflector().start()  # silent — never ACKs
        try:
            with pytest.raises(_LinkHandshakeError, match="timed out"):
                await _observe_link(_cfg(fake), 0.1)
        finally:
            fake.close()

    asyncio.run(scenario())


# --- config fail-loud, by name --------------------------------------------------------------------


class _FakeSettings:
    """A minimal Settings stand-in: ``get`` returns the mapped value, or raises a mapped exception
    (to model a required-but-unset key like ``station.callsign``)."""

    def __init__(self, values):
        self._v = values

    def get(self, key):
        v = self._v[key]
        if isinstance(v, Exception):
            raise v
        return v


def _settings(**over):
    base = {
        "link.reflector_host": "ref.example.org",
        "link.reflector_port": 17000,
        "link.reflector_module": "A",
        "link.bind_host": "0.0.0.0",
        "link.bind_port": 0,
        "station.callsign": "KE0ABC",
    }
    base.update(over)
    return _FakeSettings(base)


def test_resolve_link_cfg_fails_loud_on_missing_reflector_host():
    with pytest.raises(_LinkConfigError, match="reflector"):
        _resolve_link_cfg(_settings(**{"link.reflector_host": ""}))


def test_resolve_link_cfg_fails_loud_on_empty_callsign():
    with pytest.raises(_LinkConfigError, match="callsign"):
        _resolve_link_cfg(_settings(**{"station.callsign": ""}))


def test_resolve_link_cfg_fails_loud_when_callsign_is_unset():
    # station.callsign is a required setting; an unset value raises inside Settings.get.
    with pytest.raises(_LinkConfigError, match="callsign"):
        _resolve_link_cfg(_settings(**{"station.callsign": RuntimeError("unset")}))


def test_resolve_link_cfg_returns_full_cfg_when_present():
    cfg = _resolve_link_cfg(_settings())
    assert cfg["reflector_host"] == "ref.example.org"
    assert cfg["reflector_port"] == 17000
    assert cfg["callsign"] == "KE0ABC"  # reused as the M17 source — no second callsign


def test_link_listen_fails_loud_and_exits_1_without_config(monkeypatch, capsys):
    import radio_server.config as cfgmod

    monkeypatch.setattr(
        cfgmod, "load_settings", lambda *a, **k: _settings(**{"link.reflector_host": ""})
    )
    assert _link_listen(1.0) == 1
    err = capsys.readouterr().err
    assert "[FAIL]" in err and "reflector" in err


def test_link_listen_fails_loud_on_unresolvable_host(monkeypatch, capsys):
    import radio_server.config as cfgmod

    monkeypatch.setattr(
        cfgmod,
        "load_settings",
        lambda *a, **k: _settings(**{"link.reflector_host": "nonexistent.invalid"}),
    )

    async def _boom(*a, **k):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr("radio_server.doctor._observe_link", _boom)
    assert _link_listen(1.0) == 1
    err = capsys.readouterr().err
    assert "[FAIL]" in err and "nonexistent.invalid" in err


def test_link_decode_fails_loud_without_libcodec2(monkeypatch, capsys, tmp_path):
    import radio_server.audio.codec2 as c2

    monkeypatch.setattr(c2, "find_library", lambda name: None)  # simulate a host without libcodec2
    assert _link_decode(1.0, str(tmp_path / "x.wav")) == 1
    err = capsys.readouterr().err
    assert "[FAIL]" in err and "libcodec2" in err and "codec2" in err


# --- CLI dispatch ---------------------------------------------------------------------------------


def test_main_dispatches_link_listen_with_seconds(monkeypatch):
    seen = {}

    def fake(seconds):
        seen["seconds"] = seconds
        return 0

    monkeypatch.setattr("radio_server.doctor._link_listen", fake)
    assert main(["--link-listen", "--seconds", "3"]) == 0
    assert seen["seconds"] == 3.0


def test_main_dispatches_link_decode_with_out_and_default_seconds(monkeypatch):
    seen = {}

    def fake(seconds, out):
        seen.update(seconds=seconds, out=out)
        return 0

    monkeypatch.setattr("radio_server.doctor._link_decode", fake)
    assert main(["--link-decode", "--out", "/tmp/foo.wav"]) == 0
    assert seen == {"seconds": 60.0, "out": "/tmp/foo.wav"}


def test_main_link_modes_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        main(["--link-listen", "--link-decode"])


# --- decode → WAV (real Codec2, skip-gated) -------------------------------------------------------


@_CODEC2_SKIP
def test_observe_link_decode_writes_a_canonical_wav(tmp_path):
    from radio_server.audio import CANONICAL_FORMAT
    from radio_server.audio.codec2 import Codec2

    out = tmp_path / "decoded.wav"

    async def scenario():
        script = [_stream(4, "W1AW", 0, False), _stream(4, "W1AW", 1, True)]
        fake = await FakeReflector(_script_on_lstn(script)).start()
        codec = Codec2()
        w = wave.open(str(out), "wb")
        w.setnchannels(CANONICAL_FORMAT.channels)
        w.setsampwidth(CANONICAL_FORMAT.width)
        w.setframerate(CANONICAL_FORMAT.rate)
        try:
            report = await _observe_link(_cfg(fake), 0.15, codec=codec, wav=w)
        finally:
            w.close()
            codec.close()
            fake.close()
        return report

    report = asyncio.run(scenario())
    (st,) = report.streams
    assert st.frames == 2
    with wave.open(str(out), "rb") as r:
        assert r.getframerate() == 48000
        assert r.getnchannels() == 1
        assert r.getsampwidth() == 2
        assert r.getnframes() > 0  # a stranger's decoded audio landed on disk
