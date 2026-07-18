"""Multi-backend config: validate and enumerate the backends radio.toml declares (ADR 0074).

`radio.toml` can describe more than one backend block (`[baofeng]` and `[kv4p]`). `server.backend`
picks which one the holder builds at startup — the *initial* selection — but every *configured*
backend (its block present, plus the active one) is validated at load, so a switch target with a
broken block fails loudly at startup rather than the moment someone selects it live (the ADR 0051
"latent config surfaces on a restart months later" lesson).

This module is deliberately light — pure `settings → backend policy`, no pipeline imports — so both
the composition root (`build_radio` in `holder.py`) and the `doctor` CLI can use it without dragging
in `RxPump`/`ScanRunner`/etc. Validation is pure: a hardware backend cannot be *constructed* to
validate it (construction opens a serial port / handshakes, and v71 raises), so the checks run against
`settings` alone.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..activity import SquelchMode, load_squelch_mode
from ..backends.kv4p.radio import default_freq_range_hz
from ..config import Settings


def backend_kwargs(settings: Settings, backend: str) -> dict[str, Any]:
    """The constructor kwargs `create_radio(backend, ...)` would receive for ``backend``.

    Extracted verbatim from `build_radio`'s switch (ADR 0073) so the enumeration and the swap cycle
    can read a backend's *resolved settings* without re-deriving the mapping. Pure — no radio built.
    """
    if backend == "mock":
        # Mock-only CAT toggle (ADR 0022): a full-CAT mock by default, or audio-only when off.
        return {"supports_cat": settings.get("server.mock_cat")}
    elif backend == "baofeng":
        return {
            "serial_port": settings.get("baofeng.serial_port"),
            "ptt_line": settings.get("baofeng.ptt_line"),
            "input_device": settings.get("baofeng.input_device"),
            "output_device": settings.get("baofeng.output_device"),
            "blocksize": settings.get("baofeng.blocksize"),
            "tx_lead_seconds": settings.get("baofeng.tx_lead_seconds"),
        }
    elif backend == "kv4p":
        return {
            "serial_port": settings.get("kv4p.serial_port"),
            "module_type": settings.get("kv4p.module_type"),
            "squelch": settings.get("kv4p.squelch"),
            "tx_lead_seconds": settings.get("kv4p.tx_lead_seconds"),
            "high_power": settings.get("kv4p.high_power"),
            "tx_allowed": settings.get("kv4p.tx_allowed"),
            "frequency": settings.get("kv4p.frequency"),
            "sample_rate_correction": settings.get("kv4p.sample_rate_correction"),
        }
    # mock/v71 with no block, or an unknown name: no per-backend kwargs (v71/unknown raise in
    # create_radio, preserving the old `build_radio` else-branch `create_radio(backend)`).
    return {}


def validate_backend_config(
    settings: Settings, backend: str, *, include_construction_checks: bool
) -> None:
    """Validate ``backend``'s config block against ``settings`` — pure, fail-loud (ADR 0074).

    Never constructs the backend (that would open hardware / raise for v71). Two tiers of check:

    - **Always** — the cross-field guards a backend *constructor never performs*: the two
      `audio.squelch=cat` guards. These ran only for the active backend before this cycle (inside
      `build_radio`); running them for every configured backend is the point of the multi-backend
      validation. Messages are verbatim from the old `build_radio`.
    - **When ``include_construction_checks``** — the checks the *constructor* would do, needed only
      for a backend we will not construct (an inactive switch target): today, the kv4p frequency band
      check, against the module-type **default** band (there is no device at load to report a real
      range — the same fallback a HELLO-less `Kv4pHt` uses). The active backend leaves this to
      construction (HELLO-aware), so pass ``include_construction_checks=False`` for it.
    """
    mode = load_squelch_mode(settings)
    if backend == "baofeng":
        # The UV-5R has no hardware busy line (ADR 0015), so audio.squelch=cat would poll a radio
        # that never reports busy. Fail loud rather than gate on a line that does not exist.
        if mode is SquelchMode.CAT:
            raise RuntimeError(
                "audio.squelch=cat is invalid for server.backend='baofeng': the UV-5R has no "
                "hardware busy line. Use audio.squelch=audio (software VAD) or off."
            )
    elif backend == "kv4p":
        # kv4p HAS a busy line, so cat is valid — but only with a non-zero squelch: at level 0 the SQ
        # pin never asserts, so 'busy' reads True forever and a CAT-squelch scan dwells everywhere.
        if mode is SquelchMode.CAT and settings.get("kv4p.squelch") == 0:
            raise RuntimeError(
                "audio.squelch=cat needs a non-zero kv4p.squelch: at squelch level 0 the kv4p's "
                "hardware busy line never asserts, so 'busy' reads True forever and a CAT-squelch "
                "scan dwells on every channel. Set kv4p.squelch to a non-zero level (1-8), or use "
                "audio.squelch=audio (software VAD) or off."
            )
        if include_construction_checks:
            frequency = settings.get("kv4p.frequency")
            if frequency is not None:
                low, high = default_freq_range_hz(settings.get("kv4p.module_type"))
                if not low <= frequency <= high:
                    raise RuntimeError(
                        f"kv4p.frequency {frequency} Hz is out of band [{low}, {high}] for the "
                        f"configured kv4p.module_type={settings.get('kv4p.module_type')!r}. A "
                        f"configured switch target that can't tune fails at load, not when you "
                        f"select it live (ADR 0074) — fix kv4p.frequency or kv4p.module_type."
                    )
    # mock / v71 / unknown: no cross-field config to validate here.


def validate_configured_backends(settings: Settings) -> None:
    """Validate every configured backend's block at load, *except* the active one (ADR 0074).

    The active backend is validated where it always was: its squelch guard in `build_radio`, its
    frequency when the real backend is constructed (HELLO-aware). The inactive present blocks are
    never constructed, so they get the full pure check here (``include_construction_checks=True``).
    Presence-scoped: a single-backend config has no inactive block and this is a no-op — its boot is
    byte-identical to before this cycle.
    """
    active = settings.get("server.backend")
    for name in sorted(settings.configured_backend_names()):
        if name == active:
            continue
        validate_backend_config(settings, name, include_construction_checks=True)


@dataclass(frozen=True)
class BackendChoice:
    """One configured backend, for the swap cycle's select endpoint + UI dropdown (ADR 0074).

    ``name`` is the backend id (``"baofeng"``/``"kv4p"``/``"mock"``); ``active`` marks the current
    `server.backend` (the initial selection); ``settings`` is that backend's resolved
    :func:`backend_kwargs` — how the holder would build it. Live capabilities are *not* here: they
    require constructing the backend (touching hardware), so they stay a property of the built radio
    (`GET /capabilities`).
    """

    name: str
    active: bool
    settings: Mapping[str, Any]


def configured_backends(settings: Settings) -> tuple[BackendChoice, ...]:
    """Enumerate the backends radio.toml configures, active first (ADR 0074).

    The surface the next cycle's select endpoint + UI dropdown consume — "the backends this node is
    configured for, and how to build each." **No caller yet**: defined now so the endpoint cycle is a
    thin HTTP wrapper over this. A single-backend config yields exactly one choice.
    """
    active = settings.get("server.backend")
    names = settings.configured_backend_names()
    ordered = [active] + sorted(name for name in names if name != active)
    return tuple(
        BackendChoice(name=name, active=(name == active), settings=backend_kwargs(settings, name))
        for name in ordered
    )
