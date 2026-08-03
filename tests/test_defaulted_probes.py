"""Every probe whose default *is an answer*, pinned by name (ADR 0178).

This repo reaches optional backend surface through `getattr(radio, "name", default)`, and it has to:
ADR 0178 measured that the alternatives are unavailable here. A protocol check at the composition
root is **inoperative** — every production radio is `TotRadio`-wrapped, `TotRadio` forwards through
`__getattr__`, and CPython's `_ProtocolMeta.__instancecheck__` resolves members with
`inspect.getattr_static`, which does not invoke it, so `isinstance(TotRadio(MockRadio()), Radio)` is
`False`. Static typing forecloses itself, because `getattr(x, "literal", default)` is well-typed by
construction on an object lacking the attribute — that is the entire purpose of the three-argument
form — and nothing type-checks this repo anyway.

So the discipline is the one the codebase already keeps at ~96 % of its radio-seam probes: **default
to `None` and branch on it.** A `None` default forces the caller to write an unknown branch; a
*value* default silently answers the question the object could not.

**This scan's first act was to catch the cycle that wrote it.** ADR 0178 began with four such sites.
The fourth was `app.py`'s ``getattr(radio, "transmitting", False)`` — the instance ADR 0177 found,
whose ``False`` means "not keyed, safe to put a frame on the shared wire", measured at **0 of 81
witness carrier polls** when wrong. Extracting `_pause_source` turned it into a `None` default with
an explicit unknown branch, and this test went red on the count until the row was removed. Three
remain:

* `aioc_baofeng.py`'s `volatile` → `False` = "no need to re-assert the channel", skipping the ADR
  0143 pre-key-up confirmation on the keying path.
* two `detects_signal` → `True` = "this gate's open decision means real signal is present".

All three are currently safe and each one's enforcer is named in its row below — because "it is fine
today" is not a structure. Adding a fourth fails here and lands the reviewer on this docstring.

**The residual, stated so nobody mistakes this for a proof:** a source scan matches text, so a rename
blinds it (ADR 0167 recorded that against `test_relay_subscribers`). It is a guard, not a proof. The
consequence assert in `tests/test_cadence_pause_wiring.py` is the rename-proof half for the one site
that has a behavioural surface; this covers the other three, which do not.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent / "radio_server"

#: `file:name` → why a value default is allowed to stand there. The value is a **sentence**: a row
#: nobody can write a reason for is a row nobody has thought about.
EXPECTED = {
    "backends/aioc_baofeng.py:volatile":
        "ON THE KEYING PATH. False skips the ADR 0143 pre-key-up channel confirmation. Safe by "
        "construction, and the enforcer is nameable: `volatile` is declared on the `Uvk5Tuner` "
        "protocol and `test_every_production_tuner_satisfies_the_protocol` pins all three real "
        "tuners against it. That pin bites — a tuner missing only `volatile` fails the isinstance, "
        "verified on this interpreter (ADR 0178). If a later cycle changes this line, the fail-safe "
        "direction is to re-assert when the answer is unknown: a redundant re-assert costs ~10 ms "
        "and arms no lockout, a skipped one costs an over on a stale frequency.",
    "activity/gate.py:detects_signal":
        "mirrors the inner gate (ADR 0045). True = signal-aware, the pre-0045 behaviour, for a "
        "bare lambda. Safe because `build_rx_gate`'s whole codomain declares it.",
    "rx/pump.py:detects_signal":
        "the same default the gate applies, for the same reason, on the same objects.",
}

#: `getattr(<anything>, "<name>", <value>)` where the default is neither `None` nor another
#: `getattr`. `None`-defaulted probes are the house idiom and are deliberately not matched.
_VALUE_DEFAULTED = re.compile(
    r"""getattr\(\s*[^,()]+(?:\([^()]*\))?\s*,\s*["'](?P<name>\w+)["']\s*,\s*(?P<default>[^)]+)\)"""
)

#: Probes on objects that are not a duck-typed radio-like seam. An `argparse.Namespace`, a FastAPI
#: `app.state`, a `logging` record factory and an `OSError` are not backends of differing
#: capability; a missing flag there means "not requested", which is a real answer and not a
#: substitution for one.
_NOT_A_RADIO_SEAM = {"args", "app.state", "exc", "logging.getLogRecordFactory()", "current",
                     "secrets", "sd", "module", "preset", "entry", "defaults", "mumble",
                     "mumble.users"}


def _sites() -> dict[str, str]:
    found = {}
    for path in sorted(_ROOT.rglob("*.py")):
        rel = str(path.relative_to(_ROOT))
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("#:"):
                continue
            for m in _VALUE_DEFAULTED.finditer(line):
                default = m.group("default").strip()
                if default == "None" or default.startswith("getattr("):
                    continue
                subject = line[line.index("getattr(") + len("getattr(") :].split(",")[0].strip()
                if subject in _NOT_A_RADIO_SEAM:
                    continue
                found[f"{rel}:{m.group('name')}"] = default
    return found


def test_only_three_probes_let_a_default_stand_in_for_a_real_answer():
    found = _sites()
    assert set(found) == set(EXPECTED), (
        "The set of value-defaulted duck-typed probes changed.\n\n"
        f"  expected: {sorted(EXPECTED)}\n  found:    {sorted(found)}\n\n"
        "If you ADDED one: a value default answers the question the object could not, and the "
        "answer is indistinguishable from a measured one. Ask which way it points. If the default "
        "is the permissive direction — key up, put a frame on the wire, relay, trust — prefer a "
        "`None` default and an explicit unknown branch, which is what almost every other radio "
        "probe in this repo already does. If you keep it, add a row here saying what the default "
        "answers, what breaks if it is wrong, and what structure keeps it safe. 'It is fine "
        "today' is not a structure; name the factory, the test, or the uninstantiable class.\n\n"
        "If you REMOVED one: delete its row."
    )


def test_the_scan_can_actually_fire():
    """A scan that matches nothing passes for ever and proves nothing (ADR 0165's vacuous "0 raw").

    Three halves, really: it finds the three real sites with the three real defaults, so the pattern
    is not matching by accident; it matches a fourth that does not exist yet, so it would catch one;
    and it leaves the `None`-defaulted house idiom alone, so it is not simply matching everything.
    """
    assert _sites() == {
        "backends/aioc_baofeng.py:volatile": "False",
        "activity/gate.py:detects_signal": "True",
        "rx/pump.py:detects_signal": "True",
    }

    hypothetical = '        return bool(getattr(radio, "tx_inhibited", False))'
    m = _VALUE_DEFAULTED.search(hypothetical)
    assert m is not None and m.group("name") == "tx_inhibited"
    assert m.group("default").strip() == "False"

    # And it does not match the house idiom it is meant to leave alone.
    assert not [
        m for m in _VALUE_DEFAULTED.finditer('probe = getattr(radio, "transport_health", None)')
        if m.group("default").strip() != "None"
    ]
