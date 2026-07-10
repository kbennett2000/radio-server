"""The controller loop: pump DTMF / auth / dispatch / scan / station-ID on a live receive().

Everything below the API operates on sound-card audio and is backend-agnostic, but until now
nothing *ran* those pieces together in real time. `DtmfInput.pump`, `AuthGate.on_dtmf`, the
`Dispatcher`, `StationId.begin_session`/`check`/`sign_off`, and `ScanEngine.tick` were each
exercised only in tests. This module is the missing driver.

The design mirrors the scan engine's two-surface split (ADR 0012):

- :meth:`Controller.step` is the **pure, clock-injected core** — one iteration of the loop. It
  decodes a received audio frame to DTMF entries, routes them through the auth gate (whose
  dispatch hook transmits any service audio, auto-ID'd via its `StationId`), drives the station
  ID off real session transitions, and ticks an attached scan. Every timing decision is made
  against an injected clock, so tests drive it with the `FakeClock` and there are no real sleeps.
- :class:`ControllerRunner` is the **thin async driver** — it loops `radio.receive()` →
  `step(clock(), audio)` on a poll cadence and holds no logic of its own.

Session → station-ID wiring is the point of this cycle. `AuthGate` demotes an idle session only
*lazily* (inside `on_dtmf`) and emits no open/close signal, so the controller detects the
transitions itself: an ``ACCEPTED`` outcome opens a session and arms the ID (`begin_session`); a
newly-surfaced :meth:`AuthGate.expire_if_idle` closes an idle one and signs off (`sign_off`); and
`StationId.check` forces the periodic ID when overdue mid-session (Part 97, guardrail 5).

Layering: this package imports only ``audio``, ``auth``, ``services``, ``scan``, ``backends`` —
all below ``api`` — and emits progress through an injected ``on_event`` callback, never importing
``EventHub``. So the dependency arrow stays ``api → controller`` with no cycle, exactly as the scan
engine keeps ``api → scan``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..audio import (
    AudioFrame,
    DtmfDecoder,
    DtmfFramer,
    DtmfInput,
    MultimonDtmfDecoder,
    load_dtmf_timeout,
    load_multimon_bin,
)
from ..auth import (
    AuthGate,
    Clock,
    Outcome,
    OutcomeKind,
    Session,
    TotpVerifier,
)
from ..backends import Radio

if TYPE_CHECKING:
    from ..config import Settings
from ..scan import ScanEngine
from ..services import (
    Dispatcher,
    ServiceContext,
    ServiceRegistry,
    StationId,
    TtsEngine,
    build_id_encoder,
    load_callsign,
    load_id_interval,
    load_id_mode,
    load_timezone,
    load_tts_voice,
)
from ..services import register as register_time_service
from ..services.tts import PiperTts

#: The session-lifecycle phases the controller emits through its ``on_event`` callback. The API
#: adapts each to an ``Event(type="session", data={"phase": ...})`` on the shared hub. The ``id``
#: phase now also carries ``callsign`` + ``mode`` so the ledger records what identified the station.
CONTROLLER_PHASES = ("session_open", "id", "session_close")

#: Non-session phases the controller also emits through the same ``on_event`` channel (ADR 0019).
#: The API adapter routes these to their own hub event types — ``auth_accepted``/``auth_rejected``
#: to ``Event(type="auth", data={"result": ...})`` and ``command`` to
#: ``Event(type="command", data={"service": ...})`` — so the auth/command ledger records, whose
#: mappers shipped dead in cycle 17, now write. An auth phase carries **no** ``data``: never a code.
CONTROLLER_EVENT_PHASES = ("auth_accepted", "auth_rejected", "command")


@dataclass(frozen=True)
class ControllerEvent:
    """A controller lifecycle event, emitted through the injected ``on_event`` callback.

    ``phase`` is one of :data:`CONTROLLER_PHASES`; ``data`` carries phase-specific extras (e.g.
    whether a closing ID was actually sent). The engine never imports ``EventHub`` — the API owns
    the adapter to :class:`~radio_server.api.events.Event`, keeping ``api → controller`` acyclic.
    """

    phase: str
    data: dict | None = None


@dataclass(frozen=True)
class StepResult:
    """What one :meth:`Controller.step` produced — the caller's handle on the iteration.

    ``entries`` are the DTMF entries that completed on this frame; ``outcomes`` the auth results
    for each. ``session_open`` is the session state *after* the step. ``id_sent`` is ``True`` when
    the periodic-ID safety net fired this step; ``signed_off`` when an inactivity close signed off;
    ``scanning`` when an attached scan was ticked.
    """

    entries: tuple[str, ...]
    outcomes: tuple[Outcome, ...]
    session_open: bool
    id_sent: bool
    signed_off: bool
    scanning: bool


# --- config (guardrail 1: marked defaults, verify against hardware) ------------------------

#: Seconds between ``receive()`` → ``step()`` iterations in the async driver. VERIFY AGAINST
#: HARDWARE (guardrail 1) — the real cadence is bounded by how long ``receive()`` blocks, the
#: audio chunk size, and loop timing, all empirical bring-up facts. The mock delivers audio
#: instantly, so this value does not affect the tested `step()` logic at all.
DEFAULT_CONTROLLER_POLL = 0.5
#: Inactivity timeout (seconds) before an authenticated session is closed and signed off. An
#: operator preference (guardrail 4 keeps sessions short); feeds both ``AuthGate(timeout=...)`` and
#: the controller's idle detection, so there is one source of truth.
DEFAULT_SESSION_TIMEOUT = 300.0

RADIO_CONTROLLER_POLL_ENV_VAR = "RADIO_CONTROLLER_POLL"
RADIO_SESSION_TIMEOUT_ENV_VAR = "RADIO_SESSION_TIMEOUT"


def load_controller_poll(settings: Settings) -> float:
    """Return the loop poll cadence in seconds (`controller.poll`)."""
    return settings.get("controller.poll")


def load_session_timeout(settings: Settings) -> float:
    """Return the session inactivity timeout in seconds (`controller.session_timeout`)."""
    return settings.get("controller.session_timeout")


class Controller:
    """The pure, clock-injected loop core: one :meth:`step` is one iteration of the radio loop.

    Holds the composed stack — a `DtmfInput`, an `AuthGate` + its `Session`, and the shared
    `StationId` the gate's dispatcher also transmits through (one source of ID state) — plus an
    optional attached `ScanEngine`. Emits :class:`ControllerEvent`s through ``on_event`` (a public,
    reassignable attribute: unit tests pass a list recorder; the API rebinds it to a hub adapter
    once its `EventHub` exists).
    """

    def __init__(
        self,
        radio: Radio,
        dtmf: DtmfInput,
        gate: AuthGate,
        session: Session,
        station: StationId,
        *,
        on_event: Callable[[ControllerEvent], None] | None = None,
        scan: ScanEngine | None = None,
        clock: Clock | None = None,
    ) -> None:
        if clock is None:
            import time

            clock = time.time
        self._radio = radio
        self._dtmf = dtmf
        self._gate = gate
        self._station = station
        self._clock = clock
        #: Public so the API can rebind it to its hub adapter after construction.
        self.on_event = on_event
        #: Public so an active scan can be attached / cleared between steps.
        self.scan = scan
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    def _emit(self, phase: str, data: dict | None = None) -> None:
        if self.on_event is not None:
            self.on_event(ControllerEvent(phase=phase, data=data))

    def step(
        self, now: float | None = None, rx_audio: AudioFrame | None = None
    ) -> StepResult:
        """Advance the loop one iteration against the clock; returns a :class:`StepResult`.

        The order is load-bearing: entries are routed first (an ``ACCEPTED`` opens+arms the
        session; a ``COMMAND`` transmits via the dispatcher's `StationId`, auto-ID'd), then an idle
        session is closed+signed off, then the periodic ID safety net fires, then any attached scan
        is ticked. A session opened *this* step is never idle (its `last_activity` is ``now``), so
        the close check cannot fire a false sign-off in the same iteration.
        """
        if now is None:
            now = self._clock()

        entries: list[str] = []
        outcomes: list[Outcome] = []
        if rx_audio is not None:
            entries = self._dtmf.pump(rx_audio, now)

        for entry in entries:
            outcome = self._gate.on_dtmf(entry, self._session, now)
            outcomes.append(outcome)
            if outcome.kind is OutcomeKind.ACCEPTED:
                # A session just opened: arm the station ID for this session. Emit the auth outcome
                # (accepted) *and* the session_open lifecycle event — distinct ledger records.
                self._station.begin_session(now)
                self._emit("auth_accepted")
                self._emit("session_open")
            elif outcome.kind is OutcomeKind.REJECTED:
                # A failed auth: record *that* it failed, never the code (ADR 0019 — no data).
                self._emit("auth_rejected")
            elif outcome.kind is OutcomeKind.COMMAND:
                # A dispatched service: record which one, only when it actually transmitted (a
                # registry miss is a graceful no-op and is not a dispatch).
                result = outcome.detail
                if result is not None and result.transmitted:
                    self._emit("command", {"service": result.service})

        signed_off = False
        id_sent = False
        if self._gate.expire_if_idle(self._session, now):
            # Idle past the timeout with no further digits: close it and sign off (guardrail 5).
            sent = self._station.sign_off(now)
            signed_off = True
            self._emit("session_close", {"signed_off": sent})
        elif self._session.authenticated:
            # Periodic-ID safety net: force an ID-only over if overdue mid-session.
            id_sent = self._station.check(now)
            if id_sent:
                self._emit(
                    "id",
                    {"callsign": self._station.callsign, "mode": self._station.mode},
                )

        scanning = False
        if self.scan is not None:
            self.scan.tick(now)
            scanning = True

        return StepResult(
            entries=tuple(entries),
            outcomes=tuple(outcomes),
            session_open=self._session.authenticated,
            id_sent=id_sent,
            signed_off=signed_off,
            scanning=scanning,
        )


class ControllerRunner:
    """The thin async driver: loop ``receive()`` → ``step()`` on a poll cadence, nothing more.

    Deliberately holds no logic that isn't exercised through :meth:`Controller.step` — it is a
    shell so the tested core runs live. `run()` is cooperatively cancellable via `stop()`.
    """

    def __init__(
        self,
        radio: Radio,
        controller: Controller,
        *,
        clock: Clock | None = None,
        poll: float = DEFAULT_CONTROLLER_POLL,
    ) -> None:
        if clock is None:
            import time

            clock = time.time
        self._radio = radio
        self._controller = controller
        self._clock = clock
        self._poll = poll
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    async def run(self) -> None:
        """Pump the controller until :meth:`stop` is called.

        Guardrail 1: on real hardware ``receive()`` blocks; running it directly in the event loop
        (rather than a thread executor) is a bring-up decision, not settled here. The mock returns
        instantly, so this loop is a faithful stand-in for the software tower.
        """
        import asyncio

        self._running = True
        try:
            while self._running:
                audio = self._radio.receive()
                self._controller.step(self._clock(), audio)
                await asyncio.sleep(self._poll)
        finally:
            self._running = False

    def stop(self) -> None:
        self._running = False


def build_controller(
    settings: Settings,
    *,
    radio: Radio,
    totp_secret: str,
    decoder: DtmfDecoder | None = None,
    tts: TtsEngine | None = None,
    clock: Clock | None = None,
) -> Controller:
    """Compose the full controller stack from ``settings`` — the production root.

    Mirrors `build_scan_engine` / `build_id_encoder`: config comes from the resolved `Settings`. The
    TOTP secret is passed in explicitly (it is a secret, not a schema setting — it lives on the
    `radio_server.config.secrets` channel, never in ``radio.toml``). The one `StationId` built here
    is shared with the `Dispatcher`, so every service transmission and the lifecycle IDs draw on the
    same ID state. ``decoder`` and ``tts`` are injectable so tests wire a `FakeDtmfDecoder` +
    `StubTts` with no multimon/piper; production defaults to `MultimonDtmfDecoder` + `PiperTts`.
    Fails loud (via `load_callsign` / `load_tts_voice`) when a required setting is unset rather than
    serving un-ID'd.
    """
    if tts is None:
        tts = PiperTts(load_tts_voice(settings))
    if decoder is None:
        decoder = MultimonDtmfDecoder(load_multimon_bin(settings))

    encoder = build_id_encoder(settings, tts=tts)
    station = StationId(
        radio,
        encoder,
        load_callsign(settings),
        interval=load_id_interval(settings),
        clock=clock,
        mode=load_id_mode(settings),
    )

    registry = ServiceRegistry()
    register_time_service(registry, load_timezone(settings))
    service_clock: Clock = clock if clock is not None else _wall_clock()
    ctx = ServiceContext(clock=service_clock, tts=tts)
    dispatcher = Dispatcher(station, ctx, registry)

    verifier = TotpVerifier(totp_secret, clock=clock)
    gate = AuthGate(
        verifier,
        timeout=load_session_timeout(settings),
        clock=clock,
        dispatch=dispatcher,
    )

    framer = DtmfFramer(timeout=load_dtmf_timeout(settings), clock=clock)
    dtmf = DtmfInput(decoder, framer)

    return Controller(radio, dtmf, gate, Session(), station, clock=clock)


def _wall_clock() -> Clock:
    import time

    return time.time
