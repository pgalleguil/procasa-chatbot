from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from analytics.pricing_intelligence.time_utils import (
    BUSINESS_TZ,
    UTC,
    ensure_aware,
    is_in_previous_window,
    is_observable_before,
    local_snapshot_date,
    previous_window,
    to_business_time,
    to_utc,
)


def test_naive_datetime_is_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        ensure_aware(datetime(2026, 9, 9, 12))


def test_business_timezone_conversion_is_explicit():
    value = datetime(2026, 9, 9, 15, tzinfo=UTC)
    converted = to_business_time(value)
    assert converted.tzinfo == BUSINESS_TZ
    assert to_utc(converted).tzinfo == UTC


def test_event_before_cutoff_is_included():
    as_of = datetime(2026, 9, 9, 12, tzinfo=UTC)
    assert is_observable_before(as_of - timedelta(seconds=1), as_of)


def test_event_equal_to_cutoff_is_excluded():
    as_of = datetime(2026, 9, 9, 12, tzinfo=UTC)
    assert not is_observable_before(as_of, as_of)


def test_event_after_cutoff_is_excluded():
    as_of = datetime(2026, 9, 9, 12, tzinfo=UTC)
    assert not is_observable_before(as_of + timedelta(seconds=1), as_of)


def test_previous_windows_are_start_inclusive_end_exclusive():
    as_of = datetime(2026, 9, 9, 12, tzinfo=UTC)
    start, end = previous_window(as_of, 7)
    assert start == as_of - timedelta(days=7)
    assert end == as_of
    assert is_in_previous_window(start, as_of, 7)
    assert not is_in_previous_window(end, as_of, 7)


def test_previous_30_day_window_is_exact():
    as_of = datetime(2026, 9, 9, 12, tzinfo=UTC)
    assert is_in_previous_window(as_of - timedelta(days=30), as_of, 30)
    assert not is_in_previous_window(as_of - timedelta(days=30, seconds=1), as_of, 30)


def test_zoneinfo_handles_a_real_dst_transition_without_fixed_offset():
    zone = ZoneInfo("America/Santiago")
    offsets = {}
    for day_of_year in range(1, 366):
        current = date(2026, 1, 1) + timedelta(days=day_of_year - 1)
        offsets[current] = datetime(current.year, current.month, current.day, 12, tzinfo=zone).utcoffset()
    transitions = [day for day in sorted(offsets) if day > min(offsets) and offsets[day] != offsets[day - timedelta(days=1)]]
    assert transitions, "tzdata should expose a Chile DST transition"
    transition = transitions[0]
    before = datetime.combine(transition - timedelta(days=1), datetime.min.time().replace(hour=12), tzinfo=zone)
    after = datetime.combine(transition, datetime.min.time().replace(hour=12), tzinfo=zone)
    assert before.utcoffset() != after.utcoffset()
    assert to_utc(before).tzinfo == UTC
