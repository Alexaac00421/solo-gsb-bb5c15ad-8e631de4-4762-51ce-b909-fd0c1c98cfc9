"""Unit tests for operating-day and fare rules (no database required).

Run with either ``python -m pytest tests/test_fare_rules.py`` or
``python tests/run_unit_tests.py`` (no third-party packages needed).
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.fare import Leg, group_legs, settle_day
from app.timeutil import op_date_of, to_shanghai

BJ = ZoneInfo("Asia/Shanghai")
UTC = ZoneInfo("UTC")
GAP = timedelta(minutes=90)


def leg(no: str, hour: float, fare: int, day: int = 10) -> Leg:
    h = int(hour)
    m = int(round((hour - h) * 60))
    return Leg(no, datetime(2026, 10, day, h, m, tzinfo=BJ), fare)


class OperatingDayTests(unittest.TestCase):
    def test_boundary_is_04(self):
        self.assertEqual(op_date_of(datetime(2026, 10, 10, 3, 59, tzinfo=BJ)).day, 9)
        self.assertEqual(op_date_of(datetime(2026, 10, 10, 4, 0, tzinfo=BJ)).day, 10)
        self.assertEqual(op_date_of(datetime(2026, 10, 11, 0, 30, tzinfo=BJ)).day, 10)
        self.assertEqual(op_date_of(datetime(2026, 10, 11, 3, 59, tzinfo=BJ)).day, 10)

    def test_naive_treated_as_beijing(self):
        self.assertEqual(op_date_of(datetime(2026, 10, 10, 2, 0)).day, 9)

    def test_utc_instant_maps_to_same_op_day(self):
        instant = datetime(2026, 10, 9, 19, 30, tzinfo=UTC)  # 03:30 next day Beijing
        self.assertEqual(op_date_of(instant), op_date_of(datetime(2026, 10, 10, 3, 30, tzinfo=BJ)))

    def test_to_shanghai_preserves_instant(self):
        utc_time = datetime(2026, 10, 9, 19, 30, tzinfo=UTC)
        self.assertEqual(to_shanghai(utc_time), datetime(2026, 10, 10, 3, 30, tzinfo=BJ))


class GroupingTests(unittest.TestCase):
    def test_gap_exactly_90_minutes_same_group(self):
        self.assertEqual(len(group_legs([leg("A", 8, 200), leg("B", 9.5, 300)], max_gap=GAP)), 1)

    def test_gap_over_90_minutes_splits(self):
        self.assertEqual(len(group_legs([leg("A", 8, 200), leg("B", 9.51, 300)], max_gap=GAP)), 2)

    def test_chain_uses_previous_leg(self):
        # 08:00 -> 09:00 (60) -> 10:00 (60): one chain despite 120-min span.
        groups = group_legs([leg("C", 10, 100), leg("A", 8, 100), leg("B", 9, 500)], max_gap=GAP)
        self.assertEqual(len(groups), 1)
        self.assertEqual([l.trip_no for l in groups[0]], ["A", "B", "C"])
        self.assertEqual(max(l.base_fare for l in groups[0]), 500)

    def test_chain_break_starts_new_group(self):
        groups = group_legs([leg("A", 8, 100), leg("B", 9, 200), leg("C", 12, 300)], max_gap=GAP)
        self.assertEqual(len(groups), 2)
        self.assertEqual([l.trip_no for l in groups[0]], ["A", "B"])
        self.assertEqual([l.trip_no for l in groups[1]], ["C"])

    def test_tie_break_on_trip_no(self):
        same_time = datetime(2026, 10, 10, 8, 0, tzinfo=BJ)
        groups = group_legs([Leg("Z", same_time, 100), Leg("A", same_time, 100)], max_gap=GAP)
        self.assertEqual([l.trip_no for l in groups[0]], ["A", "Z"])

    def test_groups_never_span_days_for_separate_inputs(self):
        # Grouping is per-day; the caller enforces the day boundary.
        groups = group_legs([leg("A", 23, 100, day=10), leg("B", 2, 100, day=11)], max_gap=GAP)
        self.assertEqual(len(groups), 2)


class SettlementTests(unittest.TestCase):
    def test_group_fares_sum(self):
        # max(200,300)=300 for the first group, 400 for the second -> 700.
        groups, raw, charged = settle_day(
            [leg("A", 8, 200), leg("B", 9.5, 300), leg("C", 13, 400)],
            cap_cents=2000, max_gap=GAP,
        )
        self.assertEqual(len(groups), 2)
        self.assertEqual(raw, 700)
        self.assertEqual(charged, 700)

    def test_daily_cap(self):
        _, raw, charged = settle_day(
            [leg("A", 8, 1500), leg("B", 13, 1500)], cap_cents=2000, max_gap=GAP
        )
        self.assertEqual(raw, 3000)
        self.assertEqual(charged, 2000)

    def test_single_group_takes_max(self):
        _, raw, charged = settle_day(
            [leg("A", 8, 100), leg("B", 9, 2500)], cap_cents=2000, max_gap=GAP
        )
        self.assertEqual(raw, 2500)
        self.assertEqual(charged, 2000)

    def test_empty_day(self):
        groups, raw, charged = settle_day([], cap_cents=2000, max_gap=GAP)
        self.assertEqual(groups, [])
        self.assertEqual((raw, charged), (0, 0))

    def test_zero_fares(self):
        groups, raw, charged = settle_day(
            [leg("A", 8, 0), leg("B", 9, 0)], cap_cents=2000, max_gap=GAP
        )
        self.assertEqual(len(groups), 1)
        self.assertEqual((raw, charged), (0, 0))


if __name__ == "__main__":
    unittest.main()
