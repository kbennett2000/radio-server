"""One defect class, not three findings: an acquire whose release is guarded by a region entered
later (ADR 0167).

Every talk endpoint claims its slot and *then* suspends, with the ``try``/``finally`` that frees it
still one statement away::

    acquired = tx_slot.try_acquire()
    await websocket.accept()          # <- anything raising here strands the slot forever
    ...
    try:
        ...
    finally:
        tx_slot.release()

`TxSlot` is one bare bool — no owner, no timestamp, no timeout, no watchdog, no lifespan reset — so
a strand is permanent for the life of the process and the only remedy is a restart. The same shape
appears twice more: `/audio/rx` claims a hub subscription before an ``await`` that can fail, and
`MumbleBridge._end_session` runs an unguarded ``close()`` (whose ``ptt(False)`` ADR 0166 made a
demonstrated raiser) immediately *before* the release.

Driven as bare coroutines rather than through `TestClient`, for the reason
`test_api.py::test_ws_events_swallows_shutdown_cancellation_and_cleans_up` already records: the sync
client sends a *disconnect* on block-exit, and can never make `accept()` itself fail.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from radio_server.api import create_app
from radio_server.arbiter import RadioArbiter
from radio_server.audio import AudioFrame
from radio_server.backends import MockRadio, RadioUnavailable
from radio_server.dstar import MockGatewayClient
from radio_server.link import MockMumbleClient, MumbleBridge, resolve_mumble_entries
from radio_server.rx import AudioHub
from radio_server.tx import TxSession, TxSlot
from radio_server.vocoder.base import AMBE_BYTES_PER_FRAME, PCM_FORMAT

TOKEN = "test-lan-secret"
CANONICAL_HEADER = {"rate": 48000, "width": 2, "channels": 1}

# The two things that can actually arrive in that window. `RuntimeError` stands for a transport
# fault out of `accept()`; `CancelledError` is what a real shutdown delivers, and it is a
# BaseException — the reason ADR 0151's unwind catches `BaseException` and not `Exception`.
FAILURES = [RuntimeError, asyncio.CancelledError]


class _RaisingAcceptWS:
    """A server-side WebSocket double whose `accept()` fails: the client that went away between the
    HTTP upgrade and the app's accept."""

    def __init__(self, token: str, failure: type[BaseException]) -> None:
        self.query_params = {"token": token}
        self.sent: list = []
        self.close_code = None
        self._failure = failure

    async def accept(self) -> None:
        raise self._failure("the client vanished mid-handshake")

    async def send_json(self, data) -> None:  # pragma: no cover — never reached
        self.sent.append(data)

    async def close(self, code=None) -> None:  # pragma: no cover — never reached
        self.close_code = code


class _AcceptingWS:
    """The same double, but the handshake succeeds — for the paths that fail *after* the accept."""

    def __init__(self, token: str) -> None:
        self.query_params = {"token": token}
        self.sent: list = []
        self.close_code = None

    async def accept(self) -> None:
        pass

    async def send_json(self, data) -> None:
        self.sent.append(data)

    async def close(self, code=None) -> None:
        self.close_code = code


def _endpoint(app, path: str):
    return next(r.endpoint for r in app.routes if getattr(r, "path", "") == path)


def _drive_failed_accept(app, path: str, failure: type[BaseException]) -> None:
    """Run the handler to the point where its accept blows up, off the TestClient path."""

    async def _run():
        with pytest.raises(failure):
            await _endpoint(app, path)(_RaisingAcceptWS(TOKEN, failure))

    asyncio.run(_run())


# --- /audio/tx — the RF transmitter slot ------------------------------------------------------


@pytest.mark.parametrize("failure", FAILURES)
def test_audio_tx_slot_is_free_after_a_failed_accept(failure):
    app = create_app(MockRadio(), api_token=TOKEN)
    _drive_failed_accept(app, "/audio/tx", failure)
    assert app.state.tx_slot.occupied is False


def test_audio_tx_still_transmits_after_a_failed_accept():
    # The slot, not merely the socket: the third act keys the radio, so a `tx_log` frame is the
    # proof. A connect that only *succeeds* would also pass against a handler that accepted and
    # then refused to key.
    radio = MockRadio()
    app = create_app(radio, api_token=TOKEN)
    _drive_failed_accept(app, "/audio/tx", RuntimeError)
    with TestClient(app) as client:
        with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws:
            ws.send_json(CANONICAL_HEADER)
            assert ws.receive_json()["status"] == "ready"
            ws.send_bytes(b"\x01\x02")
    assert [f.samples for f in radio.tx_log] == [b"\x01\x02"]


# --- /audio/mumble/tx — the Mumble talk slot --------------------------------------------------

ENTRIES = resolve_mumble_entries([{"name": "home", "host": "mumble.example"}])


def _mumble_app():
    return create_app(
        MockRadio(),
        api_token=TOKEN,
        mumble_entries=ENTRIES,
        mumble_client_factory=lambda entry: MockMumbleClient(host=entry.host),
    )


@pytest.mark.parametrize("failure", FAILURES)
def test_audio_mumble_tx_slot_is_free_after_a_failed_accept(failure):
    app = _mumble_app()
    _drive_failed_accept(app, "/audio/mumble/tx", failure)
    assert app.state.mumble_talk_slot.occupied is False


def test_audio_mumble_tx_admits_the_next_talker_after_a_failed_accept():
    # `ready` rather than `busy` is the whole claim: this endpoint keys no radio, so the handshake
    # verdict is the only thing that distinguishes a freed slot from a stranded one.
    app = _mumble_app()
    _drive_failed_accept(app, "/audio/mumble/tx", RuntimeError)
    with TestClient(app) as client:
        with client.websocket_connect(f"/audio/mumble/tx?token={TOKEN}") as ws:
            ws.send_json(CANONICAL_HEADER)
            assert ws.receive_json()["status"] == "ready"


# --- /audio/dstar/tx — the D-STAR talk slot ---------------------------------------------------


class _FakeVocoder:
    def encode(self, frame: AudioFrame) -> bytes:
        return bytes(AMBE_BYTES_PER_FRAME)

    def decode(self, ambe: bytes) -> AudioFrame:
        return AudioFrame(b"\x00\x00" * 160, PCM_FORMAT)

    def close(self) -> None:
        pass


def _dstar_app():
    return create_app(
        MockRadio(),
        api_token=TOKEN,
        dstar_gateway_factory=MockGatewayClient,
        dstar_vocoder_factory=_FakeVocoder,
        dstar_callsign="AE9S",
        dstar_module="A",
    )


@pytest.mark.parametrize("failure", FAILURES)
def test_audio_dstar_tx_slot_is_free_after_a_failed_accept(failure):
    app = _dstar_app()
    _drive_failed_accept(app, "/audio/dstar/tx", failure)
    assert app.state.dstar_talk_slot.occupied is False


def test_audio_dstar_tx_admits_the_next_talker_after_a_failed_accept():
    app = _dstar_app()
    _drive_failed_accept(app, "/audio/dstar/tx", RuntimeError)
    with TestClient(app) as client:
        with client.websocket_connect(f"/audio/dstar/tx?token={TOKEN}") as ws:
            ws.send_json(CANONICAL_HEADER)
            assert ws.receive_json()["status"] == "ready"


# --- /audio/rx — the same class, different resources ------------------------------------------


def test_audio_rx_does_not_pin_the_pump_when_the_start_fails():
    # `queue = audio_hub.subscribe()` then `await _acquire_rx()` then `try:`. A raising start —
    # capture device busy or gone — leaks the subscriber AND pins `rx_demand` at 1, after which the
    # single reader can never stop for the life of the process.
    app = create_app(MockRadio(), api_token=TOKEN)

    def boom() -> None:
        raise RuntimeError("capture device is busy")

    app.state.rx_pump.start = boom

    async def _run():
        with pytest.raises(RuntimeError):
            await _endpoint(app, "/audio/rx")(_AcceptingWS(TOKEN))

    asyncio.run(_run())
    assert app.state.rx_demand == 0
    assert app.state.audio_hub.subscriber_count == 0


# --- the second family: a teardown that raises must not skip the release ----------------------
#
# This half is not hypothetical. The accept window needs an exception that this uvicorn does not
# actually produce for a dropped upgrade; these paths only need `ptt(False)` to throw, which ADR
# 0166's dead serial reader makes an ordinary event on a live process.


class _ScriptedWS(_AcceptingWS):
    """Handshake, deliver `frames`, then disconnect — enough to reach a handler's `finally`."""

    def __init__(self, token: str, frames: list[bytes]) -> None:
        super().__init__(token)
        self._frames = list(frames)

    async def receive_json(self):
        return CANONICAL_HEADER

    async def receive_bytes(self) -> bytes:
        if self._frames:
            return self._frames.pop(0)
        raise WebSocketDisconnect(1000)

    async def send_bytes(self, data) -> None:  # pragma: no cover — TX path never sends binary
        self.sent.append(data)


def test_audio_tx_slot_is_free_even_when_the_unkey_raises_on_the_way_out():
    # `session.close()` ran immediately before `tx_slot.release()`. Its `ptt(False)` is unguarded
    # (`tx/session.py` guards the *recorder* for exactly this reason and left the unkey bare), so a
    # radio that cannot un-key wedged the shared transmitter slot for the life of the process.
    app = create_app(_UnkeyFailsRadio(), api_token=TOKEN)

    async def _run():
        with pytest.raises(RadioUnavailable):
            await _endpoint(app, "/audio/tx")(_ScriptedWS(TOKEN, [b"\x01\x00" * 960]))

    asyncio.run(_run())
    assert app.state.tx_slot.occupied is False


def test_audio_dstar_tx_slot_is_free_even_when_the_over_terminator_raises():
    # `end_operator_over()` is a UDP write to a socket that may already be gone, and it ran
    # immediately before the release.
    class _BoomBridge:
        async def send_operator_audio(self, data) -> None:
            pass

        def end_operator_over(self) -> None:
            raise RuntimeError("the gateway socket is gone")

    app = _dstar_app()
    app.state.dstar_bridge = _BoomBridge()

    async def _run():
        with pytest.raises(RuntimeError):
            await _endpoint(app, "/audio/dstar/tx")(_ScriptedWS(TOKEN, [b"\x01\x00" * 960]))

    asyncio.run(_run())
    assert app.state.dstar_talk_slot.occupied is False


def test_audio_tx_slot_is_free_after_a_shutdown_cancellation_mid_talk():
    # The real shutdown path for a talker who is holding the button: uvicorn cancels the parked
    # task. The handler swallows it (ADR 0122) and must still give the slot back.
    radio = MockRadio()
    app = create_app(radio, api_token=TOKEN)

    class _ParkingWS(_ScriptedWS):
        async def receive_bytes(self) -> bytes:
            if self._frames:
                return self._frames.pop(0)
            await asyncio.Event().wait()  # park, exactly where a live talker waits
            raise AssertionError("unreachable")

    async def _run():
        task = asyncio.ensure_future(
            _endpoint(app, "/audio/tx")(_ParkingWS(TOKEN, [b"\x01\x00" * 960]))
        )
        for _ in range(1000):
            await asyncio.sleep(0)
            if app.state.tx_slot.occupied and radio.tx_log:
                break
        assert app.state.tx_slot.occupied is True  # keyed and holding
        task.cancel()
        await task
        assert not task.cancelled()  # swallowed, per ADR 0122

    asyncio.run(_run())
    assert app.state.tx_slot.occupied is False


# --- MumbleBridge._end_session — the release must survive the teardown ------------------------


class _UnkeyFailsRadio(MockRadio):
    """A radio whose *unkey* raises. Not hypothetical: ADR 0166's dead serial reader is exactly a
    backend whose `ptt(False)` throws, on a live process."""

    def ptt(self, on: bool) -> None:
        if not on:
            raise RadioUnavailable("the serial reader stopped answering (ADR 0166)")
        super().ptt(on)


def test_mumble_end_session_frees_the_shared_slot_when_the_unkey_raises():
    # `session.close()` runs unguarded immediately before `self._tx_slot.release()`, so a raising
    # unkey strands the slot the browser, the D-STAR bridge and this relay all share — from any of
    # `_end_session`'s six call sites. `DStarBridge` already wraps its close in
    # `contextlib.suppress`; the Mumble side diverging is the defect.
    radio = _UnkeyFailsRadio()
    arbiter = RadioArbiter()
    slot = TxSlot()
    bridge = MumbleBridge(
        MockMumbleClient(host="mumble.example"),
        radio,
        arbiter=arbiter,
        tx_slot=slot,
        audio_hub=AudioHub(),
        acquire_rx=_noop,
        release_rx=_noop,
        tx_hang=0.05,
    )
    assert slot.try_acquire()
    session = TxSession(radio, idle_timeout=0.05, arbiter=arbiter)
    session.feed(b"\x01\x00" * 960)  # keys the radio

    bridge._end_session(session)  # must not take the relay loop down with it
    assert slot.occupied is False


async def _noop() -> None:
    pass
