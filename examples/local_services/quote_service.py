"""Quote service: digit "5" reads a random quote from a LAN quote API.

A sibling of `weather_service`, reading a single ``/api/quotes/random`` endpoint through the injected
`Fetcher`. The endpoint returns ``{author, text, tags}``; ``author`` is sometimes ``"Unknown"`` (then
omitted from speech) and ``text`` is occasionally paragraph-length.

Because a paragraph-long quote is an uncomfortably long single over on a shared repeater, the service
**refetches until short**: it asks up to :data:`MAX_TRIES` times and speaks the first quote whose text
is at most :data:`MAX_QUOTE_WORDS` words; if every try is long, it truncates the last one to the cap
plus an ellipsis. A whole short quote beats a truncated long one. A dead/garbled endpoint degrades to a
spoken "unavailable" — never a crashed controller loop.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from radio_server.auth import Session
from radio_server.backends import AudioFrame
from radio_server.services.dispatch import Service, ServiceContext
from radio_server.services.fetch import Fetcher, FetchError

if TYPE_CHECKING:
    from radio_server.config import Settings
    from radio_server.services.plugin import PluginBuildContext

#: Digit that invokes this service, plus its ledger name and operator-facing description.
QUOTE_DIGIT = "5"
QUOTE_NAME = "quote"
QUOTE_DESCRIPTION = "Read a random quote"

#: Path appended to the configured ``quote.base_url``.
QUOTE_PATH = "/api/quotes/random"

#: Spoken when the quote API can't be reached or its JSON is unusable.
UNAVAILABLE = "Quote service unavailable"

#: Length cap for one spoken quote, and how many times to refetch chasing a short one.
MAX_QUOTE_WORDS = 50
MAX_TRIES = 5

#: Authors that carry no attribution — spoken without a "— author" tail.
_ANONYMOUS = {"", "unknown", "anonymous"}


def load_quote_base_url(settings: Settings) -> str:
    """Return the quote API base URL (`quote.base_url`); empty string when unset."""
    return settings.extra("quote.base_url", "")


def _shorten(text: str) -> str:
    """Truncate ``text`` to :data:`MAX_QUOTE_WORDS` words, adding "…" only when it was cut."""
    words = text.split()
    if len(words) <= MAX_QUOTE_WORDS:
        return text.strip()
    return " ".join(words[:MAX_QUOTE_WORDS]) + " …"


def format_spoken_quote(payload: Mapping[str, Any]) -> str:
    """Format a quote payload as one spoken line (text, then the author unless anonymous).

    Pure and isolated so wording stays out of dispatch and tests assert the exact string. Raises
    `KeyError`/`TypeError` when ``text`` is missing — the service catches that and speaks
    :data:`UNAVAILABLE`.
    """
    text = str(payload["text"]).strip()
    body = _shorten(text)
    author = str(payload.get("author") or "").strip()
    if author.lower() in _ANONYMOUS:
        return body
    return f"{body} — {author}."


def _text_words(payload: Mapping[str, Any]) -> int:
    """Word count of the payload's quote text (0 when the field is missing/blank)."""
    return len(str(payload.get("text") or "").split())


def quote_service(base_url: str, fetcher: Fetcher) -> Service:
    """Build the quote handler bound to a base URL and a fetcher (both at construction)."""
    url = base_url.rstrip("/") + QUOTE_PATH

    def announce_quote(session: Session, ctx: ServiceContext) -> AudioFrame:
        payload: Mapping[str, Any] | None = None
        try:
            for _ in range(MAX_TRIES):
                payload = fetcher.fetch_json(url)
                if _text_words(payload) <= MAX_QUOTE_WORDS:
                    break
            text = format_spoken_quote(payload) if payload is not None else UNAVAILABLE
        except (FetchError, KeyError, TypeError, ValueError):
            text = UNAVAILABLE
        return ctx.tts.render(text)

    return announce_quote


class QuotePlugin:
    """The quote service as a `ServicePlugin`; enabled when ``quote.base_url`` is configured."""

    id = QUOTE_NAME
    description = QUOTE_DESCRIPTION

    def enabled(self, settings: Settings) -> bool:
        return bool(load_quote_base_url(settings))

    def build(self, ctx: PluginBuildContext) -> Service:
        return quote_service(load_quote_base_url(ctx.settings), ctx.fetcher())


#: Module-level plugin singleton, referenced from `services.plugin.PLUGINS`.
PLUGIN = QuotePlugin()
