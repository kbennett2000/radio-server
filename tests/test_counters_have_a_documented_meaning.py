"""Every counter this server reports has a documented meaning for a **nonzero** value (ADR 0179).

A counter with no reader is an instrument nobody can use; a counter with a reader and no documented
meaning is worse, because it invites a reading. ADR 0177's `wire` block is the sharp case and the
reason this file exists: ``wire_busy_at_key_up`` climbing means *the race fired and the drain
handled it*, while ``keyed_with_wire_busy`` climbing means *the station keyed with a poll on the
wire*. Those are different facts, one of them is nearly harmless and the other is the one that can
still reach the air damaged, and a reader with only the field name in front of them will treat them
alike.

So this locks the thing that actually rots: `docs/api.md` is written by hand at the end of a cycle
and **nothing fails when it isn't** — which is `test_docs_contract`'s own founding observation, one
level down. A field added to `RssiCadence` or `WireStats` without a paragraph fails here.

**Two halves, and they are not equally strong. Say so rather than letting the green imply more:**

* The dataclass half is **rename-proof**. It reads `dataclasses.fields()`, so a renamed or added
  field is caught by construction, not by matching text.
* The bridge half is a **source scan**, and a source scan is blinded by a rename (ADR 0167 recorded
  exactly that against `test_relay_subscribers`). It is a guard, not a proof. It is still worth
  having: the failure mode it catches is *a key was added to `tx_stats()` and nobody documented it*,
  which is what happened to `skipped`, `polls` and `pause_errors` between ADR 0163 and ADR 0179.

**And the residual that neither half closes:** a name appearing in a document is not a true sentence
about it. This proves a paragraph exists; it cannot prove the paragraph is right. That still needs
review, and saying so is the ADR 0147 discipline rather than letting a green suite imply an audit.
"""

from __future__ import annotations

import re
from dataclasses import fields
from pathlib import Path

import pytest

from radio_server.backends.base import RssiCadence, WireStats

_ROOT = Path(__file__).resolve().parent.parent
_API_MD = _ROOT / "docs" / "api.md"

#: `tx_stats()` keys that come off the broadcast-FM cadence. Sourced from the bridges themselves
#: below; this is the prefix that identifies them.
_DEAFENED_KEY = re.compile(r'"(deafened_\w+)"\s*:')

_BRIDGES = (
    _ROOT / "radio_server" / "link" / "bridge.py",
    _ROOT / "radio_server" / "dstar" / "bridge.py",
)


def _documented(name: str) -> bool:
    """Is `name` named in `api.md` as code, rather than merely occurring inside some other word?

    Backticks, not a bare substring: without them ``polls`` matches ``deafened_polls`` and every
    counter would look documented the moment one of them was.
    """
    return f"`{name}`" in _API_MD.read_text()


def _undocumented(names) -> list[str]:
    return sorted(n for n in names if not _documented(n))


@pytest.mark.parametrize("block", [RssiCadence, WireStats], ids=lambda b: b.__name__)
def test_every_field_of_an_instrument_block_is_documented(block):
    missing = _undocumented(f.name for f in fields(block))
    assert not missing, (
        f"{block.__name__} fields with no paragraph in docs/api.md: {missing}\n\n"
        "A number an operator can read and cannot interpret is not a reader. Add a bullet saying "
        "what a NONZERO value means, and — this is the part that is easy to skip — what it does "
        "NOT mean. The model to follow is `keyed_with_wire_busy` versus `wire_busy_at_key_up`: one "
        "says the race fired and was handled, the other says the station keyed anyway, and only "
        "the second is a prompt to go and measure the RF."
    )


def test_every_cadence_key_the_bridges_report_is_documented():
    """The three keys ADR 0179 stopped dropping, plus the two ADR 0163 already forwarded."""
    keys: set[str] = set()
    for path in _BRIDGES:
        keys.update(_DEAFENED_KEY.findall(path.read_text()))
    # The scan must have found something, or this passes for ever on a typo in the regex.
    assert len(keys) >= 5, f"the bridge scan found only {sorted(keys)} — it is not matching"
    missing = _undocumented(keys)
    assert not missing, (
        f"`tx_stats()` keys with no paragraph in docs/api.md: {missing}\n\n"
        "These ride on `GET /link/status` and `GET /dstar/status`. `deafened_polls` is the "
        "denominator that makes the rest readable — `deafened_unknown: 0` beside "
        "`deafened_polls: 0` means no probe ever ran, and beside `deafened_polls: 900` it is a "
        "measurement — so it needs a sentence as much as the counters do."
    )


def test_every_status_counter_block_is_documented():
    """The `stats()` blocks on `GET /status`: `events` (ADR 0180) and `ledger` (ADR 0181).

    This is the **strong** half of the file's two: it calls `stats()` and reads the keys that
    actually ship, so an added or renamed counter is caught by construction rather than by a source
    scan that a rename blinds. Both blocks are the same hazard as `wire` — numbers an operator can
    read at `/status` and cannot interpret without a sentence — and `ledger.dropped_records` is the
    sharpest of them, because a nonzero value there is a gap in the Part 97 operating log rather
    than a performance note.
    """
    from radio_server.api.events import EventHub
    from radio_server.eventlog import ThreadedSink

    class _NullSink:
        def write(self, record):  # pragma: no cover - never called
            pass

        def close(self):
            pass

    sink = ThreadedSink(_NullSink(), maxsize=4)
    try:
        blocks = {"events": EventHub().stats(), "ledger": sink.stats()}
    finally:
        sink.close()

    for name, block in blocks.items():
        assert block, f"the {name} block is empty — this check would pass vacuously"
        missing = _undocumented(block)
        assert not missing, (
            f"`{name}` keys on GET /status with no paragraph in docs/api.md: {missing}\n\n"
            "Say what a NONZERO value means and what it does not. `ledger.dropped_records` is the "
            "model: nonzero means the operating log has a hole, the drop is the newest record so "
            "what survives is a contiguous prefix, and it is NOT a statement about the radio."
        )


def test_the_check_can_actually_fire():
    """A documentation check that matches everything passes for ever (ADR 0165's vacuous "0 raw").

    Three things: a name that is genuinely absent is reported; a name that is present is not; and
    the backticks are load-bearing, so a counter is not credited with somebody else's paragraph.
    """
    assert _undocumented(["no_such_counter_xyzzy"]) == ["no_such_counter_xyzzy"]
    assert _undocumented(["key_ups"]) == []
    # The backticks are the load-bearing part. `eafened_poll` occurs in `api.md` — inside
    # `deafened_polls` — and must still read as undocumented, because a bare-substring check would
    # credit one counter with another's paragraph and mark a whole block documented by accident.
    assert "eafened_poll" in _API_MD.read_text()
    assert _undocumented(["eafened_poll"]) == ["eafened_poll"]
