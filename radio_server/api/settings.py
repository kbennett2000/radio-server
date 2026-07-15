"""The settings REST surface — the config system (ADR 0025) served over HTTP (ADR 0026).

A thin, schema-driven layer: `GET /settings` serializes the `SettingSpec` registry with current
values, `PATCH /settings` validates a partial map against the schema and round-trips it to
``radio.toml``, and two write-only endpoints rotate the secrets. There is **no per-setting logic** —
adding a setting to the registry adds it to the API for free. Validation is `resolve_settings`,
persistence is `save_settings`/`save_secret`/`rotate`; this module only serializes the schema and
marshals errors to `400`s.

Security: secrets are never in the `SETTINGS` schema, so a secret value can never appear in `GET`.
The read path reports only whether each secret is *set*; the write path reveals a freshly-minted
secret exactly once and never reads an existing one back.

Apply semantics are **restart-to-apply (v1)**: writes persist to file but do not reconfigure the
running server — every write response says which values need a restart.
"""

from __future__ import annotations

from enum import Enum
from typing import Any
from zoneinfo import ZoneInfoNotFoundError

from fastapi import APIRouter, FastAPI, HTTPException, status
from pydantic import BaseModel

from ..auth import TotpVerifier
from ..config import (
    BY_KEY,
    KNOWN_SECRETS,
    SETTINGS,
    resolve_settings,
    rotate,
    save_secret,
    save_settings,
)
from ..config.spec import SettingSpec, coerce_id_mode
from ..services.voice_id import ID_MODES


class PatchBody(BaseModel):
    """A partial settings update: dotted-key → new value."""

    values: dict[str, Any]


class ApiTokenRotate(BaseModel):
    """Optional explicit API token; omitted → the server generates one."""

    token: str | None = None


class TotpEnroll(BaseModel):
    """The account label to embed in the enrollment URI."""

    account: str = "radio-server"


def _json_value(value: Any) -> Any:
    """A JSON-native rendering of a resolved value (enums → their string ``.value``)."""
    return value.value if isinstance(value, Enum) else value


def _setting_type(spec: SettingSpec) -> tuple[str, list[str] | None]:
    """Derive ``(type, choices)`` from the schema — a generic pass, not per-setting logic.

    ``bool`` is checked before ``int`` because ``bool`` subclasses ``int``. Enum settings expose
    their choices; ``station.id_mode`` is enum-like (a fixed tuple, not a `StrEnum`) so it is keyed
    off its coercer.
    """
    default = spec.default
    if spec.coerce is coerce_id_mode:
        return "enum", list(ID_MODES)
    if isinstance(default, bool):
        return "boolean", None
    if isinstance(default, Enum):
        return "enum", [m.value for m in type(default)]
    if isinstance(default, int):
        return "integer", None
    if isinstance(default, float):
        return "number", None
    return "string", None


def _serialize_setting(spec: SettingSpec, settings: Any) -> dict[str, Any]:
    type_, choices = _setting_type(spec)
    entry: dict[str, Any] = {
        "key": spec.key,
        "group": spec.group,
        "type": type_,
        "default": None if spec.required else _json_value(spec.default),
        "value": _json_value(settings.get(spec.key)) if settings.is_set(spec.key) else None,
        "required": spec.required,
        "description": spec.description,
    }
    if choices is not None:
        entry["choices"] = choices
    return entry


def _secrets_presence(app: FastAPI) -> dict[str, dict[str, bool]]:
    """Report only whether each secret is set — never a value."""
    secrets = getattr(app.state, "secrets", None)
    if secrets is not None:
        api_set = secrets.api_token is not None
        totp_set = secrets.totp_secret is not None
    else:  # bare create_app without a Secrets: infer from what is wired.
        api_set = bool(app.state.api_token)
        totp_set = app.state.controller is not None
    return {"api_token": {"set": api_set}, "totp_secret": {"set": totp_set}}


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def register_settings_routes(api: APIRouter, app: FastAPI) -> None:
    """Attach the settings + secret-rotation routes to the token-gated ``api`` router."""

    @api.get("/settings")
    def get_settings() -> dict[str, Any]:
        settings = app.state.settings
        return {
            "settings": [_serialize_setting(spec, settings) for spec in SETTINGS],
            "secrets": _secrets_presence(app),
            "apply": "restart",
        }

    @api.patch("/settings")
    def patch_settings(body: PatchBody) -> dict[str, Any]:
        patch = body.values
        if not patch:
            raise _bad_request("no values to update")
        # Reject secret and unknown keys up front, with a clear message, before any validation.
        for key in patch:
            if key in KNOWN_SECRETS:
                raise _bad_request(
                    f"{key!r} is a secret and cannot be set via /settings; "
                    "use the secret rotation endpoints"
                )
            if key not in BY_KEY:
                raise _bad_request(f"unknown setting {key!r}")
        # Validate the WHOLE patch atomically by resolving {current values} | patch. resolve_settings
        # coerces/range-checks every value and raises (naming the bad key) BEFORE any file write.
        current = app.state.settings
        base = {spec.key: current.get(spec.key) for spec in SETTINGS if current.is_set(spec.key)}
        try:
            new = resolve_settings({**base, **patch})
        except (RuntimeError, ZoneInfoNotFoundError) as exc:
            raise _bad_request(str(exc)) from exc  # atomic: nothing written
        save_settings(new, app.state.config_path)
        app.state.settings = new  # so GET reflects the persisted file (display; run is unchanged)
        changed = sorted(patch)
        return {"updated": changed, "restart_required": changed, "apply": "restart"}

    @api.post("/settings/secrets/api-token/rotate")
    def rotate_api_token(body: ApiTokenRotate | None = None) -> dict[str, Any]:
        provided = body.token if (body and body.token) else None
        if provided:
            save_secret(app.state.secrets_path, "api_token", provided)
            token = provided
        else:
            token = rotate(app.state.secrets_path, "api_token")
        return {
            "api_token": token,
            "restart_required": True,
            "note": (
                "re-authenticate with this token after restarting the server; "
                "it is shown only once"
            ),
        }

    @api.post("/settings/secrets/totp/enroll")
    def enroll_totp(body: TotpEnroll | None = None) -> dict[str, Any]:
        secret = rotate(app.state.secrets_path, "totp_secret")  # always a fresh secret
        account = body.account if (body and body.account) else "radio-server"
        uri = TotpVerifier(secret).provisioning_uri(account)
        return {
            "provisioning_uri": uri,
            "secret": secret,
            "restart_required": True,
            "note": (
                "shown only once; re-enroll your authenticator with this URI, then restart "
                "the server"
            ),
        }
