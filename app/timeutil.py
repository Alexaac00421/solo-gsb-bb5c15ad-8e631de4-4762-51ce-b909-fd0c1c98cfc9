"""Beijing-time helpers.

The operating day boundary is 04:00 Asia/Shanghai: a ride boarded before
04:00 belongs to the *previous* calendar date's operating day.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
DAY_BOUNDARY_HOUR = 4
OPERATING_DAY_SHIFT = timedelta(hours=-DAY_BOUNDARY_HOUR)


def to_shanghai(value: datetime) -> datetime:
    """Return the same instant expressed in Beijing time."""
    if value.tzinfo is None:
        # Naive timestamps are accepted as Beijing local time.
        return value.replace(tzinfo=SHANGHAI_TZ)
    return value.astimezone(SHANGHAI_TZ)


def op_date_of(value: datetime) -> date:
    """Operating date for a boarding instant (04:00 boundary, Beijing time)."""
    return (to_shanghai(value) + OPERATING_DAY_SHIFT).date()
