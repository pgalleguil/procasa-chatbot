from datetime import date, datetime, timezone

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
