"""The radio holder — one owner of the active radio and its pipeline lifecycle (ADR 0073).

The app was single-radio to the bone: `build_app` built one `radio` and threaded that instance into
the `RxPump`, the `ScanRunner`, the DTMF `Controller`, and every `TxSession`, while the lifespan tore
those pieces down inline. Live backend switching is impossible until **one object owns the radio + the
radio-bound pipeline and can stop and restart them**. `RadioHolder` is that seam.

This cycle it is pure indirection — no switching, no second backend, no config. `create_app` builds the
stable, radio-independent collaborators (the hubs, the arbiter, the gate, the recorder, the controller)
exactly as before and hands them to the holder; the holder builds the radio-*bound* pieces (`RxPump`,
`ScanRunner`) against `self.radio` in :meth:`start`, and tears the whole pipeline down cleanly and
idempotently in :meth:`stop`. The swap cycle then reduces to ``await holder.stop(); <new radio>;
holder.start()`` — the shape :meth:`stop` is deliberately designed for.

`build_radio` lives here (the composition root), not in `backends/factory.py`: the backend classes are
deliberately Settings-free (the composition root owns the settings→kwargs mapping), and the switch
carries config-layer squelch validation. `api/` is the top import layer, so reading Settings/activity
here introduces no cycle (`config/spec.py` documents that config must not import `api`).
"""

from __future__ import annotations

from ..arbiter import RadioArbiter
from ..backends import Radio, create_radio
from ..config import Settings
from ..controller import Controller
from ..rx import AudioHub, RxActivityGate, RxPump, RxRecorder, null_recorder, pass_through_gate
from ..scan import ScanEvent, ScanRunner, build_scan_engine
from .backend_config import backend_kwargs, validate_backend_config
from .events import Event, EventHub


def build_radio(settings: Settings) -> Radio:
    """Construct the active radio from resolved ``settings`` — the backend switch (ADR 0073/0074).

    Validates the active backend's block (the squelch guards — construction never does them) and
    builds it via the extracted `backend_kwargs` mapping. The active backend's *construction* checks
    (e.g. the kv4p frequency band, HELLO-aware) run inside the backend constructor as before, so
    `validate_backend_config` is called with ``include_construction_checks=False`` here — its
    behaviour is byte-identical to the old inline switch. The mapping + guards live in
    `api/backend_config.py` (ADR 0074) so `validate_configured_backends`/`configured_backends` reuse
    them; `create_radio` is still looked up locally so the wiring test's monkeypatch is unchanged.
    Kept at the composition root (not `backends/factory.py`) because the backend classes stay pure DI
    objects (Settings-free) — the mapping is the composition root's job.
    """
    backend = settings.get("server.backend")
    validate_backend_config(settings, backend, include_construction_checks=False)
    return create_radio(backend, **backend_kwargs(settings, backend))


class RadioHolder:
    """Owns the active radio and the lifecycle of the pipeline pieces bound to it (ADR 0073).

    Constructed with the active ``radio`` plus the stable, radio-*independent* collaborators the
    pipeline binds against (the hubs, the arbiter, the gate, the recorder, the controller, and the scan
    config). :meth:`start` builds the radio-bound pieces (:class:`RxPump`, :class:`ScanRunner`) against
    :attr:`radio`; :meth:`stop` tears the whole pipeline down. The app reaches the active radio through
    :attr:`radio` — the one place that owns it.

    Naming: ``start``/``stop`` name the *holder's* lifecycle (the swap contract ``stop(); …; start()``),
    NOT a task. :meth:`start` starts no task — the pump is demand-started (`_acquire_rx`) and a scan is
    plan-started (`scan_runner.start(plan)`); it only *constructs* the pieces so those on-demand starts
    have something to drive.
    """

    def __init__(
        self,
        radio: Radio,
        *,
        hub: EventHub,
        audio_hub: AudioHub,
        arbiter: RadioArbiter,
        scan_settings: Settings,
        scan_poll: float,
        gate: RxActivityGate = pass_through_gate,
        recorder: RxRecorder = null_recorder,
        controller: Controller | None = None,
    ) -> None:
        self._radio = radio
        self._hub = hub
        self._audio_hub = audio_hub
        self._arbiter = arbiter
        self._scan_settings = scan_settings
        self._scan_poll = scan_poll
        self._gate = gate
        self._recorder = recorder
        self._controller = controller
        # Built in start(); None until then (and after a future teardown-and-rebuild).
        self.rx_pump: RxPump | None = None
        self.scan_runner: ScanRunner | None = None

    @classmethod
    def from_settings(cls, settings: Settings, **collaborators: object) -> "RadioHolder":
        """Build a holder whose radio comes straight from config — ``cls(build_radio(settings), ...)``.

        A convenience for the swap cycle (build a holder for a freshly-selected backend); ``create_app``
        uses the plain constructor because it already holds the injected ``radio``. ``collaborators`` are
        the same keyword args :meth:`__init__` takes.
        """
        return cls(build_radio(settings), **collaborators)  # type: ignore[arg-type]

    @property
    def radio(self) -> Radio:
        """The active radio — the single reference the app owns it through."""
        return self._radio

    @property
    def controller(self) -> Controller | None:
        """The DTMF controller bound to this radio (``None`` when none is wired)."""
        return self._controller

    def start(self) -> None:
        """Construct the radio-bound pipeline against :attr:`radio` (idempotent; starts no task).

        Builds the single-reader :class:`RxPump` and the :class:`ScanRunner`, wiring their hub-publish
        adapters here (both only need ``hub``, so they belong with the holder rather than scattered in
        ``create_app``). Idempotent: a second call is a no-op, so it never rebuilds a live pipeline.
        """
        if self.rx_pump is not None:
            return
        # The single capture reader (ADR 0031): reads receive() and fans each frame to the browser
        # hub/recorder AND — when a controller is configured — to controller.step for DTMF decode.
        self.rx_pump = RxPump(
            self._radio,
            self._audio_hub,
            gate=self._gate,
            arbiter=self._arbiter,
            recorder=self._recorder,
            controller=self._controller,
            on_activity=self._publish_rx_activity,
        )
        # The async scan runner (ADR 0028): the engine is built per scan via this factory, which closes
        # over the radio, the scan config, and the shared arbiter (so a TX key-up pauses the scan).
        self.scan_runner = ScanRunner(
            lambda plan, on_event: build_scan_engine(
                self._scan_settings,
                radio=self._radio,
                plan=plan,
                on_event=on_event,
                arbiter=self._arbiter,
            ),
            on_event=self._publish_scan,
            poll=self._scan_poll,
        )

    async def stop(self) -> None:
        """Tear the radio pipeline down — cleanly, idempotently, fail-safe (ADR 0073).

        Ordered as the proven lifespan teardown, with each step independently guarded so it is safe when
        a piece was never started (and safe as the first half of a swap): drop PTT if the arbiter holds
        the transmitter, stop a running scan, halt the pump, reap the controller's DTMF decoder, and
        close the radio device.
        """
        # Drop PTT if the app's half-duplex arbiter says we hold the transmitter — the closest thing to
        # an app-level keyed flag (a session mid-key at teardown/swap holds it), so an arbiter-holding
        # session can never leave the transmitter latched across a swap. Conditional, not unconditional:
        # a quiescent shutdown (arbiter idle) must NOT add a spurious `ptt(False)`, or it would change
        # the keying contract every clean teardown asserts. It can't cover the direct POST /ptt path,
        # which bypasses the arbiter (finding a) — that residual gap is why a future app-level
        # keyed-state owner is still worth having. Guarded so a dead device can't wedge the teardown.
        if self._arbiter.transmitting:
            try:
                self._radio.ptt(False)
            except Exception:
                pass
        if self.scan_runner is not None:
            await self.scan_runner.stop()
        if self.rx_pump is not None:
            await self.rx_pump.stop()
        # Reap the controller's DTMF decoder AFTER the pump has stopped feeding it (the persistent
        # multimon-ng process in streaming mode, ADR 0038). Idempotent; a no-op for the buffered decoder.
        if self._controller is not None:
            try:
                self._controller.close()
            except Exception:
                pass
        # close() is not on the Radio protocol (the V71 backend has none; finding b) — reach it
        # fail-safe. A no-op on MockRadio; releases the serial device on the real backends.
        close = getattr(self._radio, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass

    def _publish_rx_activity(self, active: bool) -> None:
        # Surface squelch open/close in the operating log (ADR 0031's gate is the only real RX-activity
        # signal on the audio-only Baofeng — status.busy is always False there).
        self._hub.publish(Event(type="rx", data={"active": active}))

    def _publish_scan(self, event: ScanEvent) -> None:
        # Adapt a scan-engine event to a "scan" event on the shared hub. Keeping the adapter with the
        # holder (not in the scan package) is what lets scan stay below the API with no cycle.
        self._hub.publish(
            Event(
                type="scan",
                data={
                    "phase": event.phase,
                    "frequency": event.frequency,
                    "channel": event.channel,
                },
            )
        )
