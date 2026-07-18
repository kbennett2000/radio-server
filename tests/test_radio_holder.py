"""The radio holder: it owns the active radio and the lifecycle of the radio-bound pipeline (ADR 0073).

These are the tests the swap cycle builds on. They drive :class:`RadioHolder` directly with
``asyncio.run(...)`` — no pytest-asyncio, the same convention as ``test_scan_runner.py`` — against a
``MockRadio``. The load-bearing proofs: ``start()`` constructs the pipeline (and starts no task);
``start()``/``stop()`` are idempotent and ``stop()`` is safe before ``start()``; ``stop()`` drops PTT
if the radio was keyed and halts a running scan. Behaviour-preservation of the whole app around the
seam is proved by the rest of the suite staying green.
"""

from __future__ import annotations

import asyncio

from radio_server.api.events import EventHub
from radio_server.api.holder import RadioHolder
from radio_server.arbiter import RadioArbiter
from radio_server.backends import MockRadio
from radio_server.config import resolve_settings
from radio_server.rx import AudioHub
from radio_server.scan import ScanPlan

FREQS = [146_500_000, 146_520_000, 146_540_000]


class _PttSpyRadio(MockRadio):
    """A MockRadio that records its `ptt()` calls, so a test can assert the drop actually happened.

    MockRadio keeps only the latest ptt state (a single bool `transmit()` also toggles), so proving
    "keyed, then dropped by teardown" needs this spy — the `_PttSpyRadio` idiom from `test_tx_audio.py`.
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.ptt_log: list[bool] = []

    def ptt(self, on: bool) -> None:
        self.ptt_log.append(on)
        super().ptt(on)


def _make_holder(
    radio: MockRadio,
    *,
    controller: object | None = None,
    arbiter: RadioArbiter | None = None,
) -> RadioHolder:
    """A holder over ``radio`` with real, radio-independent collaborators and no real sleeps.

    `scan_poll=0.0` keeps a started scan's loop from sleeping for real; the scan engine is built from
    default settings (the swap cycle's collaborators are the same keyword args `create_app` passes).
    Pass ``arbiter`` when the test needs to drive the transmitter-ownership the holder consults.
    """
    return RadioHolder(
        radio,
        hub=EventHub(),
        audio_hub=AudioHub(),
        arbiter=arbiter if arbiter is not None else RadioArbiter(),
        scan_settings=resolve_settings({}),
        scan_poll=0.0,
        controller=controller,  # type: ignore[arg-type]
    )


def test_start_builds_the_pipeline_without_running_a_task():
    holder = _make_holder(MockRadio())
    assert holder.rx_pump is None and holder.scan_runner is None
    holder.start()
    # Constructed against the radio, but neither task is running — the pump is demand-started and a
    # scan is plan-started; start() only builds them.
    assert holder.rx_pump is not None and holder.scan_runner is not None
    assert holder.rx_pump.running is False
    assert holder.scan_runner.running is False


def test_start_is_idempotent_and_does_not_rebuild():
    holder = _make_holder(MockRadio())
    holder.start()
    pump, runner = holder.rx_pump, holder.scan_runner
    holder.start()  # a second call must not rebuild a live pipeline
    assert holder.rx_pump is pump
    assert holder.scan_runner is runner


def test_stop_is_safe_before_start():
    # A never-started holder has no pump/scan to stop; stop() is still a clean no-op (fail-safe).
    holder = _make_holder(MockRadio())
    asyncio.run(holder.stop())
    assert holder.rx_pump is None and holder.scan_runner is None


def test_stop_is_idempotent():
    holder = _make_holder(MockRadio())
    holder.start()

    async def scenario() -> None:
        await holder.stop()
        await holder.stop()  # calling twice is harmless

    asyncio.run(scenario())


def test_stop_drops_ptt_when_a_session_holds_the_transmitter():
    radio = _PttSpyRadio()
    arbiter = RadioArbiter()
    holder = _make_holder(radio, arbiter=arbiter)
    holder.start()
    arbiter.acquire_tx()  # a session mid-key at teardown/swap holds the transmitter
    radio.ptt(True)
    assert radio.status().transmitting is True
    asyncio.run(holder.stop())
    # The holder consults the arbiter (the app's half-duplex owner) and keys down.
    assert radio.ptt_log[-1] is False
    assert radio.status().transmitting is False


def test_stop_does_not_key_down_when_nothing_holds_the_transmitter():
    # Behaviour-preservation: a quiescent teardown (arbiter idle) must NOT add a spurious `ptt(False)`
    # — that would change the keying contract every clean `with TestClient(...)` shutdown asserts.
    radio = _PttSpyRadio()
    holder = _make_holder(radio)
    holder.start()
    asyncio.run(holder.stop())
    assert radio.ptt_log == []
    assert radio.status().transmitting is False


def test_stop_halts_a_running_scan():
    radio = MockRadio(supports_cat=True, busy_frequencies={FREQS[1]})
    holder = _make_holder(radio)
    holder.start()
    plan = ScanPlan.from_frequencies(FREQS)

    async def scenario() -> None:
        assert holder.scan_runner.start(plan) is True
        assert holder.scan_runner.running is True
        await holder.stop()
        assert holder.scan_runner.running is False

    asyncio.run(scenario())
