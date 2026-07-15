"""The JSON fetch seam: StubFetcher behavior and UrllibFetcher error-wrapping (no real network)."""

from __future__ import annotations

import urllib.error

import pytest

from radio_server.services import fetch
from radio_server.services.fetch import FetchError, StubFetcher, UrllibFetcher


def test_stub_returns_payload_and_records_calls():
    f = StubFetcher({"http://a/x": {"k": 1}})
    assert f.fetch_json("http://a/x") == {"k": 1}
    assert f.calls == ["http://a/x"]


def test_stub_default_answers_any_url():
    f = StubFetcher(default={"ok": True})
    assert f.fetch_json("http://anything") == {"ok": True}


def test_stub_raises_for_unknown_url_and_when_configured_to_fail():
    with pytest.raises(FetchError):
        StubFetcher().fetch_json("http://nope")
    with pytest.raises(FetchError):
        StubFetcher(default={"ok": True}, fail=True).fetch_json("http://x")


def test_urllib_fetcher_wraps_connection_errors(monkeypatch):
    def boom(*_a, **_k):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(fetch.urllib.request, "urlopen", boom)
    with pytest.raises(FetchError):
        UrllibFetcher(timeout=0.1).fetch_json("http://x/y")


def test_urllib_fetcher_wraps_bad_json(monkeypatch):
    class _Resp:
        def read(self):
            return b"not json"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(fetch.urllib.request, "urlopen", lambda *_a, **_k: _Resp())
    with pytest.raises(FetchError):
        UrllibFetcher().fetch_json("http://x/y")


def test_urllib_fetcher_rejects_non_object_json(monkeypatch):
    class _Resp:
        def read(self):
            return b"[1, 2, 3]"  # valid JSON, but a list, not an object

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(fetch.urllib.request, "urlopen", lambda *_a, **_k: _Resp())
    with pytest.raises(FetchError):
        UrllibFetcher().fetch_json("http://x/y")
