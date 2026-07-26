"""Canonical commercial notification calendar."""
from datetime import datetime, timedelta, timezone

import pytz


CHILE = pytz.timezone("America/Santiago")
BUSINESS_DAYS = frozenset({0, 1, 2, 3, 4})


def as_chile(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("commercial scheduling requires a timezone-aware datetime")
    return value.astimezone(CHILE)


def is_business_time(value: datetime, *, start_hour: int = 9, end_hour: int = 19) -> bool:
    local = as_chile(value)
    return local.weekday() in BUSINESS_DAYS and start_hour <= local.hour < end_hour


def next_business_slot_utc(value: datetime, *, start_hour: int = 9,
                           end_hour: int = 19) -> datetime:
    """Current business instant or next opening; always timezone-aware UTC."""
    local = as_chile(value)
    if is_business_time(local, start_hour=start_hour, end_hour=end_hour):
        return local.astimezone(timezone.utc)

    target_date = local.date()
    if local.weekday() not in BUSINESS_DAYS or local.hour >= end_hour:
        target_date += timedelta(days=1)
    while target_date.weekday() not in BUSINESS_DAYS:
        target_date += timedelta(days=1)
    naive = datetime.combine(target_date, datetime.min.time()).replace(hour=start_hour)
    return CHILE.localize(naive, is_dst=None).astimezone(timezone.utc)
