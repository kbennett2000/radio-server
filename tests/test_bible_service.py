"""Bible service (7#): the pure formatter, the translation query parameter, degradation.

Hardware/network-free — a `StubFetcher` returns canned JSON shaped like Concord's /v1/random and
`StubTts` embeds the spoken text so the exact string is asserted through the returned `AudioFrame`.
"""

from __future__ import annotations

from radio_server.auth import Session
from radio_server.services import ServiceContext, StubFetcher, StubTts
from radio_server.services.bible_service import (
    BIBLE_PATH,
    UNAVAILABLE,
    format_spoken_bible,
    bible_service,
)

BASE = "http://concord.local"

VERSE = {
    "translation": "ESV",
    "verse": {
        "book": "2TI",
        "chapter": 4,
        "verse": 13,
        "reference": "2 Timothy 4:13",
        "text": "When you come, bring the cloak that I left with Carpus at Troas.",
    },
}
ESV_URL = BASE + BIBLE_PATH + "?translation=ESV"


def _ctx() -> ServiceContext:
    return ServiceContext(clock=lambda: 0.0, tts=StubTts())


def test_format_speaks_reference_then_text():
    assert format_spoken_bible(VERSE) == (
        "2 Timothy 4:13. When you come, bring the cloak that I left with Carpus at Troas."
    )


def test_service_requests_the_configured_translation():
    fetcher = StubFetcher({ESV_URL: VERSE})
    audio = bible_service(BASE, "ESV", fetcher)(Session(), _ctx())
    assert audio == StubTts().render(format_spoken_bible(VERSE))
    assert fetcher.calls == [ESV_URL]  # translation carried as a query parameter


def test_service_omits_query_when_translation_blank():
    plain = BASE + BIBLE_PATH
    fetcher = StubFetcher({plain: VERSE})
    bible_service(BASE, "", fetcher)(Session(), _ctx())
    assert fetcher.calls == [plain]


def test_service_speaks_unavailable_when_fetch_fails():
    audio = bible_service(BASE, "ESV", StubFetcher(fail=True))(Session(), _ctx())
    assert audio == StubTts().render(UNAVAILABLE)


def test_service_speaks_unavailable_on_unexpected_shape():
    fetcher = StubFetcher({ESV_URL: {"verse": {"reference": "x"}}})  # missing text
    audio = bible_service(BASE, "ESV", fetcher)(Session(), _ctx())
    assert audio == StubTts().render(UNAVAILABLE)


def test_base_url_trailing_slash_is_normalized():
    fetcher = StubFetcher({ESV_URL: VERSE})
    bible_service(BASE + "/", "ESV", fetcher)(Session(), _ctx())
    assert fetcher.calls == [ESV_URL]  # no double slash
