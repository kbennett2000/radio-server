"""Tests for the pure channel-activity summarizer (ADR 0036).

Entirely literal-driven and deterministic: ``now`` is a plain float and ``tz`` a real
:class:`~zoneinfo.ZoneInfo`. No disk, no clock, no ``Settings`` — the summarizer is a leaf-pure
function of ``(records, now, tz, window, min_duration)``. The suite proves pairing (incl. unpaired
open/close and a re-opened open), the ``min_duration`` and ``window`` filters, DST-correct local
bucketing, empty→zeroed input, and malformed-record skipping.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from radio_server.eventlog import ChannelActivity, summarize_activity

UTC = ZoneInfo("UTC")
NY = ZoneInfo("America/New_York")

# A fixed "now" well clear of any epoch/DST edge (2023-11-14T22:13:20Z).
NOW = 1_700_000_000.0
DAY = 86_400.0


# --- helpers -------------------------------------------------------------------------------------


def _open(ts: float) -> dict:
    return {"ts": ts, "type": "rx_open"}


def _close(ts: float, *, duration=None) -> dict:
    rec = {"ts": ts, "type": "rx_close"}
    if duration is not None:
        rec["duration"] = duration
    return rec


def _summarize(records, *, now=NOW, tz=UTC, **kw) -> ChannelActivity:
    return summarize_activity(records, now=now, tz=tz, **kw)


# --- pairing -------------------------------------------------------------------------------------


def test_one_open_close_pairs_into_one_event() -> None:
    open_ts, close_ts = NOW - 100.0, NOW - 95.0  # 5s busy, recent, > min_duration
    summary = _summarize([_open(open_ts), _close(close_ts)])

    assert summary.busy_count == 1
    assert summary.total_airtime == 5.0
    assert summary.last_heard == open_ts
    # bucketed by the LOCAL open time; exactly one event lands in exactly one bucket.
    local = datetime.fromtimestamp(open_ts, UTC)
    assert summary.by_hour[local.hour] == 1
    assert sum(summary.by_hour) == 1
    assert summary.by_weekday[local.weekday()] == 1
    assert sum(summary.by_weekday) == 1


def test_unpaired_open_is_skipped() -> None:
    # An open whose close never arrived (crash / still busy) contributes nothing.
    summary = _summarize([_open(NOW - 100.0)])
    assert summary == _zeroed()


def test_unpaired_close_is_skipped() -> None:
    # A close with no preceding open contributes nothing.
    summary = _summarize([_close(NOW - 100.0, duration=None)])
    assert summary == _zeroed()


def test_reopened_open_drops_the_earlier_open() -> None:
    # open@t1, open@t2, close@t3: t1 is overwritten (unpaired) and skipped; t2 pairs with t3.
    t1, t2, t3 = NOW - 300.0, NOW - 100.0, NOW - 90.0
    summary = _summarize([_open(t1), _open(t2), _close(t3)])

    assert summary.busy_count == 1
    assert summary.total_airtime == 10.0
    assert summary.last_heard == t2  # the surviving open, not the dropped t1


def test_trailing_open_after_a_paired_event_is_skipped() -> None:
    # A complete event followed by a dangling open: one event, the trailing open dropped.
    summary = _summarize(
        [_open(NOW - 200.0), _close(NOW - 190.0), _open(NOW - 50.0)]
    )
    assert summary.busy_count == 1
    assert summary.total_airtime == 10.0


# --- min_duration filter -------------------------------------------------------------------------


def test_min_duration_excludes_a_short_crackle() -> None:
    # 0.3s squelch crackle < default 1.0s min_duration → excluded → zeroed.
    summary = _summarize([_open(NOW - 100.0), _close(NOW - 99.7)])
    assert summary == _zeroed()


def test_min_duration_keeps_a_long_event_and_drops_the_short_one() -> None:
    records = [
        _open(NOW - 100.0),
        _close(NOW - 99.7),  # 0.3s — dropped
        _open(NOW - 50.0),
        _close(NOW - 40.0),  # 10s — kept
    ]
    summary = _summarize(records)
    assert summary.busy_count == 1
    assert summary.total_airtime == 10.0
    assert summary.last_heard == NOW - 50.0


def test_min_duration_is_overridable() -> None:
    # With a tiny threshold the 0.3s event survives.
    summary = _summarize([_open(NOW - 100.0), _close(NOW - 99.7)], min_duration=0.1)
    assert summary.busy_count == 1
    assert round(summary.total_airtime, 3) == 0.3


# --- window filter -------------------------------------------------------------------------------


def test_window_excludes_events_older_than_the_default_seven_days() -> None:
    old_open, old_close = NOW - 8 * DAY, NOW - 8 * DAY + 10.0  # 8 days ago, before the 7-day cutoff
    summary = _summarize([_open(old_open), _close(old_close)])
    assert summary == _zeroed()


def test_window_keeps_recent_drops_old() -> None:
    records = [
        _open(NOW - 8 * DAY),
        _close(NOW - 8 * DAY + 10.0),  # old — dropped
        _open(NOW - 2 * DAY),
        _close(NOW - 2 * DAY + 10.0),  # 2 days ago — kept
    ]
    summary = _summarize(records)
    assert summary.busy_count == 1
    assert summary.last_heard == NOW - 2 * DAY


def test_window_is_overridable() -> None:
    # A 30-day window admits the 8-day-old event.
    old_open = NOW - 8 * DAY
    summary = _summarize(
        [_open(old_open), _close(old_open + 10.0)], window=timedelta(days=30)
    )
    assert summary.busy_count == 1


# --- local-time bucketing (DST) ------------------------------------------------------------------


def test_bucketing_is_local_time_across_a_dst_boundary() -> None:
    # 7am local in January (EST, UTC-5) and 7am local in July (EDT, UTC-4) have different UTC
    # offsets but must both bucket to local hour 7 — "busiest at 7am" means 7am at the station.
    jan_open = datetime(2025, 1, 15, 7, 0, 0, tzinfo=NY).timestamp()
    jul_open = datetime(2025, 7, 15, 7, 0, 0, tzinfo=NY).timestamp()
    now = datetime(2025, 7, 16, 0, 0, 0, tzinfo=NY).timestamp()

    summary = _summarize(
        [
            _open(jan_open),
            _close(jan_open + 5.0),
            _open(jul_open),
            _close(jul_open + 5.0),
        ],
        now=now,
        tz=NY,
        window=timedelta(days=400),  # wide enough to include the January event
    )

    assert summary.busy_count == 2
    assert summary.by_hour[7] == 2
    assert sum(summary.by_hour) == 2


def test_weekday_bucketing_uses_local_weekday() -> None:
    # 2025-07-15 is a Tuesday (weekday 1) in America/New_York.
    open_ts = datetime(2025, 7, 15, 10, 0, 0, tzinfo=NY).timestamp()
    now = datetime(2025, 7, 15, 12, 0, 0, tzinfo=NY).timestamp()
    summary = _summarize([_open(open_ts), _close(open_ts + 5.0)], now=now, tz=NY)

    assert summary.by_weekday[1] == 1  # Tuesday
    assert sum(summary.by_weekday) == 1
    assert summary.by_hour[10] == 1


# --- empty / malformed ---------------------------------------------------------------------------


def test_empty_input_is_a_zeroed_summary_not_an_error() -> None:
    assert _summarize([]) == _zeroed()


def test_malformed_records_are_skipped_valid_ones_still_process() -> None:
    records = [
        "not a dict",                        # non-dict
        {"type": "rx_open"},                 # missing ts
        {"ts": "later", "type": "rx_open"},  # non-numeric ts
        {"ts": True, "type": "rx_open"},     # bool ts is not a real timestamp
        {"ts": NOW - 500.0, "type": "tx_key_up"},  # unrelated record type
        {"ts": NOW - 100.0, "type": "rx_open"},    # a valid pair follows
        {"ts": NOW - 90.0, "type": "rx_close", "duration": 10.0},
    ]
    summary = _summarize(records)
    assert summary.busy_count == 1
    assert summary.total_airtime == 10.0
    assert summary.last_heard == NOW - 100.0


def test_only_rx_records_are_consumed() -> None:
    # A stream of purely non-rx ledger records summarizes to nothing.
    records = [
        {"ts": NOW - 10.0, "type": "session_open"},
        {"ts": NOW - 9.0, "type": "tx_key_up"},
        {"ts": NOW - 8.0, "type": "scan", "phase": "active"},
    ]
    assert _summarize(records) == _zeroed()


# --- shared zeroed expectation -------------------------------------------------------------------


def _zeroed() -> ChannelActivity:
    return ChannelActivity(
        busy_count=0,
        total_airtime=0.0,
        last_heard=None,
        by_hour=(0,) * 24,
        by_weekday=(0,) * 7,
    )
