"""Time service: exact 24-hour local formatting and RADIO_TZ config."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from radio_server.services import (
    format_spoken_time,
    load_timezone,
)

from .conftest import make_settings

# 1970-01-12 13:46:40 UTC — a fixed, known instant (matches the conftest FakeClock base).
BASE = 1_000_000.0


def test_format_utc_is_exact():
    assert format_spoken_time(BASE, ZoneInfo("UTC")) == "The time is 13:46 UTC"


def test_format_respects_local_timezone():
    # January in New York is EST (UTC-5): 13:46 UTC -> 08:46 EST.
    assert format_spoken_time(BASE, ZoneInfo("America/New_York")) == "The time is 08:46 EST"


def test_format_is_24_hour():
    # 20:00 UTC must read "20:00", never "8:00 PM".
    twenty_hundred = 20 * 3600  # 1970-01-01 20:00:00 UTC
    assert format_spoken_time(twenty_hundred, ZoneInfo("UTC")) == "The time is 20:00 UTC"


def test_load_timezone_reads_env():
    tz = load_timezone(make_settings({"time.tz": "America/New_York"}))
    assert tz.key == "America/New_York"


def test_load_timezone_defaults_to_utc():
    assert load_timezone(make_settings({})).key == "UTC"


def test_load_timezone_rejects_bad_zone():
    # Validated at resolution: an unknown zone raises ZoneInfoNotFoundError (same type as before).
    with pytest.raises(ZoneInfoNotFoundError):
        make_settings({"time.tz": "Not/AZone"})
