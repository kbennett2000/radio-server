"""PyMumbleClient — the pymumble adapter, driven against a fake pymumble module (ADR 0041 Cycle C).

The `test_aioc_baofeng.py` pattern: a hand-written fake module injected via the `_pymumble=` seam,
so no network, no pymumble, no libopus. The fake mirrors the 1.6.1 surface the adapter uses
(verified against the real library during bring-up): a `Mumble` Thread-like with `callbacks`,
`set_receive_sound`, per-connection `sound_output`/`channels` that only exist after "connecting"
(`sync()` in the fake), `my_channel()`, `stop()`/`join()`, and the `constants`/`errors` namespaces.
"""

from __future__ import annotations

import sys
import types

import pytest

from radio_server.link import MumbleClient, PyMumbleClient
from radio_server.link.pymumble_client import _EXTRA_MSG  # noqa: F401  (message shape asserted below)


class FakeSoundOutput:
    def __init__(self) -> None:
        self.sounds: list[bytes] = []

    def add_sound(self, pcm: bytes) -> None:
        self.sounds.append(pcm)


class FakeChannel:
    def __init__(self, name: str, users: list | None = None) -> None:
        self.name = name
        self.users = users if users is not None else []
        self.moved_in = 0

    def move_in(self) -> None:
        self.moved_in += 1

    def get_users(self) -> list:
        return self.users


class UnknownChannelError(Exception):
    pass


class FakeChannels:
    def __init__(self, channels: dict[str, FakeChannel]) -> None:
        self._channels = channels

    def find_by_name(self, name: str) -> FakeChannel:
        try:
            return self._channels[name]
        except KeyError:
            raise UnknownChannelError(f"Channel {name} does not exists") from None


class FakeMumble:
    """The Thread-like the adapter drives. `sync()` plays the library's successful-connect role:
    it creates the per-connection objects (like `init_connection` + a server sync) and fires the
    `connected` callback — before it, `sound_output`/`channels` do not exist, exactly like 1.6.1."""

    def __init__(self, host, user, port=64738, password="", certfile=None,
                 keyfile=None, reconnect=False, tokens=None, stereo=False, debug=False):
        self.ctor = dict(host=host, user=user, port=port, password=password, reconnect=reconnect)
        self.receive_sound = False
        self.callbacks_registry: dict[str, object] = {}
        self.callbacks = types.SimpleNamespace(
            set_callback=lambda name, fn: self.callbacks_registry.__setitem__(name, fn)
        )
        self.connected = 0  # PYMUMBLE_CONN_STATE_NOT_CONNECTED
        self.started = False
        self.stopped = False
        self.joined = False
        self.bandwidth_set: int | None = None
        self._channels: dict[str, FakeChannel] = {}
        self._my_channel: FakeChannel | None = None

    # --- the adapter's surface ---
    def set_receive_sound(self, value) -> None:
        self.receive_sound = bool(value)

    def set_bandwidth(self, bandwidth: int) -> None:
        self.bandwidth_set = bandwidth

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def is_alive(self) -> bool:
        return self.started and not self.stopped

    def join(self, timeout=None) -> None:
        self.joined = True

    def my_channel(self) -> FakeChannel:
        assert self._my_channel is not None
        return self._my_channel

    # --- test helpers ---
    def sync(self, channels: dict[str, FakeChannel] | None = None, my_channel: FakeChannel | None = None) -> None:
        """Simulate the library thread reaching the server-synced state."""
        self._channels = channels or {}
        self.channels = FakeChannels(self._channels)
        self.sound_output = FakeSoundOutput()
        self._my_channel = my_channel if my_channel is not None else FakeChannel("Root", users=[object()])
        self.connected = 2  # PYMUMBLE_CONN_STATE_CONNECTED
        fired = self.callbacks_registry.get("connected")
        if fired is not None:
            fired()

    def speak(self, pcm: bytes) -> None:
        """Simulate a received voice frame via the sound_received callback."""
        handler = self.callbacks_registry["sound_received"]
        handler(object(), types.SimpleNamespace(pcm=pcm))


def make_fake_module():
    """A pymumble_py3-like module: `.Mumble`, `.constants`, `.errors`."""
    mod = types.SimpleNamespace()
    instances: list[FakeMumble] = []

    def mumble_factory(*args, **kwargs):
        m = FakeMumble(*args, **kwargs)
        instances.append(m)
        return m

    mod.Mumble = mumble_factory
    mod.constants = types.SimpleNamespace(
        PYMUMBLE_CLBK_SOUNDRECEIVED="sound_received",
        PYMUMBLE_CLBK_CONNECTED="connected",
        PYMUMBLE_CONN_STATE_NOT_CONNECTED=0,
        PYMUMBLE_CONN_STATE_AUTHENTICATING=1,
        PYMUMBLE_CONN_STATE_CONNECTED=2,
        PYMUMBLE_CONN_STATE_FAILED=3,
    )
    mod.errors = types.SimpleNamespace(UnknownChannelError=UnknownChannelError)
    mod.instances = instances
    return mod


def make_client(**kwargs):
    fake = make_fake_module()
    defaults = dict(host="murmur.example", port=64738, username="radio-server", channel="")
    defaults.update(kwargs)
    client = PyMumbleClient(**defaults, _pymumble=fake)
    return client, fake


def test_satisfies_the_protocol():
    client, _ = make_client()
    assert isinstance(client, MumbleClient)


def test_connect_wires_receive_callbacks_and_reconnect():
    client, fake = make_client(password="hunter2")
    client.connect()
    (m,) = fake.instances
    assert m.ctor == dict(
        host="murmur.example", user="radio-server", port=64738,
        password="hunter2", reconnect=True,
    )
    assert m.receive_sound is True  # incoming voice enabled...
    assert set(m.callbacks_registry) == {"sound_received", "connected"}  # ...and consumed
    assert m.started is True


def test_connect_is_idempotent():
    client, fake = make_client()
    client.connect()
    client.connect()
    assert len(fake.instances) == 1


def test_connect_is_nonblocking_before_server_sync():
    # No is_ready() — connect returns with the link not yet up; status reports the truth.
    client, fake = make_client()
    client.connect()
    assert client.status().connected is False


def test_sound_callback_forwards_pcm_to_on_audio():
    client, fake = make_client()
    got: list[bytes] = []
    client.on_audio = got.append
    client.connect()
    fake.instances[0].sync()
    fake.instances[0].speak(b"\x01\x02")
    assert got == [b"\x01\x02"]


def test_sink_fault_does_not_escape_the_library_callback():
    client, fake = make_client()
    client.on_audio = lambda pcm: (_ for _ in ()).throw(RuntimeError("sink boom"))
    client.connect()
    fake.instances[0].sync()
    fake.instances[0].speak(b"\x01\x02")  # must not raise into the (library) caller


def test_connected_callback_joins_the_configured_channel():
    client, fake = make_client(channel="Ham Net")
    client.connect()
    net = FakeChannel("Ham Net")
    fake.instances[0].sync(channels={"Ham Net": net})
    assert net.moved_in == 1


def test_connected_callback_caps_the_bandwidth():
    # Load-bearing, bench-confirmed: uncapped, pymumble adopts the server's max (Murmur default
    # 558 kbps) and its oversized voice frames are silently dropped by the server. The cap must be
    # (re)applied on every (re)connect — the library resets bandwidth per connection.
    client, fake = make_client(bandwidth=96000)
    client.connect()
    fake.instances[0].sync()
    assert fake.instances[0].bandwidth_set == 96000


def test_missing_channel_is_survived():
    client, fake = make_client(channel="No Such Room")
    client.connect()
    fake.instances[0].sync(channels={})  # UnknownChannelError inside — must not raise
    assert client.status().connected is True  # still linked, in the root


def test_empty_channel_skips_the_join():
    client, fake = make_client(channel="")
    client.connect()
    fake.instances[0].sync(channels={})
    # No find_by_name call happened (would have raised on the empty dict via our fake if queried
    # with ""), and the link is up in the server's default channel.
    assert client.status().connected is True


def test_send_audio_dropped_until_connected():
    client, fake = make_client()
    client.connect()
    client.send_audio(b"\x01\x02")  # pre-sync: sound_output does not even exist yet
    fake.instances[0].sync()
    client.send_audio(b"\x03\x04")
    assert fake.instances[0].sound_output.sounds == [b"\x03\x04"]


def test_send_audio_noop_when_never_connected():
    client, _ = make_client()
    client.send_audio(b"\x01\x02")  # no connect() at all — silently dropped


def test_status_maps_state_and_peers_excluding_self():
    client, fake = make_client(channel="Ham Net")
    client.connect()
    me, alice, bob = object(), object(), object()
    room = FakeChannel("Ham Net", users=[me, alice, bob])
    fake.instances[0].sync(channels={"Ham Net": room}, my_channel=room)
    status = client.status()
    assert status.connected is True
    assert status.host == "murmur.example"
    assert status.channel == "Ham Net"
    assert status.peers == 2  # three in the room minus this client


def test_disconnect_stops_and_joins_and_is_idempotent():
    client, fake = make_client()
    client.connect()
    fake.instances[0].sync()
    client.disconnect()
    m = fake.instances[0]
    assert m.stopped is True
    assert client.status().connected is False
    client.disconnect()  # idempotent
    assert len(fake.instances) == 1


def test_reconnect_after_disconnect_builds_a_fresh_connection():
    client, fake = make_client()
    client.connect()
    client.disconnect()
    client.connect()
    assert len(fake.instances) == 2


def test_missing_pymumble_gives_actionable_error(monkeypatch):
    client = PyMumbleClient(host="murmur.example")  # no injected module → real lazy import
    monkeypatch.setitem(sys.modules, "pymumble_py3", None)  # make `import pymumble_py3` raise
    with pytest.raises(RuntimeError, match="mumble.*extra"):
        client.connect()


# --- live-Murmur integration (bring-up; skipped unless a server is provided) -----------------

import math
import os
import struct
import time

MURMUR = os.environ.get("RADIO_TEST_MURMUR", "")


@pytest.mark.skipif(not MURMUR, reason="set RADIO_TEST_MURMUR=host:port to run against a live Murmur")
def test_two_clients_pass_audio_through_a_live_murmur():
    """The full loop with no GUI client: B speaks into the channel, A hears PCM (ADR 0041 Cycle C).

    Proves connect/TLS/auth, Opus encode (B) and decode (A), and the sound callback path against a
    real server — the "plug it in" bar. Needs the mumble extra + libopus + a reachable Murmur.
    """
    host, _, port = MURMUR.partition(":")
    port = int(port or 64738)

    def wait(predicate, timeout=15.0, step=0.2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(step)
        return False

    a = PyMumbleClient(host=host, port=port, username="radio-server-rx")
    b = PyMumbleClient(host=host, port=port, username="radio-server-tx")
    heard: list[bytes] = []
    a.on_audio = heard.append
    try:
        a.connect()
        b.connect()
        assert wait(lambda: a.status().connected), "client A never connected"
        assert wait(lambda: b.status().connected), "client B never connected"
        # A sees B in the room (peer count excludes self).
        assert wait(lambda: (a.status().peers or 0) >= 1), "A never saw B in the channel"

        # B speaks one second of 440 Hz tone (canonical PCM), fed in 20 ms frames like the bridge.
        frame_samples = 960
        tone = b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * n / 48000)))
            for n in range(48000)
        )
        for i in range(0, len(tone), frame_samples * 2):
            b.send_audio(tone[i : i + frame_samples * 2])
            time.sleep(0.02)  # roughly real-time pacing

        assert wait(lambda: len(heard) > 0), "A never received audio from B"
        pcm = b"".join(heard)
        assert len(pcm) % 2 == 0 and len(pcm) > 0  # whole 16-bit samples of decoded audio
    finally:
        a.disconnect()
        b.disconnect()
