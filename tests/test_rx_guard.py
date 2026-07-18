"""The post-transmit RX guard (ADR 0085): armed on the arbiter's TX→RX edge.

These are composition-level tests — they build the real app and drive the real `RadioArbiter`, so
they exercise the actual `on_change` arming closure in `create_app` (not a reimplementation). The
arbiter is source-agnostic: a plain `acquire_tx()`/`release_tx()` is exactly the path *both* a browser
talker (`/audio/tx`) and the Mumble bridge's own Mumble→RF transmit take, so this covers the
"arms on a browser-talker release too" requirement. The frame-level suppression is tested against the
bridge fakes in `test_link_bridge.py`.
"""

from __future__ import annotations

from radio_server.api.app import build_app

from .conftest import make_secrets, make_settings

SECRETS = make_secrets(api_token="rx-guard-token")


def _build(tmp_path, overrides=None):
    base = {"logging.path": str(tmp_path / "log.jsonl")}
    base.update(overrides or {})
    # A nonexistent config_path keeps the [[mumble.servers]] load empty and deterministic.
    return build_app(make_settings(base), SECRETS, config_path=str(tmp_path / "absent.toml"))


def test_release_tx_arms_the_rx_guard(tmp_path):
    # The TX→RX edge — any local TX source releasing (browser talker or Mumble bridge) — arms the
    # guard for the configured window.
    app = _build(tmp_path)
    arbiter, guard = app.state.arbiter, app.state.rx_guard
    assert not guard.muted()  # idle: nothing to suppress
    arbiter.acquire_tx()
    assert not guard.muted()  # keyed: still not armed (we suppress *after* TX ends)
    arbiter.release_tx()
    assert guard.muted()  # TX→RX turnaround: guard is now suppressing the Mumble feed


def test_rx_guard_seconds_zero_disables(tmp_path):
    # 0 keeps today's behaviour: the relay resumes the instant TX releases, no guard.
    app = _build(tmp_path, {"mumble.rx_guard_seconds": "0"})
    arbiter, guard = app.state.arbiter, app.state.rx_guard
    arbiter.acquire_tx()
    arbiter.release_tx()
    assert not guard.muted()


def test_rx_guard_not_armed_on_non_tx_edges(tmp_path):
    # Only leaving TRANSMITTING arms it — the RX pump's own IDLE↔RECEIVING flips must not.
    app = _build(tmp_path)
    arbiter, guard = app.state.arbiter, app.state.rx_guard
    arbiter.begin_receive()  # IDLE → RECEIVING
    assert not guard.muted()
    arbiter.end_receive()  # RECEIVING → IDLE
    assert not guard.muted()
