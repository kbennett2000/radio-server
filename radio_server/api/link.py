"""The network-link REST surface — enable/connect a Link over HTTP (ADR 0042).

The Link (ADR 0041) is a peer on the audio bus, not the antenna. This module exposes it on the same
token-gated router the rest of the API uses, mirroring ``api/activity.py``'s registration shape. It
routes **no audio** — that splits by direction across later cycles. It exposes only *state*: status,
the enable gate, connect/disconnect, and the peer directory.

Two disciplines are load-bearing here:

- **503 when unwired.** ``link.backend = "none"`` (the default) means ``app.state.link`` is ``None``;
  every route then answers ``503 "link not configured in this deployment"`` — the identical fail-loud
  shape ``POST /controller`` uses, never a silent no-op.
- **501 *by name* for a missing capability (guardrail 3).** ``GET /link/directory`` raises ``501``
  naming ``directory`` when the backend lacks ``DIRECTORY`` — it never returns an empty list that
  pretends the feature exists.

The enable gate itself lives in the Link (non-sticky; ADR 0041): there is no ``enabled`` config key and
no startup path to enabled, so the only route to ``enabled=True`` is ``POST /link/enable``.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, status
from pydantic import BaseModel

from ..activity import SquelchMode, load_squelch_mode
from ..controller import load_require_auth
from ..link import UnsupportedLinkCapability
from .events import Event


class LinkConnectBody(BaseModel):
    target: str


def register_link_routes(api: APIRouter, app: FastAPI) -> None:
    """Attach the ``/link`` routes to the token-gated ``api`` router."""

    def _require_link():
        # The `POST /controller` idiom (app.py): a clear 503 — not a silent no-op — when the
        # deployment did not configure a link (`link.backend = "none"` → app.state.link is None).
        link = app.state.link
        if link is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="link not configured in this deployment",
            )
        return link

    def _publish(phase: str, **fields: Any) -> None:
        # Link events originate at the API layer, so publish inline (the `/ptt` idiom) — no sub-engine
        # adapter. The ledger's `link` mapper whitelists these fields (ADR 0018).
        app.state.hub.publish(Event(type="link", data={"phase": phase, **fields}))

    @api.get("/link")
    def get_link() -> dict[str, Any]:
        return asdict(_require_link().status())

    @api.post("/link/enable")
    async def enable_link() -> dict[str, Any]:
        link = _require_link()
        # The load-bearing precondition (ADR 0044): refuse to enable when there is no squelch. With
        # `audio.squelch = "off"` the RX gate never closes, so the outbound feeder never ends its
        # stream — it would transmit the receiver's noise floor to every peer continuously. That is
        # antisocial output, not a degraded feature. Fail loud, by name — the same instinct as
        # rejecting `id_interval > 600`. "audio" or "cat" required.
        if load_squelch_mode(app.state.settings) is SquelchMode.OFF:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "cannot enable link: audio.squelch='off' has no gate edge, so the outbound feed "
                    "would transmit the receiver's noise floor to every peer continuously. Set "
                    "audio.squelch to 'audio' or 'cat'."
                ),
            )
        # The load-bearing composition refusal (ADR 0046): with `controller.require_auth` off, any DTMF
        # digits dispatch without a login. Enabling the link on top of that would let anyone on frequency
        # connect the licensee's transmitter to the network — "he makes it announce the time" becomes "a
        # stranger connects your transmitter to any reflector." Refuse loud, by name — the same instinct
        # as the squelch='off' refusal above. Auth off is a licensee's choice; pairing it with a live
        # internet link is not one this server makes for them.
        if not load_require_auth(app.state.settings):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "cannot enable link: controller.require_auth is false, so any DTMF digits dispatch "
                    "without a login. Enabling the link would let anyone on frequency connect the "
                    "transmitter to the network. Set controller.require_auth=true to enable the link."
                ),
            )
        link.enable(True)
        _publish("enabled")
        # Start the outbound feeder (ADR 0044): it subscribes to the RX hub and takes RX demand, so
        # the shared reader runs even with no browser listening. `None` when no link is configured.
        if app.state.link_feeder is not None:
            await app.state.link_feeder.start()
        return asdict(link.status())

    @api.post("/link/disable")
    async def disable_link() -> dict[str, Any]:
        link = _require_link()
        # Stop the feeder BEFORE flipping the gate: it sends a final EOT if a stream was open and drops
        # its RX demand (stopping the shared reader when nothing else wants it).
        if app.state.link_feeder is not None:
            await app.state.link_feeder.stop()
        link.enable(False)
        _publish("disabled")
        return asdict(link.status())

    @api.post("/link/connect")
    def connect_link(body: LinkConnectBody) -> dict[str, Any]:
        # `connect` is deliberately NOT gated on `enabled`: enable gates *audio routing* (a later
        # cycle's concern), not joining a reflector. Routes stay thin — one Link method each.
        link = _require_link()
        link.connect(body.target)
        _publish("connected", target=body.target)
        return asdict(link.status())

    @api.post("/link/disconnect")
    def disconnect_link() -> dict[str, Any]:
        link = _require_link()
        link.disconnect()
        _publish("disconnected")
        return asdict(link.status())

    @api.get("/link/directory")
    def link_directory() -> list[dict[str, Any]]:
        link = _require_link()
        try:
            return [asdict(station) for station in link.directory()]
        except UnsupportedLinkCapability as exc:
            # 501 *by name* (guardrail 3): name the missing capability, never an empty list pretending.
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail=f"link capability not supported by this backend: {exc.capability}",
            ) from exc
