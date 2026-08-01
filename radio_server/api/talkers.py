"""Claiming a shared resource and entering the region that frees it, as ONE unit (ADR 0167).

The rule these context managers exist to enforce: **a release must be a scope exit, never a
statement that can be skipped.** Every websocket endpoint that claims something used to spell the
claim, the handshake and the guarded region as three separate steps::

    acquired = tx_slot.try_acquire()
    await websocket.accept()          # anything raising here strands the slot forever
    ...
    try:
        ...
    finally:
        tx_slot.release()             # ...because this is a statement, and it is never reached

`TxSlot` is one bare bool — no owner, no acquire timestamp, no timeout, no watchdog, no lifespan
reset — so a strand is permanent for the life of the process and the only remedy is a restart.
Three endpoints had that shape verbatim, which is the real argument for a helper: three identical
copies is how the fourth one gets written wrong.

Putting the release in an *outer* scope closes a second family at the same time. The teardown that
each handler still runs in its own ``finally`` — ``TxSession.close()``, whose ``ptt(False)`` ADR 0166
made a demonstrated raiser, or ``end_operator_over()``'s UDP write to a possibly-dead socket — can
no longer skip the release by raising, because the release is no longer downstream of it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import WebSocket, status

from ..rx import AudioHub
from ..tx import TxSlot

logger = logging.getLogger(__name__)


#: The label every websocket talker claims under. The RF slot's other two claimants are the relays
#: (``mumble-relay``, ``dstar-relay``), which claim in their own loops — so this is what tells an
#: operator reading ``/status`` whether the transmitter is held by a person or by a bridge.
BROWSER = "browser"


@asynccontextmanager
async def talker_slot(
    websocket: WebSocket,
    slot: TxSlot,
    name: str,
    *,
    publish: Callable[[dict], None] | None = None,
    stale_after_s: float | None = None,
) -> AsyncIterator[bool]:
    """Claim a single-talker slot and complete the handshake as one unit.

    Yields whether the slot was claimed; a caller that gets ``False`` has already been told why and
    closed, and must simply ``return``. The slot is released on *every* exit from the block — normal
    return, exception, or cancellation — but only if this caller is the one that claimed it, so a
    refused talker never frees the slot the holder is using.

    ``try``/``finally`` rather than ``except Exception``: a real shutdown delivers
    :class:`asyncio.CancelledError`, which is a ``BaseException`` — the same reason ADR 0151's
    key-up unwind catches ``BaseException``.

    Ordering inside is load-bearing. A browser cannot observe a *pre-accept* close code — a rejected
    WS handshake surfaces as a generic 1006 and the app-level 1013 is lost — so a refused talker is
    accepted first, told ``{"status":"busy"}`` in a message it can actually read, and only then
    closed 1013.

    ``publish`` (ADR 0170) emits the refusal as a ``busy`` event; ``None`` keeps the endpoint silent,
    which is what every test that does not care about events wants.
    """
    acquired = slot.try_acquire(BROWSER)
    try:
        await websocket.accept()
        if not acquired:
            # A refusal now leaves THREE traces of one fact, deliberately: this log line for the
            # journal (ADR 0167 — the only one that existed), the `busy` event for anything watching
            # live, and the slot's own counters for `/status`. Before ADR 0170 a leaked slot and a
            # busy station were the same observation, and the operator got the same sentence.
            held_s = slot.held_s
            logger.info(
                "talk slot %s refused: held by %s for %.1fs",
                name,
                slot.holder or "an unlabelled claimant",
                held_s if held_s is not None else 0.0,
            )
            # The holder and the age travel WITH the refusal, because the browser cannot read a
            # close code and this message is its only channel (the ADR 0161 mechanism). Without them
            # the client can only repeat "another operator is transmitting", which is false in the
            # one case that matters.
            occupancy = {"slot": name, "holder": slot.holder, "held_s": held_s}
            if publish is not None:
                publish(dict(occupancy))
            await websocket.send_json(
                {"status": "busy", **occupancy, "stale_after_s": stale_after_s}
            )
            await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER)
        yield acquired
    finally:
        if acquired:
            slot.release()


@asynccontextmanager
async def rx_listener(
    audio_hub: AudioHub,
    acquire: Callable[[], Awaitable[None]],
    release: Callable[[], Awaitable[None]],
) -> AsyncIterator[asyncio.Queue]:
    """Subscribe to received audio and add a reader demand as one unit.

    The same rule over different resources: the subscription and the demand are two claims, and the
    ``await`` between them could leave the first taken and the second not. On the way out the demand
    is released only if it was actually taken, mirroring ``talker_slot``'s ``if acquired`` — a
    reader that never started must not be un-demanded.

    The parameter name ``audio_hub`` is load-bearing, not incidental: ``test_relay_subscribers``
    pins every subscriber of the RF hub by scanning source text for ``audio_hub.subscribe()``,
    deliberately rather than through a registry a subscriber could skip. Renaming it here would hide
    the browser Listen path from a Part 97 control (ADR 0162).
    """
    queue = audio_hub.subscribe()
    acquired = False
    try:
        await acquire()
        acquired = True
        yield queue
    finally:
        audio_hub.unsubscribe(queue)
        if acquired:
            await release()


async def _watch_disconnect(websocket: WebSocket) -> None:
    """Wait until the peer goes away, by reading the channel that already says so.

    **This reads the socket's INBOUND control channel and touches no audio.** That is the whole
    Part 97 argument, and it is a property of construction rather than of convention: the DTMF
    decoder taps `RxPump` at `radio.receive()` level, on the raw `AudioFrame`, before the gate and
    before any hub publish; ADR 0162's relay mute sits on `AudioHub` subscriber queues. A liveness
    scheme made of synthetic frames would have been visible to both — on the D-STAR side a tick
    reaches `_rf_gate` *before* `_deafened()`, mutating the hysteresis state of a Part 97 control.
    Nothing here is published, subscribed to, or injected.

    Every RX socket in this tree is receive-only: no client sends on one, so anything that does
    arrive is discarded rather than answered. Discarding is deliberate — treating an unexpected
    message as a disconnect would be a brand-new way to drop a working listener, which is worse than
    the leak this exists to fix.
    """
    while True:
        if (await websocket.receive())["type"] == "websocket.disconnect":
            return


async def stream_until_disconnect(
    websocket: WebSocket,
    queue: asyncio.Queue,
    send: Callable[[Any], Awaitable[None]],
) -> None:
    """Drain ``queue`` to ``websocket`` until the queue's producer stops or the peer disconnects.

    **The disconnect was always being delivered; nobody was listening.** ASGI posts
    ``{"type": "websocket.disconnect"}`` on the receive channel when uvicorn tears a connection
    down, and every streaming handler here parked on an unbounded ``queue.get()`` and only ever
    *sent* — so it could learn its client was gone only from the next published frame. On a quiet
    channel there is no next frame (the deployed station runs ``squelch = "audio"``, a gate that is
    closed on silence), and on a reset transport uvicorn drops sends **silently** rather than
    raising, so even a frame did not tell it. ADR 0170 measured a dropped listener still counted at
    +50 s, through a signal that woke every other reader.

    So this is not a timeout, and deliberately so. ADR 0170's "one loop has a bounded await and the
    other does not" is true of TX, but the timeout is not what makes TX safe — *reading the receive
    channel at all* is. On RX a silent queue is legitimate, so a timeout could only busy-loop or
    disconnect a listener that is doing nothing wrong.

    Measured against real uvicorn on the production ``websockets`` path (ADR 0171): a clean close is
    seen immediately, an RST after **19.7 s** (the keepalive *write* fails), and a peer that goes
    silent with its socket still open after **40.0 s** (``ws_ping_interval + ws_ping_timeout``). That
    spread is why the entrypoint pins both values instead of inheriting them — see
    ``WS_PING_INTERVAL_SECONDS`` in ``radio_server.__main__``.

    The caller keeps its own ``except (WebSocketDisconnect, CancelledError)`` and its own unwind:
    anything the drain raises is re-raised here unchanged.
    """

    async def _drain() -> None:
        while True:
            await send(await queue.get())

    tasks = {asyncio.create_task(_drain()), asyncio.create_task(_watch_disconnect(websocket))}
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()  # re-raise WebSocketDisconnect to the caller that knows how to handle it
    finally:
        # Cancel BOTH, unconditionally: on the disconnect path the drain is still parked on
        # `queue.get()`, and on shutdown this coroutine is itself cancelled while parked in `wait`.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
