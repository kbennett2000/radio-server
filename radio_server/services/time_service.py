"""Announce-the-time service: digit "1" speaks the current local time.

The first service and the first thing the server transmits. It reads the *same* injected
clock the session timeout uses (via `ctx.clock()`), formats a spoken string, and renders
it through the context's TTS. The dispatcher transmits the result.

Formatting is isolated in `format_spoken_time` — a pure function of `(now, tz)` — so
24h↔12h and wording tweaks never touch dispatch, and tests assert exact output. The
timezone is configuration (`RADIO_TZ`) with a marked default, mirroring the fail-loud
`load_totp_secret` pattern; a bad zone fails loud rather than silently guessing.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from ..backends import AudioFrame
from ..auth import Session
from .dispatch import Service, ServiceContext, ServiceRegistry

if TYPE_CHECKING:
    from ..config import Settings

#: Environment variable naming the station's local timezone (an IANA name, e.g.
#: "America/New_York"). Optional: unset falls back to the marked default below.
RADIO_TZ_ENV_VAR = "RADIO_TZ"

#: Marked default. UTC is deterministic and always available; set RADIO_TZ to the
#: station's locale for a local-time announcement. (Not a security-relevant fact, so a
#: sensible default is fine here — unlike the TOTP secret, which must fail loud.)
_DEFAULT_TZ = "UTC"

#: Digit that invokes this service.
TIME_DIGIT = "1"
TIME_NAME = "time"
#: Operator-facing description for the services list (`/services`, the web UI panel, the README).
TIME_DESCRIPTION = "Announce the current local time"


def load_timezone(settings: Settings) -> ZoneInfo:
    """Return the station timezone (`time.tz`) as a `ZoneInfo`.

    The name was validated when the config was resolved (an invalid zone raises
    ``ZoneInfoNotFoundError`` there); this reconstructs the `ZoneInfo` from the stored name.
    """
    return ZoneInfo(settings.get("time.tz"))


def format_spoken_time(now: float, tz: ZoneInfo) -> str:
    """Format a unix timestamp as the spoken local date and time (24-hour).

    Pure and isolated so voice/format tweaks stay out of dispatch and tests can assert
    the exact string. Example: ``"Today is Monday, January 12. The time is 14:26 EST"``.
    """
    local = datetime.fromtimestamp(now, tz)
    return f"Today is {local:%A, %B %-d}. The time is {local:%H:%M %Z}"


def time_service(tz: ZoneInfo) -> Service:
    """Build the announce-the-time handler bound to a timezone.

    The tz is bound at construction (not read from the environment per call) so tests
    pass an explicit `ZoneInfo` and stay deterministic regardless of the host's zone.
    """

    def announce_time(session: Session, ctx: ServiceContext) -> AudioFrame:
        text = format_spoken_time(ctx.clock(), tz)
        return ctx.tts.render(text)

    return announce_time


def register(registry: ServiceRegistry, tz: ZoneInfo) -> None:
    """Register the time service under its digit into `registry`."""
    registry.register(TIME_DIGIT, TIME_NAME, time_service(tz), TIME_DESCRIPTION)
