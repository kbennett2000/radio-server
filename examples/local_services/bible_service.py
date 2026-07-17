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

from radio_server.auth import Session
from radio_server.backends import AudioFrame
from radio_server.services.dispatch import Service, ServiceContext
from radio_server.services.fetch import Fetcher, FetchError

if TYPE_CHECKING:
    from radio_server.config import Settings
    from radio_server.services.plugin import PluginBuildContext

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
    return settings.extra("bible.base_url", "")


def load_bible_translation(settings: Settings) -> str:
    """Return the configured translation id (`bible.translation`), e.g. ``"ESV"``."""
    return settings.extra("bible.translation", "ESV")


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


class BiblePlugin:
    """The bible service as a `ServicePlugin`; enabled when ``bible.base_url`` is configured. Also
    binds the operator's ``bible.translation`` at build time.
    """

    id = BIBLE_NAME
    description = BIBLE_DESCRIPTION

    def enabled(self, settings: Settings) -> bool:
        return bool(load_bible_base_url(settings))

    def build(self, ctx: PluginBuildContext) -> Service:
        return bible_service(
            load_bible_base_url(ctx.settings),
            load_bible_translation(ctx.settings),
            ctx.fetcher(),
        )


#: Module-level plugin singleton, referenced from `services.plugin.PLUGINS`.
PLUGIN = BiblePlugin()
