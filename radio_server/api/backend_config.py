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

from ..activity import SquelchMode, resolve_squelch_mode
from ..backends.kv4p.radio import default_freq_range_hz
from ..backends.uvk5.radio import DEFAULT_FREQ_MAX_HZ as UVK5_FREQ_MAX_HZ
from ..backends.uvk5.radio import DEFAULT_FREQ_MIN_HZ as UVK5_FREQ_MIN_HZ
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
            "tx_gain": settings.get("kv4p.tx_gain"),
        }
    elif backend == "uvk5":
        return {
            "serial_port": settings.get("uvk5.serial_port"),
            "frequency": settings.get("uvk5.frequency"),
            "tone": settings.get("uvk5.tone"),
            "mode": settings.get("uvk5.mode"),
            "tx_allowed": settings.get("uvk5.tx_allowed"),
            "input_device": settings.get("uvk5.input_device"),
            "output_device": settings.get("uvk5.output_device"),
            "blocksize": settings.get("uvk5.blocksize"),
            "tx_lead_seconds": settings.get("uvk5.tx_lead_seconds"),
            "squelch_threshold": settings.get("uvk5.squelch_threshold"),
        }
    # mock/v71 with no block, or an unknown name: no per-backend kwargs (v71/unknown raise in
    # create_radio, preserving the old `build_radio` else-branch `create_radio(backend)`).
    return {}


def validate_backend_config(
    settings: Settings, backend: str, *, include_construction_checks: bool
) -> None:
    """Validate ``backend``'s config block against ``settings`` — pure, fail-loud (ADR 0074).

    Never constructs the backend (that would open hardware / raise for v71). Two tiers of check:

    - **Always** — the cross-field guards a backend *constructor never performs*: the squelch=cat
      guards, checked against the backend's EFFECTIVE mode (`resolve_squelch_mode`; ADR 0121), not the
      raw global. These ran only for the active backend before ADR 0074 (inside `build_radio`);
      running them for every configured backend is the point of the multi-backend validation, and
      resolving per-backend is what stops a global `cat` (for uvk5) from wrongly failing an inactive
      `[baofeng]` block that has no busy line.
    - **When ``include_construction_checks``** — the checks the *constructor* would do, needed only
      for a backend we will not construct (an inactive switch target): today, the kv4p frequency band
      check, against the module-type **default** band (there is no device at load to report a real
      range — the same fallback a HELLO-less `Kv4pHt` uses). The active backend leaves this to
      construction (HELLO-aware), so pass ``include_construction_checks=False`` for it.
    """
    # Each backend is validated against ITS effective mode (ADR 0121): the per-backend
    # `<backend>.squelch_mode` if set, else the global `audio.squelch`. This is what lets a box run
    # uvk5 with `cat` while a stale/configured `[baofeng]` block resolves to its own `audio` default
    # instead of choking on the global `cat` (the multi-backend unstartable-config bug).
    mode = resolve_squelch_mode(settings, backend)
    if backend == "baofeng":
        # The UV-5R has no hardware busy line (ADR 0015), so cat would poll a radio that never
        # reports busy. Fail loud rather than gate on a line that does not exist. This fires only if
        # the [baofeng] SECTION explicitly asks for cat — baofeng's own default is `audio`, so the
        # global `audio.squelch=cat` no longer reaches here (and the message names the section/key,
        # never `server.backend`, since baofeng may be an inactive switch target).
        if mode is SquelchMode.CAT:
            raise RuntimeError(
                "the [baofeng] section sets baofeng.squelch_mode=cat, but the UV-5R has no hardware "
                "busy line. Set baofeng.squelch_mode=audio (software VAD) or off."
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
    elif backend == "uvk5":
        # The UV-K5 HAS a real busy line (the reg-0x67 RSSI COS, ADR 0112), so cat is valid — but
        # only with a non-zero threshold: at 0 the gate reads busy forever and a CAT-squelch scan
        # dwells everywhere (the kv4p squelch-0 lesson, applied to uvk5's RSSI threshold). The mode
        # is uvk5's own `uvk5.squelch_mode` (default cat; ADR 0121), so the message names that key.
        if mode is SquelchMode.CAT and settings.get("uvk5.squelch_threshold") == 0:
            raise RuntimeError(
                "the [uvk5] section resolves squelch_mode=cat, which needs a non-zero "
                "uvk5.squelch_threshold: at threshold 0 the UV-K5's RSSI busy gate reads True "
                "forever, so a CAT-squelch scan dwells on every channel. Set uvk5.squelch_threshold "
                "to a non-zero level, or set uvk5.squelch_mode=audio (software VAD) or off."
            )
        if include_construction_checks:
            frequency = settings.get("uvk5.frequency")
            if not UVK5_FREQ_MIN_HZ <= frequency <= UVK5_FREQ_MAX_HZ:
                raise RuntimeError(
                    f"uvk5.frequency {frequency} Hz is out of band "
                    f"[{UVK5_FREQ_MIN_HZ}, {UVK5_FREQ_MAX_HZ}]. A configured switch target that "
                    f"can't tune fails at load, not when you select it live (ADR 0074) — fix "
                    f"uvk5.frequency."
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
