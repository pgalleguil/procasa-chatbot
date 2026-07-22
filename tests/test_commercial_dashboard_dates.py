import random
import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from analytics.commercial_periods import (
    TIMEZONE, VALID_COMPARISONS, comparison_period, local_today,
    preset_range, shift_year, validate_explicit_range,
)
from analytics.leads_queries import _build_chile_period_bounds


class CommercialDashboardDateTests(unittest.TestCase):
    ANCHOR = date(2026, 7, 21)

    def assert_previous_invariants(self, start, end, comp_start, comp_end):
        self.assertLessEqual(start, end)
        self.assertLessEqual(comp_start, comp_end)
        self.assertEqual((end - start).days, (comp_end - comp_start).days)
        self.assertLess(comp_end, start)

    def test_reference_ranges(self):
        expected = {
            "today": (date(2026, 7, 21), date(2026, 7, 21), date(2026, 7, 20), date(2026, 7, 20)),
            "week": (date(2026, 7, 20), date(2026, 7, 21), date(2026, 7, 13), date(2026, 7, 14)),
            "month": (date(2026, 7, 1), date(2026, 7, 21), date(2026, 6, 1), date(2026, 6, 21)),
            "30d": (date(2026, 6, 22), date(2026, 7, 21), date(2026, 5, 23), date(2026, 6, 21)),
        }
        for preset, values in expected.items():
            start, end = preset_range(preset, self.ANCHOR)
            comp_start, comp_end, _ = comparison_period(start, end, "prev", preset)
            self.assertEqual((start, end, comp_start, comp_end), values)
        start, end = date(2026, 7, 10), date(2026, 7, 15)
        self.assertEqual(comparison_period(start, end, "prev", "custom")[:2],
                         (date(2026, 7, 4), date(2026, 7, 9)))

    def test_every_anchor_2024_through_2028(self):
        anchor = date(2024, 1, 1)
        last = date(2028, 12, 31)
        while anchor <= last:
            for preset in ("today", "week", "month", "30d"):
                start, end = preset_range(preset, anchor)
                self.assertLessEqual(start, end)
                for mode in VALID_COMPARISONS:
                    cs, ce, _ = comparison_period(start, end, mode, preset)
                    if mode == "none":
                        self.assertIsNone(cs); self.assertIsNone(ce)
                    elif mode == "prev":
                        self.assert_previous_invariants(start, end, cs, ce)
                    else:
                        self.assertLessEqual(cs, ce)
            anchor += timedelta(days=1)

    def test_custom_durations_and_seeded_property_cases(self):
        durations = (1, 2, 6, 7, 28, 29, 30, 31, 90, 365, 366)
        anchors = (date(2024, 2, 29), date(2024, 9, 8), date(2025, 4, 6),
                   date(2025, 12, 31), date(2026, 3, 1), date(2026, 7, 20))
        cases = [(end - timedelta(days=n - 1), end) for end in anchors for n in durations]
        rng = random.Random(20260721)
        base = date(2020, 1, 1)
        for _ in range(1000):
            start = base + timedelta(days=rng.randrange(0, 3653))
            cases.append((start, start + timedelta(days=rng.randrange(0, 731))))
        for start, end in cases:
            cs, ce, _ = comparison_period(start, end, "prev", "custom")
            self.assert_previous_invariants(start, end, cs, ce)

    def test_leap_year_and_month_end_rules(self):
        self.assertEqual(shift_year(date(2024, 2, 29)), date(2023, 2, 28))
        for anchor, expected in ((date(2025, 3, 31), (date(2025, 1, 29), date(2025, 2, 28))),
                                 (date(2024, 3, 31), (date(2024, 1, 30), date(2024, 2, 29)))):
            start, end = preset_range("month", anchor)
            cs, ce, _ = comparison_period(start, end, "auto", "month")
            self.assertEqual((cs, ce), expected)

    def test_timezone_bounds_are_local_and_end_exclusive(self):
        for day in (date(2024, 4, 6), date(2024, 4, 7), date(2024, 9, 7), date(2024, 9, 8)):
            start, end = _build_chile_period_bounds(day.isoformat(), day.isoformat())
            local_start = start.astimezone(TIMEZONE)
            local_end = end.astimezone(TIMEZONE)
            # Chile advances clocks at local midnight; on that civil date 00:00
            # does not exist and the first valid instant is 01:00.
            self.assertIn((local_start.hour, local_start.minute), ((0, 0), (1, 0)))
            self.assertEqual(local_start.date(), day)
            self.assertEqual(local_end.date(), day + timedelta(days=1))
            self.assertIn((local_end.hour, local_end.minute), ((0, 0), (1, 0)))
            self.assertGreater(end, start)

    def test_clock_is_santiago_aware(self):
        instant = datetime(2026, 7, 21, 16, tzinfo=timezone.utc)
        self.assertEqual(local_today(instant), self.ANCHOR)

    def test_invalid_preset_is_rejected(self):
        for value in ("", "yesterday", None):
            with self.assertRaises((ValueError, TypeError)):
                preset_range(value, self.ANCHOR)

    def test_invalid_explicit_ranges_are_rejected(self):
        invalid = (("2026-07-22", "2026-07-21"), ("", "2026-07-21"),
                   ("2026-02-30", "2026-03-01"), ("21-07-2026", "2026-07-21"),
                   ("2026-07-21", "2026-07-22"))
        for start, end in invalid:
            with self.assertRaises(ValueError):
                validate_explicit_range(start, end, "custom", today=self.ANCHOR)

    def test_explicit_dates_override_incompatible_preset(self):
        _, _, preset = validate_explicit_range("2026-07-10", "2026-07-15", "today", today=self.ANCHOR)
        self.assertEqual(preset, "custom")


if __name__ == "__main__":
    unittest.main()
