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
import os
from dataclasses import asdict

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
from pydantic import BaseModel

from ..activity import build_rx_gate
from ..arbiter import RadioArbiter
from ..audio import CANONICAL_FORMAT, AudioFormatMismatch, AudioFrame
from ..auth import SECRET_ENV_VAR
from ..backends import Capability, Radio, UnsupportedCapability, create_radio
from ..controller import (
    Controller,
    ControllerEvent,
    ControllerRunner,
    build_controller,
    load_controller_poll,
)
from ..eventlog import EventLog, JsonlSink, load_log_path
from ..recording import Recorder, build_recorder
from ..rx import (
    AudioHub,
    RxActivityGate,
    RxPump,
    RxRecorder,
    null_recorder,
    pass_through_gate,
)
from ..scan import ScanEvent, ScanPlan, build_scan_engine
from ..tx import (
    DEFAULT_TX_IDLE_TIMEOUT,
    TxSession,
    TxSlot,
    load_tx_idle_timeout,
    parse_tx_format,
)
from .auth import (
    RADIO_API_TOKEN_ENV_VAR,  # noqa: F401  (re-exported via package __init__)
    load_api_token,
    make_require_token,
    token_matches,
)
from .events import Event, EventHub, status_event

#: Environment variable selecting the backend for `build_app`. Defaults to the mock so the
#: composition root is exercisable without hardware; real backends raise on construction until
#: their bring-up cycle.
RADIO_BACKEND_ENV_VAR = "RADIO_BACKEND"


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


def create_app(
    radio: Radio,
    *,
    api_token: str,
    controller: Controller | None = None,
    runner: ControllerRunner | None = None,
    rx_gate: RxActivityGate = pass_through_gate,
    tx_idle_timeout: float = DEFAULT_TX_IDLE_TIMEOUT,
    event_log: EventLog | None = None,
    recorder: Recorder | None = None,
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
        yield
        # Belt-and-suspenders: stop the RX pump on shutdown so a client still connected at
        # teardown never leaks the pump task. `stop()` is idempotent, so this is harmless when
        # the last `/audio/rx` disconnect already stopped it. The await runs in the app loop
        # (not a per-connection cancel scope), so it reliably joins the task.
        await app_.state.rx_pump.stop()
        # The pump's own teardown finalizes any open recording segment; close() is an idempotent
        # belt-and-suspenders that also releases the handle if the pump never ran.
        if app_.state.recorder is not None:
            app_.state.recorder.close()
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
    rx_pump = RxPump(
        radio, audio_hub, gate=rx_gate, arbiter=arbiter, recorder=recorder or null_recorder
    )
    # TX audio ingest (ADR 0016): the mirror direction. One single-talker slot guards the shared
    # transmitter (you cannot key one radio from two clients); each `/audio/tx` connection builds
    # its own per-stream `TxSession` (keying + idle timeout). No hub/pump — TX is fan-in, not
    # fan-out — and no lifespan teardown, since a session tears itself down in the endpoint's
    # `finally` (drops PTT, releases the slot).
    tx_slot = TxSlot()
    app.state.radio = radio
    app.state.hub = hub
    app.state.audio_hub = audio_hub
    app.state.rx_pump = rx_pump
    app.state.tx_slot = tx_slot
    app.state.arbiter = arbiter
    app.state.tx_idle_timeout = tx_idle_timeout
    app.state.event_log = event_log
    app.state.recorder = recorder
    app.state.api_token = api_token
    app.state.controller = controller
    app.state.runner = runner
    app.state.controller_task = None

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
        else:
            hub.publish(
                Event(type="session", data={"phase": phase, **(event.data or {})})
            )

    if controller is not None:
        controller.on_event = _publish_controller

    def _controller_state() -> dict | None:
        if controller is None or runner is None:
            return None
        return {
            "running": runner.running,
            "session_open": controller.session.authenticated,
        }

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

    @api.get("/status")
    def get_status() -> dict:
        # RadioStatus is a frozen dataclass; asdict gives the exact JSON shape (note the
        # field is `transmitting`, not `ptt`). The `controller` block is null when no
        # controller was wired in; otherwise it carries running + session-open live state.
        return {**asdict(radio.status()), "controller": _controller_state()}

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

    def _publish_scan(event: ScanEvent) -> None:
        # Adapt a scan-engine event to a "scan" event on the shared hub. Keeping the adapter
        # here (not in the scan package) is what lets scan stay below the API with no cycle.
        hub.publish(
            Event(
                type="scan",
                data={
                    "phase": event.phase,
                    "frequency": event.frequency,
                    "channel": event.channel,
                },
            )
        )

    @api.post("/scan")
    def scan(body: ScanBody) -> dict:
        # Gated exactly like the other CAT endpoints: 501 (naming "scan") on an audio-only
        # backend. This cycle runs one synchronous sweep that stops-and-holds at the first
        # active channel; the live real-time pump is a later controller-loop cycle.
        _require_cat(Capability.SCAN)
        plan = _scan_plan(body)
        try:
            engine = build_scan_engine(radio=radio, plan=plan, on_event=_publish_scan)
            held = engine.sweep()
        except UnsupportedCapability as exc:  # pragma: no cover - pre-check already guards
            raise _unsupported(exc.capability) from exc
        hub.publish(status_event(radio))
        return {"held": held, "status": asdict(radio.status())}

    @api.post("/controller")
    async def controller_route(body: ControllerBody) -> dict:
        # Start/stop the live loop. A clear 503 (not a silent no-op) when no controller was
        # configured — the same fail-loud posture as the CAT capability gate.
        if controller is None or runner is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="controller not configured in this deployment",
            )
        if body.on:
            if app.state.controller_task is None:
                app.state.controller_task = asyncio.create_task(runner.run())
                # Yield so run() flips `running` True (and does one step) before we report.
                await asyncio.sleep(0)
        else:
            runner.stop()
            task = app.state.controller_task
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                app.state.controller_task = None
        hub.publish(status_event(radio))
        return {"controller": _controller_state()}

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
        queue = audio_hub.subscribe()
        if audio_hub.subscriber_count == 1:
            rx_pump.start()
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
            if audio_hub.subscriber_count == 0:
                await rx_pump.stop()

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
        if not tx_slot.try_acquire():
            # One transmitter, one talker: refuse a second concurrent client (can't key twice).
            # 1013 "try again later" — distinct from the 1008 token rejection — closed before
            # accept, so no second stream is ever keyed.
            await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER)
            return
        await websocket.accept()
        # `on_key` publishes the same `ptt` on/off events the REST `/ptt` path does, so streaming-TX
        # keying lands in the ledger as `tx_key_up`/`tx_key_down` (with duration) too (ADR 0019).
        session = TxSession(
            radio,
            idle_timeout=app.state.tx_idle_timeout,
            arbiter=arbiter,
            on_key=lambda on: hub.publish(Event(type="ptt", data={"on": on})),
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

    return app


def build_app(env: dict[str, str] | os._Environ = os.environ) -> FastAPI:
    """Compose the app from the environment — the top-level composition root.

    Selects the backend via ``RADIO_BACKEND`` (default ``mock``) and loads the bearer token
    fail-loud via `load_api_token`. Mirrors `build_id_encoder`'s env-first shape; raises
    loudly (via `load_api_token`) when ``RADIO_API_TOKEN`` is unset rather than serving open.
    The RX squelch/VAD gate is selected via ``RADIO_SQUELCH`` (default ``off`` → relay everything,
    the cycle-13 behavior); see `build_rx_gate` (ADR 0015). The TX ingest idle timeout is read from
    ``RADIO_TX_IDLE_TIMEOUT`` (marked verify-on-hardware default); see `load_tx_idle_timeout`
    (ADR 0016). The station ledger writes to ``RADIO_LOG_PATH`` (marked default), opened fail-loud
    here; see `EventLog`/`JsonlSink` (ADR 0018). Audio recording is opt-in via ``RADIO_RECORD``
    (default off); when on, received audio is written to ``RADIO_RECORD_PATH`` as WAV segments,
    opened fail-loud here; see `build_recorder`/`Recorder` (ADR 0020).

    The live controller loop is wired only when the deployment configures it — gated on
    ``RADIO_TOTP_SECRET`` being present, since `build_controller` fails loud without the auth
    secret (and, in production, needs multimon + a TTS voice). Without it the app is exactly the
    prior REST/WS surface: ``/controller`` reports 503 and ``/status`` carries a null controller
    block. Full production wiring (real multimon/piper) lands with the hardware bring-up.
    """
    radio = create_radio(env.get(RADIO_BACKEND_ENV_VAR, "mock"))
    controller: Controller | None = None
    runner: ControllerRunner | None = None
    if env.get(SECRET_ENV_VAR):
        controller = build_controller(env, radio=radio)
        runner = ControllerRunner(radio, controller, poll=load_controller_poll(env))
    # The station ledger (ADR 0018): open the JSONL sink at the composition root so a set-but-
    # unwritable RADIO_LOG_PATH fails loud here, alongside the other load_* loaders.
    event_log = EventLog(JsonlSink(load_log_path(env)))
    # Audio recording (ADR 0020): opt-in via RADIO_RECORD (default off → None). When on, the
    # Recorder is opened here so a set-but-unwritable RADIO_RECORD_PATH fails loud at the
    # composition root, alongside the other loaders.
    recorder = build_recorder(env)
    return create_app(
        radio,
        api_token=load_api_token(env),
        controller=controller,
        runner=runner,
        rx_gate=build_rx_gate(env, radio=radio),
        tx_idle_timeout=load_tx_idle_timeout(env),
        event_log=event_log,
        recorder=recorder,
    )
