from calendar import monthrange
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TIMEZONE = ZoneInfo("America/Santiago")
VALID_PRESETS = ("today", "week", "month", "30d", "custom")
VALID_COMPARISONS = ("auto", "prev", "yoy", "none")


def local_today(now=None):
    current = now or datetime.now(TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=TIMEZONE)
    return current.astimezone(TIMEZONE).date()


def shift_year(value, years=-1):
    year = value.year + years
    return value.replace(year=year, day=min(value.day, monthrange(year, value.month)[1]))


def preset_range(preset, anchor):
    """Return the inclusive local-date range for a dashboard preset."""
    if preset not in VALID_PRESETS or preset == "custom":
        raise ValueError("preset requires an explicit valid range")
    if isinstance(anchor, datetime):
        anchor = local_today(anchor)
    if not isinstance(anchor, date):
        raise TypeError("anchor must be a date or datetime")
    if preset == "today":
        start = anchor
    elif preset == "week":
        # “Semana” representa una ventana móvil de 7 días incluido hoy.
        start = anchor - timedelta(days=6)
    elif preset == "month":
        start = anchor.replace(day=1)
    else:
        start = anchor - timedelta(days=29)
    return start, anchor


def canonical_preset(start, end, declared=None):
    """Keep a declared preset only when the explicit inclusive range has its shape."""
    days = (end - start).days + 1
    matches = {
        "today": days == 1,
        "week": days == 7,
        "month": start.day == 1 and start.year == end.year and start.month == end.month,
        "30d": days == 30,
        "custom": True,
    }
    return declared if declared in matches and matches[declared] else "custom"


def validate_explicit_range(period_start, period_end, preset=None, today=None):
    if bool(period_start) != bool(period_end):
        raise ValueError("Desde y hasta deben informarse juntos")
    if not period_start:
        return None, None, preset
    try:
        start = datetime.strptime(period_start, "%Y-%m-%d").date()
        end = datetime.strptime(period_end, "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise ValueError("Fecha inválida; use AAAA-MM-DD") from exc
    if start > end:
        raise ValueError("Desde no puede ser posterior a hasta")
    if end > (today or local_today()):
        raise ValueError("El periodo no puede terminar en el futuro")
    return start, end, canonical_preset(start, end, preset)


def comparison_period(start, end, mode="auto", preset=None):
    if mode == "none":
        return None, None, "custom_no_comparison"
    if mode == "yoy":
        return shift_year(start), shift_year(end), "custom_vs_yoy"
    if mode in ("auto", "prev"):
        if preset == "today":
            offset = 7 if mode == "auto" else 1
            return start - timedelta(days=offset), end - timedelta(days=offset), f"today_vs_previous_{'week' if mode == 'auto' else 'day'}"
        if preset == "week":
            return start - timedelta(days=7), end - timedelta(days=7), f"{preset}_vs_previous_week"
        if preset == "month":
            previous_month_end = start - timedelta(days=1)
            previous_start = previous_month_end.replace(day=1)
            duration = (end - start).days + 1
            candidate_end = previous_start + timedelta(days=duration - 1)
            if candidate_end > previous_month_end:
                previous_end = previous_month_end
                previous_start = previous_end - timedelta(days=duration - 1)
            else:
                previous_end = candidate_end
            return previous_start, previous_end, "month_vs_previous_month"
    duration = (end - start).days + 1
    return start - timedelta(days=duration), start - timedelta(days=1), "custom_vs_previous"
