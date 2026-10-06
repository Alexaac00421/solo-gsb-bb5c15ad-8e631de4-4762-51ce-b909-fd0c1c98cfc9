"""Pure fare-clearing rules: transfer grouping and daily settlement.

No database access here so the rules can be unit tested in isolation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .timeutil import to_shanghai


@dataclass(frozen=True)
class Leg:
    trip_no: str
    board_time: datetime  # timezone-aware
    base_fare: int


def group_legs(
    legs: list[Leg],
    max_gap: timedelta = timedelta(minutes=90),
) -> list[list[Leg]]:
    """Group consecutive legs into transfer chains.

    Within one operating day, legs are ordered by (boarding time, trip no).
    Each leg whose gap from the previous leg is at most ``max_gap`` joins the
    previous group; otherwise a new group starts. Cross-day grouping never
    happens because callers pass a single operating day.
    """
    ordered = sorted(
        legs,
        key=lambda leg: (to_shanghai(leg.board_time), leg.trip_no),
    )
    groups: list[list[Leg]] = []
    for leg in ordered:
        if groups and to_shanghai(leg.board_time) - to_shanghai(groups[-1][-1].board_time) <= max_gap:
            groups[-1].append(leg)
        else:
            groups.append([leg])
    return groups


def settle_day(
    legs: list[Leg],
    cap_cents: int = 2000,
    max_gap: timedelta = timedelta(minutes=90),
) -> tuple[list[list[Leg]], int, int]:
    """Return (groups, raw_fare, charged_fare) for one account/operating day.

    * Each transfer group costs the highest base fare inside it.
    * ``raw_fare`` is the sum of group fares.
    * ``charged_fare`` is capped at ``cap_cents``.
    """
    groups = group_legs(legs, max_gap=max_gap)
    group_fares = [max(leg.base_fare for leg in group) for group in groups]
    raw_fare = sum(group_fares)
    charged_fare = min(raw_fare, cap_cents)
    return groups, raw_fare, charged_fare
