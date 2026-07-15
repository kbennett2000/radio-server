"""GET /activity/summary — the Tier-0 activity rollup over HTTP (ADR 0039).

Driven through Starlette's ``TestClient`` over ``create_app(MockRadio(...))`` like the rest of the
API suite — no server binds, no hardware. The route composes ``read_records`` (ADR 0038) into
``summarize_activity`` (ADR 0036), reading the ledger path and window/min_duration from settings and
the timezone from ``time.tz``. These tests prove: a seeded ledger rolls up to a summary; an empty or
missing ledger is a zeroed ``200`` (not ``404``/``500``); the bearer gate is enforced; and the
blocking read+walk runs off the event loop (``asyncio.to_thread``), not inline in the handler.
"""

import json
import threading
import time

from fastapi.testclient import TestClient

from radio_server.api import create_app
from radio_server.backends import MockRadio
from radio_server.eventlog import ChannelActivity

TOKEN = "test-lan-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _client(settings) -> TestClient:
    return TestClient(create_app(MockRadio(supports_cat=False), api_token=TOKEN, settings=settings))


def _settings(tmp_path, *, filename="radio-server.jsonl", tz="UTC"):
    # Point the ledger at tmp_path; the file need not exist (missing → zeroed summary).
    from tests.conftest import make_settings

    return make_settings({"logging.path": str(tmp_path / filename), "time.tz": tz})


def _write_ledger(path, records) -> None:
    """Write records the way JsonlSink does — compact, one JSON object per line."""
    path.write_text(
        "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records), encoding="utf-8"
    )


def test_seeded_ledger_returns_summary(tmp_path):
    # Two busy events inside the 7-day window; duration is close_ts - open_ts (the `duration` field
    # is the summarizer's own — it recomputes from the edges). Seed relative to now so both survive.
    base = time.time()
    o1, c1 = base - 100.0, base - 95.0  # 5.0 s
    o2, c2 = base - 40.0, base - 20.0  # 20.0 s
    ledger = tmp_path / "radio-server.jsonl"
    _write_ledger(
        ledger,
        [
            {"ts": o1, "type": "rx_open"},
            {"ts": c1, "type": "rx_close"},
            {"ts": o2, "type": "rx_open"},
            {"ts": c2, "type": "rx_close"},
        ],
    )

    resp = _client(_settings(tmp_path)).get("/activity/summary", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()

    assert body["busy_count"] == 2
    assert body["total_airtime"] == 25.0
    assert body["last_heard"] == o2  # most recent open
    assert len(body["by_hour"]) == 24 and sum(body["by_hour"]) == 2
    assert len(body["by_weekday"]) == 7 and sum(body["by_weekday"]) == 2


def test_empty_ledger_returns_zeroed_summary_200(tmp_path):
    ledger = tmp_path / "radio-server.jsonl"
    ledger.write_text("", encoding="utf-8")

    resp = _client(_settings(tmp_path)).get("/activity/summary", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["busy_count"] == 0
    assert body["total_airtime"] == 0.0
    assert body["last_heard"] is None
    assert sum(body["by_hour"]) == 0 and sum(body["by_weekday"]) == 0


def test_missing_ledger_returns_zeroed_summary_not_404(tmp_path):
    # A fresh install has never received — no ledger file yet. That is a valid "nothing heard"
    # answer (200), never a 404 or a 500.
    settings = _settings(tmp_path, filename="never-written.jsonl")
    resp = _client(settings).get("/activity/summary", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["busy_count"] == 0


def test_auth_enforced(tmp_path):
    client = _client(_settings(tmp_path))
    assert client.get("/activity/summary").status_code == 401  # no bearer
    assert (
        client.get(
            "/activity/summary", headers={"Authorization": "Bearer wrong-token"}
        ).status_code
        == 401
    )


def test_handler_does_not_block_the_event_loop(tmp_path, monkeypatch):
    # The blocking read+walk must run off the loop via asyncio.to_thread. Spy on the summarize call
    # (invoked inside the offloaded _run_summary) to capture the thread it runs on; a worker-thread
    # id different from this test's main thread proves it was genuinely offloaded, not run inline.
    main_ident = threading.get_ident()
    recorded: dict[str, int] = {}

    def spy(records, **kwargs) -> ChannelActivity:
        recorded["ident"] = threading.get_ident()
        # Consume the generator as the real summarizer would, so the file I/O also happens in-thread.
        list(records)
        return ChannelActivity(
            busy_count=7, total_airtime=0.0, last_heard=None, by_hour=(0,) * 24, by_weekday=(0,) * 7
        )

    monkeypatch.setattr("radio_server.api.activity.summarize_activity", spy)

    resp = _client(_settings(tmp_path)).get("/activity/summary", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["busy_count"] == 7  # the route returned the offloaded call's result
    assert recorded["ident"] != main_ident  # ran in a worker thread, not the caller's thread
