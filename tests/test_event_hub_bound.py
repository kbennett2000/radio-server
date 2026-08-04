"""`EventHub`'s queues are bounded, and what happens when one fills (ADR 0180).

**The defect this pins.** `EventHub.subscribe()` handed out a plain `asyncio.Queue()` — no
`maxsize` — and `publish()` did a plain `put_nowait` into every one of them. `AudioHub`'s own
docstring names the contrast ("`EventHub` uses unbounded queues — correct for low-rate control
events") and the `/events` handler's comment already said it "leaks the worst". ADR 0171 reaped the
zombie *subscription*; a subscriber that is still registered and still slow grew its queue without
limit, and every published event is referenced from every queue.

**The two policies, and why there are two.** The hub has two kinds of subscriber and only one of
them has a way back. `/events` sends a full `status` snapshot on connect, so dropping that
subscriber is recoverable — the browser reconnects in about a second and resyncs. The station
ledger's drain task is not a socket, has no reconnect, and dropping it would stop the Part 97
operating log permanently and silently. So the policy is chosen per subscriber at `subscribe()`
time, the default is the conservative one, and the source-scan at the bottom of this file makes the
next person state theirs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from radio_server.api.events import (
    DEFAULT_EVENT_QUEUE_MAXSIZE,
    DROP_OLDEST,
    DROP_SUBSCRIBER,
    Event,
    EventHub,
)


def _flood(hub: EventHub, n: int, kind: str = "status") -> None:
    for i in range(n):
        hub.publish(Event(type=kind, data={"i": i}))


# --- the bound itself ---------------------------------------------------------------------


def test_a_subscriber_that_never_drains_does_not_grow_the_queue_without_limit():
    """FAIL-FIRST. On master this queue holds every event ever published to it.

    Deliberately checked with the conservative policy, so this is about the *bound* and not about
    the drop being loud: even the subscriber that is kept must stop growing.
    """
    hub = EventHub()
    queue = hub.subscribe(on_overflow=DROP_OLDEST)

    _flood(hub, DEFAULT_EVENT_QUEUE_MAXSIZE + 50)

    assert queue.qsize() <= DEFAULT_EVENT_QUEUE_MAXSIZE, (
        f"a subscriber that never drained holds {queue.qsize()} events after "
        f"{DEFAULT_EVENT_QUEUE_MAXSIZE + 50} publishes — the queue is unbounded"
    )


def test_the_bound_is_the_one_the_hub_reports():
    """The published threshold and the enforced one are the same number.

    `queue_maxsize` in `GET /status` exists to make `deepest_queue` readable, the way ADR 0179's
    `stale_after_s` ships beside `age_s`. A hub whose reported bound disagreed with the one it
    enforces would be worse than reporting nothing.
    """
    hub = EventHub(maxsize=7)
    queue = hub.subscribe(on_overflow=DROP_OLDEST)

    _flood(hub, 40)

    assert hub.stats()["queue_maxsize"] == 7
    assert queue.qsize() == 7


@pytest.mark.parametrize("policy", [DROP_OLDEST, DROP_SUBSCRIBER])
def test_publish_never_raises_into_its_caller(policy):
    """`publish` is called from REST handlers, the RX pump, the arbiter and the transport watcher.

    A `QueueFull` escaping into any of those would let a slow browser fault a transmission path.
    ADR 0018's rule for the ledger applies to the hub verbatim: a passive consumer is a place data
    goes to rest, never a place a fault comes from.
    """
    hub = EventHub(maxsize=4)
    hub.subscribe(on_overflow=policy)

    _flood(hub, 200)  # would raise on the 5th publish if `publish` were not total


# --- drop-oldest: keep the subscriber, lose an event ------------------------------------------


def test_drop_oldest_keeps_the_subscriber_and_the_newest_events():
    hub = EventHub(maxsize=3)
    queue = hub.subscribe(on_overflow=DROP_OLDEST)

    _flood(hub, 6)  # i = 0..5

    assert hub.subscriber_count == 1, "the ledger's drain must survive its own backlog"
    assert [queue.get_nowait().data["i"] for _ in range(3)] == [3, 4, 5]
    assert hub.stats()["dropped_deliveries"] == 3


def test_dropped_deliveries_counts_deliveries_and_not_events():
    """One event missed by two subscribers is 2, and `api.md` says so beside the number.

    Named for what it counts (the ADR 0170 `requested` rule): a reader who took this for a count of
    distinct events would under-read the loss by exactly the number of subscribers.
    """
    hub = EventHub(maxsize=1)
    hub.subscribe(on_overflow=DROP_OLDEST)
    hub.subscribe(on_overflow=DROP_OLDEST)

    _flood(hub, 3)  # the 2nd and 3rd publish overflow BOTH queues

    assert hub.stats()["dropped_deliveries"] == 4
    assert hub.stats()["published"] == 3


# --- drop-subscriber: lose the subscriber, loudly ---------------------------------------------


def test_drop_subscriber_unregisters_and_leaves_exactly_one_readable_notice():
    """The whole design in one test: the backlog goes, the notice stays, the subscriber is gone.

    The backlog is discarded rather than delivered because draining it to a consumer that is by
    definition too slow to keep up takes longer than the reconnect, and every event in it is about
    to be superseded by the connect snapshot.
    """
    hub = EventHub(maxsize=5)
    queue = hub.subscribe(on_overflow=DROP_SUBSCRIBER)

    _flood(hub, 9)

    assert hub.subscriber_count == 0, "a dropped subscriber must stop receiving, not just fall behind"
    assert queue.qsize() == 1
    notice = queue.get_nowait()
    assert notice.type == "overflow"
    # 5 queued events it will never read, plus the one that did not fit.
    assert notice.data == {"missed": 6, "queue_maxsize": 5}
    assert hub.stats()["dropped_subscribers"] == 1
    assert hub.stats()["dropped_deliveries"] == 6


def test_a_dropped_subscriber_receives_nothing_further():
    hub = EventHub(maxsize=2)
    queue = hub.subscribe(on_overflow=DROP_SUBSCRIBER)

    _flood(hub, 3)
    assert queue.qsize() == 1  # just the notice
    _flood(hub, 50, kind="ptt")

    assert queue.qsize() == 1, "the hub kept publishing into a queue it had already given up on"
    assert queue.get_nowait().type == "overflow"


def test_one_slow_subscriber_does_not_cost_a_healthy_one_anything():
    """The fan-out isolation `AudioHub` already has, restated for events.

    A browser on a weak link must not be able to blank the station ledger, and the ledger's disk
    must not be able to stall a browser.
    """
    hub = EventHub(maxsize=3)
    slow = hub.subscribe(on_overflow=DROP_SUBSCRIBER)
    healthy = hub.subscribe(on_overflow=DROP_OLDEST)

    for i in range(10):
        hub.publish(Event(type="status", data={"i": i}))
        if healthy.qsize():
            healthy.get_nowait()  # a consumer that keeps up

    assert slow.qsize() == 1 and slow.get_nowait().type == "overflow"
    assert hub.subscriber_count == 1
    assert hub.stats()["dropped_subscribers"] == 1
    # The healthy subscriber drained every publish, so it lost nothing at all.
    assert healthy.qsize() == 0


# --- the counters -----------------------------------------------------------------------------


def test_published_counts_events_and_not_deliveries():
    """The denominator that makes the zeroes readable (`WireStats.key_ups`, `RssiCadence.polls`).

    `dropped_subscribers: 0` beside `published: 0` means nothing has happened yet; beside
    `published: 48213` it is a measurement.
    """
    hub = EventHub()
    assert hub.stats()["published"] == 0

    _flood(hub, 5)  # nobody subscribed
    assert hub.stats()["published"] == 5

    hub.subscribe(on_overflow=DROP_OLDEST)
    hub.subscribe(on_overflow=DROP_OLDEST)
    hub.publish(Event(type="ptt", data={"on": False}))
    assert hub.stats()["published"] == 6, "one publish is one event, whoever is listening"


def test_deepest_queue_is_a_high_water_mark_and_not_a_current_depth():
    """The number that says whether the derived bound is generous or about to break.

    A *current* depth would read 0 on every healthy station and answer nothing; the high-water mark
    is the evidence for re-deriving `DEFAULT_EVENT_QUEUE_MAXSIZE` when the taxonomy grows.
    """
    hub = EventHub(maxsize=20)
    queue = hub.subscribe(on_overflow=DROP_OLDEST)

    _flood(hub, 6)
    assert hub.stats()["deepest_queue"] == 6

    while queue.qsize():
        queue.get_nowait()
    hub.publish(Event(type="ptt", data={"on": False}))

    assert queue.qsize() == 1
    assert hub.stats()["deepest_queue"] == 6, "the high-water mark fell back to the current depth"


def test_the_stats_block_is_json_ready_and_complete():
    """It is served straight into `GET /status`, so every value has to survive the trip as-is."""
    hub = EventHub()
    hub.subscribe(on_overflow=DROP_SUBSCRIBER)

    stats = hub.stats()

    assert set(stats) == {
        "published",
        "subscribers",
        "queue_maxsize",
        "deepest_queue",
        "dropped_subscribers",
        "dropped_deliveries",
    }
    assert all(isinstance(v, int) for v in stats.values())
    assert stats["subscribers"] == 1


def test_unsubscribing_is_still_idempotent_after_a_drop():
    """The `/events` handler's `finally` runs whether or not the hub already dropped it."""
    hub = EventHub(maxsize=2)
    queue = hub.subscribe(on_overflow=DROP_SUBSCRIBER)
    _flood(hub, 5)

    hub.unsubscribe(queue)
    hub.unsubscribe(queue)

    assert hub.subscriber_count == 0


# --- the subscriber pin -------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent / "radio_server"

#: Every place that takes a subscription on the **event** hub, and the policy it chose.
#:
#: A source scan and not a registry, for `test_relay_subscribers`'s reason: a registry is a
#: mechanism a subscriber can decline to use, and the text of the call is the thing that cannot be.
#:
#: The pin is on the *argument text*, not just the file, because the two live in the same module and
#: they made opposite choices for a reason that is about the subscriber and not about the hub.
_EXPECTED = {
    (
        "api/app.py",
        "on_overflow=DROP_SUBSCRIBER",
    ): "the /events websocket — sends a status snapshot on connect, so a reconnect is a resync",
    (
        "api/app.py",
        "on_overflow=DROP_OLDEST",
    ): "the station ledger's drain — not a socket, no reconnect; dropping it stops the Part 97 log",
}

#: `\bhub\.` and not `hub\.`: `audio_hub`/`rx_hub`/`mumble_rx_hub`/`dstar_rx_hub` are `AudioHub`s
#: with their own (drop-oldest, ADR 0014) policy, and this pin must not claim them.
_SUBSCRIBE = re.compile(r"\bhub\.subscribe\(([^)]*)\)")


def test_every_event_hub_subscriber_states_its_overflow_policy():
    found: dict[tuple[str, str], int] = {}
    for path in sorted(_ROOT.rglob("*.py")):
        for arg in _SUBSCRIBE.findall(path.read_text()):
            key = (str(path.relative_to(_ROOT)), " ".join(arg.split()))
            found[key] = found.get(key, 0) + 1

    assert set(found) == set(_EXPECTED), (
        "The set of EVENT-hub subscribers (or their overflow policies) changed.\n\n"
        f"  expected: {sorted(_EXPECTED)}\n  found:    {sorted(found)}\n\n"
        "If you ADDED one: can this subscriber get its state back after being dropped? A WebSocket "
        "handler that sends a snapshot on connect can — use DROP_SUBSCRIBER, and make the handler "
        "close the socket when it reads the `overflow` notice, so the client reconnects and "
        "resyncs. Anything with no way back (a task, a file writer, a bridge) must use DROP_OLDEST "
        "instead: dropping it would stop that consumer forever, silently. An empty argument list "
        "shows up here as `''` — state the policy rather than inheriting the default (ADR 0180)."
    )
    assert all(n == 1 for n in found.values()), f"a call site appears more than once: {found}"


def test_the_pin_can_actually_fire():
    """ADR 0165's discipline: a check that cannot fail is not a check.

    Two ways this must fire — a bare call (no stated policy) and the wrong hub being claimed.
    """
    bare = _SUBSCRIBE.findall("    queue = hub.subscribe()\n")
    assert bare == [""], "an unstated policy must be visible to the scanner, not invisible"
    assert ("api/app.py", "") not in _EXPECTED

    assert _SUBSCRIBE.findall("q = audio_hub.subscribe()") == [], (
        "the pin claimed an AudioHub subscriber, which has its own policy and its own guard"
    )
