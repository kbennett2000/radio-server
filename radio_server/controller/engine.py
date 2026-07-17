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
all below ``api`` — plus the pure-data ``link.entries`` module (ADR 0042), and emits progress
through an injected ``on_event`` callback, never importing ``EventHub``. The Mumble link *action*
crosses the boundary the same way: the controller fires the rebindable ``on_link`` callback and
never touches the ``LinkManager``, so the dependency arrow stays ``api → controller`` with no
cycle, exactly as the scan engine keeps ``api → scan``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..audio import (
    DECODE_MODE_BUFFERED,
    DECODE_MODE_NATIVE,
    AudioFrame,
    BufferedDtmfInput,
    DtmfDecoder,
    DtmfFramer,
    GoertzelStream,
    MultimonDtmfDecoder,
    MultimonStream,
    StreamingDtmfInput,
    dtmf_window_bytes,
    load_dtmf_buffer_seconds,
    load_dtmf_decode_mode,
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
from ..link.entries import MumbleEntry, validate_link_digits

if TYPE_CHECKING:
    from ..config import Settings
from ..scan import ScanEngine
from ..services import (
    Dispatcher,
    ServiceContext,
    StationId,
    TtsEngine,
    build_id_encoder,
    load_callsign,
    load_id_interval,
    load_id_mode,
    load_tts_voice,
)
from ..services.fetch import Fetcher
from ..services.plugin import (
    BUILTIN_IDS,
    ID_BUILTIN,
    ID_DIGIT,
    LOGOUT_BUILTIN,
    LOGOUT_DIGIT,
    PLUGINS,
    PluginBuildContext,
    ServicePlugin,
    build_registry,
    builtin_digits,
    resolve_bindings,
)
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
#: ``link`` (ADR 0042) records that a Mumble link combo was received (``{"entry": name | None}``);
#: the resulting connection transitions are the `LinkManager`'s own events, not the controller's.
CONTROLLER_EVENT_PHASES = ("auth_accepted", "auth_rejected", "command", "link")


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

#: Whether TOTP auth is enforced on the over-RF DTMF plane (ADR 0048). On by default (guardrail 4).
#: When off, any keyed DTMF entry dispatches directly with no code — an explicit operator opt-in to
#: un-gated access; automatic station ID (guardrail 5) still fires, and the web UI flags the state.
DEFAULT_TOTP_ENABLED = True

#: Spoken session-lifecycle confirmations. Configurable (`controller.*_announcement`) and editable in
#: the settings screen; these are the friendly defaults. An empty value falls back to the default (see
#: `coerce_str`); the controller simply omits a line whose rendered frame is None.
DEFAULT_LOGIN_ANNOUNCEMENT = "Welcome."
DEFAULT_TIMEOUT_ANNOUNCEMENT = "Session timed out."
DEFAULT_LOGOUT_ANNOUNCEMENT = "Goodbye."

#: Spoken Mumble-link confirmations (ADR 0042). The connect one is a template: ``{name}`` becomes
#: the entry's name with underscores spoken as spaces ("mumble_demo" -> "mumble demo"). Blank =
#: silent, like the session announcements above.
DEFAULT_LINK_ANNOUNCEMENT = "Linked to {name}."
DEFAULT_LINK_OFF_ANNOUNCEMENT = "Link off."

RADIO_CONTROLLER_POLL_ENV_VAR = "RADIO_CONTROLLER_POLL"
RADIO_SESSION_TIMEOUT_ENV_VAR = "RADIO_SESSION_TIMEOUT"


def load_controller_poll(settings: Settings) -> float:
    """Return the loop poll cadence in seconds (`controller.poll`)."""
    return settings.get("controller.poll")


def load_session_timeout(settings: Settings) -> float:
    """Return the session inactivity timeout in seconds (`controller.session_timeout`)."""
    return settings.get("controller.session_timeout")


def load_totp_enabled(settings: Settings) -> bool:
    """Return whether TOTP auth is enforced on the DTMF plane (`auth.totp_enabled`)."""
    return settings.get("auth.totp_enabled")


def load_login_announcement(settings: Settings) -> str:
    """Return the spoken login confirmation (`controller.login_announcement`)."""
    return settings.get("controller.login_announcement")


def load_timeout_announcement(settings: Settings) -> str:
    """Return the spoken idle-timeout confirmation (`controller.timeout_announcement`)."""
    return settings.get("controller.timeout_announcement")


def load_logout_announcement(settings: Settings) -> str:
    """Return the spoken force-logout confirmation (`controller.logout_announcement`)."""
    return settings.get("controller.logout_announcement")


def load_link_announcement(settings: Settings) -> str:
    """Return the spoken link-connect confirmation template (`mumble.link_announcement`)."""
    return settings.get("mumble.link_announcement")


def load_link_off_announcement(settings: Settings) -> str:
    """Return the spoken link-disconnect confirmation (`mumble.link_off_announcement`)."""
    return settings.get("mumble.link_off_announcement")


class Controller:
    """The pure, clock-injected loop core: one :meth:`step` is one iteration of the radio loop.

    Holds the composed stack — a `BufferedDtmfInput`, an `AuthGate` + its `Session`, and the shared
    `StationId` the gate's dispatcher also transmits through (one source of ID state) — plus an
    optional attached `ScanEngine`. Emits :class:`ControllerEvent`s through ``on_event`` (a public,
    reassignable attribute: unit tests pass a list recorder; the API rebinds it to a hub adapter
    once its `EventHub` exists).
    """

    def __init__(
        self,
        radio: Radio,
        dtmf: BufferedDtmfInput | StreamingDtmfInput,
        gate: AuthGate,
        session: Session,
        station: StationId,
        *,
        on_event: Callable[[ControllerEvent], None] | None = None,
        scan: ScanEngine | None = None,
        clock: Clock | None = None,
        service_catalog: list[dict[str, str]] | None = None,
        dispatcher: Dispatcher | None = None,
        login_audio: AudioFrame | None = None,
        timeout_audio: AudioFrame | None = None,
        logout_audio: AudioFrame | None = None,
        id_digits: frozenset[str] = frozenset({ID_DIGIT}),
        logout_digits: frozenset[str] = frozenset({LOGOUT_DIGIT}),
        link_digits: Mapping[str, str] | None = None,
        link_off_digits: frozenset[str] = frozenset(),
        link_audio: Mapping[str, AudioFrame] | None = None,
        link_off_audio: AudioFrame | None = None,
        totp_enforced: bool = True,
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
        #: The registered DTMF services (``{digit, name, description}``), for the `/services` endpoint
        #: and the web UI reference panel. Fixed at construction (services don't change at runtime).
        self._service_catalog = service_catalog or []
        #: The service dispatcher, held directly so :meth:`trigger` can run a service from the API
        #: (web-UI button) without decoding DTMF audio or going through the TOTP gate. The gate holds
        #: the same dispatcher for the over-the-air path.
        self._dispatcher = dispatcher
        #: Pre-rendered session-lifecycle announcement frames (None → that line is silent). Rendered
        #: once at construction from the configured text, so the controller needs no `TtsEngine` and
        #: the frames stay byte-assertable in tests.
        self._login_audio = login_audio
        self._timeout_audio = timeout_audio
        self._logout_audio = logout_audio
        #: The digits the operator mapped to the two controller built-ins (ADR 0034). Any digit — the
        #: default ``4``/``99`` or a remap — that lands here runs the built-in instead of a service.
        self._id_digits = id_digits
        self._logout_digits = logout_digits
        #: The Mumble link built-ins (ADR 0042): combo → entry slug, the disconnect combo(s), and
        #: pre-rendered spoken confirmations ("linked to <name>" / "link off"). The action itself
        #: crosses to the `LinkManager` through `on_link` — the controller only announces + emits.
        self._link_digits = dict(link_digits or {})
        self._link_off_digits = link_off_digits
        self._link_audio = dict(link_audio or {})
        self._link_off_audio = link_off_audio
        #: Public, rebindable (the `on_event` pattern): the API points this at the `LinkManager`
        #: (scheduling the async connect/disconnect on the loop). ``None`` = no link configured.
        self.on_link: Callable[[str | None], None] | None = None
        #: Public, rebindable (the `on_event` pattern): fired once per decoded DTMF digit, as
        #: heard — before framing/auth. The API points it at the Mumble DTMF mute (ADR 0045) so
        #: control tones can be squelched out of the link feed. ``None`` = nobody listening.
        self.on_digit: Callable[[str], None] | None = None
        #: Whether the over-RF TOTP auth plane is enforced (ADR 0048). Mirrors the gate's ``enforce``
        #: so the API can report the *running* state (`/auth/totp`) honestly under restart-to-apply.
        self.totp_enforced = totp_enforced
        # Interpose on the decoder's per-key hook (post-dedup) rather than taking a constructor
        # param: `doctor --dtmf` keeps using the raw inputs' own `on_digit` untouched.
        dtmf.on_digit = self._forward_digit

    @property
    def session(self) -> Session:
        return self._session

    @property
    def service_catalog(self) -> list[dict[str, str]]:
        """The registered DTMF services as ``{digit, name, description}`` dicts."""
        return self._service_catalog

    def close(self) -> None:
        """Release loop-owned resources (idempotent).

        Reaps the DTMF decoder's persistent ``multimon-ng`` process when in streaming mode (ADR 0038);
        a no-op for the buffered decoder, which owns no long-running process. Called from the RX pump's
        teardown and the API's lifespan shutdown so no orphan process lingers.
        """
        close = getattr(self._dtmf, "close", None)
        if callable(close):
            close()

    def _emit(self, phase: str, data: dict | None = None) -> None:
        if self.on_event is not None:
            self.on_event(ControllerEvent(phase=phase, data=data))

    def _forward_digit(self, digit: str) -> None:
        """Relay a decoded digit to :attr:`on_digit`, guarded — a listener fault (e.g. the Mumble
        mute gate) must never break DTMF decode, the control plane."""
        if self.on_digit is None:
            return
        try:
            self.on_digit(digit)
        except Exception:
            pass

    def _close_session(self, now: float, audio: AudioFrame | None) -> bool:
        """Speak an optional closing announcement, then send the Part-97 closing ID; emit the event.

        Shared by the idle-timeout path and the ``99#`` force-logout. The announcement over marks the
        station as having transmitted, so `sign_off` reliably keys the closing ID. Returns whether an
        ID was actually sent.
        """
        if audio is not None:
            self._station.transmit(audio, now)
        sent = self._station.sign_off(now)
        self._emit("session_close", {"signed_off": sent})
        return sent

    def _run_command(self, digit: str, now: float) -> bool:
        """Run a reserved built-in command; return ``True`` iff ``digit`` was one.

        Shared by the over-the-air path (`step`) and the API trigger (`trigger`). The station-ID
        digit (``01#`` by default) plays the station ID unconditionally; the logout digit (``99#``
        by default) force-logs-out an authenticated session with a voice confirmation; a Mumble
        link combo connects its entry and the disconnect combo (``98#`` by default) drops the link,
        both with spoken confirmations (ADR 0042). The digits come from the operator's keypad map
        (ADR 0034) and entry list. Any other digit is not a built-in (returns ``False``).
        """
        if digit in self._id_digits:
            self._station.identify(now)
            self._emit("id", {"callsign": self._station.callsign, "mode": self._station.mode})
            return True
        if digit in self._logout_digits:
            if self._gate.logout(self._session):
                self._close_session(now, self._logout_audio)
            return True
        if digit in self._link_digits:
            # A Mumble link combo (ADR 0042): fire the manager through the callback (the connect is
            # async and non-blocking — the announcement confirms the *command*, not the connection),
            # speak the confirmation through the station (auto-ID'd), and record it. Keyed by the
            # entry slug (ADR 0052).
            slug = self._link_digits[digit]
            if self.on_link is not None:
                self.on_link(slug)
            audio = self._link_audio.get(slug)
            if audio is not None:
                self._station.transmit(audio, now)
            self._emit("link", {"entry": slug})
            return True
        if digit in self._link_off_digits:
            if self.on_link is not None:
                self.on_link(None)
            if self._link_off_audio is not None:
                self._station.transmit(self._link_off_audio, now)
            self._emit("link", {"entry": None})
            return True
        return False

    def trigger(self, digit: str, now: float | None = None) -> dict:
        """Run a service or built-in command by digit from the API (web-UI button), and transmit.

        The control-operator seam: it bypasses DTMF decode and the TOTP gate (the LAN token is the
        operator's credential, like `/ptt`), so a browser button keys the radio directly. Built-ins
        (`4#`/`99#`) run via :meth:`_run_command`; a registered digit renders + transmits through the
        shared `Dispatcher` (auto-ID'd by `StationId`). Returns a small result dict for the endpoint.
        """
        if now is None:
            now = self._clock()
        if self._run_command(digit, now):
            return {"digit": digit, "builtin": True, "transmitted": True}
        if self._dispatcher is None:
            return {"digit": digit, "builtin": False, "service": None, "transmitted": False}
        result = self._dispatcher(digit, self._session)
        if result.transmitted:
            self._emit("command", {"service": result.service})
        return {
            "digit": digit,
            "builtin": False,
            "service": result.service,
            "transmitted": result.transmitted,
        }

    def open_session(self, now: float | None = None) -> dict:
        """Open the over-the-air session from the API (the web UI's OTA-code chip), and transmit.

        The control-operator seam, like :meth:`trigger`: the LAN token is the operator's
        credential, so this bypasses the TOTP gate entirely — no code is verified and **none is
        burned**, so an RF caller's code stays valid in its window (ADR 0046). On-air behavior is
        identical to a DTMF-accepted auth (the `step` ACCEPTED branch): arm the station ID, speak
        the login confirmation, emit ``auth_accepted`` + ``session_open``. Calling it on an
        already-open session just refreshes the inactivity clock.
        """
        if now is None:
            now = self._clock()
        opened = self._gate.open(self._session, now)
        if opened:
            self._station.begin_session(now)
            if self._login_audio is not None:
                self._station.transmit(self._login_audio, now)
            self._emit("auth_accepted")
            self._emit("session_open")
        return {"opened": opened, "session_open": True}

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

        signed_off = False
        for entry in entries:
            if entry in self._link_off_digits:
                # Dropping the link is the one un-gated RF command (ADR 0043): purely
                # de-escalating, so it must work after the session times out mid-net. Bypassing
                # the gate also means no activity stamp — a disconnect never extends a session.
                self._run_command(entry, now)
                outcomes.append(Outcome(OutcomeKind.COMMAND))
                continue
            was_authenticated = self._session.authenticated
            outcome = self._gate.on_dtmf(entry, self._session, now)
            outcomes.append(outcome)
            if (
                not was_authenticated
                and self._session.authenticated
                and outcome.kind is OutcomeKind.COMMAND
            ):
                # Auth disabled (ADR 0048): the gate implicitly opened the session on this command
                # (never an ACCEPTED). Arm the station ID and record the open so ID coverage stays
                # intact — periodic-ID net runs, idle timeout signs off — but speak no login line
                # (there was no login). The command itself is then dispatched by the COMMAND branch.
                self._station.begin_session(now)
                self._emit("session_open")
            if outcome.kind is OutcomeKind.ACCEPTED:
                # A session just opened: arm the station ID for this session, then speak the login
                # confirmation (the reset makes the ID due, so the over is `<id> + "Welcome."`). Emit
                # the auth outcome (accepted) *and* the session_open lifecycle event — distinct records.
                self._station.begin_session(now)
                if self._login_audio is not None:
                    self._station.transmit(self._login_audio, now)
                self._emit("auth_accepted")
                self._emit("session_open")
            elif outcome.kind is OutcomeKind.REJECTED:
                # A failed auth: record *that* it failed, never the code (ADR 0019 — no data).
                self._emit("auth_rejected")
            elif outcome.kind is OutcomeKind.COMMAND:
                # A built-in command (4#/99#) is handled by the controller itself; otherwise a
                # dispatched service — record which one, only when it actually transmitted (a registry
                # miss is a graceful no-op and is not a dispatch).
                result = outcome.detail
                if self._run_command(result.digits if result is not None else "", now):
                    signed_off = signed_off or result.digits in self._logout_digits
                elif result is not None and result.transmitted:
                    self._emit("command", {"service": result.service})

        id_sent = False
        if self._gate.expire_if_idle(self._session, now):
            # Idle past the timeout with no further digits: announce and sign off (guardrail 5).
            self._close_session(now, self._timeout_audio)
            signed_off = True
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
    totp_secret: str | None,
    decoder: DtmfDecoder | None = None,
    tts: TtsEngine | None = None,
    clock: Clock | None = None,
    dedup: bool = True,
    fetcher: Fetcher | None = None,
    service_bindings: Mapping[str, str] | None = None,
    mumble_entries: tuple[MumbleEntry, ...] = (),
    plugins: tuple[ServicePlugin, ...] = PLUGINS,
) -> Controller:
    """Compose the full controller stack from ``settings`` — the production root.

    Mirrors `build_scan_engine` / `build_id_encoder`: config comes from the resolved `Settings`. The
    TOTP secret is passed in explicitly (it is a secret, not a schema setting — it lives on the
    `radio_server.config.secrets` channel, never in ``radio.toml``). It may be ``None`` when
    ``auth.totp_enabled`` is off (ADR 0048): the gate then runs un-enforced and dispatches every
    keyed entry directly, so DTMF still works with no secret enrolled. The one `StationId` built here
    is shared with the `Dispatcher`, so every service transmission and the lifecycle IDs draw on the
    same ID state. ``decoder`` and ``tts`` are injectable so tests wire a `FakeDtmfDecoder` +
    `StubTts` with no multimon/piper; production defaults to `MultimonDtmfDecoder` + `PiperTts`.
    Fails loud (via `load_callsign` / `load_tts_voice`) when a required setting is unset rather than
    serving un-ID'd.

    DTMF is decoded through a `StreamingDtmfInput` by default (ADR 0038): the continuous RX stream is
    piped through one persistent `multimon-ng` process, so repeated-digit codes like ``99#`` decode
    reliably. Setting ``dtmf.decode_mode=buffered`` selects the older `BufferedDtmfInput` (ADR 0030)
    fixed-window accumulator as an in-field fallback. An injected ``decoder`` (a `DtmfDecoder`, as the
    controller tests wire a `FakeDtmfDecoder`) forces the buffered path with that decoder; ``dedup``
    (default on, as production needs) is that test seam — a decoder double returning whole pre-formed
    entries per call passes ``dedup=False`` so held-tone collapsing doesn't fold repeated digits.

    ``service_bindings`` is the operator's digit→service map (the ``[services]`` table, loaded via
    `load_service_bindings`); ``None`` falls back to the default keypad layout
    (`services.plugin.DEFAULT_BINDINGS`). It is validated here (`resolve_bindings`), and a bad entry —
    unknown service, reserved digit — fails loud. ``plugins`` is the full plugin set to bind against:
    the in-tree `PLUGINS` by default; `build_app` passes it extended with the operator's
    ``local_services/`` discoveries (ADR 0051).
    """
    if tts is None:
        tts = PiperTts(load_tts_voice(settings))

    encoder = build_id_encoder(settings, tts=tts)
    station = StationId(
        radio,
        encoder,
        load_callsign(settings),
        interval=load_id_interval(settings),
        clock=clock,
        mode=load_id_mode(settings),
    )

    # Voice services are pluggable (ADR 0034): each `ServicePlugin` in ``plugins`` self-describes
    # its id, its enable gate, and its factory. The operator's ``[services]`` table — or
    # `DEFAULT_BINDINGS` when unset — maps a DTMF digit to each; a bound-but-disabled service is a
    # graceful miss, exactly as before. The one shared LAN `Fetcher` is built lazily inside
    # `PluginBuildContext` on the first fetch-backed service (ADR 0033); an injected ``fetcher``
    # (tests) is used as-is.
    bindings = resolve_bindings(service_bindings, {plugin.id for plugin in plugins})

    # Mumble link combos (ADR 0042): validated against the resolved keypad here — the one place
    # both channels are known — and pre-rendered like the lifecycle announcements below. The
    # disconnect combo is only live when entries exist (no entries = no link built-ins at all).
    # Confirmations are configurable (`mumble.link_announcement`, a `{name}` template, and
    # `mumble.link_off_announcement`); blank = silent, the announcement convention. Entries key by
    # their derived slug (ADR 0052); the spoken/display text is the free-text name (with any
    # legacy-slug underscores read as spaces).
    link_digits = {entry.dtmf: entry.slug for entry in mumble_entries if entry.dtmf}
    disconnect_dtmf = str(settings.get("mumble.disconnect_dtmf"))
    if mumble_entries:
        validate_link_digits(mumble_entries, disconnect_dtmf, bindings)
    link_off_digits = frozenset({disconnect_dtmf}) if mumble_entries else frozenset()
    link_template = load_link_announcement(settings)
    link_audio = {
        entry.slug: tts.render(link_template.format(name=entry.name.replace("_", " ")))
        for entry in mumble_entries
        if entry.dtmf and link_template
    }
    link_off_text = load_link_off_announcement(settings)
    link_off_audio = tts.render(link_off_text) if mumble_entries and link_off_text else None

    registry = build_registry(plugins, bindings, PluginBuildContext(settings, fetcher))
    service_clock: Clock = clock if clock is not None else _wall_clock()
    ctx = ServiceContext(clock=service_clock, tts=tts)
    dispatcher = Dispatcher(station, ctx, registry)

    # The built-in commands (station-id / logout) aren't `ServiceRegistry` services — the controller
    # runs them — but they share the operator's keypad map, so their digit is assignable like a
    # service's (ADR 0034). Resolve which digit(s) each sits on, and list them in the catalog so
    # `/services` and the web UI show them alongside the voice services.
    id_digits = builtin_digits(bindings, ID_BUILTIN)
    logout_digits = builtin_digits(bindings, LOGOUT_BUILTIN)
    builtin_entries = [
        {"digit": digit, "name": target_id, "description": BUILTIN_IDS[target_id]}
        for digit, target_id in bindings.items()
        if target_id in BUILTIN_IDS
    ]
    # The Mumble link combos belong on the same keypad reference (ADR 0042): one row per entry
    # with a combo, plus the disconnect combo. Their digits fire through the same trigger seam as
    # every service, so the web UI's Transmit buttons work on them unchanged.
    link_entries = [
        {
            "digit": entry.dtmf,
            "name": f"link:{entry.slug}",
            "description": f"Connect the Mumble link to {entry.name.replace('_', ' ')}",
        }
        for entry in mumble_entries
        if entry.dtmf
    ]
    if mumble_entries:
        link_entries.append(
            {
                "digit": disconnect_dtmf,
                "name": "link-off",
                "description": "Disconnect the Mumble link",
            }
        )
    catalog = sorted(
        [*registry.catalog(), *builtin_entries, *link_entries],
        key=lambda entry: entry["digit"],
    )

    # Pre-render the session-lifecycle announcements once (a blank setting → default via `coerce_str`).
    def _render(text: str) -> AudioFrame | None:
        return tts.render(text) if text else None

    login_audio = _render(load_login_announcement(settings))
    timeout_audio = _render(load_timeout_announcement(settings))
    logout_audio = _render(load_logout_announcement(settings))

    # TOTP auth is enforced by default (ADR 0048). When disabled, the gate needs no verifier and the
    # secret may be absent entirely (`build_app` builds the controller anyway so DTMF still works).
    totp_enabled = load_totp_enabled(settings)
    verifier = TotpVerifier(totp_secret, clock=clock) if totp_secret else None
    gate = AuthGate(
        verifier,
        timeout=load_session_timeout(settings),
        clock=clock,
        dispatch=dispatcher,
        enforce=totp_enabled,
    )

    framer = DtmfFramer(timeout=load_dtmf_timeout(settings), clock=clock)
    dtmf: BufferedDtmfInput | StreamingDtmfInput
    decode_mode = load_dtmf_decode_mode(settings)
    if decoder is not None or decode_mode == DECODE_MODE_BUFFERED:
        # Injected-decoder test seam, or `dtmf.decode_mode=buffered`: the ADR 0030 fixed-window
        # accumulator over a per-decode `MultimonDtmfDecoder` (or the injected double).
        dtmf = BufferedDtmfInput(
            decoder if decoder is not None else MultimonDtmfDecoder(load_multimon_bin(settings)),
            framer,
            window_bytes=dtmf_window_bytes(load_dtmf_buffer_seconds(settings)),
            dedup=dedup,
        )
    elif decode_mode == DECODE_MODE_NATIVE:
        # In-process Goertzel decoder — no multimon-ng binary, works on native Windows (ADR 0054).
        dtmf = StreamingDtmfInput(GoertzelStream(), framer)
    else:
        # Default: stream the continuous RX through one persistent multimon process (ADR 0038).
        dtmf = StreamingDtmfInput(MultimonStream(load_multimon_bin(settings)), framer)

    return Controller(
        radio,
        dtmf,
        gate,
        Session(),
        station,
        clock=clock,
        service_catalog=catalog,
        dispatcher=dispatcher,
        login_audio=login_audio,
        timeout_audio=timeout_audio,
        logout_audio=logout_audio,
        id_digits=id_digits,
        logout_digits=logout_digits,
        link_digits=link_digits,
        link_off_digits=link_off_digits,
        link_audio=link_audio,
        link_off_audio=link_off_audio,
        totp_enforced=totp_enabled,
    )


def _wall_clock() -> Clock:
    import time

    return time.time
