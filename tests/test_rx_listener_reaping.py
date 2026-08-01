"""A dead listener gets reaped on a quiet channel (ADR 0171).

ADR 0170 measured the leak and carried the fix: a dropped ``/audio/rx`` listener was **still counted
at +50 s**, past uvicorn's 20 s keepalive and through a signal that woke every other reader. The
handler parks on an unbounded ``queue.get()`` and only ever *sends*, and uvicorn drops sends silently
on a reset transport rather than raising — so **send-based liveness detection is ruled out by
measurement**, not by taste.

The fix is not a timeout. It is that **the disconnect is already being delivered and nobody is
listening**: ASGI posts ``{"type": "websocket.disconnect"}`` on the socket's *receive* channel, and
these handlers never call ``receive()``. Measured against real uvicorn on the production
``websockets`` path (`probe_asgi_disconnect.py`, ADR 0171):

===========================  ==========================================
peer goes away by...         disconnect posted after
===========================  ==========================================
clean close                  immediate (within measurement resolution)
RST                          **19.7 s** — the ping *write* fails
silence, socket still open   **40.0 s** — ``interval + timeout``
===========================  ==========================================

and the control arm — master's send-only shape — was **still parked at 55 s**.

These tests are about whether the handler ever ASKS, so the double answers immediately. The seconds
above are uvicorn's to spend and are pinned separately by ``test_entrypoint_tls``.

Driven as bare coroutines rather than through ``TestClient``, for the reason `test_slot_unwind`
already records: the sync client sends its own disconnect on block exit and cancels the task, so it
would pass on master and prove nothing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from radio_server.api import create_app
from radio_server.audio import AudioFrame
from radio_server.backends import MockRadio
from radio_server.dstar import MockGatewayClient
from radio_server.link import MockMumbleClient, resolve_mumble_entries
from radio_server.vocoder.base import AMBE_BYTES_PER_FRAME, PCM_FORMAT

TOKEN = "test-lan-secret"

#: Generous next to the milliseconds a reap actually takes, and short enough that master fails FAST.
#: A red run you have to kill is not a red run (ADR 0170) — on master these handlers park forever, so
#: without a bound the suite would hang instead of reporting.
REAP_BUDGET_S = 1.0

ENTRIES = resolve_mumble_entries([{"name": "home", "host": "mumble.example"}])


class _DeadPeerWS:
    """A websocket whose peer is gone but whose *transport* refuses to say so.

    ``send_bytes``/``send_json`` accept and silently discard, which is what uvicorn measurably does
    on a reset transport (ADR 0170) — so a handler that only ever sends learns nothing, no matter how
    much audio flows. The disconnect is available on ``receive()`` and only there. That asymmetry is
    the whole defect, and this double is built to reproduce exactly it.
    """

    def __init__(self, token: str, *, inbound: list | None = None) -> None:
        self.query_params = {"token": token}
        self.sent: list = []
        self.close_code: int | None = None
        self.receives = 0
        #: Messages delivered before the disconnect — a client that speaks on a receive-only socket.
        self._inbound = list(inbound or ())

    async def accept(self) -> None:
        pass

    async def send_json(self, data) -> None:
        self.sent.append(data)

    async def send_bytes(self, data) -> None:
        self.sent.append(bytes(data))

    async def close(self, code=None) -> None:
        self.close_code = code

    async def receive(self) -> dict:
        self.receives += 1
        if self._inbound:
            return self._inbound.pop(0)
        return {"type": "websocket.disconnect", "code": 1006}


class _LivePeerWS(_DeadPeerWS):
    """A peer that is present and never disconnects — the listener a reaper must NOT drop."""

    async def receive(self) -> dict:
        self.receives += 1
        await asyncio.Event().wait()  # a real client that simply has nothing to say
        raise AssertionError("unreachable")


def _endpoint(app, path: str):
    return next(r.endpoint for r in app.routes if getattr(r, "path", "") == path)


def _drive(app, path: str, websocket, budget: float = REAP_BUDGET_S) -> None:
    """Run the handler to completion, failing loudly (and quickly) if it parks forever.

    ``shield`` is load-bearing and was found the hard way: a bare
    ``wait_for(handler(), timeout=...)`` **passes on master**. The timeout cancels the handler, the
    handler swallows ``CancelledError`` by design (ADR 0122) and unwinds its context managers on the
    way out, and the cleanup this test is meant to be checking happens — driven by the test harness
    rather than by the code under test. That is a test that cannot fail, dressed as one that passed.

    Shielding keeps the cancellation off the handler, so a parked handler stays parked and the
    failure is real. The cancel afterwards is only tidy-up so the suite does not leak a task.
    """

    async def _run():
        task = asyncio.create_task(_endpoint(app, path)(websocket))
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=budget)
        except asyncio.TimeoutError:  # pragma: no cover - the point of the red run
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            pytest.fail(
                f"{path} never noticed its peer was gone: still parked after {budget}s with the "
                "ASGI disconnect sitting unread on the receive channel"
            )

    asyncio.run(_run())


def _mumble_app():
    return create_app(
        MockRadio(),
        api_token=TOKEN,
        mumble_entries=ENTRIES,
        mumble_client_factory=lambda entry: MockMumbleClient(host=entry.host),
    )


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


# --- the four handlers ------------------------------------------------------------------------
#
# A bare `MockRadio()` IS a quiet channel: `canned_rx` defaults to an empty frame, and `RxPump` skips
# publishing one (`if frame.samples:`), so nothing ever reaches the hub. That is the deployed
# station's condition too — `squelch = "audio"` is an `AudioLevelGate`, closed on a silent 147.555 —
# and it is the case master cannot escape, because only a published frame ever woke the handler.


def test_audio_rx_reaps_a_dead_listener_on_a_quiet_channel():
    app = create_app(MockRadio(), api_token=TOKEN)
    ws = _DeadPeerWS(TOKEN)
    _drive(app, "/audio/rx", ws)
    # The narrowest statement of the defect: master never ASKS. `receives == 0` is the whole bug,
    # and it is why no amount of audio, keepalive or waiting ever fixed it.
    assert ws.receives >= 1
    # Both, not just the subscription: the demand is what pins the single capture reader open for the
    # life of the process (ADR 0031), which is the part a restart was the only cure for.
    assert app.state.rx_demand == 0
    assert app.state.audio_hub.subscriber_count == 0


def test_audio_mumble_rx_reaps_a_dead_listener():
    app = _mumble_app()
    ws = _DeadPeerWS(TOKEN)
    _drive(app, "/audio/mumble/rx", ws)
    assert ws.receives >= 1
    assert app.state.mumble_rx_hub.subscriber_count == 0


def test_audio_dstar_rx_reaps_a_dead_listener():
    app = _dstar_app()
    ws = _DeadPeerWS(TOKEN)
    _drive(app, "/audio/dstar/rx", ws)
    assert ws.receives >= 1
    assert app.state.dstar_rx_hub.subscriber_count == 0


def test_events_reaps_a_dead_listener():
    # The fourth handler with this shape, and it was written FIRST — before any of the three RX
    # paths. It also leaks worse: `EventHub`'s queue is unbounded (no maxsize, no overflow handling)
    # where `AudioHub` caps at 64 frames with drop-oldest.
    app = create_app(MockRadio(), api_token=TOKEN)
    ws = _DeadPeerWS(TOKEN)
    _drive(app, "/events", ws)
    assert ws.receives >= 1
    assert app.state.hub.subscriber_count == 0


# --- the reaper must not drop a listener that is merely quiet -----------------------------------


def test_a_stray_client_message_is_discarded_not_treated_as_a_disconnect():
    # These sockets are receive-only by contract: nothing in the tree sends on them. Reading the
    # channel must not turn "the client said something unexpected" into "the client is gone" —
    # that would be a new way to drop a working listener, which is worse than the leak.
    app = create_app(MockRadio(), api_token=TOKEN)
    chatty = _DeadPeerWS(
        TOKEN,
        inbound=[
            {"type": "websocket.receive", "text": "hello?"},
            {"type": "websocket.receive", "bytes": b"\x01\x02"},
        ],
    )
    _drive(app, "/audio/rx", chatty)
    assert chatty.receives == 3  # two ignored, then the real disconnect
    assert app.state.rx_demand == 0
    # Nothing was echoed back: only the format header ever went out.
    assert chatty.sent == [{"status": "ready", "format": {"rate": 48000, "width": 2, "channels": 1}}]


def test_a_live_listener_is_not_reaped_and_still_receives_audio():
    # A reaper that drops working listeners is worse than the leak it fixes.
    radio = MockRadio(rx_frames=[AudioFrame(b"\x01\x02"), AudioFrame(b"\x03\x04")])
    app = create_app(radio, api_token=TOKEN)
    live = _LivePeerWS(TOKEN)

    async def _run():
        task = asyncio.create_task(_endpoint(app, "/audio/rx")(live))
        for _ in range(200):
            if len([m for m in live.sent if isinstance(m, bytes)]) >= 2:
                break
            await asyncio.sleep(0.01)
        assert not task.done(), "a connected listener was reaped"
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(_run())
    assert [m for m in live.sent if isinstance(m, bytes)][:2] == [b"\x01\x02", b"\x03\x04"]


# --- the Part 97 constraint: the mechanism is off the audio path by construction ----------------
#
# The two taps sit at different layers and the watcher is on neither. DTMF decode taps `RxPump` at
# `radio.receive()` level, on the raw `AudioFrame`, BEFORE the gate and before any hub publish
# (`rx/pump.py`); ADR 0162's relay mute sits on `AudioHub` subscriber queues, which carry plain
# bytes. A liveness scheme built out of synthetic frames would have been visible to both — on the
# D-STAR side a tick reaches `AudioFrame(...)` → `_rf_gate` BEFORE `_deafened()`, perturbing the
# hysteresis state of a Part 97 control. Reading the ASGI receive channel is visible to neither, and
# these tests are what make that a checked property rather than a claim in an ADR.


def test_reaping_publishes_nothing_to_the_audio_hub():
    app = create_app(MockRadio(), api_token=TOKEN)
    published: list = []
    app.state.audio_hub.publish = lambda frame: published.append(frame)  # type: ignore[method-assign]
    _drive(app, "/audio/rx", _DeadPeerWS(TOKEN))
    assert published == [], "the reaper put something on the RF audio path"


async def _reap_beside_a_live_listener(app, ready: Callable[[], bool]) -> None:
    """Reap a dead listener while a live one keeps the capture pump running.

    Both of these need the pump to still be producing *after* the reap, and it is not enough to reap
    and then look: releasing the last demand STOPS the pump (ADR 0031), so a lone dead listener
    reaps correctly and takes the audio with it — measured here as one frame instead of two. That is
    the test's own timing, not a defect, but asserting a frame count through it would be asserting
    how fast the pump ran.

    Holding a live listener open fixes it and asks a better question at the same time: with one dead
    peer and one live one on the same hub, does exactly the dead one go away, and does the audio the
    survivor sees stay byte-for-byte the radio's own?
    """
    live = _LivePeerWS(TOKEN)
    keeper = asyncio.create_task(_endpoint(app, "/audio/rx")(live))
    try:
        for _ in range(200):  # let the pump come up before anything is measured
            if app.state.rx_pump.running:
                break
            await asyncio.sleep(0.01)
        dead = _DeadPeerWS(TOKEN)
        reap = asyncio.create_task(_endpoint(app, "/audio/rx")(dead))
        await asyncio.wait_for(asyncio.shield(reap), timeout=REAP_BUDGET_S)
        for _ in range(200):
            if ready():
                break
            await asyncio.sleep(0.01)
        assert not keeper.done(), "the live listener was reaped along with the dead one"
        assert app.state.rx_demand == 1, "the survivor's demand did not survive"
    finally:
        keeper.cancel()
        await asyncio.gather(keeper, return_exceptions=True)


def test_a_relay_subscriber_sees_the_radios_bytes_byte_for_byte_while_a_listener_is_reaped():
    # The bridges are the other subscribers to this hub, and they are where ADR 0162's mute lives. A
    # synthetic keepalive frame would arrive here indistinguishable from real received audio — and
    # on the D-STAR side it would reach `_rf_gate` before `_deafened()`, perturbing the hysteresis
    # state of a Part 97 control.
    frames = [AudioFrame(b"\x11\x22"), AudioFrame(b"\x33\x44")]
    app = create_app(MockRadio(rx_frames=list(frames)), api_token=TOKEN)
    got: list = []

    async def _run():
        # Subscribed BEFORE any demand starts the pump, so nothing can be missed in the gap.
        relay = app.state.audio_hub.subscribe()

        async def _collect() -> None:
            while True:
                got.append(await relay.get())

        drain = asyncio.create_task(_collect())
        try:
            await _reap_beside_a_live_listener(app, lambda: len(got) >= len(frames))
        finally:
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)
            app.state.audio_hub.unsubscribe(relay)

    asyncio.run(_run())
    # Exactly the radio's bytes, in order, and NOTHING else: the trailing check is the one that
    # would catch a tick, since an extra frame appends rather than corrupts.
    assert got == [f.samples for f in frames]


def test_the_dtmf_tap_sees_exactly_the_frames_the_radio_produced():
    # `controller.step` is the DTMF decoder's feed, and it is upstream of the hub — it runs on the
    # RAW frame before the gate, so it sees everything `radio.receive()` produced whether the hub
    # does or not. A tick injected anywhere at pump level would decode as audio.
    app = create_app(
        MockRadio(rx_frames=[AudioFrame(b"\x01\x02"), AudioFrame(b"\x03\x04")]), api_token=TOKEN
    )
    stepped: list = []

    class _SpyController:
        def step(self, now, frame=None):
            stepped.append(frame)
            return []

    app.state.rx_pump._controller = _SpyController()  # noqa: SLF001 - the tap has no public seam

    async def _run():
        await _reap_beside_a_live_listener(app, lambda: len([f for f in stepped if f.samples]) >= 2)

    asyncio.run(_run())
    assert stepped, "the DTMF tap saw nothing at all — the test proved nothing"
    assert all(isinstance(f, AudioFrame) for f in stepped)
    # The empties are the mock's canned silence once the script runs out; the point is that the only
    # NON-empty frames are the radio's own, with nothing invented in between.
    assert [f.samples for f in stepped if f.samples] == [b"\x01\x02", b"\x03\x04"]
