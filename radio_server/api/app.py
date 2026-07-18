"""The FastAPI application: REST + WebSocket over an injected ``Radio`` (ADR 0011).

The app is a thin, honest HTTP surface over the existing backend contract. The load-bearing
piece is the **capability split (guardrail 3)**: CAT endpoints check
``Capability`` membership before dispatching, and on an audio-only backend return
``501 Not Implemented`` with the *specific missing capability named in the body* — never a
silent no-op. The web UI keys its control-greying on that named field.

``create_app(radio, *, api_token)`` is the dependency-injection seam the tests drive against
``MockRadio``; ``build_app(env)`` is the top-level composition root that wires the environment
to a running app (the project's first). No server binds here — that is uvicorn's job at the
real entrypoint.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..activity import SquelchMode, build_rx_gate, load_squelch_mode
from ..arbiter import RadioArbiter
from ..audio import CANONICAL_FORMAT, AudioFormatMismatch, AudioFrame
from ..auth import Session, TotpVerifier
from ..backends import Capability, Radio, UnsupportedCapability
from ..controller import (
    Controller,
    ControllerEvent,
    ControllerRunner,
    build_controller,
    load_fixed_code_enabled,
    load_totp_enabled,
)
from ..services import (
    StreamingId,
    build_id_encoder,
    discover_local_plugins,
    load_callsign,
    load_id_interval,
    load_id_mode,
)
from ..services.plugin import PLUGINS, ServicePlugin
from ..eventlog import EventLog, JsonlSink, load_log_path
from ..recording import Recorder, build_recorder, build_tx_recorder, load_record_enabled
from ..rx import (
    AudioHub,
    RxActivityGate,
    null_recorder,
    pass_through_gate,
)
from ..scan import ScanPlan, load_scan_poll
from ..tx import (
    DEFAULT_TX_IDLE_TIMEOUT,
    TxIdentifier,
    TxSession,
    TxSlot,
    load_tx_idle_timeout,
    parse_tx_format,
)
from ..tx import null_recorder as tx_null_recorder
from ..link import (
    DEFAULT_DTMF_MUTE,
    DEFAULT_DTMF_MUTE_HOLD,
    DEFAULT_MUMBLE_TX_HANG,
    ClientFactory,
    DtmfMuteGate,
    DtmfToneDetector,
    LinkManager,
    MumbleBridge,
    MumbleClient,
    MumbleEntry,
    PyMumbleClient,
    link_username,
    mumble_password_secret,
    resolve_mumble_entries,
    slugify,
)
from ..config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_SECRETS_PATH,
    SETTINGS,
    Secrets,
    Settings,
    load_mumble_servers,
    load_secrets,
    load_service_bindings,
    load_settings,
    resolve_settings,
    save_settings,
)
from .auth import (
    RADIO_API_TOKEN_ENV_VAR,  # noqa: F401  (re-exported via package __init__)
    make_require_token,
    token_matches,
)
from .backend_config import configured_backends, validate_configured_backends
from .events import Event, EventHub, capabilities_event, status_event
from .holder import RadioHolder, build_radio
from .settings import register_settings_routes

#: Module logger — the composition root emits a startup warning here when recording is configured in
#: a time-segmented (not activity-segmented) mode (ADR 0021). Standard `logging`; no handler config,
#: so it propagates to the root logger (and `caplog` in tests) without imposing output on callers.
logger = logging.getLogger(__name__)


def _default_restart_runner(cmd: str) -> None:
    """Spawn the configured restart command (ADR 0047): split shell-style, no shell, detached."""
    subprocess.Popen(shlex.split(cmd), start_new_session=True)

#: Environment variable selecting the backend for `build_app`. Defaults to the mock so the
#: composition root is exercisable without hardware; real backends raise on construction until
#: their bring-up cycle.
RADIO_BACKEND_ENV_VAR = "RADIO_BACKEND"

#: Environment variable pointing `build_app` at the built web-UI directory to serve same-origin
#: (ADR 0022). Marked default → the repo's `web/dist` (relative to the package root). When the
#: directory is absent/unbuilt the app serves a friendly "run the build" placeholder rather than
#: crashing, so the API stays runnable before the SPA is built.
RADIO_WEB_DIR_ENV_VAR = "RADIO_WEB_DIR"

#: Environment variable toggling the *mock* backend's CAT support (ADR 0022). Marked default `on`
#: → a full-CAT mock. Set to `off`/`0`/`false`/`no` to bring up an audio-only mock so the web UI's
#: capability-greying (guardrail 3) can be demonstrated in a browser without hardware. Ignored for
#: non-mock backends.
RADIO_MOCK_CAT_ENV_VAR = "RADIO_MOCK_CAT"

#: Marked default for `RADIO_WEB_DIR`: `<repo>/web/dist`. `app.py` lives at
#: `radio_server/api/app.py`, so two `.parent` hops reach the package root and a third reaches the
#: repo root. Verify against the deployment layout; override with `RADIO_WEB_DIR` when packaged.
DEFAULT_WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web" / "dist"


# --- request bodies ----------------------------------------------------------------------

class PttBody(BaseModel):
    on: bool


class FrequencyBody(BaseModel):
    hz: int


class ChannelBody(BaseModel):
    n: int


class ToneBody(BaseModel):
    tone: float | None = None


class ModeBody(BaseModel):
    mode: str


class ScanBody(BaseModel):
    """A scan request: either an explicit ``frequencies`` list or a ``start/stop/step`` range.

    ``lockout`` frequencies are skipped; ``priority`` (if set) is re-checked between steps.
    """

    frequencies: list[int] | None = None
    start_hz: int | None = None
    stop_hz: int | None = None
    step_hz: int | None = None
    lockout: list[int] = []
    priority: int | None = None


class ControllerBody(BaseModel):
    """Start (``on=True``) or stop (``on=False``) the live controller loop. Mirrors ``PttBody``."""

    on: bool


class LinkBody(BaseModel):
    """Connect a named ``[[mumble.servers]]`` entry (``on=True``) or disconnect (``on=False``).

    ``entry`` may be omitted on connect only when exactly one entry is configured (ADR 0042); it is
    ignored on disconnect (there is at most one active link to drop).
    """

    entry: str | None = None
    on: bool


class SelectBody(BaseModel):
    """Select which configured backend is live (ADR 0076): ``{"backend": "kv4p"}``."""

    backend: str


def _scan_plan(body: ScanBody) -> ScanPlan:
    """Build a :class:`ScanPlan` from the request, requiring exactly one addressing form."""
    has_list = body.frequencies is not None
    has_range = None not in (body.start_hz, body.stop_hz, body.step_hz)
    if has_list == has_range:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="provide either 'frequencies' or all of 'start_hz'/'stop_hz'/'step_hz'",
        )
    try:
        if has_list:
            return ScanPlan.from_frequencies(
                body.frequencies, lockout=body.lockout, priority=body.priority
            )
        return ScanPlan.from_range(
            body.start_hz,
            body.stop_hz,
            body.step_hz,
            lockout=body.lockout,
            priority=body.priority,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc


def _unsupported(capability: Capability) -> HTTPException:
    """The one, consistent gated-CAT error: 501 with the missing capability named in the body.

    The ``capability`` field is machine-readable (``"set_frequency"``, …) so a client/UI can
    grey out exactly the right control — actionable, not merely loud (guardrail 3).
    """
    return HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "error": "capability not supported in this mode",
            "capability": str(capability),
        },
    )


#: The placeholder served at `/` when a web dir is configured but not yet built (no `index.html`).
#: Keeps the API runnable before `npm run build` and tells the operator exactly what to do, rather
#: than crashing app construction or serving a bare 404.
_WEB_NOT_BUILT_HTML = """<!doctype html>
<title>radio-server</title>
<h1>Web UI not built</h1>
<p>The API is running, but the web UI bundle is missing. Build it, then reload:</p>
<pre>cd web && npm install && npm run build</pre>
<p>Or point <code>RADIO_WEB_DIR</code> at an existing build.</p>
"""


def _mount_web_ui(app: FastAPI, web_dir: Path) -> None:
    """Serve the built SPA same-origin at ``/`` (ADR 0022).

    Called *after* the API router and WebSocket routes are registered so those always take
    precedence over the catch-all static mount. When ``web_dir`` holds an ``index.html`` the whole
    directory is mounted (``html=True`` serves ``index.html`` for ``/`` and unknown client-side
    routes). When it is absent/unbuilt a single ``GET /`` returns the build-me placeholder, so a
    set-but-unbuilt dir never crashes construction and the token-gated API stays fully usable.
    """
    if (web_dir / "index.html").is_file():
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
        return

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def _web_not_built() -> str:
        return _WEB_NOT_BUILT_HTML


def create_app(
    radio: Radio,
    *,
    api_token: str,
    controller: Controller | None = None,
    controller_factory: Callable[[Settings, Radio], Controller | None] | None = None,
    radio_factory: Callable[[Settings], Radio] = build_radio,
    runner: ControllerRunner | None = None,
    rx_gate: RxActivityGate = pass_through_gate,
    tx_idle_timeout: float = DEFAULT_TX_IDLE_TIMEOUT,
    station_id: TxIdentifier | None = None,
    event_log: EventLog | None = None,
    recorder: Recorder | None = None,
    tx_recorder: Recorder | None = None,
    mumble_entries: tuple[MumbleEntry, ...] = (),
    mumble_client_factory: ClientFactory | None = None,
    mumble_tx_hang: float = DEFAULT_MUMBLE_TX_HANG,
    mumble_dtmf_mute: bool = DEFAULT_DTMF_MUTE,
    mumble_dtmf_mute_hold: float = DEFAULT_DTMF_MUTE_HOLD,
    restart_command: str = "",
    restart_runner: Callable[[str], None] | None = None,
    web_dir: str | os.PathLike[str] | None = None,
    settings: Settings | None = None,
    config_path: str | os.PathLike[str] | None = None,
    secrets: Secrets | None = None,
    secrets_path: str | os.PathLike[str] | None = None,
    service_plugins: tuple[ServicePlugin, ...] = PLUGINS,
) -> FastAPI:
    """Build the API over an injected ``radio`` and shared-secret ``api_token``.

    The DI seam for tests: ``create_app(MockRadio(supports_cat=...), api_token="secret")``.
    Holds one :class:`EventHub` shared by all WebSocket connections. REST routes are gated by
    the bearer-token dependency; the WebSocket authenticates via a ``?token=`` query parameter
    (browsers cannot set headers on a WebSocket handshake).

    When a ``controller`` (and its ``runner``) is supplied, the app exposes ``POST /controller``
    to start/stop the live loop, surfaces its state in ``/status``, and streams its lifecycle as
    ``session`` events. The controller's ``on_event`` is rebound here to the hub adapter — the hub
    does not exist at ``build_controller`` time, so wiring happens post-construction (the scan
    engine's ``on_event`` seam, applied one layer up).

    ``radio_factory``/``controller_factory`` are the live-switch seams (ADR 0076): ``POST /radio/select``
    drives ``holder.rebuild(new_settings)``, which constructs the target backend through
    ``radio_factory`` (default ``build_radio``) and a fresh controller through ``controller_factory``.
    Omit both (the DI-seam default) and the app never switches — behaviour is exactly as before.

    When ``web_dir`` is given (ADR 0022), the built SPA is served same-origin at ``/`` (mounted
    last, so the token-gated API always wins); ``None`` (the default all existing tests use) adds
    no ``/`` route. An unbuilt ``web_dir`` serves a "run the build" placeholder, never a crash.
    """
    @contextlib.asynccontextmanager
    async def _lifespan(app_: FastAPI):
        # The event log (ADR 0018) is a passive subscriber: a background task drains its own hub
        # queue and writes durable records. It hangs off the shared `EventHub`, so it never blocks
        # `publish` (unbounded queue) and its faults are caught in `EventLog.handle` — a logging
        # failure can never reach the event pump or a transmission.
        log_task: asyncio.Task | None = None
        log_queue = None
        if app_.state.event_log is not None:
            log_queue = hub.subscribe()

            async def _drain_log() -> None:
                assert log_queue is not None  # narrow for type-checkers; set just above
                while True:
                    event = await log_queue.get()
                    app_.state.event_log.handle(event)

            log_task = asyncio.create_task(_drain_log())
        # Auto-start the controller loop on boot (ADR 0037): the web UI's manual Start/Stop button was
        # removed, so a configured controller is brought up here exactly as `POST /controller {on}`
        # would — reference-count a demand for the shared rx_pump and mark it active. Gated on an
        # explicit `settings` (the real `build_app` path); the bare `create_app(...)` DI seam used by
        # tests passes `settings=None` and never autostarts. No-op when no controller is configured.
        if (
            controller is not None
            and settings is not None
            and settings.get("controller.autostart")
        ):
            app_.state.controller_active = True
            await _acquire_rx()
        # Auto-connect the Mumble entry marked `autoconnect` on boot (ADR 0042), the same posture
        # as the controller autostart. At most one entry can carry the flag (validated at resolve);
        # entries without it are connected on demand via `POST /link` or a DTMF combo.
        if app_.state.link_manager is not None:
            auto = next(
                (e.slug for e in app_.state.link_manager.entries if e.autoconnect), None
            )
            if auto is not None:
                await app_.state.link_manager.connect(auto)
        yield
        # Drop the link first so its rx demand is released before the belt-and-suspenders pump stop
        # below; `disconnect()` is idempotent (harmless when nothing is connected).
        if app_.state.link_manager is not None:
            await app_.state.link_manager.disconnect()
        # Tear the radio pipeline down through the holder (ADR 0073): drop PTT, stop a running scan,
        # halt the RX pump, reap the controller's DTMF decoder (the persistent multimon-ng process in
        # streaming mode, ADR 0038), and close the radio device. Every step is idempotent and
        # independently guarded, so this is a belt-and-suspenders — harmless when the last `/audio/rx`
        # disconnect already stopped the pump and nothing is scanning. Runs in the app loop (not a
        # per-connection cancel scope), so it reliably joins the pump/scan tasks.
        await app_.state.holder.stop()
        # The pump's own teardown finalizes any open recording segment; close() is an idempotent
        # belt-and-suspenders that also releases the handle if the pump never ran.
        if app_.state.recorder is not None:
            app_.state.recorder.close()
        # TX recorder (ADR 0021): a live `/audio/tx` session finalizes its own segment on close, but
        # a still-connected talker at shutdown could leave one open — close() is the idempotent
        # belt-and-suspenders (harmless when nothing is open).
        if app_.state.tx_recorder is not None:
            app_.state.tx_recorder.close()
        if log_task is not None:
            log_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await log_task
            # Drain anything published-but-not-yet-recorded so a graceful shutdown loses no
            # ledger entries, then unsubscribe and flush.
            while not log_queue.empty():
                app_.state.event_log.handle(log_queue.get_nowait())
            hub.unsubscribe(log_queue)
            app_.state.event_log.close()

    app = FastAPI(title="radio-server API", version="0.1.0", lifespan=_lifespan)
    # Scan config is resolved from `settings` (ADR 0025). `create_app` is otherwise config-free (a
    # DI seam), but the on-demand `/scan` route needs the scan timing/mode; default to pure defaults
    # so direct `create_app(...)` callers behave exactly as before (when scan read an unset env).
    scan_settings = settings if settings is not None else resolve_settings({})
    # The resolved config the settings API reads/writes (ADR 0026). `scan_settings` is the same
    # object; the settings API also needs the file paths (to persist) and the `Secrets` (presence
    # only) — all stashed on app.state below. The `/scan` route keeps using this startup snapshot,
    # so a PATCH (restart-to-apply) updates the stored config for GET without reconfiguring the run.
    hub = EventHub()
    # RX audio streaming (ADR 0014): one bounded, drop-oldest fan-out and one pump reading
    # `receive()`, shared by all `/audio/rx` listeners. The pump is demand-driven — started on
    # the first subscriber, stopped on the last — so it never relays when nobody is listening.
    audio_hub = AudioHub()
    # The half-duplex arbiter (ADR 0017): one shared owner of "who has the radio right now",
    # injected into the RX pump and every TX session. TX claims it on key-up so the pump (and a
    # live scan) stand down while keyed — a real radio can't receive and transmit at once. Its
    # `on_change` publishes an "arbiter" event on each real mode transition so the ledger records
    # them (ADR 0019); `publish` is non-raising, so a logging fault can never reach the arbiter.
    arbiter = RadioArbiter(
        on_change=lambda mode: hub.publish(Event(type="arbiter", data={"mode": str(mode)}))
    )
    # The activity gate (ADR 0015) is the squelch/VAD; `pass_through_gate` (relay everything) is the
    # default so the DI seam is unchanged for callers that don't opt in. `build_app` selects a real
    # gate from the environment.
    # The audio recorder (ADR 0020): a passive sink for the same gate-open frames the hub streams,
    # tapped inside the pump so segmentation follows the gate-close edge. Off by default (`None` →
    # `null_recorder`); `build_app` injects one when `RADIO_RECORD` is on. Its writes are guarded in
    # the pump, so a recording fault can never break RX.
    # The radio holder (ADR 0073): owns the active radio and the lifecycle of the pipeline pieces
    # bound to it. `start()` constructs the single capture reader (`RxPump`) and the `ScanRunner`
    # against the radio; teardown goes through `holder.stop()` in the lifespan. `create_app` still owns
    # the stable, radio-independent collaborators (the hubs, arbiter, gate, recorder, controller) and
    # hands them to the holder — this cycle is pure indirection. Live backend switching is a later
    # cycle that tears the holder down and rebuilds it against a new radio.
    holder = RadioHolder(
        radio,
        hub=hub,
        audio_hub=audio_hub,
        arbiter=arbiter,
        scan_settings=scan_settings,
        scan_poll=load_scan_poll(scan_settings),
        gate=rx_gate,
        recorder=recorder or null_recorder,
        controller=controller,
        radio_factory=radio_factory,
        controller_factory=controller_factory,
    )
    holder.start()
    # Rebind the local the closures below capture (the demand-counter `_acquire_rx`/`_release_rx` and
    # the Mumble bridge's `rx_active`) so they reach the holder's pump. The single capture reader
    # (ADR 0031): this one pump reads `receive()` and fans each frame to the browser hub/recorder AND
    # — when a controller is configured — to `controller.step` for DTMF decode.
    rx_pump = holder.rx_pump
    # TX audio ingest (ADR 0016): the mirror direction. One single-talker slot guards the shared
    # transmitter (you cannot key one radio from two clients); each `/audio/tx` connection builds
    # its own per-stream `TxSession` (keying + idle timeout). No hub/pump — TX is fan-in, not
    # fan-out — and no lifespan teardown, since a session tears itself down in the endpoint's
    # `finally` (drops PTT, releases the slot).
    tx_slot = TxSlot()
    # The holder owns the active radio + its pipeline lifecycle (ADR 0073); `app.state.radio`/`rx_pump`
    # stay pointed at the holder's instances for the routes and tests that read them by those names.
    app.state.holder = holder
    app.state.radio = radio
    app.state.hub = hub
    app.state.audio_hub = audio_hub
    app.state.rx_pump = rx_pump
    app.state.tx_slot = tx_slot
    app.state.arbiter = arbiter
    app.state.tx_idle_timeout = tx_idle_timeout
    app.state.event_log = event_log
    app.state.recorder = recorder
    app.state.tx_recorder = tx_recorder
    app.state.api_token = api_token
    app.state.controller = controller
    app.state.runner = runner
    # The full plugin set — in-tree plus the operator's local_services/ discoveries (ADR 0051) —
    # so the settings API validates `[services]` bindings against the same ids the controller does.
    app.state.service_plugins = service_plugins
    # Single-reader lifecycle (ADR 0031): the one `rx_pump` runs while there is any demand for
    # received audio — a connected `/audio/rx` listener OR an active controller loop. Reference-count
    # those demands so the reader starts on the first and stops on the last, and so an active
    # controller keeps decoding DTMF even with no browser listening.
    app.state.rx_demand = 0
    app.state.controller_active = False

    async def _acquire_rx() -> None:
        app.state.rx_demand += 1
        if app.state.rx_demand == 1:
            rx_pump.start()

    async def _release_rx() -> None:
        if app.state.rx_demand > 0:
            app.state.rx_demand -= 1
        if app.state.rx_demand == 0:
            await rx_pump.stop()

    # The shared streaming station-ID scheduler (ADR 0041, Part 97): one instance identifies BOTH
    # streaming TX sources — the browser `/audio/tx` talker and the Mumble bridge — so neither goes
    # out un-ID'd. `None` (the DI-seam default) preserves the historical un-ID'd streaming behaviour,
    # so `create_app(...)` callers and the no-callsign default app are unchanged.
    app.state.streaming_id = station_id
    # The Mumble/Murmur link (ADR 0041/0042): a network *peer*, not a backend. A `LinkManager` owns
    # the configured `[[mumble.servers]]` entries and keeps at most one `MumbleBridge` live (switch
    # semantics — one radio, one talker slot). Built only when entries + a client factory are
    # injected (`build_app` wires `PyMumbleClient`; tests pass a `MockMumbleClient` factory). Each
    # bridge subscribes to the audio hub for RF->Mumble and keys through a `TxSession` (sharing
    # `tx_slot` and the arbiter) for Mumble->RF, deferring to a live RF signal via the pump's
    # `active` flag; per-entry `tx_to_rf` selects two-way vs receive-only.
    link_manager: LinkManager | None = None
    dtmf_mute: DtmfMuteGate | None = None
    tone_detector: DtmfToneDetector | None = None
    # Browser-as-Mumble-client seams (ADR 0050): a second audio hub the bridge publishes received
    # Mumble voice into (fanned out to `/audio/mumble/rx`), and a talker slot distinct from the RF
    # `tx_slot` so talking on Mumble never blocks RF TX and vice versa. Built only alongside the link.
    mumble_rx_hub: AudioHub | None = None
    mumble_talk_slot: TxSlot | None = None
    if mumble_entries and mumble_client_factory is not None:
        mumble_rx_hub = AudioHub()
        mumble_talk_slot = TxSlot()
        # DTMF activity gate (ADR 0049): one gate + one real-time tone detector outlive the
        # per-connect bridges. Built whenever muting is enabled — INDEPENDENT of the controller,
        # because the detector (not multimon's decode) now drives muting and the Mumble→RF yield, so
        # it works even on a deployment with no TOTP secret. `on_digit` is still wired as a secondary
        # hold-extender when a controller is present.
        if mumble_dtmf_mute:
            dtmf_mute = DtmfMuteGate(hold=mumble_dtmf_mute_hold)
            tone_detector = DtmfToneDetector()
            if controller is not None:
                controller.on_digit = dtmf_mute.note_digit

        def _bridge_factory(client: MumbleClient, entry: MumbleEntry) -> MumbleBridge:
            return MumbleBridge(
                client,
                radio,
                arbiter=arbiter,
                tx_slot=tx_slot,
                audio_hub=audio_hub,
                acquire_rx=_acquire_rx,
                release_rx=_release_rx,
                station_id=station_id,
                tx_to_rf=entry.tx_to_rf,
                tx_hang=mumble_tx_hang,
                rx_active=lambda: rx_pump.active,
                dtmf_mute=dtmf_mute,
                tone_detector=tone_detector,
                mumble_rx_hub=mumble_rx_hub,
            )

        def _publish_link_change(name: str, state: str) -> None:
            # Every transition — browser, DTMF, or autoconnect — lands in the ledger and on the
            # web UI's WebSocket as a `link` event (ADR 0042). The event carries the full link
            # block (`{active, entries}`) because WS `status` frames are RadioStatus-only — this
            # is the only push channel the link card has.
            hub.publish(
                Event(
                    type="link",
                    data={"entry": name, "state": state, **app.state.link_manager.status()},
                )
            )

        link_manager = LinkManager(
            mumble_entries,
            client_factory=mumble_client_factory,
            bridge_factory=_bridge_factory,
            on_change=_publish_link_change,
        )
    app.state.link_manager = link_manager
    app.state.mumble_rx_hub = mumble_rx_hub
    app.state.mumble_talk_slot = mumble_talk_slot
    app.state.dtmf_mute = dtmf_mute
    # Whether POST /server/restart will act (ADR 0047) — surfaced by GET /settings so the web UI
    # only shows the Restart button when it works in this deployment.
    app.state.restart_available = bool(restart_command)
    # Settings API (ADR 0026): the resolved config + the file paths to persist to + the secrets
    # (presence only). `config_path`/`secrets_path` default to the standard locations so a bare
    # `create_app(...)` can still serve/patch a config; tests point them at temp files.
    app.state.settings = scan_settings
    app.state.config_path = config_path if config_path is not None else DEFAULT_CONFIG_PATH
    app.state.secrets = secrets
    app.state.secrets_path = secrets_path if secrets_path is not None else DEFAULT_SECRETS_PATH

    def _publish_controller(event: ControllerEvent) -> None:
        # Adapt a controller event to the shared hub — the `_publish_scan` pattern, keeping the
        # controller below the API with no import cycle. One `on_event` channel fans out by phase
        # (ADR 0019): auth outcomes and command dispatch get their own hub event types so the
        # ledger's auth/command mappers (dead since cycle 17) now write; every other phase is the
        # session lifecycle. An auth event carries only the result — never a code.
        phase = event.phase
        if phase in ("auth_accepted", "auth_rejected"):
            result = "accepted" if phase == "auth_accepted" else "rejected"
            hub.publish(Event(type="auth", data={"result": result}))
        elif phase == "command":
            hub.publish(
                Event(type="command", data={"service": (event.data or {}).get("service")})
            )
        elif phase == "link":
            # A link combo was received over the air (ADR 0042) — the command record. The resulting
            # connect/disconnect transitions are published by the LinkManager's own on_change.
            hub.publish(
                Event(type="link", data={**(event.data or {}), "via": "dtmf"})
            )
        else:
            hub.publish(
                Event(type="session", data={"phase": phase, **(event.data or {})})
            )

    if controller is not None:
        controller.on_event = _publish_controller

    if controller is not None and link_manager is not None:
        # The DTMF link built-ins (ADR 0042): the controller fires `on_link` synchronously from
        # `step` (already on the event loop — the rx pump / an async route drives it), and the
        # actual connect/disconnect runs as a task so a slow Mumble handshake never stalls the
        # audio loop. Failures are logged, never raised — a bad link must not kill the pump.
        async def _apply_link(name: str | None) -> None:
            try:
                if name is None:
                    await link_manager.disconnect()
                else:
                    await link_manager.connect(name)
            except Exception as exc:
                logger.exception("mumble link %s failed", "disconnect" if name is None else name)
                # The operator already heard the spoken confirmation — make the failure visible
                # too: an error `link` event lands in the ledger/event log, and it carries the
                # full block so the card's state refreshes (nothing stays stuck "Connecting…").
                hub.publish(
                    Event(
                        type="link",
                        data={
                            "entry": name,
                            "state": "error",
                            "detail": str(exc),
                            **link_manager.status(),
                        },
                    )
                )

        controller.on_link = lambda name: asyncio.get_running_loop().create_task(
            _apply_link(name)
        )

    def _controller_state() -> dict | None:
        if controller is None:
            return None
        # `running` now reflects whether the controller loop is active (its audio is pumped by the
        # shared `rx_pump`, ADR 0031), not a separate runner task.
        return {
            "running": app.state.controller_active,
            "session_open": controller.session.authenticated,
        }

    def _link_state() -> dict | None:
        # The Mumble link block for `/status` and `/link/status`: null when no entries are
        # configured (the `_controller_state` convention), else the manager's per-entry snapshot
        # (`{active, entries: [...]}` — every entry, live connection state on the active one).
        manager = app.state.link_manager
        if manager is None:
            return None
        return manager.status()

    require_token = make_require_token(api_token)
    api = APIRouter(dependencies=[Depends(require_token)])

    def _require_cat(capability: Capability) -> None:
        if capability not in radio.capabilities():
            raise _unsupported(capability)

    # --- shared surface (always present) -------------------------------------------------

    @api.get("/capabilities")
    def get_capabilities() -> list[str]:
        # Capability is a StrEnum, so it JSON-serializes to its string value directly.
        return sorted(str(c) for c in radio.capabilities())

    @api.get("/services")
    def get_services() -> list[dict]:
        # The DTMF voice services actually wired in this deployment (`{digit, name, description}`),
        # for the web UI reference panel. Empty when no controller is configured, and reflects config
        # (a plugin appears only when its `enabled(settings)` gate passes).
        return controller.service_catalog if controller is not None else []

    @api.get("/status")
    def get_status() -> dict:
        # RadioStatus is a frozen dataclass; asdict gives the exact JSON shape (note the
        # field is `transmitting`, not `ptt`). The `controller` block is null when no
        # controller was wired in; otherwise it carries running + session-open live state.
        # The `scan` block (ADR 0028) carries the background scan runner's live state so the UI
        # can enable/disable the stop button.
        return {
            **asdict(radio.status()),
            "controller": _controller_state(),
            "scan": _scan_state(),
            "link": _link_state(),
        }

    @api.post("/ptt")
    def set_ptt(body: PttBody) -> dict:
        radio.ptt(body.on)
        # A semantic `ptt` event (the state that changed) followed by a fresh status snapshot
        # (its full effect) — a client can act on either.
        hub.publish(Event(type="ptt", data={"on": body.on}))
        snapshot = status_event(radio)
        hub.publish(snapshot)
        return snapshot.data

    @api.post("/transmit")
    async def transmit(request: Request) -> dict:
        # Raw PCM in the request body, wrapped in a canonical-format frame. Minimal but real:
        # it lands in radio.tx_log exactly as a service's audio would.
        body = await request.body()
        radio.transmit(AudioFrame(body))
        hub.publish(status_event(radio))
        return {"transmitted_bytes": len(body)}

    @api.post("/services/{digit}")
    async def run_service(digit: str) -> dict:
        # Fire a DTMF service / built-in command from the control operator (web UI) and transmit it
        # over the air. No RF-auth check — the LAN token is the operator's credential, exactly like
        # /ptt and /transmit which key TX directly. A clear 503 (not a silent no-op) when no controller
        # was configured, mirroring POST /controller. Run on the event loop (an async route calling
        # trigger() with no await inside) so it serializes with the RxPump's controller.step and cannot
        # race StationId state. trigger() itself emits the command/session/id event to the hub, so the
        # event log updates; we add a status snapshot for the transmit's full effect.
        if controller is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="controller not configured in this deployment",
            )
        result = controller.trigger(digit)
        hub.publish(status_event(radio))
        return result

    @api.get("/auth/totp")
    def current_totp() -> dict:
        # The code an enrolled authenticator shows right now, for the web UI's code card — so the
        # operator can key a DTMF login without their phone. Token-gated like everything else,
        # and no new capability: the LAN token already transmits directly (/ptt, /services/...).
        # Returns ONLY the current code + timing, never the secret (ADR 0025 posture), and is
        # read-only against the auth plane (single-use burn still applies when the code is keyed).
        #
        # ``enforced`` reports the RUNNING controller's TOTP state (ADR 0048), not the persisted
        # setting — honest under restart-to-apply. When auth is off there is no login code, so the
        # web UI shows an "un-gated" indicator instead of a code (no 503, even without a secret).
        enforced = controller.totp_enforced if controller is not None else True
        if not enforced:
            return {"enforced": False}
        secrets = app.state.secrets
        # Fixed-code mode (ADR 0083): a static code is in use. It is write-only — NEVER echo it back
        # (unlike a rotating TOTP code, which the card shows to help the operator). The card just
        # indicates a fixed code is required; 503 if the mode is on but no code has been set yet.
        # Detected from the RUNNING controller (honest under restart-to-apply, like `enforced`); when
        # no controller is wired (unconfigured), fall back to the persisted setting.
        app_settings = getattr(app.state, "settings", None)
        fixed_mode = (
            controller.auth_method == "fixed"
            if controller is not None
            else (app_settings is not None and load_fixed_code_enabled(app_settings))
        )
        if fixed_mode:
            if secrets is None or not secrets.fixed_code:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="a fixed login code is selected but not set (set one first)",
                )
            return {"enforced": True, "fixed": True}
        totp_secret = secrets.totp_secret if secrets is not None else None
        if not totp_secret:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="TOTP not configured in this deployment (enroll a secret first)",
            )
        verifier = TotpVerifier(totp_secret)
        return {
            "enforced": True,
            "code": verifier.current_code(),
            "seconds_remaining": verifier.seconds_remaining(),
            "interval": verifier.interval,
        }

    @api.post("/auth/session")
    async def open_auth_session() -> dict:
        # Open the over-the-air session from the web UI (clicking the OTA-code chip), with the
        # same on-air effect as a DTMF-keyed auth — welcome announcement, station ID armed,
        # session events (ADR 0046). No RF-auth check and NO TOTP burn: the LAN token is the
        # operator's credential (the /services posture), and consuming a code here would lock an
        # RF caller out of that window. Async-no-await so it serializes with the RxPump's
        # controller.step (the /services/{digit} pattern).
        if controller is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="controller not configured in this deployment",
            )
        result = controller.open_session()
        hub.publish(status_event(radio))
        return result

    def _run_restart(cmd: str) -> None:
        # `start_new_session` detaches the child from this process group; with systemd's
        # --no-block the job is queued in the manager, so it survives this process's own stop.
        runner = restart_runner if restart_runner is not None else _default_restart_runner
        try:
            runner(cmd)
        except Exception:
            logger.exception("restart command failed: %s", cmd)

    @api.post("/server/restart")
    async def restart_server() -> dict:
        # Restart the whole server process from the settings screen (ADR 0047) — settings are
        # restart-to-apply, so this closes the loop after a save. The configured command is
        # handed to the deployment's supervisor (systemd-user by default); the spawn is delayed a
        # beat so this response reaches the browser before the stop signal lands. Empty command
        # (bare bench runs) → a clear 503, and the UI hides the button via /settings.
        if not restart_command:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="server.restart_command not configured in this deployment",
            )
        loop = asyncio.get_running_loop()
        loop.call_later(0.3, _run_restart, restart_command)
        return {"restarting": True}

    # --- CAT surface (gated on capability — guardrail 3) ---------------------------------

    @api.post("/frequency")
    def set_frequency(body: FrequencyBody) -> dict:
        _require_cat(Capability.SET_FREQUENCY)
        try:
            radio.set_frequency(body.hz)
        except UnsupportedCapability as exc:  # pragma: no cover - pre-check already guards
            raise _unsupported(exc.capability) from exc
        hub.publish(status_event(radio))
        return asdict(radio.status())

    @api.post("/channel")
    def set_channel(body: ChannelBody) -> dict:
        _require_cat(Capability.SET_CHANNEL)
        try:
            radio.set_channel(body.n)
        except UnsupportedCapability as exc:  # pragma: no cover
            raise _unsupported(exc.capability) from exc
        hub.publish(status_event(radio))
        return asdict(radio.status())

    @api.post("/tone")
    def set_tone(body: ToneBody) -> dict:
        _require_cat(Capability.SET_TONE)
        try:
            radio.set_tone(body.tone)
        except UnsupportedCapability as exc:  # pragma: no cover
            raise _unsupported(exc.capability) from exc
        hub.publish(status_event(radio))
        return asdict(radio.status())

    @api.post("/mode")
    def set_mode(body: ModeBody) -> dict:
        _require_cat(Capability.SET_MODE)
        try:
            radio.set_mode(body.mode)
        except UnsupportedCapability as exc:  # pragma: no cover
            raise _unsupported(exc.capability) from exc
        hub.publish(status_event(radio))
        return asdict(radio.status())

    # The async scan runner (ADR 0028) is built by the holder's `start()` (ADR 0073), against the same
    # radio/scan-config/arbiter, with its progress→hub adapter owned by the holder. Rebind the local so
    # `_scan_state` and the `/scan` routes below reach the holder's runner.
    scan_runner = holder.scan_runner
    app.state.scan_runner = scan_runner

    def _scan_state() -> dict:
        # Live scan state for `/status`, mirroring `_controller_state`. Always present (the runner
        # exists regardless); `running` is False and `frequency` None on an audio-only backend that
        # can never start a scan.
        return {
            "running": scan_runner.running,
            "frequency": scan_runner.current_frequency,
        }

    @api.post("/scan")
    async def scan(body: ScanBody) -> dict:
        # Gated exactly like the other CAT endpoints: 501 (naming "scan") on an audio-only backend.
        # Non-blocking now (ADR 0028): starts a background scan and returns an ack immediately. A
        # start while one is already running is a 409 (one scan at a time — never silently stacked).
        _require_cat(Capability.SCAN)
        plan = _scan_plan(body)
        try:
            started = scan_runner.start(plan)
        except UnsupportedCapability as exc:  # pragma: no cover - pre-check already guards
            raise _unsupported(exc.capability) from exc
        if not started:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="a scan is already running",
            )
        # Yield so run() does its first tick (which emits `scanning`) before we snapshot status.
        await asyncio.sleep(0)
        hub.publish(status_event(radio))
        return {"scanning": True, "status": asdict(radio.status())}

    @api.post("/scan/stop")
    async def scan_stop() -> dict:
        # Signal the background scan to stop; it ends cleanly at the next tick boundary and emits a
        # `stopped` event. Idempotent — a stop when nothing is scanning is a clean no-op ack, not an
        # error. Capability-gated like `/scan` (501 naming "scan") so the endpoint doesn't exist on
        # an audio-only backend, matching how the whole scan feature is absent there (guardrail 3).
        _require_cat(Capability.SCAN)
        stopped = await scan_runner.stop()
        hub.publish(status_event(radio))
        return {"scanning": False, "stopped": stopped}

    @api.post("/controller")
    async def controller_route(body: ControllerBody) -> dict:
        # Start/stop the live loop. A clear 503 (not a silent no-op) when no controller was
        # configured — the same fail-loud posture as the CAT capability gate.
        if controller is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="controller not configured in this deployment",
            )
        # The controller no longer owns a receive loop (ADR 0031): "on" adds a demand for the shared
        # `rx_pump` (which feeds `controller.step` each frame), "off" drops it. Idempotent per state.
        if body.on:
            if not app.state.controller_active:
                app.state.controller_active = True
                await _acquire_rx()
        else:
            if app.state.controller_active:
                app.state.controller_active = False
                await _release_rx()
        hub.publish(status_event(radio))
        return {"controller": _controller_state()}

    @api.get("/link/status")
    def link_status() -> dict:
        # The Mumble link snapshot (ADR 0041/0042); a null block when no entries are configured,
        # mirroring how `/status` carries a null controller block.
        return {"link": _link_state()}

    @api.post("/link")
    async def link_route(body: LinkBody) -> dict:
        # Connect a named entry (switch semantics — the manager drops any current link first) or
        # disconnect. A clear 503 (not a silent no-op) when no entries are configured — the same
        # fail-loud posture as `/controller`.
        manager = app.state.link_manager
        if manager is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "mumble link not configured in this deployment "
                    "(add [[mumble.servers]] entries to radio.toml)"
                ),
            )
        if body.on:
            entry = body.entry
            if entry is None:
                # Back-compat convenience: a bare {on: true} still works when the choice is
                # unambiguous (exactly one entry). With several, the caller must name one.
                if len(manager.entries) == 1:
                    entry = manager.entries[0].slug
                else:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        detail="'entry' is required when more than one mumble server is configured",
                    )
            try:
                # Accept the display name or the slug (ADR 0052): slugifying either lands on the
                # manager's key (a slug slugifies to itself).
                await manager.connect(slugify(entry))
            except KeyError:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"unknown mumble entry {entry!r}",
                ) from None
            except RuntimeError as exc:
                # A connect that fails synchronously — e.g. the mumble extra / libopus is not
                # installed (the lazy import raises with the install command). Surface the
                # actionable message instead of a bare 500; the web card renders 503 detail.
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
                ) from exc
        else:
            await manager.disconnect()
        hub.publish(status_event(radio))
        return {"link": _link_state()}

    # --- live backend switch (ADR 0076) --------------------------------------------------

    def _backend_list() -> dict:
        # Current selection + the backends this node is configured for (ADR 0074's
        # `configured_backends`), each with its resolved ctor kwargs, for the UI's select dropdown.
        # Live capabilities are given only for the ACTIVE backend (it is the one that is constructed);
        # advertising the others' would need construction/hardware — ADR 0074's deliberate exclusion.
        current = app.state.settings
        choices = configured_backends(current)
        return {
            "active": current.get("server.backend"),
            "active_capabilities": sorted(str(c) for c in app.state.holder.radio.capabilities()),
            "backends": [
                {"name": c.name, "active": c.active, "settings": dict(c.settings)} for c in choices
            ],
        }

    @api.get("/radio/backends")
    def list_backends() -> dict:
        # What the UI needs to render the choices and the current one (ADR 0076).
        return _backend_list()

    @api.post("/radio/select")
    async def select_backend(body: SelectBody) -> dict:
        # Rebind the closure locals the routes/WS read so they follow the live radio after a swap —
        # ADR 0073 deferred this "routes read holder.radio live" step to the swap cycle.
        nonlocal radio, rx_pump, scan_runner, controller
        current = app.state.settings
        target = body.backend
        configured = {c.name for c in configured_backends(current)}
        # Only a configured backend may be selected — never an arbitrary name (ADR 0074 enumerates
        # them). 409 (not 404): the request conflicts with the server's configuration.
        if target not in configured:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"backend {target!r} is not configured; configured backends: "
                    + ", ".join(sorted(configured))
                ),
            )
        # Build the target's settings by patching server.backend onto the current set, revalidated
        # atomically (the patch_settings idiom, ADR 0026) so a bad value fails before any teardown.
        base = {spec.key: current.get(spec.key) for spec in SETTINGS if current.is_set(spec.key)}
        try:
            # Carry the [plugins.*] extra channel (ADR 0051) through the patch. `base` is schema-only,
            # so without `extra=` the rebuilt settings would drop every local plugin's config and the
            # rebuilt controller would gate them all off — a switch would silently shrink the service
            # catalog until a restart (ADR 0078). A switch must preserve everything a fresh boot loads.
            new_settings = resolve_settings(
                {**base, "server.backend": target}, extra=current.extras()
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        # The atomic swap (ADR 0076). On failure the holder has already rolled back to the previous
        # working radio — surface 503 and leave the running config untouched (nothing persisted).
        try:
            await holder.rebuild(new_settings)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"failed to switch to backend {target!r}: {exc}; "
                    f"still on {current.get('server.backend')!r}"
                ),
            ) from exc
        # Success: persist the selection through the schema (ADR 0051 — preserves the rest of
        # radio.toml) so a restart comes up on the same radio, and update the config the API reads.
        save_settings(new_settings, app.state.config_path)
        app.state.settings = new_settings
        # Re-point the closure locals + app.state at the holder's freshly-built pieces. Late-binding
        # closures (`_require_cat`, `get_capabilities`, `_acquire_rx`/`_release_rx`, the scan routes,
        # the Mumble bridge's `rx_active`) then transparently reach the new radio/pump/controller.
        radio = holder.radio
        rx_pump = holder.rx_pump
        scan_runner = holder.scan_runner
        controller = holder.controller
        app.state.radio = radio
        app.state.rx_pump = rx_pump
        app.state.scan_runner = scan_runner
        app.state.controller = controller
        # The new pump is demand-started; if a listener (or the active controller) already holds RX
        # demand, start it now so received audio follows the newly-selected radio without a reconnect.
        if app.state.rx_demand > 0:
            rx_pump.start()
        # Push the new capability set (so a connected client re-greys) then a fresh status snapshot.
        hub.publish(capabilities_event(radio))
        hub.publish(status_event(radio))
        return {"backend": target, **_backend_list()}

    # Settings + secret-rotation routes (ADR 0026) — attached to the same token-gated router.
    register_settings_routes(api, app)

    app.include_router(api)

    # --- WebSocket event stream (own auth plane: ?token=) --------------------------------

    @app.websocket("/events")
    async def events(websocket: WebSocket) -> None:
        token = websocket.query_params.get("token")
        if not token_matches(token, api_token):
            # Reject the handshake before accepting — closed by default, like the REST plane.
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        await websocket.accept()
        queue = hub.subscribe()
        try:
            # An initial status snapshot so a fresh subscriber has current state immediately.
            await websocket.send_json(status_event(radio).as_json())
            while True:
                event = await queue.get()
                await websocket.send_json(event.as_json())
        except WebSocketDisconnect:
            pass
        finally:
            hub.unsubscribe(queue)

    # --- WebSocket RX audio stream (binary raw PCM; own auth plane: ?token=) --------------

    @app.websocket("/audio/rx")
    async def audio_rx(websocket: WebSocket) -> None:
        # Binary siblings of `/events`: same `?token=` handshake, but frames are raw canonical
        # PCM sent via `send_bytes` (ADR 0014), not JSON. The pump is started on the first
        # listener and stopped on the last, so it is demand-driven and controller-independent.
        token = websocket.query_params.get("token")
        if not token_matches(token, api_token):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        await websocket.accept()
        # First message declares the stream format, mirroring `/audio/tx`'s `{"status":"ready",...}`
        # ack (ADR 0023 — the deferred cycle-15 symmetry decision). A client reads this to configure
        # playback (Web Audio at 48k), and it stays robust if it instead assumes canonical. Sent
        # before any binary frame so the leading message is always the header, never PCM.
        await websocket.send_json({"status": "ready", "format": asdict(CANONICAL_FORMAT)})
        queue = audio_hub.subscribe()
        # Add a listener demand for the shared reader (ADR 0031). It may already be running for the
        # controller; either way it keeps running until the last listener AND the controller release.
        await _acquire_rx()
        try:
            # Blocks on `queue.get()` until a frame arrives; a disconnect is surfaced on the next
            # `send_bytes`. On real hardware `receive()` yields continuous PCM (silence is
            # non-empty), so frames flow steadily and the disconnect is seen promptly — the
            # empty-queue stall is a mock/edge case, the same shape `/events` already accepts.
            while True:
                frame = await queue.get()
                await websocket.send_bytes(frame)
        except WebSocketDisconnect:
            pass
        finally:
            audio_hub.unsubscribe(queue)
            await _release_rx()

    # --- WebSocket TX audio ingest (binary raw PCM in; own auth plane: ?token=) ------------

    @app.websocket("/audio/tx")
    async def audio_tx(websocket: WebSocket) -> None:
        # The mirror of `/audio/rx`, other direction (ADR 0016): the client streams canonical PCM
        # *in* and we feed it to `radio.transmit()`, keying PTT for the stream's duration
        # (guardrail 2 — never a CAT TX). Same `?token=` handshake, then a single-talker guard, a
        # format-declaration handshake, and the binary frame loop.
        token = websocket.query_params.get("token")
        if not token_matches(token, api_token):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        # One transmitter, one talker: a second concurrent client is refused (can't key twice). A
        # browser cannot observe a *pre-accept* close code — a rejected WS handshake surfaces as a
        # generic 1006, so the app-level 1013 is lost. So we accept first, send an explicit
        # `{"status":"busy"}` message the client can read, then close 1013. Ordering is load-bearing:
        # we do NOT enter the `session`/`finally` below on this path, so we never release the slot the
        # *other* talker holds (`try_acquire` returned False — we hold nothing to release).
        acquired = tx_slot.try_acquire()
        await websocket.accept()
        if not acquired:
            await websocket.send_json({"status": "busy"})
            await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER)
            return
        # `on_key` publishes the same `ptt` on/off events the REST `/ptt` path does, so streaming-TX
        # keying lands in the ledger as `tx_key_up`/`tx_key_down` (with duration) too (ADR 0019).
        # `recorder` captures the transmitted frames to a `tx-` WAV when `RADIO_RECORD_TX` is on
        # (ADR 0021); the shared instance is only ever fed by one talker (the slot refuses a second
        # above), and its calls in `feed`/`close` are guarded so a disk fault can't break keying.
        session = TxSession(
            radio,
            idle_timeout=app.state.tx_idle_timeout,
            arbiter=arbiter,
            on_key=lambda on: hub.publish(Event(type="ptt", data={"on": on})),
            recorder=app.state.tx_recorder or tx_null_recorder,
            # Auto-identify the browser talker (ADR 0041, Part 97): the shared scheduler prepends the
            # ID into this same keyed over when due. `None` when no callsign is configured — unchanged.
            station_id=app.state.streaming_id,
        )
        try:
            # Format handshake: the first message declares the stream format (no per-frame tag
            # rides a raw binary wire). A malformed / non-canonical declaration fails loud with a
            # 1003 before any audio is accepted or the transmitter keys.
            try:
                header = await asyncio.wait_for(
                    websocket.receive_json(), timeout=session.idle_timeout
                )
                parse_tx_format(header)
            except asyncio.TimeoutError:
                return
            except AudioFormatMismatch:
                await websocket.close(code=status.WS_1003_UNSUPPORTED_DATA)
                return
            await websocket.send_json(
                {"status": "ready", "format": asdict(CANONICAL_FORMAT)}
            )
            # Binary frame loop. `wait_for` is only the wakeup; the idle *decision* lives in the
            # clock-injected session, so a stalled stream drops PTT (`on_idle`) rather than holding
            # the transmitter keyed on a dead connection.
            while True:
                try:
                    data = await asyncio.wait_for(
                        websocket.receive_bytes(), timeout=session.idle_timeout
                    )
                except asyncio.TimeoutError:
                    session.on_idle()
                    break
                try:
                    session.feed(data)
                except AudioFormatMismatch:
                    await websocket.close(code=status.WS_1003_UNSUPPORTED_DATA)
                    break
        except WebSocketDisconnect:
            pass
        finally:
            # Any exit — clean close, idle, format error, crash — drops PTT (idempotent) and frees
            # the slot for the next talker.
            session.close()
            tx_slot.release()

    # --- WebSocket: browser as a Mumble client (ADR 0050) --------------------------------
    # When a link is active, the web UI monitors/talks on the Mumble channel through the one shared
    # connection (owned by the bridge). Inbound is a fan-out of `mumble_rx_hub`; outbound routes to
    # the bridge's single sender with an operator-talk yield. Neither path keys the radio.

    @app.websocket("/audio/mumble/rx")
    async def audio_mumble_rx(websocket: WebSocket) -> None:
        # The Mumble twin of `/audio/rx`: same `?token=` handshake and canonical PCM frames, but the
        # source is the received Mumble voice the bridge publishes into `mumble_rx_hub` — NOT the RF
        # pump, so no rx demand is taken. With no link up the hub is simply idle (frames resume when a
        # bridge connects). Returns cleanly (1008) if Mumble isn't configured at all.
        token = websocket.query_params.get("token")
        if not token_matches(token, api_token):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        if mumble_rx_hub is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        await websocket.accept()
        await websocket.send_json({"status": "ready", "format": asdict(CANONICAL_FORMAT)})
        queue = mumble_rx_hub.subscribe()
        try:
            while True:
                frame = await queue.get()
                await websocket.send_bytes(frame)
        except WebSocketDisconnect:
            pass
        finally:
            mumble_rx_hub.unsubscribe(queue)

    @app.websocket("/audio/mumble/tx")
    async def audio_mumble_tx(websocket: WebSocket) -> None:
        # The Mumble twin of `/audio/tx`, but it keys NO radio (guardrail 2 is moot here — there is no
        # RF): each frame is forwarded to the live bridge's single Mumble sender via
        # `send_operator_audio`, which arms the operator-talk yield so the RF→Mumble relay steps aside.
        # A separate `mumble_talk_slot` (not the RF `tx_slot`) means Mumble talk and RF talk don't
        # block each other. No `TxSession`, no arbiter, no station ID.
        token = websocket.query_params.get("token")
        if not token_matches(token, api_token):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        if mumble_talk_slot is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        # One Mumble talker at a time (mirrors `/audio/tx`): accept first so the browser can read the
        # explicit reason, then close 1013 without entering the `finally` that would free the slot.
        acquired = mumble_talk_slot.try_acquire()
        await websocket.accept()
        if not acquired:
            await websocket.send_json({"status": "busy"})
            await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER)
            return
        try:
            # Format handshake, same canonical contract and 1003 as `/audio/tx`.
            try:
                header = await asyncio.wait_for(
                    websocket.receive_json(), timeout=app.state.tx_idle_timeout
                )
                parse_tx_format(header)
            except asyncio.TimeoutError:
                return
            except AudioFormatMismatch:
                await websocket.close(code=status.WS_1003_UNSUPPORTED_DATA)
                return
            await websocket.send_json(
                {"status": "ready", "format": asdict(CANONICAL_FORMAT)}
            )
            while True:
                try:
                    data = await asyncio.wait_for(
                        websocket.receive_bytes(), timeout=app.state.tx_idle_timeout
                    )
                except asyncio.TimeoutError:
                    break
                # Resolve the live bridge per frame — a link can drop or switch mid-talk. No link →
                # tell the client and stop; nothing to send to.
                bridge = (
                    app.state.link_manager.active_bridge
                    if app.state.link_manager is not None
                    else None
                )
                if bridge is None:
                    await websocket.send_json({"status": "no_link"})
                    break
                bridge.send_operator_audio(data)
        except WebSocketDisconnect:
            pass
        finally:
            mumble_talk_slot.release()

    # --- Same-origin web UI (ADR 0022) ---------------------------------------------------
    # Mounted LAST so the token-gated REST routes and the `?token=` WebSockets above always win
    # over the catch-all static mount at `/`. Opt-in via `web_dir`: `None` (the DI-seam default all
    # existing tests use) leaves the surface exactly as before — no `/` route at all.
    if web_dir is not None:
        _mount_web_ui(app, Path(web_dir))

    return app


def _pymumble_client_factory(secrets: Secrets, username: str) -> ClientFactory:
    """The production `ClientFactory` (ADR 0042): a fresh `PyMumbleClient` per connect.

    The `MumbleEntry` → `PyMumbleClient` kwargs mapping, keeping the client itself a pure DI
    object (Settings-free, the `AiocBaofeng` posture). The nick is not per-entry: every server
    sees the same `link_username` — ``<CALLSIGN> (radio-server)`` — because the station always
    identifies as the licensee. Each entry's Murmur password: the secrets channel
    (``mumble_password_<slug>``) overrides the entry's plaintext ``password`` field (the
    public-gate-code case, ADR 0052). Construction is import-free: a missing `mumble` extra
    surfaces at `connect()` with an actionable install message, not here.
    """

    def factory(entry: MumbleEntry) -> MumbleClient:
        return PyMumbleClient(
            host=entry.host,
            port=entry.port,
            username=username,
            channel=entry.channel,
            password=secrets.get(mumble_password_secret(entry.slug)) or entry.password or "",
        )

    return factory


def build_app(
    settings: Settings | None = None,
    secrets: Secrets | None = None,
    *,
    config_path: str | os.PathLike[str] = DEFAULT_CONFIG_PATH,
    secrets_path: str | os.PathLike[str] = DEFAULT_SECRETS_PATH,
) -> FastAPI:
    """Compose the app from resolved `Settings` + `Secrets` — the top-level composition root (ADR 0025).

    ``settings``/``secrets`` default to loading from the default locations (``radio.toml`` +
    ``radio-secrets.toml``/env), so ``build_app()`` still works with no args. Selects the backend via
    ``server.backend`` (default ``mock``) and requires the bearer token from the secrets channel —
    `secrets.require("api_token")` raises when it is unset rather than serving open. The RX squelch/VAD
    gate is selected via ``audio.squelch`` (default ``off``; ADR 0015). The TX ingest idle timeout is
    ``tx.idle_timeout`` (ADR 0016). The station ledger writes to ``logging.path``, opened fail-loud
    here (ADR 0018). Audio recording is opt-in via ``recording.enabled`` → WAV segments under
    ``recording.path``, opened fail-loud here (ADR 0020).

    The live controller loop is wired only when the deployment configures it — gated on the **TOTP
    secret being present** (never on any ``radio.toml`` setting), since `build_controller` needs the
    secret (and, in production, multimon + a TTS voice). Without it the app is exactly the prior
    REST/WS surface: ``/controller`` reports 503 and ``/status`` carries a null controller block.
    """
    if settings is None:
        settings = load_settings(config_path)
    if secrets is None:
        secrets = load_secrets(secrets_path)
    # Validate every configured backend's block at load (ADR 0074): a config can describe more than
    # one backend (`[baofeng]` and `[kv4p]`), and a present-but-broken block for an *inactive* switch
    # target fails loud here rather than the moment someone selects it live. Presence-scoped, so a
    # single-backend config is a no-op (the active backend is validated below in `build_radio`).
    validate_configured_backends(settings)
    # Construct the active radio from config via the holder seam (ADR 0073): the backend switch +
    # squelch validation now lives in `build_radio` (api/holder.py), the one place the swap cycle can
    # call to build a different backend. `create_app` wraps this radio in the `RadioHolder` that owns
    # its pipeline lifecycle.
    radio = build_radio(settings)
    controller: Controller | None = None
    # Gated on the TOTP secret's presence — a secret, not a schema setting — so the default mock app
    # (no secret) never reads the required callsign/voice settings and starts cleanly. The controller
    # no longer needs a separate `ControllerRunner` — the shared `rx_pump` drives `step` (ADR 0031).
    # When `auth.totp_enabled` is off (ADR 0048), the controller is built even without a secret so
    # over-the-air DTMF still works un-gated (it dispatches every keyed entry directly). Either way it
    # still requires a callsign/voice for Part 97 ID — you can never run un-ID'd.
    # The Mumble destinations (ADR 0042): the `[[mumble.servers]]` channel, resolved fail-loud
    # (slugs, hosts, duplicate combos) before anything is built on top of it. An empty/absent list
    # means no link surface at all — `/link` reports 503 and `/status` carries a null link block.
    mumble_entries = resolve_mumble_entries(load_mumble_servers(config_path))
    # Operator-authored plugins from ./local_services/ (ADR 0051), discovered once here — the one
    # composition-root call — and passed everywhere the plugin set matters (controller bindings,
    # the settings API's `[services]` validation). Fail-loud: a broken local plugin stops startup.
    service_plugins = PLUGINS + discover_local_plugins()
    # The controller is built through a factory (ADR 0076) so a live backend switch can rebuild it
    # against the new radio — `holder.stop()` reaps its DTMF decoder, and the controller captures the
    # radio for TX responses/ID, so a swap needs a fresh one. The factory captures the file-derived
    # deps (service bindings, Mumble entries, plugins) — stable across a switch — and takes (settings,
    # radio). `controller_factory` is None (and `controller` stays None) when this deployment runs no
    # controller, so a rebuild never conjures one that wasn't there. Same gate as before.
    controller_factory: Callable[[Settings, Radio], Controller | None] | None = None
    # Build the controller when auth is either off, or configured — i.e. the credential for the
    # SELECTED login scheme is present: a TOTP secret in the default mode, or a fixed code when
    # `auth.fixed_code` is on (ADR 0083). When `auth.fixed_code` is off this is exactly the prior
    # gate (`secrets.totp_secret or not load_totp_enabled(settings)`).
    _fixed_mode = load_fixed_code_enabled(settings)
    _credential = secrets.fixed_code if _fixed_mode else secrets.totp_secret
    if _credential or not load_totp_enabled(settings):
        service_bindings = load_service_bindings(config_path)
        # One long-lived over-the-air auth Session, owned here and captured by the factory closure
        # (ADR 0079). The auth session belongs to the operator at the station, not to the per-radio
        # controller, so it must outlive a live backend switch: `holder.rebuild` rebuilds the
        # controller via this same closure, injecting the SAME Session — an authenticated operator's
        # state + inactivity clock survive the swap. The AuthGate is rebuilt fresh (stateless re: the
        # session; it re-wires to the new dispatcher/station ID) and just operates on this Session.
        auth_session = Session()

        def controller_factory(cf_settings: Settings, cf_radio: Radio) -> Controller | None:
            return build_controller(
                cf_settings,
                radio=cf_radio,
                totp_secret=secrets.totp_secret,
                fixed_code=secrets.fixed_code,
                service_bindings=service_bindings,
                mumble_entries=mumble_entries,
                plugins=service_plugins,
                session=auth_session,
            )

        controller = controller_factory(settings, radio)
    # The station ledger (ADR 0018): open the JSONL sink at the composition root so a set-but-
    # unwritable logging.path fails loud here, alongside the other composition-time opens.
    event_log = EventLog(JsonlSink(load_log_path(settings)))
    # Audio recording (ADR 0020): opt-in via recording.enabled (default off → None). When on, the
    # Recorder is opened here so a set-but-unwritable recording.path fails loud at the composition
    # root. TX recording (ADR 0021) is a separate opt-in (recording.tx) with a `tx-` prefix.
    recorder = build_recorder(settings)
    tx_recorder = build_tx_recorder(settings)
    # Safety rail (ADR 0021): with audio.squelch=off there is no gate-close edge, so RX segmentation
    # is purely time-based (the recording.max_seconds roll), not activity-based. That is bounded
    # and safe, but surprising — warn once at startup rather than silently. Do not fail.
    if load_record_enabled(settings) and load_squelch_mode(settings) is SquelchMode.OFF:
        logger.warning(
            "recording.enabled is on with audio.squelch=off: there is no gate-close edge, so RX "
            "recordings are segmented by time (recording.max_seconds roll), not by activity. "
            "Set audio.squelch=audio|cat for one WAV per received transmission."
        )
    # The shared streaming station-ID scheduler (ADR 0041, Part 97): identifies BOTH the browser
    # `/audio/tx` talker and the Mumble bridge, closing the pre-existing gap where streaming TX went
    # out un-ID'd. Gated on a configured callsign — the default no-callsign app has nothing to ID
    # with, so it stays `None` (streaming TX unchanged) rather than failing loud at startup. The
    # encoder is the same CW/voice one the controller uses (`station.id_mode`); CW needs no TTS.
    streaming_id = None
    if settings.is_set("station.callsign"):
        streaming_id = StreamingId(
            build_id_encoder(settings),
            load_callsign(settings),
            interval=load_id_interval(settings),
            mode=load_id_mode(settings),
        )
    return create_app(
        radio,
        api_token=secrets.require("api_token"),
        controller=controller,
        controller_factory=controller_factory,
        rx_gate=build_rx_gate(settings, radio=radio),
        tx_idle_timeout=load_tx_idle_timeout(settings),
        station_id=streaming_id,
        event_log=event_log,
        recorder=recorder,
        tx_recorder=tx_recorder,
        mumble_entries=mumble_entries,
        mumble_client_factory=(
            _pymumble_client_factory(
                secrets,
                link_username(load_callsign(settings) if settings.is_set("station.callsign") else None),
            )
            if mumble_entries
            else None
        ),
        mumble_tx_hang=settings.get("mumble.tx_hang"),
        mumble_dtmf_mute=settings.get("mumble.dtmf_mute"),
        mumble_dtmf_mute_hold=settings.get("mumble.dtmf_mute_hold"),
        restart_command=settings.get("server.restart_command"),
        web_dir=settings.get("server.web_dir"),
        settings=settings,
        config_path=config_path,
        secrets=secrets,
        secrets_path=secrets_path,
        service_plugins=service_plugins,
    )
