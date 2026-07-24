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
from typing import Callable

import pytest

from radio_server.activity import CatBusyGate
from radio_server.api.events import EventHub
from radio_server.api.holder import RadioHolder
from radio_server.arbiter import RadioArbiter
from radio_server.backends import MockRadio, Radio
from radio_server.config import Settings, resolve_settings
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
    scan_settings: Settings | None = None,
    radio_factory: Callable[[Settings], Radio] | None = None,
    controller_factory: Callable[[Settings, Radio], object | None] | None = None,
) -> RadioHolder:
    """A holder over ``radio`` with real, radio-independent collaborators and no real sleeps.

    `scan_poll=0.0` keeps a started scan's loop from sleeping for real; the scan engine is built from
    default settings (the swap cycle's collaborators are the same keyword args `create_app` passes).
    Pass ``arbiter`` when the test needs to drive the transmitter-ownership the holder consults.
    `radio_factory`/`controller_factory` are the ADR 0076 swap seams — pass fakes to drive `rebuild`;
    `scan_settings` is the settings carrier `rebuild` reads for the *previous* backend on rollback.
    """
    kwargs: dict = {}
    if radio_factory is not None:
        kwargs["radio_factory"] = radio_factory
    if controller_factory is not None:
        kwargs["controller_factory"] = controller_factory
    return RadioHolder(
        radio,
        hub=EventHub(),
        audio_hub=AudioHub(),
        arbiter=arbiter if arbiter is not None else RadioArbiter(),
        scan_settings=scan_settings if scan_settings is not None else resolve_settings({}),
        scan_poll=0.0,
        controller=controller,  # type: ignore[arg-type]
        **kwargs,
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


# --- the live backend switch (ADR 0076) ----------------------------------------------------------
#
# `rebuild()` drives the swap seam against *fakes*: a `radio_factory` keyed on `server.backend` returns
# a distinct radio per backend, and a `controller_factory` a distinct controller. These prove the swap
# (active radio becomes the target, old pipeline stopped), the load-bearing rollback (a target that
# fails to construct leaves the holder on the previous working radio), the lock (concurrent selects
# serialize), and the controller rebuild (a fresh controller bound to the new radio).


class _CloseSpyRadio(MockRadio):
    """A MockRadio tagged by backend name that records whether `close()` was called (the swap reaps it)."""

    def __init__(self, tag: str, *, supports_cat: bool = False) -> None:
        super().__init__(supports_cat=supports_cat)
        self.tag = tag
        self.closed = False

    def close(self) -> None:
        self.closed = True
        super().close()


class _FakeController:
    """A stand-in DTMF controller that remembers the radio it was built against and its `close()`."""

    def __init__(self, radio: Radio) -> None:
        self.radio = radio
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _settings(backend: str) -> Settings:
    # No enum on server.backend (coerce_str) and validation lives above resolve_settings (ADR 0074),
    # so an arbitrary backend name resolves cleanly — the holder's injected factory keys on it.
    return resolve_settings({"server.backend": backend})


def _radio_factory(created: dict[str, list[_CloseSpyRadio]]):
    def make(settings: Settings) -> Radio:
        name = settings.get("server.backend")
        radio = _CloseSpyRadio(name, supports_cat=(name == "cat"))
        created.setdefault(name, []).append(radio)
        return radio

    return make


def test_rebuild_swaps_the_active_radio_and_pipeline():
    a = _CloseSpyRadio("a")
    holder = _make_holder(a, radio_factory=_radio_factory({}), scan_settings=_settings("a"))
    holder.start()
    pump_a, runner_a = holder.rx_pump, holder.scan_runner

    asyncio.run(holder.rebuild(_settings("b")))

    assert a.closed is True  # the outgoing radio was closed by stop()
    assert holder.radio is not a and holder.radio.tag == "b"  # active radio is the target
    # A fresh pipeline was built against the new radio (not the old, stopped one).
    assert holder.rx_pump is not None and holder.rx_pump is not pump_a
    assert holder.scan_runner is not None and holder.scan_runner is not runner_a


def test_rebuild_reselects_the_gate_for_the_new_backend_and_repoints_it():
    # ADR 0121: a live switch must re-select the RX gate for the new backend AND re-point a
    # CatBusyGate at the freshly built radio — the gate closes over the radio it was built with, so
    # reusing the old one would poll the now-closed previous radio. Rebuild to uvk5 (its squelch_mode
    # default is cat) and assert the holder's gate became a CatBusyGate over the new radio.
    a = _CloseSpyRadio("a")
    holder = _make_holder(a, radio_factory=_radio_factory({}), scan_settings=_settings("a"))
    holder.start()

    asyncio.run(holder.rebuild(_settings("uvk5")))

    assert isinstance(holder._gate, CatBusyGate)
    assert holder._gate._radio is holder.radio  # re-pointed at the new radio, not the closed 'a'
    assert holder.radio is not a


def test_rebuild_rolls_back_to_the_previous_radio_when_the_target_fails():
    a = _CloseSpyRadio("a")

    def failing(settings: Settings) -> Radio:
        name = settings.get("server.backend")
        if name == "bad":
            raise RuntimeError("cannot open bad")
        return _CloseSpyRadio(name)

    holder = _make_holder(a, radio_factory=failing, scan_settings=_settings("a"))
    holder.start()

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="cannot open bad"):
            await holder.rebuild(_settings("bad"))

    asyncio.run(scenario())

    # Never radio-less: the previous backend ("a") was reconstructed fresh (the old was closed by
    # stop()) and restarted, so the holder is live on the radio it had.
    assert a.closed is True
    assert holder.radio is not a and holder.radio.tag == "a"
    assert holder.rx_pump is not None and holder.scan_runner is not None


def test_concurrent_rebuilds_serialize_via_the_lock():
    holder = _make_holder(
        _CloseSpyRadio("a"), radio_factory=_radio_factory({}), scan_settings=_settings("a")
    )
    holder.start()

    # Wrap stop() to detect two rebuilds tearing down at once. rebuild() acquires the lock *before*
    # calling stop(), so a second rebuild blocks at the lock and depth never exceeds 1. Without the
    # lock the `await asyncio.sleep(0)` yield would let the two interleave (depth would reach 2).
    depth = {"cur": 0, "max": 0}
    orig_stop = holder.stop

    async def tracking_stop() -> None:
        depth["cur"] += 1
        depth["max"] = max(depth["max"], depth["cur"])
        await orig_stop()
        await asyncio.sleep(0)
        depth["cur"] -= 1

    holder.stop = tracking_stop  # type: ignore[method-assign]

    async def scenario() -> None:
        await asyncio.gather(holder.rebuild(_settings("b")), holder.rebuild(_settings("c")))

    asyncio.run(scenario())

    assert depth["max"] == 1  # the lock kept the two rebuilds from interleaving their teardown
    assert holder.radio.tag in {"b", "c"}  # one of them won; the pipeline is consistent
    assert holder.rx_pump is not None


def test_rebuild_reconstructs_the_controller_against_the_new_radio():
    built: list[_FakeController] = []

    def controller_factory(settings: Settings, radio: Radio) -> _FakeController:
        controller = _FakeController(radio)
        built.append(controller)
        return controller

    holder = _make_holder(
        _CloseSpyRadio("a"),
        radio_factory=_radio_factory({}),
        controller_factory=controller_factory,
        scan_settings=_settings("a"),
    )
    holder.start()  # first start builds a controller against radio "a" via the factory
    assert len(built) == 1 and built[0].radio.tag == "a"

    asyncio.run(holder.rebuild(_settings("b")))

    # stop() reaped the old controller; start() rebuilt a fresh one bound to the NEW radio.
    assert built[0].closed is True
    assert len(built) == 2 and built[1].radio.tag == "b"
    assert holder.controller is built[1]
