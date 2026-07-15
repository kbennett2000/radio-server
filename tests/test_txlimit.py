"""The TX time limiter — a pure policy that bounds a stuck-on transmission (ADR 0045).

`tx.idle_timeout` catches a stream that goes silent; this catches the opposite runaway — continuous
audio that never goes silent (a stuck VOX, a looped bridge), which would key the radio indefinitely.
`TxLimiter` is a policy oracle: told the keying edges, it answers `expired`/`may_key` and fires
`on_change` on real transitions. It touches no radio and imports no clock — every time-dependent method
takes an explicit `now`, so the whole policy is driven here from a fake clock (plain floats), no sleeps.

Two instruments: direct unit tests of the policy, and an AST test proving `txlimit/` imports nothing
from `radio_server` (the strictest leaf discipline — even stdlib `time` is absent).
"""

from __future__ import annotations

import ast
import pathlib

from radio_server.txlimit import TxLimiter, TxLimitState

MAX = 180.0
COOLOFF = 10.0


def _limiter(**kwargs) -> TxLimiter:
    return TxLimiter(MAX, COOLOFF, **kwargs)


# --- expiry -------------------------------------------------------------------------------------


def test_expired_only_after_max_seconds_of_a_key_down():
    lim = _limiter()
    assert lim.expired(0.0) is False  # not keyed → cannot be expired
    lim.key_down(0.0)
    assert lim.expired(0.0) is False
    assert lim.expired(MAX - 0.001) is False  # just under the limit
    assert lim.expired(MAX) is True  # at the limit
    assert lim.expired(MAX + 100) is True


def test_a_normal_stop_before_expiry_never_fires_and_starts_no_cooloff():
    lim = _limiter()
    lim.key_down(0.0)
    lim.key_up(MAX - 50)  # peer stopped on its own, well under the limit
    assert lim.expired(MAX + 1000) is False  # not keyed → never expired
    assert lim.may_key(MAX - 50) is True  # a normal stop opens no cooloff
    assert lim.state(MAX - 50) is TxLimitState.IDLE


# --- cooloff ------------------------------------------------------------------------------------


def test_cooloff_refuses_rekey_then_permits_it():
    lim = _limiter()
    lim.key_down(0.0)
    lim.force_unkey(MAX)  # the limit forced the unkey at t=MAX
    assert lim.may_key(MAX) is False  # refuses immediately
    assert lim.state(MAX) is TxLimitState.COOLOFF
    assert lim.may_key(MAX + COOLOFF - 0.001) is False  # still refusing
    assert lim.may_key(MAX + COOLOFF) is True  # cooloff elapsed → permitted
    assert lim.state(MAX + COOLOFF) is TxLimitState.IDLE  # derived back to idle, no event needed


def test_rekey_after_cooloff_elapsed_starts_a_fresh_keyed_period():
    lim = _limiter()
    lim.key_down(0.0)
    lim.force_unkey(MAX)
    lim.key_down(MAX + COOLOFF + 5)  # caller re-keys after cooloff
    assert lim.state(MAX + COOLOFF + 5) is TxLimitState.KEYED
    assert lim.may_key(MAX + COOLOFF + 5) is True
    assert lim.expired(MAX + COOLOFF + 5) is False  # a fresh key-down, timer restarts


def test_may_key_is_true_while_keyed_and_when_idle():
    lim = _limiter()
    assert lim.may_key(0.0) is True  # idle
    lim.key_down(0.0)
    assert lim.may_key(1.0) is True  # keyed is not a cooloff — only cooloff refuses


# --- on_change: fires only on real transitions --------------------------------------------------


def test_on_change_fires_only_on_real_state_transitions():
    seen: list[TxLimitState] = []
    lim = _limiter(on_change=seen.append)
    lim.key_down(0.0)  # IDLE -> KEYED
    lim.key_up(10.0)  # KEYED -> IDLE
    lim.key_down(20.0)  # IDLE -> KEYED
    lim.force_unkey(MAX + 20)  # KEYED -> COOLOFF
    assert seen == [
        TxLimitState.KEYED,
        TxLimitState.IDLE,
        TxLimitState.KEYED,
        TxLimitState.COOLOFF,
    ]


def test_on_change_is_silent_on_no_op_calls():
    seen: list[TxLimitState] = []
    lim = _limiter(on_change=seen.append)
    lim.key_up(0.0)  # not keyed → no-op
    lim.force_unkey(0.0)  # not keyed → no-op
    lim.key_down(1.0)  # IDLE -> KEYED (the only real edge)
    lim.key_down(2.0)  # already keyed → no-op (does not restart or re-fire)
    assert seen == [TxLimitState.KEYED]


def test_repeated_key_down_keeps_the_original_start_time():
    lim = _limiter()
    lim.key_down(0.0)
    lim.key_down(100.0)  # a spurious second key_down must not reset the clock
    assert lim.expired(MAX) is True  # still measured from t=0, not t=100


def test_no_callback_is_safe():
    lim = _limiter()  # on_change=None
    lim.key_down(0.0)
    lim.force_unkey(MAX)  # must not raise with no callback wired
    assert lim.state(MAX) is TxLimitState.COOLOFF


# --- acceptance: txlimit/ imports NOTHING from radio_server (strictest leaf) ---------------------


def _absolute_import_targets(path: pathlib.Path, package: str) -> set[str]:
    """Resolve every `import`/`from` in `path` to absolute module names.

    `package` is the dotted package the file lives in (e.g. "radio_server.txlimit"), used to resolve
    relative (`from ..x`) imports.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    targets.add(node.module)
            else:
                # Resolve `level` dots relative to `package`: level 1 = this package, 2 = parent, ...
                base = package.split(".")
                trimmed = base[: len(base) - (node.level - 1)]
                prefix = ".".join(trimmed)
                targets.add(f"{prefix}.{node.module}" if node.module else prefix)
    return targets


def test_txlimit_package_imports_nothing_from_radio_server():
    txlimit_dir = pathlib.Path(__file__).resolve().parent.parent / "radio_server" / "txlimit"
    offenders: dict[str, set[str]] = {}
    for py in sorted(txlimit_dir.glob("*.py")):
        targets = _absolute_import_targets(py, "radio_server.txlimit")
        bad = {
            t
            for t in targets
            if t.startswith("radio_server") and not t.startswith("radio_server.txlimit")
        }
        if bad:
            offenders[py.name] = bad
    assert offenders == {}, f"txlimit/ must import nothing from radio_server; found: {offenders}"
