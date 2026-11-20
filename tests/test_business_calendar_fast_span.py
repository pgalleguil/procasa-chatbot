"""Equivalence and complexity checks for the fast Chilean business calendar.

The reference algorithm deliberately reproduces the previous per-day loop.
No customer or production data is loaded and no SLA records are modified.
"""
from datetime import datetime, timedelta, timezone
from random import Random

import pytest
import pytz

from chatbot import business_calendar as cal


CHILE = pytz.timezone("America/Santiago")


def _slow_reference(start_dt, end_dt, *, start_hour=9, end_hour=19):
    """Previous implementation retained verbatim to protect policy semantics."""
    if not start_dt or not end_dt:
        return 0.0
    start = CHILE.localize(start_dt) if start_dt.tzinfo is None else start_dt.astimezone(CHILE)
    end = CHILE.localize(end_dt) if end_dt.tzinfo is None else end_dt.astimezone(CHILE)
    if start >= end:
        return 0.0
    total = 0.0
    day = start.date()
    while day <= end.date():
        if cal.is_business_day(day):
            opening = cal._local_boundary(day, start_hour)
            closing = cal._local_boundary(day, end_hour)
            left = max(start, opening)
            right = min(end, closing)
            if left < right:
                total += (right - left).total_seconds() / 60.0
        day += timedelta(days=1)
    return total


@pytest.mark.parametrize(
    "start,end",
    [
        (datetime(2026, 9, 4, 18, 30), datetime(2026, 9, 7, 10, 30)),
        (datetime(2026, 10, 9, 18, 0), datetime(2026, 10, 13, 11, 0)),
        (datetime(2026, 12, 31, 10, 0), datetime(2027, 1, 5, 19, 0)),
        (datetime(2025, 3, 29, 10, 0), datetime(2027, 7, 13, 12, 0)),
        (datetime(2026, 11, 20, 19, 0), datetime(2026, 11, 20, 18, 0)),
        (datetime(2026, 11, 20, 9, 0), datetime(2026, 11, 20, 19, 0)),
        (datetime(2026, 11, 20, 23, 0), datetime(2026, 11, 21, 2, 0)),
    ],
)
def test_fast_business_minutes_exactly_matches_previous_day_loop(start, end):
    for hours in ((9, 19), (8, 18), (10, 16), (19, 9)):
        for aware in (False, True):
            begin = CHILE.localize(start) if aware else start
            finish = CHILE.localize(end) if aware else end
            expected = _slow_reference(begin, finish, start_hour=hours[0], end_hour=hours[1])
            actual = cal.calculate_business_minutes(begin, finish, start_hour=hours[0], end_hour=hours[1])
            assert actual == expected, (start, end, hours, aware, expected, actual)


def test_fast_business_minutes_matches_reference_for_deterministic_spans():
    random = Random(20261120)
    for _ in range(75):
        year = random.choice((2024, 2025, 2026, 2027))
        day = random.randint(1, 26)
        month = random.randint(1, 12)
        begin = CHILE.localize(datetime(year, month, day, random.randint(0, 23), random.randint(0, 59)))
        span = random.choice((0, 1, 2, 7, 14, 60, 120, 365, 730, 1200))
        end = begin.astimezone(timezone.utc) + timedelta(days=span, minutes=random.randint(0, 3000))
        for hours in ((9, 19), (8, 18)):
            actual = cal.calculate_business_minutes(begin, end, start_hour=hours[0], end_hour=hours[1])
            expected = _slow_reference(begin, end, start_hour=hours[0], end_hour=hours[1])
            assert actual == expected, (begin, end, hours, actual, expected)


def test_multi_year_span_does_not_call_is_business_day_for_every_date(monkeypatch):
    original = cal.is_business_day
    calls = []

    def observed(day):
        calls.append(day)
        return original(day)

    monkeypatch.setattr(cal, "is_business_day", observed)
    start = CHILE.localize(datetime(2023, 2, 15, 12, 0))
    end = CHILE.localize(datetime(2026, 11, 20, 13, 0))
    minutes = cal.calculate_business_minutes(start, end)
    assert minutes > 0
    assert len(calls) <= 2, "Interior dates must not be visited by the SLA loop"


def test_empty_reverse_and_same_day_semantics_remain_intact():
    start = CHILE.localize(datetime(2026, 11, 20, 12, 0))
    assert cal.calculate_business_minutes(None, start) == 0
    assert cal.calculate_business_minutes(start, None) == 0
    assert cal.calculate_business_minutes(start, start) == 0
    assert cal.calculate_business_minutes(start, start - timedelta(hours=1)) == 0
