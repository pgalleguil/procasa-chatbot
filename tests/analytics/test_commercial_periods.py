from datetime import date, datetime, timezone

import pytest

from analytics.commercial_periods import comparison_period, local_today, shift_year


def test_chile_date_when_utc_is_next_day():
    assert local_today(datetime(2026, 7, 22, 1, 30, tzinfo=timezone.utc)) == date(2026, 7, 21)


def test_local_before_and_after_20_hours():
    assert local_today(datetime(2026, 7, 21, 19, 59)) == date(2026, 7, 21)
    assert local_today(datetime(2026, 7, 21, 20, 1)) == date(2026, 7, 21)


def test_month_comparable_end_and_year_boundary():
    assert comparison_period(date(2026, 7, 1), date(2026, 7, 21), "auto", "month")[:2] == (date(2026, 6, 1), date(2026, 6, 21))
    assert comparison_period(date(2026, 1, 1), date(2026, 1, 31), "auto", "month")[:2] == (date(2025, 12, 1), date(2025, 12, 31))
    assert comparison_period(date(2026, 3, 1), date(2026, 3, 31), "auto", "month")[:2] == (date(2026, 2, 1), date(2026, 2, 28))


def test_previous_equivalent_and_none():
    assert comparison_period(date(2026, 7, 1), date(2026, 7, 21), "prev")[:2] == (date(2026, 6, 10), date(2026, 6, 30))
    assert comparison_period(date(2026, 7, 1), date(2026, 7, 21), "none")[:2] == (None, None)


def test_yoy_clamps_leap_day():
    assert shift_year(date(2024, 2, 29)) == date(2023, 2, 28)


@pytest.mark.parametrize("preset,start,end,mode,expected", [
    ("today", date(2026, 7, 21), date(2026, 7, 21), "auto", (date(2026, 7, 14), date(2026, 7, 14))),
    ("today", date(2026, 7, 21), date(2026, 7, 21), "prev", (date(2026, 7, 20), date(2026, 7, 20))),
    ("today", date(2026, 7, 21), date(2026, 7, 21), "yoy", (date(2025, 7, 21), date(2025, 7, 21))),
    ("today", date(2026, 7, 21), date(2026, 7, 21), "none", (None, None)),
    ("week", date(2026, 7, 20), date(2026, 7, 21), "auto", (date(2026, 7, 13), date(2026, 7, 14))),
    ("week", date(2026, 7, 20), date(2026, 7, 21), "prev", (date(2026, 7, 18), date(2026, 7, 19))),
    ("week", date(2026, 7, 20), date(2026, 7, 21), "yoy", (date(2025, 7, 20), date(2025, 7, 21))),
    ("week", date(2026, 7, 20), date(2026, 7, 21), "none", (None, None)),
    ("month", date(2026, 7, 1), date(2026, 7, 21), "auto", (date(2026, 6, 1), date(2026, 6, 21))),
    ("month", date(2026, 7, 1), date(2026, 7, 21), "prev", (date(2026, 6, 10), date(2026, 6, 30))),
    ("month", date(2026, 7, 1), date(2026, 7, 21), "yoy", (date(2025, 7, 1), date(2025, 7, 21))),
    ("month", date(2026, 7, 1), date(2026, 7, 21), "none", (None, None)),
    ("30d", date(2026, 6, 22), date(2026, 7, 21), "auto", (date(2026, 5, 23), date(2026, 6, 21))),
    ("30d", date(2026, 6, 22), date(2026, 7, 21), "prev", (date(2026, 5, 23), date(2026, 6, 21))),
    ("30d", date(2026, 6, 22), date(2026, 7, 21), "yoy", (date(2025, 6, 22), date(2025, 7, 21))),
    ("30d", date(2026, 6, 22), date(2026, 7, 21), "none", (None, None)),
    ("custom", date(2026, 7, 10), date(2026, 7, 15), "auto", (date(2026, 7, 4), date(2026, 7, 9))),
    ("custom", date(2026, 7, 10), date(2026, 7, 15), "prev", (date(2026, 7, 4), date(2026, 7, 9))),
    ("custom", date(2026, 7, 10), date(2026, 7, 15), "yoy", (date(2025, 7, 10), date(2025, 7, 15))),
    ("custom", date(2026, 7, 10), date(2026, 7, 15), "none", (None, None)),
])
def test_preset_comparison_matrix(preset, start, end, mode, expected):
    assert comparison_period(start, end, mode, preset)[:2] == expected


@pytest.mark.parametrize("start,end", [
    (date(2026, 7, 20), date(2026, 7, 20)),  # Monday
    (date(2026, 7, 19), date(2026, 7, 19)),  # Sunday
    (date(2026, 1, 1), date(2026, 1, 1)),    # Year boundary
])
def test_auto_today_preserves_weekday(start, end):
    previous_start, previous_end, _ = comparison_period(start, end, "auto", "today")
    assert previous_start.weekday() == start.weekday()
    assert previous_end.weekday() == end.weekday()


def test_prev_has_equal_inclusive_duration():
    start, end = date(2026, 7, 1), date(2026, 7, 21)
    previous_start, previous_end, _ = comparison_period(start, end, "prev", "month")
    assert (previous_end - previous_start).days == (end - start).days
