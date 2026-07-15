"""Quote service (5#): the pure formatter, refetch-until-short, author omission, degradation.

Hardware/network-free — a `StubFetcher` returns canned JSON and `StubTts` embeds the spoken text so
the exact string is asserted through the returned `AudioFrame`. `StubFetcher` returns the same payload
on every call, so a long stub exercises the truncate branch in one pass (the loop can't get a shorter
one) and a short stub returns on the first fetch.
"""

from __future__ import annotations

from radio_server.auth import Session
from radio_server.services import ServiceContext, StubFetcher, StubTts
from radio_server.services.quote_service import (
    MAX_QUOTE_WORDS,
    QUOTE_PATH,
    UNAVAILABLE,
    format_spoken_quote,
    quote_service,
)

BASE = "http://quotes.local"
URL = BASE + QUOTE_PATH

SHORT = {"author": "Henry Adams", "text": "A friend in power is a friend lost."}
ANON = {"author": "Unknown", "text": "The universe meets you at the depth of your surrender."}
LONG = {"author": "Verbose", "text": "word " * 120}


def _ctx() -> ServiceContext:
    return ServiceContext(clock=lambda: 0.0, tts=StubTts())


def test_format_appends_author():
    assert format_spoken_quote(SHORT) == "A friend in power is a friend lost. — Henry Adams."


def test_format_omits_unknown_author():
    assert format_spoken_quote(ANON) == "The universe meets you at the depth of your surrender."


def test_format_omits_blank_and_anonymous_author():
    assert format_spoken_quote({"text": "No name.", "author": ""}) == "No name."
    assert format_spoken_quote({"text": "No name.", "author": "Anonymous"}) == "No name."


def test_long_quote_is_truncated_to_the_cap_with_ellipsis():
    spoken = format_spoken_quote(LONG)
    body = spoken.rsplit(" — ", 1)[0]
    assert body.endswith(" …")
    assert len(body.replace(" …", "").split()) == MAX_QUOTE_WORDS


def test_service_speaks_a_short_quote_on_the_first_fetch():
    fetcher = StubFetcher({URL: SHORT})
    audio = quote_service(BASE, fetcher)(Session(), _ctx())
    assert audio == StubTts().render(format_spoken_quote(SHORT))
    assert fetcher.calls == [URL]  # short → no refetch


def test_service_refetches_then_truncates_when_only_long_quotes_come_back():
    fetcher = StubFetcher({URL: LONG})
    audio = quote_service(BASE, fetcher)(Session(), _ctx())
    assert audio == StubTts().render(format_spoken_quote(LONG))  # truncated last try
    assert len(fetcher.calls) == 5  # MAX_TRIES — chased a short one, never found it


def test_service_speaks_unavailable_when_fetch_fails():
    audio = quote_service(BASE, StubFetcher(fail=True))(Session(), _ctx())
    assert audio == StubTts().render(UNAVAILABLE)


def test_service_speaks_unavailable_on_unexpected_shape():
    fetcher = StubFetcher({URL: {"nope": 1}})
    audio = quote_service(BASE, fetcher)(Session(), _ctx())
    assert audio == StubTts().render(UNAVAILABLE)


def test_base_url_trailing_slash_is_normalized():
    fetcher = StubFetcher({URL: SHORT})
    quote_service(BASE + "/", fetcher)(Session(), _ctx())
    assert fetcher.calls == [URL]  # no double slash
