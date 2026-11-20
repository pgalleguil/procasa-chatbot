"""Canonical Chilean commercial calendar used by assignment and SLA flows."""
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache

import holidays
import pytz


CHILE = pytz.timezone("America/Santiago")
BUSINESS_DAYS = frozenset({0, 1, 2, 3, 4})
BUSINESS_START_HOUR = 9
BUSINESS_END_HOUR = 19


@lru_cache(maxsize=32)
def _chile_holidays(year: int):
    """Use the maintained Chile public-holiday calendar for a local year."""
    return holidays.country_holidays("CL", years=[int(year)], observed=True)


def as_chile(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("commercial scheduling requires a timezone-aware datetime")
    return value.astimezone(CHILE)


def _local_date(value: date | datetime) -> date:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return CHILE.localize(value).date()
        return value.astimezone(CHILE).date()
    if isinstance(value, date):
        return value
    raise ValueError("commercial calendar requires a date or datetime")


def is_business_day(value: date | datetime) -> bool:
    """Return whether a Chile-local date is a weekday and not a public holiday."""
    local_date = _local_date(value)
    return (
        local_date.weekday() in BUSINESS_DAYS
        and local_date not in _chile_holidays(local_date.year)
    )


def _local_boundary(day: date, hour: int) -> datetime:
    return CHILE.localize(datetime(day.year, day.month, day.day, hour), is_dst=None)


def is_business_time(value: datetime, *, start_hour: int = 9, end_hour: int = 19) -> bool:
    local = as_chile(value)
    return is_business_day(local.date()) and start_hour <= local.hour < end_hour


def next_business_slot_utc(value: datetime, *, start_hour: int = 9,
                           end_hour: int = 19) -> datetime:
    """Current business instant or next opening; always timezone-aware UTC."""
    local = as_chile(value)
    if is_business_time(local, start_hour=start_hour, end_hour=end_hour):
        return local.astimezone(timezone.utc)

    target_date = local.date()
    if not is_business_day(target_date) or local.hour >= end_hour:
        target_date += timedelta(days=1)
    elif local.hour >= start_hour:
        # The only remaining non-business-time case on a work day is the
        # closing boundary; move to the next local date.
        target_date += timedelta(days=1)
    while not is_business_day(target_date):
        target_date += timedelta(days=1)
    return _local_boundary(target_date, start_hour).astimezone(timezone.utc)


def _business_weekdays_inclusive(first: date, last: date) -> int:
    """Count business dates in [first, last] without iterating over every day.

    This uses the same country_holidays('CL', observed=True) cache as
    is_business_day; only public-holiday dates are inspected individually.
    """
    if first > last:
        return 0
    days = (last - first).days + 1
    whole_weeks, extra_days = divmod(days, 7)
    workdays = whole_weeks * 5 + sum(
        (first.weekday() + offset) % 7 in BUSINESS_DAYS
        for offset in range(extra_days)
    )
    seen_holidays = set()
    for year in range(first.year, last.year + 1):
        for holiday in _chile_holidays(year):
            if first <= holiday <= last and holiday.weekday() in BUSINESS_DAYS:
                seen_holidays.add(holiday)
    return max(workdays - len(seen_holidays), 0)


def _partial_business_minutes(
    day: date, first: datetime, last: datetime, *,
    start_hour: int, end_hour: int,
) -> float:
    """Exact original single-day clamp, preserving local-boundary behavior."""
    if not is_business_day(day):
        return 0.0
    opening = _local_boundary(day, start_hour)
    closing = _local_boundary(day, end_hour)
    period_start = max(first, opening)
    period_end = min(last, closing)
    if period_start < period_end:
        return (period_end - period_start).total_seconds() / 60.0
    return 0.0


def calculate_business_minutes(start_dt: datetime, end_dt: datetime, *,
                               start_hour: int = BUSINESS_START_HOUR,
                               end_hour: int = BUSINESS_END_HOUR) -> float:
    """Count business minutes between instants using Chile-local days.

    Local calendar boundaries are rebuilt for each date. This keeps the
    calculation correct when America/Santiago changes its UTC offset.
    """
    if not start_dt or not end_dt:
        return 0.0
    start = CHILE.localize(start_dt) if start_dt.tzinfo is None else start_dt.astimezone(CHILE)
    end = CHILE.localize(end_dt) if end_dt.tzinfo is None else end_dt.astimezone(CHILE)
    if start >= end:
        return 0.0

    first_day, last_day = start.date(), end.date()
    if first_day == last_day:
        return _partial_business_minutes(
            first_day, start, end,
            start_hour=start_hour, end_hour=end_hour,
        )

    # Only the two edge dates depend on the exact clock times. Every complete
    # interior business day contributes the same hour window; count weekdays
    # and Chilean holidays arithmetically instead of iterating over months or
    # years of historical SLA cycles on the asyncio event loop.
    minutes = _partial_business_minutes(
        first_day, start, end,
        start_hour=start_hour, end_hour=end_hour,
    ) + _partial_business_minutes(
        last_day, start, end,
        start_hour=start_hour, end_hour=end_hour,
    )
    if (last_day - first_day).days > 1 and start_hour < end_hour:
        interior_days = _business_weekdays_inclusive(
            first_day + timedelta(days=1),
            last_day - timedelta(days=1),
        )
        minutes += interior_days * (end_hour - start_hour) * 60.0
    return minutes


def add_business_minutes(start_dt: datetime, minutes_to_add: float, *,
                         start_hour: int = BUSINESS_START_HOUR,
                         end_hour: int = BUSINESS_END_HOUR) -> datetime:
    """Add SLA minutes inside the Chilean business calendar and return UTC."""
    if not isinstance(start_dt, datetime):
        raise ValueError("business-minute start must be a datetime")
    current = CHILE.localize(start_dt) if start_dt.tzinfo is None else start_dt.astimezone(CHILE)
    remaining = float(minutes_to_add)
    if remaining <= 0:
        return current.astimezone(timezone.utc)

    current = next_business_slot_utc(
        current, start_hour=start_hour, end_hour=end_hour
    ).astimezone(CHILE)
    while remaining > 0:
        day_end = _local_boundary(current.date(), end_hour)
        available = max(0.0, (day_end - current).total_seconds() / 60.0)
        if remaining <= available:
            return (current + timedelta(minutes=remaining)).astimezone(timezone.utc)
        remaining -= available

        next_date = current.date() + timedelta(days=1)
        while not is_business_day(next_date):
            next_date += timedelta(days=1)
        current = _local_boundary(next_date, start_hour)
    return current.astimezone(timezone.utc)
