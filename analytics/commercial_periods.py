from calendar import monthrange
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TIMEZONE = ZoneInfo("America/Santiago")


def local_today(now=None):
    current = now or datetime.now(TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=TIMEZONE)
    return current.astimezone(TIMEZONE).date()


def shift_year(value, years=-1):
    year = value.year + years
    return value.replace(year=year, day=min(value.day, monthrange(year, value.month)[1]))


def comparison_period(start, end, mode="auto", preset=None):
    if mode == "none":
        return None, None, "custom_no_comparison"
    if mode == "yoy":
        return shift_year(start), shift_year(end), "custom_vs_yoy"
    if mode == "auto" and preset == "month":
        previous_month_end = start - timedelta(days=1)
        previous_start = previous_month_end.replace(day=1)
        previous_end = previous_start.replace(day=min(end.day, monthrange(previous_start.year, previous_start.month)[1]))
        return previous_start, previous_end, "month_vs_previous_month"
    duration = (end - start).days + 1
    return start - timedelta(days=duration), start - timedelta(days=1), "custom_vs_previous"
