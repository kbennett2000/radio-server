"""Every consumer of the RF audio hub, pinned by name (ADR 0162).

**This test exists to answer the one real argument against the seam this cycle chose.** ADR 0162
puts the broadcast-FM mute in each bridge's relay loop rather than in `AudioHub`, following ADR
0085. That is right for four reasons the ADR gives — but it has a genuine weakness the hub seam does
not: **a new subscriber inherits no policy.** The next relay somebody adds — an Icecast feed, an
AllStar link, a second reflector — will call `audio_hub.subscribe()`, will work perfectly, and will
quietly retransmit a commercial broadcast station the day an operator presses F+0.

A Part 97 control that has to be *remembered* will eventually be forgotten. So the set of
subscribers is pinned here instead: adding a fourth fails CI and lands the reviewer on this
docstring, which tells them the decision they now have to make.

It is deliberately a source scan and not an import-time registry. A registry would be a mechanism
that could itself be bypassed by a subscriber that did not register; the text of the call is the
thing that cannot be.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent / "radio_server"

#: Every place that takes a subscription on the **RF** hub — the one `RxPump` feeds — and what each
#: does about a station that cannot hear its own channel.
#:
#: The browser is not an oversight. Hearing broadcast FM in the browser is the *feature*; the hazard
#: is retransmission, and a browser tab does not retransmit.
EXPECTED = {
    "api/app.py": "browser Listen — RELAYS broadcast FM on purpose (ADR 0162's asymmetry)",
    "link/bridge.py": "Mumble — muted while the station is measurably deaf",
    "dstar/bridge.py": "D-STAR reflector — muted, and reaps its outbound over when it does",
}

_SUBSCRIBE = re.compile(r"^\s*[\w.\[\]]+\s*=\s*(?:self\.)?_?audio_hub\.subscribe\(\)", re.M)


def test_the_rf_audio_hub_has_exactly_three_subscribers():
    found = {}
    for path in sorted(_ROOT.rglob("*.py")):
        hits = len(_SUBSCRIBE.findall(path.read_text()))
        if hits:
            found[str(path.relative_to(_ROOT))] = hits

    assert set(found) == set(EXPECTED), (
        "The set of RF audio-hub subscribers changed.\n\n"
        f"  expected: {sorted(EXPECTED)}\n  found:    {sorted(found)}\n\n"
        "If you ADDED one: does it put audio anywhere a listener could hear it off this station? "
        "If so it must carry the broadcast-FM mute (ADR 0162) — `AudioHub` does not apply one for "
        "you, deliberately. Wire the `broadcast_fm` predicate in as `MumbleBridge` and `DStarBridge` "
        "do, count the drops in whatever this component's status surface is, then add it here with a "
        "line saying which side of the asymmetry it is on."
    )
    assert all(n == 1 for n in found.values()), f"a file subscribes more than once: {found}"
