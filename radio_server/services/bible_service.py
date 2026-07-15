"""Bible service: digit "7" reads a random verse from a Concord (LAN Scripture API) instance.

A sibling of `weather_service`, reading Concord's ``/v1/random`` endpoint through the injected
`Fetcher`. The endpoint returns ``{translation, verse:{reference, text}}``; the translation is a
config setting (``bible.translation``, default ``ESV``) sent as a ``?translation=`` query parameter —
Concord defaults to KJV with no parameter, so the operator's chosen translation is passed explicitly.
The reference and text are spoken together (``"2 Timothy 4:13. When you come, bring the cloak…"``). A
dead/garbled endpoint degrades to a spoken "unavailable".
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from ..auth import Session
from ..backends import AudioFrame
from .dispatch import Service, ServiceContext, ServiceRegistry
from .fetch import Fetcher, FetchError

if TYPE_CHECKING:
    from ..config import Settings

#: Digit that invokes this service, plus its ledger name and operator-facing description.
BIBLE_DIGIT = "7"
BIBLE_NAME = "bible"
BIBLE_DESCRIPTION = "Read a random bible verse"

#: Path appended to the configured ``bible.base_url`` (a Concord instance).
BIBLE_PATH = "/v1/random"

#: Spoken when Concord can't be reached or its JSON is unusable.
UNAVAILABLE = "Verse service unavailable"


def load_bible_base_url(settings: Settings) -> str:
    """Return the Concord base URL (`bible.base_url`); empty string when unset."""
    return settings.get("bible.base_url")


def load_bible_translation(settings: Settings) -> str:
    """Return the configured translation id (`bible.translation`), e.g. ``"ESV"``."""
    return settings.get("bible.translation")


def format_spoken_bible(payload: Mapping[str, Any]) -> str:
    """Format a Concord random-verse payload as one spoken line (reference, then text).

    Pure and isolated so wording stays out of dispatch and tests assert the exact string. Raises
    `KeyError`/`TypeError` when the ``verse`` block or its fields are missing — the service catches
    that and speaks :data:`UNAVAILABLE`.
    """
    verse = payload["verse"]
    reference = str(verse["reference"]).strip()
    text = str(verse["text"]).strip()
    return f"{reference}. {text}"


def bible_service(base_url: str, translation: str, fetcher: Fetcher) -> Service:
    """Build the bible handler bound to a base URL, translation, and fetcher (all at construction)."""
    url = base_url.rstrip("/") + BIBLE_PATH
    if translation:
        url += "?" + urlencode({"translation": translation})

    def announce_bible(session: Session, ctx: ServiceContext) -> AudioFrame:
        try:
            text = format_spoken_bible(fetcher.fetch_json(url))
        except (FetchError, KeyError, TypeError, ValueError):
            text = UNAVAILABLE
        return ctx.tts.render(text)

    return announce_bible


def register(
    registry: ServiceRegistry, base_url: str, translation: str, fetcher: Fetcher
) -> None:
    """Register the bible service under its digit into `registry`."""
    registry.register(
        BIBLE_DIGIT, BIBLE_NAME, bible_service(base_url, translation, fetcher), BIBLE_DESCRIPTION
    )
