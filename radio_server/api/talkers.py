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

from fastapi import WebSocket, status

from ..rx import AudioHub
from ..tx import TxSlot

logger = logging.getLogger(__name__)


@asynccontextmanager
async def talker_slot(
    websocket: WebSocket, slot: TxSlot, name: str
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
    """
    acquired = slot.try_acquire()
    try:
        await websocket.accept()
        if not acquired:
            # The one signal a strand leaves behind. `TxSlot` has no holder, no timestamp and no
            # counter, and nothing publishes slot state to `/status` or `/events`, so without this
            # line a leaked slot is indistinguishable from a busy station — see ADR 0167's carried
            # finding. One line, at INFO, so a refusal storm is greppable in the journal.
            logger.info("talk slot %s refused: already held", name)
            await websocket.send_json({"status": "busy"})
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
