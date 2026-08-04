"""ADR 0182: waiting out the radio's TX lockout must not own the event loop.

A UV-K5 mutes its own transmitter for six seconds after any EEPROM conversation, so with
``uvk5_tune_persist = true`` every tune arms a lockout and the next key-up has to sit through it.
That wait is **necessary** — ADR 0142 shipped without it and the bench failed every carrier row on
attempt #1, because the firmware silently swallows PTT for the duration. Nothing here shortens or
skips it.

What was wrong is *where* it was spent. ``AiocBaofeng._await_tx_lockout`` called ``time.sleep`` from
``ptt(True)``, and the loop-side keying paths — ``/audio/tx``, ``POST /transmit``, both bridge relay
tasks — call into that synchronously from the event loop. So the whole server stopped for the
duration: no ``/status``, no ``/events``, no bridge relay, no ledger drain.

Measured on the deployed station (ADR 0182), two arms with the same 6.49 s lockout:

===========================  ==========  ===============  ============
arm                          probes      median /status   worst
===========================  ==========  ===============  ============
``POST /ptt`` (threadpool)   142         5.8 ms           **12.9 ms**
``POST /transmit`` (loop)    17          11.1 ms          **7123.5 ms**
===========================  ==========  ===============  ============

Same wait, same duration; one route kept the server answering and the other stopped it dead. That
is the fault, and it is availability — **not** ADR 0181's stuck carrier. The sleep runs before
``_key_on_locked``, so the PTT line is still low and there is no carrier to be unable to drop.

Timing style follows ``test_ledger_does_not_stall_the_loop.py``: real wall-clock bounds with
generous margins, and every assert reports the number it measured.
"""

from __future__ import annotations

import asyncio
import threading
import time

from fastapi.testclient import TestClient

from radio_server.api import create_app
from radio_server.backends.mock import MockRadio

TOKEN = "secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
#: The canonical format-declaration header, as `test_tx_audio.py` writes it.
CANONICAL_HEADER = {"rate": 48000, "width": 2, "channels": 1}

#: How long the staged lockout holds a key-up. Shorter than the firmware's real 6.5 s so the suite
#: stays fast; the deployed station measured 6.49 s (ADR 0145's `run_storage_contrast`, 5/5) and the
#: journal has held key-ups from 2.7 s to the full 6.5 s. The SHAPE is what is under test.
LOCKOUT = 0.5


class _LockedOutRadio(MockRadio):
    """A radio whose ``ptt(True)`` sits out a serial TX lockout, exactly as ``AiocBaofeng`` does.

    Mirrors the real ordering deliberately: ``_await_tx_lockout`` sleeps and only then does
    ``_key_on_locked`` assert the line, so the wait happens with PTT still LOW. Getting that
    backwards would make this file test a stuck carrier, which is not what this hazard is.
    """

    def __init__(self, lockout: float = LOCKOUT, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._deadline = time.monotonic() + lockout
        self._cap = lockout
        self.ptt_log: list[bool] = []
        self.ptt_at: list[tuple[bool, float]] = []
        #: Every wait the BACKEND itself served, i.e. every caller that did not pre-wait.
        self.slept: list[float] = []

    def tx_ready_in(self) -> float | None:
        """Seconds until the radio will accept a key-up, or ``None`` if it will accept one now."""
        remaining = self._deadline - time.monotonic()
        return remaining if remaining > 0 else None

    def ptt(self, on: bool) -> None:
        if on:
            remaining = self.tx_ready_in()
            if remaining is not None:
                self.slept.append(remaining)
                time.sleep(min(remaining, self._cap))
        super().ptt(on)
        self.ptt_log.append(on)
        self.ptt_at.append((on, time.monotonic()))


def _keyed_through_tx_socket(client: TestClient, radio: _LockedOutRadio) -> None:
    """Key once through ``/audio/tx`` — the browser Talk path, inline on the event loop.

    Holds the socket open until the key-up has actually happened. The wait is now awaited rather
    than slept through, so closing straight after ``send_bytes`` would race the key-up instead of
    observing it — and a test that measures the loop during a key-up has to contain one.
    """
    with client.websocket_connect(f"/audio/tx?token={TOKEN}") as ws:
        ws.send_json(CANONICAL_HEADER)
        assert ws.receive_json()["status"] == "ready"
        ws.send_bytes(b"\x01\x02")  # the first real frame keys
        deadline = time.monotonic() + LOCKOUT * 6
        while not radio.ptt_log and time.monotonic() < deadline:
            time.sleep(0.005)


# --- the hazard, through the wired app ------------------------------------------------------------


def test_a_locked_out_key_up_does_not_stall_a_concurrent_request() -> None:
    """FAIL-FIRST (ADR 0182). Red on master: the whole server waits out the radio's lockout.

    This is the hardware measurement in miniature. ``/audio/tx`` keys from the event loop, so while
    the backend sleeps out the lockout nothing else on the loop can be dispatched — including the
    ``GET /healthz`` below, which touches no radio at all.
    """
    radio = _LockedOutRadio()
    app = create_app(radio, api_token=TOKEN)

    worst = 0.0
    with TestClient(app) as client:
        latencies: list[float] = []
        stop = threading.Event()

        def probe() -> None:
            while not stop.is_set():
                started = time.monotonic()
                client.get("/healthz")
                latencies.append(time.monotonic() - started)
                time.sleep(0.01)

        prober = threading.Thread(target=probe, daemon=True)
        prober.start()
        time.sleep(0.05)
        _keyed_through_tx_socket(client, radio)
        stop.set()
        prober.join(timeout=5)
        worst = max(latencies) if latencies else 0.0

    assert radio.ptt_log, "the test never keyed at all"
    assert worst < LOCKOUT / 2, (
        f"an unrelated GET /healthz waited {worst * 1000:.0f} ms while the radio sat out a "
        f"{LOCKOUT * 1000:.0f} ms TX lockout — the wait is being spent on the event loop"
    )


def test_the_event_loop_keeps_running_while_the_radio_waits_out_its_lockout() -> None:
    """The mechanism, measured directly: a probe coroutine watches the loop across a key-up.

    This is the quantity every other symptom is downstream of, and the one the bench measured as
    7123 ms on the deployed station.
    """

    async def scenario() -> float:
        radio = _LockedOutRadio()
        gaps: list[float] = []

        async def probe() -> None:
            last = time.monotonic()
            while True:
                await asyncio.sleep(0.005)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        probe_task = asyncio.create_task(probe())
        await asyncio.sleep(0.05)
        try:
            from radio_server.tx.lockout import await_tx_ready

            await await_tx_ready(radio)
            radio.ptt(True)
            radio.ptt(False)
        finally:
            probe_task.cancel()
            await asyncio.gather(probe_task, return_exceptions=True)
        return max(gaps) if gaps else 0.0

    worst = asyncio.run(scenario())
    assert worst < LOCKOUT / 2, (
        f"the event loop was unavailable for {worst * 1000:.0f} ms while the radio waited out a "
        f"{LOCKOUT * 1000:.0f} ms TX lockout (probe ticks every 5 ms)"
    )


# --- the wait itself is not shortened -------------------------------------------------------------


def test_the_lockout_is_still_waited_out_in_full() -> None:
    """The RF-safety invariant, and the reason this is not just "stop sleeping".

    ADR 0142: a radio told to tune swallows the next six seconds of PTT. Keying early transmits
    nothing. So the key-up must still take the whole lockout — it just must not spend it on the loop.
    """

    async def scenario() -> tuple[float, list[bool]]:
        from radio_server.tx.lockout import await_tx_ready

        radio = _LockedOutRadio()
        started = time.monotonic()
        await await_tx_ready(radio)
        radio.ptt(True)
        return time.monotonic() - started, radio.ptt_log

    elapsed, keys = asyncio.run(scenario())
    assert keys == [True], f"keying sequence was {keys}"
    assert elapsed >= LOCKOUT * 0.9, (
        f"the key-up completed in {elapsed * 1000:.0f} ms against a {LOCKOUT * 1000:.0f} ms "
        f"lockout — the wait was SHORTENED, which is the ADR 0142 fault coming back"
    )


def test_the_backend_still_enforces_the_wait_for_a_caller_that_did_not_pre_wait() -> None:
    """Safe degradation: a keying path that forgets the pre-wait must still be correct, not fast.

    The pre-wait is an optimisation of *where* the time is spent, never the thing that keeps RF
    correct — ``_key_on`` stays the enforcement, exactly as its docstring says. So a missed call
    site degrades to today's blocking-but-correct behaviour rather than to an early key-up.
    """
    radio = _LockedOutRadio()
    started = time.monotonic()
    radio.ptt(True)  # no pre-wait at all
    elapsed = time.monotonic() - started
    assert radio.slept, "the backend did not enforce the lockout on an un-pre-waited key-up"
    assert elapsed >= LOCKOUT * 0.9, (
        f"an un-pre-waited key-up took {elapsed * 1000:.0f} ms against a "
        f"{LOCKOUT * 1000:.0f} ms lockout — the backend stopped enforcing it"
    )


def test_a_pre_waited_key_up_leaves_the_backend_nothing_to_sleep_on() -> None:
    """The two halves compose: after the async wait the backend's own wait is already satisfied."""

    async def scenario() -> list[float]:
        from radio_server.tx.lockout import await_tx_ready

        radio = _LockedOutRadio()
        await await_tx_ready(radio)
        radio.ptt(True)
        return radio.slept

    slept = asyncio.run(scenario())
    assert slept == [], (
        f"the backend still slept {slept} after the caller had already waited the lockout out — "
        f"the pre-wait is not actually satisfying it"
    )


# --- the probe, and the bound it must not undercut ------------------------------------------------


def test_a_radio_with_no_lockout_is_not_waited_on() -> None:
    """Most backends have no lockout at all, and must pay nothing for this.

    ``MockRadio``, ``kv4p`` and the plain ``uvk5`` dock backend never define ``tx_ready_in``; only
    an AIOC with a UV-K5 tuner does. Duck-typed exactly so the capability split of guardrail 3 holds.
    """
    from radio_server.tx.lockout import tx_lockout_remaining

    assert tx_lockout_remaining(MockRadio()) is None
    assert asyncio.run(_wait(MockRadio())) == 0.0


def test_a_probe_that_raises_falls_through_to_the_backend() -> None:
    """A pre-wait is an optimisation and must never be the thing that refuses a key-up."""
    from radio_server.tx.lockout import tx_lockout_remaining

    class _Angry:
        def tx_ready_in(self) -> float:
            raise RuntimeError("serial gone")

    assert tx_lockout_remaining(_Angry()) is None


def test_the_pre_wait_cap_is_not_tighter_than_the_firmware_lockout() -> None:
    """DERIVED, not chosen: the cap must cover the whole lockout it is waiting out.

    A tighter cap would return early and hand a still-muted radio to the key-up — the ADR 0142
    fault, which the bench caught as every carrier row failing on attempt #1. The constant is
    duplicated rather than imported so ``tx`` keeps its ``tx -> {audio, backends}`` arrow; this is
    what stops the duplicate drifting.
    """
    from radio_server.backends.uvk5.tuner import SERIAL_TX_LOCKOUT_S
    from radio_server.tx.lockout import MAX_TX_LOCKOUT_WAIT_S

    assert MAX_TX_LOCKOUT_WAIT_S >= SERIAL_TX_LOCKOUT_S, (
        f"the pre-wait caps at {MAX_TX_LOCKOUT_WAIT_S}s but the firmware lockout is "
        f"{SERIAL_TX_LOCKOUT_S}s — the wait would end while the radio is still muted"
    )


async def _wait(radio: object) -> float:
    from radio_server.tx.lockout import await_tx_ready

    return await await_tx_ready(radio)


# --- every loop-side keying path pre-waits --------------------------------------------------------


def test_every_loop_side_keying_path_waits_the_lockout_out_off_the_loop() -> None:
    """The enumeration pin, in the ``test_event_hub_bound`` ``_EXPECTED`` idiom.

    The pre-wait lives at each call site, so the failure mode is a **new** loop-side keying path
    that forgets it. This is a source scan and therefore a guard rather than a proof (ADR 0167
    recorded exactly that limitation against ``test_relay_subscribers``) — but the fault it catches
    is the one that will actually happen: someone adds a seventh keying path and nobody notices.

    Two paths are deliberately absent and named here rather than left to be rediscovered:

    * ``dstar/bridge.py`` — ``_emit_rx_pcm`` is sync inside two async callers, so the change is
      mechanically small, but it is the crossband keying path the stuck-key incident hardened
      (ADR 0090-0099) and that crossband is DISABLED pending a cold-boot re-proof. An unverified
      change there is not worth a fix to a disabled feature; it still blocks, exactly as today.
    * ``POST /services/{digit}`` and ``POST /auth/session`` — both are ``async def`` with no await
      inside **on purpose**, so they serialize against the RX pump's ``controller.step``. Adding an
      await would break that ordering, which is a controller-concurrency question and not this
      ADR's to answer. They still block.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for path, why in (
        ("radio_server/api/app.py", "/audio/tx and POST /transmit"),
        ("radio_server/link/bridge.py", "the Mumble relay task"),
    ):
        source = (root / path).read_text()
        assert "await await_tx_ready(" in source, (
            f"{path} keys the radio from the event loop ({why}) but never awaits the TX lockout — "
            f"a key-up there will spend it on the loop and stop the whole server (ADR 0182)"
        )
