"""Waiting out a backend's transmit lockout without owning the event loop (ADR 0182).

A UV-K5 mutes its own transmitter for six seconds after any EEPROM conversation — the HELLO, every
EEPROM read and every write arm ``gSerialConfigCountDown_500ms`` alike (ADR 0144/0145) — so with
``baofeng.uvk5_tune_persist = true`` **every** tune arms a lockout and the next key-up has to sit
through whatever is left of it. ``AiocBaofeng._await_tx_lockout`` does exactly that, and it is
right to: ADR 0142 shipped without the wait and the bench failed every carrier row on attempt #1,
because the firmware silently swallows PTT for the duration. **Nothing here shortens or skips it.**

What was wrong is *where* the time was spent. The wait was a ``time.sleep`` inside ``ptt(True)``,
and the loop-side keying paths — ``/audio/tx``, ``POST /transmit``, both bridge relay tasks — call
into that synchronously **from the event loop**. Measured on the deployed station, two arms with the
same 6.49 s lockout: ``POST /ptt`` (a plain ``def`` route, so Starlette runs it in a threadpool)
served 142 concurrent ``GET /status`` probes with a 12.9 ms worst case, while ``POST /transmit``
(an ``async def``, so the loop) served 17 and one of them took **7123 ms**. Same wait, same
duration; one route kept the server answering and the other stopped it dead.

So the loop-side callers wait it out with :func:`await_tx_ready` **before** entering the synchronous
keying path, and the backend's own enforcement is left exactly as it was.

**This is deliberately an optimisation of where, never of whether.** ``_key_on`` stays the thing
that keeps RF correct — its docstring's *"a browser must never be the thing keeping RF correct"*
still holds — so a keying path that forgets to pre-wait degrades to the old blocking-but-correct
behaviour rather than to an early key-up that transmits nothing. That safe degradation is why this
is an added ``await`` at the call sites rather than a restructuring of ``ptt()``.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

#: Hard cap on how long a pre-wait will hold a key-up, mirroring the cap ``_await_tx_lockout``
#: already applies. It **must not be tighter** than the firmware lockout it is waiting out, or the
#: pre-wait would return early and hand a still-muted radio to the key-up — the ADR 0142 fault.
#: Pinned against ``SERIAL_TX_LOCKOUT_S`` by ``test_tx_lockout_does_not_stall_the_loop.py`` rather
#: than imported, so ``tx`` keeps its ``tx -> {audio, backends}`` arrow and does not reach into one
#: specific backend's tuner for a constant.
MAX_TX_LOCKOUT_WAIT_S = 6.5


def tx_lockout_remaining(radio: object) -> float | None:
    """Seconds this radio will refuse a key-up for, or ``None`` if it will accept one now.

    Duck-typed on purpose (the ``getattr``-and-check idiom ``RxPump`` uses for the gate's optional
    ``start``/``stop``): only ``AiocBaofeng`` with a UV-K5 tuner has a lockout to report, and the
    capability split of guardrail 3 says a backend advertises what it implements rather than every
    caller knowing which backends those are. A radio without the method simply has no wait.

    Never raises. A probe that fails is treated as "nothing to pre-wait", which falls through to the
    backend's own enforcement — the safe direction.
    """
    probe = getattr(radio, "tx_ready_in", None)
    if not callable(probe):
        return None
    try:
        remaining = probe()
    except Exception:
        # A pre-wait is an optimisation; it must never be the thing that refuses a key-up.
        logger.debug("tx lockout probe failed; leaving the wait to the backend", exc_info=True)
        return None
    if remaining is None:
        return None
    try:
        remaining = float(remaining)
    except (TypeError, ValueError):
        return None
    return remaining if remaining > 0 else None


async def await_tx_ready(radio: object) -> float:
    """Wait out this radio's transmit lockout **on** the event loop instead of **in** it.

    Returns the seconds waited (``0.0`` when there was nothing to wait for), so a caller can log or
    count it. Bounded by :data:`MAX_TX_LOCKOUT_WAIT_S`, so a stuck deadline cannot hold a key-up
    open-endedly — "a key-up must never be blocked or delayed unboundedly" holds before and after.

    Call this immediately before the synchronous key-up, not earlier: the deadline is read at the
    moment of asking, and anything awaited in between could arm a fresh one.
    """
    remaining = tx_lockout_remaining(radio)
    if remaining is None:
        return 0.0
    waited = min(remaining, MAX_TX_LOCKOUT_WAIT_S)
    logger.info(
        "holding key-up %.1fs for the radio's serial TX lockout (off the event loop)", waited
    )
    await asyncio.sleep(waited)
    return waited
