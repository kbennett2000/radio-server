"""Per-backend squelch mode — the fix for the unstartable mixed-radio box (ADR 0121).

`audio.squelch` used to be a single global mode, and ADR 0074 validated *every* configured backend
against it — so `audio.squelch=cat` (which the docked UV-K6 needs post-F3) plus any configured
audio-only block (`[baofeng]`) was an unstartable config. This cycle gives uvk5 and baofeng their
own `squelch_mode` key (with backend-declared defaults, the `uvk5.tot` pattern); backends without a
dedicated key fall back to the global. Each backend is then validated against ITS effective mode, so
the stale-`[baofeng]`-blocks-`cat` failure disappears by construction. Proven here without hardware.
"""

from __future__ import annotations

import pytest

from radio_server.activity import CatBusyGate, SquelchMode, resolve_squelch_mode
from radio_server.activity.gate import AudioLevelGate, build_rx_gate
from radio_server.api.backend_config import validate_backend_config, validate_configured_backends
from radio_server.backends.aioc_baofeng import DEFAULT_SQUELCH_MODE as BAOFENG_DEFAULT
from radio_server.backends.mock import MockRadio
from radio_server.backends.uvk5.radio import DEFAULT_SQUELCH_MODE as UVK5_DEFAULT

from .conftest import make_settings


# --- backend-declared defaults (the uvk5.tot pattern) -------------------------------------------

def test_uvk5_squelch_mode_defaults_to_the_backend_declared_cat():
    assert UVK5_DEFAULT == "cat"
    assert make_settings({}).get("uvk5.squelch_mode") is SquelchMode.CAT


def test_baofeng_squelch_mode_defaults_to_the_backend_declared_audio():
    assert BAOFENG_DEFAULT == "audio"
    assert make_settings({}).get("baofeng.squelch_mode") is SquelchMode.AUDIO


@pytest.mark.parametrize("mode", ["off", "audio", "cat"])
def test_uvk5_squelch_mode_override_lands(mode):
    assert make_settings({"uvk5.squelch_mode": mode}).get("uvk5.squelch_mode") is SquelchMode(mode)


def test_squelch_mode_rejects_an_unknown_value_naming_the_key():
    with pytest.raises(RuntimeError, match="uvk5.squelch_mode"):
        make_settings({"uvk5.squelch_mode": "loud"})
    with pytest.raises(RuntimeError, match="baofeng.squelch_mode"):
        make_settings({"baofeng.squelch_mode": "loud"})


# --- resolve_squelch_mode: per-backend key, global as fallback ----------------------------------

def test_resolve_uses_the_dedicated_key_for_uvk5_and_baofeng():
    s = make_settings({"uvk5.squelch_mode": "audio", "baofeng.squelch_mode": "off"})
    assert resolve_squelch_mode(s, "uvk5") is SquelchMode.AUDIO
    assert resolve_squelch_mode(s, "baofeng") is SquelchMode.OFF


def test_resolve_falls_back_to_the_global_for_backends_without_a_key():
    # kv4p / mock have no dedicated key — they read the global exactly as before (ADR 0121).
    s = make_settings({"audio.squelch": "cat", "kv4p.squelch": "4"})
    assert resolve_squelch_mode(s, "kv4p") is SquelchMode.CAT
    assert resolve_squelch_mode(s, "mock") is SquelchMode.CAT
    # ...and the per-backend keys are independent of that global.
    assert resolve_squelch_mode(s, "uvk5") is SquelchMode.CAT  # uvk5's own default
    assert resolve_squelch_mode(s, "baofeng") is SquelchMode.AUDIO  # baofeng's own default


# --- the headline: the mixed / stale-block config that used to be unstartable -------------------

def test_uvk5_active_with_global_cat_and_stale_baofeng_block_validates():
    # THE bench config: uvk5 needs cat; a stale [baofeng] block is present. Before ADR 0121 the
    # inactive baofeng was validated against the global cat and boot failed. Now baofeng resolves to
    # its own `audio` default, so validate_configured_backends passes.
    s = make_settings({
        "server.backend": "uvk5",
        "audio.squelch": "cat",
        "baofeng.serial_port": "/dev/ttyACM0",  # makes baofeng a configured switch target
    })
    validate_configured_backends(s)  # no raise


def test_mixed_box_baofeng_audio_and_uvk5_cat_both_validate():
    s = make_settings({
        "server.backend": "uvk5",
        "uvk5.squelch_mode": "cat",
        "baofeng.squelch_mode": "audio",
        "baofeng.serial_port": "/dev/ttyACM0",
    })
    validate_configured_backends(s)  # no raise
    validate_backend_config(s, "uvk5", include_construction_checks=False)  # active path, no raise


def test_explicit_baofeng_cat_still_rejected():
    # The guard still bites when the [baofeng] section explicitly asks for cat (no busy line).
    s = make_settings({"server.backend": "baofeng", "baofeng.squelch_mode": "cat"})
    with pytest.raises(RuntimeError, match="baofeng.squelch_mode"):
        validate_backend_config(s, "baofeng", include_construction_checks=False)


# --- build_rx_gate selects per active backend (feeds the live-switch gate rebuild) --------------

def test_build_rx_gate_gives_a_cat_gate_for_active_uvk5():
    radio = MockRadio(supports_cat=True)
    gate = build_rx_gate(make_settings({"server.backend": "uvk5"}), radio=radio)
    assert isinstance(gate, CatBusyGate)
    assert gate._radio is radio  # closes over the passed radio — what the swap must re-point


def test_build_rx_gate_gives_an_audio_gate_for_active_baofeng():
    gate = build_rx_gate(make_settings({"server.backend": "baofeng"}), radio=MockRadio())
    assert isinstance(gate, AudioLevelGate)


def test_build_rx_gate_falls_back_to_the_global_for_kv4p():
    # kv4p reads the global; audio.squelch=off → the pass-through gate (a plain callable, not a class).
    gate = build_rx_gate(make_settings({"server.backend": "kv4p"}), radio=MockRadio())
    assert not isinstance(gate, (CatBusyGate, AudioLevelGate))


# --- back-compat: a single-global config resolves unchanged for its realistic active backend -----

def test_old_single_global_configs_resolve_unchanged():
    # kv4p relies wholly on the global — unchanged.
    assert resolve_squelch_mode(make_settings(
        {"server.backend": "kv4p", "audio.squelch": "cat", "kv4p.squelch": "4"}), "kv4p"
    ) is SquelchMode.CAT
    # uvk5 with an explicit cat and baofeng with an explicit audio match their new defaults, so a
    # config that named the global to get those modes resolves to the same value.
    assert resolve_squelch_mode(make_settings(
        {"server.backend": "uvk5", "audio.squelch": "cat"}), "uvk5"
    ) is SquelchMode.CAT
    assert resolve_squelch_mode(make_settings(
        {"server.backend": "baofeng", "audio.squelch": "audio"}), "baofeng"
    ) is SquelchMode.AUDIO
