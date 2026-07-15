"""Tests for the streaming ledger reader (ADR 0038).

``read_records`` is a stdlib-only generator over ``radio-server.jsonl``: it streams the file line by
line, skips torn/garbage/non-dict lines instead of raising, treats a missing file as empty history,
and passes unknown record types through untouched. The suite writes JSONL to ``tmp_path`` in the same
compact one-object-per-line shape ``JsonlSink`` produces, then reads it back. A final test proves the
reader→summarizer seam end to end with literals (no reliance on the gitignored real ledger).
"""

import itertools
import json
import types
from zoneinfo import ZoneInfo

from radio_server.eventlog import ChannelActivity, read_records, summarize_activity

UTC = ZoneInfo("UTC")
NOW = 1_700_000_000.0


def _write_lines(path, lines) -> None:
    """Write raw lines (each already a string, no trailing newline) as a JSONL file."""
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def _write_records(path, records) -> None:
    """Write dict records the way JsonlSink does — compact, one object per line."""
    _write_lines(path, [json.dumps(r, separators=(",", ":")) for r in records])


# --- normal / empty / missing --------------------------------------------------------------------


def test_normal_file_round_trips_records_in_order(tmp_path) -> None:
    records = [
        {"ts": 1.0, "type": "rx_open"},
        {"ts": 2.0, "type": "rx_close", "duration": 1.0},
        {"ts": 3.0, "type": "scan", "phase": "scanning"},
    ]
    path = tmp_path / "ledger.jsonl"
    _write_records(path, records)

    assert list(read_records(path)) == records


def test_empty_file_yields_nothing(tmp_path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    assert list(read_records(path)) == []


def test_missing_file_yields_nothing_and_does_not_raise(tmp_path) -> None:
    # A fresh install has no ledger yet — that is not an error (asymmetry with the fail-loud sink).
    missing = tmp_path / "never-written.jsonl"
    assert not missing.exists()
    assert list(read_records(missing)) == []


# --- corruption tolerance ------------------------------------------------------------------------


def test_torn_final_line_is_skipped_earlier_records_survive(tmp_path) -> None:
    # The writer crashed mid-append, leaving a truncated last line.
    path = tmp_path / "torn.jsonl"
    _write_lines(
        path,
        [
            json.dumps({"ts": 1.0, "type": "rx_open"}, separators=(",", ":")),
            json.dumps({"ts": 2.0, "type": "rx_close"}, separators=(",", ":")),
            '{"ts":3.0,"type":"rx_ope',  # torn
        ],
    )
    assert list(read_records(path)) == [
        {"ts": 1.0, "type": "rx_open"},
        {"ts": 2.0, "type": "rx_close"},
    ]


def test_garbage_line_mid_file_is_skipped(tmp_path) -> None:
    path = tmp_path / "garbage.jsonl"
    _write_lines(
        path,
        [
            json.dumps({"ts": 1.0, "type": "rx_open"}, separators=(",", ":")),
            "this is not json at all",  # garbage between two valid records
            json.dumps({"ts": 2.0, "type": "rx_close"}, separators=(",", ":")),
        ],
    )
    assert list(read_records(path)) == [
        {"ts": 1.0, "type": "rx_open"},
        {"ts": 2.0, "type": "rx_close"},
    ]


def test_blank_lines_are_skipped(tmp_path) -> None:
    path = tmp_path / "blanks.jsonl"
    _write_lines(
        path,
        [
            json.dumps({"ts": 1.0, "type": "rx_open"}, separators=(",", ":")),
            "",
            "   ",
            json.dumps({"ts": 2.0, "type": "rx_close"}, separators=(",", ":")),
        ],
    )
    assert list(read_records(path)) == [
        {"ts": 1.0, "type": "rx_open"},
        {"ts": 2.0, "type": "rx_close"},
    ]


def test_unknown_record_type_passes_through(tmp_path) -> None:
    # The reader parses; it does NOT filter by type. An unknown/other type is yielded unchanged.
    records = [
        {"ts": 1.0, "type": "tx_key_up"},
        {"ts": 2.0, "type": "session_open", "user": "K1ABC"},
        {"ts": 3.0, "type": "some_future_type", "extra": [1, 2, 3]},
    ]
    path = tmp_path / "mixed.jsonl"
    _write_records(path, records)
    assert list(read_records(path)) == records


def test_non_dict_json_is_skipped(tmp_path) -> None:
    # A line that parses to a bare value/list is not a ledger record; dicts around it still pass.
    path = tmp_path / "nondict.jsonl"
    _write_lines(
        path,
        [
            json.dumps({"ts": 1.0, "type": "rx_open"}, separators=(",", ":")),
            "42",
            "[1, 2, 3]",
            '"a bare string"',
            json.dumps({"ts": 2.0, "type": "rx_close"}, separators=(",", ":")),
        ],
    )
    assert list(read_records(path)) == [
        {"ts": 1.0, "type": "rx_open"},
        {"ts": 2.0, "type": "rx_close"},
    ]


# --- streaming -----------------------------------------------------------------------------------


def test_reader_streams_and_does_not_load_the_whole_file(tmp_path) -> None:
    # Prove it is a lazy generator consumed incrementally, not a slurp-then-yield.
    n = 20_000
    path = tmp_path / "big.jsonl"
    _write_records(
        path, [{"ts": float(i), "type": "rx_open"} for i in range(n)]
    )

    gen = read_records(path)
    assert isinstance(gen, types.GeneratorType)

    # Pull just the first few via islice — a lazy generator serves these without materialising all n.
    first_three = list(itertools.islice(gen, 3))
    assert first_three == [
        {"ts": 0.0, "type": "rx_open"},
        {"ts": 1.0, "type": "rx_open"},
        {"ts": 2.0, "type": "rx_open"},
    ]

    # Full consumption (fresh generator) still yields every record.
    assert sum(1 for _ in read_records(path)) == n


# --- reader -> summarizer seam -------------------------------------------------------------------


def test_read_records_feeds_the_summarizer(tmp_path) -> None:
    # End to end with literals: a written ledger, read and summarized, yields a ChannelActivity.
    records = [
        {"ts": NOW - 100.0, "type": "rx_open"},
        {"ts": NOW - 90.0, "type": "rx_close", "duration": 10.0},
        {"ts": NOW - 50.0, "type": "tx_key_up"},  # ignored by the summarizer
        {"ts": NOW - 40.0, "type": "rx_open"},
        {"ts": NOW - 35.0, "type": "rx_close", "duration": 5.0},
    ]
    path = tmp_path / "activity.jsonl"
    _write_records(path, records)

    summary = summarize_activity(read_records(path), now=NOW, tz=UTC)

    assert isinstance(summary, ChannelActivity)
    assert summary.busy_count == 2
    assert summary.total_airtime == 15.0
    assert summary.last_heard == NOW - 40.0
