"""Centralized timezone and strict as-of utilities."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

UTC = timezone.utc
BUSINESS_TZ = ZoneInfo("America/Santiago")


class HistoricalSnapshotNotSupported(ValueError):
    """Raised when V1 is asked to fabricate a historical snapshot."""


def ensure_aware(value: datetime, *, field_name: str = "datetime") -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def parse_aware_datetime(value: Any, *, field_name: str = "datetime") -> datetime:
    """Parse only values that explicitly carry timezone information."""

    if isinstance(value, datetime):
        return ensure_aware(value, field_name=field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be an aware datetime or ISO string")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a valid ISO datetime") from exc
    return ensure_aware(parsed, field_name=field_name)


def to_business_time(value: datetime) -> datetime:
    return ensure_aware(value).astimezone(BUSINESS_TZ)


def to_utc(value: datetime) -> datetime:
    return ensure_aware(value).astimezone(UTC)


def local_snapshot_date(as_of: datetime) -> date:
    return to_business_time(as_of).date()


def previous_window(as_of: datetime, days: int) -> tuple[datetime, datetime]:
    if days <= 0:
        raise ValueError("days must be positive")
    end = ensure_aware(as_of)
    return end - timedelta(days=days), end


def is_observable_before(event_time: datetime, as_of: datetime) -> bool:
    """The strict leakage guard: event_time < as_of."""

    return ensure_aware(event_time, field_name="event_time") < ensure_aware(as_of, field_name="as_of")


def is_in_previous_window(event_time: datetime, as_of: datetime, days: int) -> bool:
    start, end = previous_window(as_of, days)
    event = ensure_aware(event_time, field_name="event_time")
    return start <= event < end and is_observable_before(event, end)


def validate_operational_as_of(as_of: datetime, *, now: datetime) -> datetime:
    """Permit only a current-local-date operational snapshot in V1."""

    as_of = ensure_aware(as_of, field_name="as_of")
    now = ensure_aware(now, field_name="now")
    as_of_local = to_business_time(as_of)
    now_local = to_business_time(now)
    if as_of > now:
        raise HistoricalSnapshotNotSupported("V1 no permite un cutoff futuro")
    if as_of_local.date() != now_local.date():
        raise HistoricalSnapshotNotSupported(
            "V1 solo permite snapshots operacionales de la fecha local actual; "
            "la reconstrucción histórica no está soportada"
        )
    return as_of
