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

from ..audio import AudioFrame
from ..backends import Capability, Radio, UnsupportedCapability, create_radio
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


def create_app(radio: Radio, *, api_token: str) -> FastAPI:
    """Build the API over an injected ``radio`` and shared-secret ``api_token``.

    The DI seam for tests: ``create_app(MockRadio(supports_cat=...), api_token="secret")``.
    Holds one :class:`EventHub` shared by all WebSocket connections. REST routes are gated by
    the bearer-token dependency; the WebSocket authenticates via a ``?token=`` query parameter
    (browsers cannot set headers on a WebSocket handshake).
    """
    app = FastAPI(title="radio-server API", version="0.1.0")
    hub = EventHub()
    app.state.radio = radio
    app.state.hub = hub
    app.state.api_token = api_token

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
        # field is `transmitting`, not `ptt`).
        return asdict(radio.status())

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

    return app


def build_app(env: dict[str, str] | os._Environ = os.environ) -> FastAPI:
    """Compose the app from the environment — the top-level composition root.

    Selects the backend via ``RADIO_BACKEND`` (default ``mock``) and loads the bearer token
    fail-loud via `load_api_token`. Mirrors `build_id_encoder`'s env-first shape; raises
    loudly (via `load_api_token`) when ``RADIO_API_TOKEN`` is unset rather than serving open.
    """
    radio = create_radio(env.get(RADIO_BACKEND_ENV_VAR, "mock"))
    return create_app(radio, api_token=load_api_token(env))
