"""Read-only lead temperature and age rules for historical price-drop outreach."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


CHILE_TZ = ZoneInfo("America/Santiago")
TEMPERATURE_CHOICES = ("HOT", "NORMAL", "COLD", "HOT,NORMAL", "ALL")
KNOWN_TEMPERATURES = frozenset({"HOT", "NORMAL", "COLD"})


class CampaignFilterError(ValueError):
    pass


def normalize_temperature_filter(value: Any) -> str:
    raw = str(value or "ALL").strip().upper().replace(" ", "")
    raw = {"TODOS": "ALL", "HOT+NORMAL": "HOT,NORMAL"}.get(raw, raw)
    if raw not in TEMPERATURE_CHOICES:
        raise CampaignFilterError("temperature_must_be_HOT_NORMAL_COLD_HOT_NORMAL_or_ALL")
    return raw


def temperature_filter_matches(temperature: Any, selected: Any) -> bool:
    selected = normalize_temperature_filter(selected)
    value = str(temperature or "").strip().upper()
    if selected == "ALL":
        return value in KNOWN_TEMPERATURES or value in {"", "UNKNOWN", "REVISIÓN_PENDIENTE"}
    return value in set(selected.split(","))


def normalize_lead_max_age_days(value: Any) -> int | None:
    """Return an age limit; explicit 0 means the operator chose no limit."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise CampaignFilterError("lead_max_age_days_must_be_0_or_positive_integer")
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise CampaignFilterError("lead_max_age_days_must_be_0_or_positive_integer") from exc
    if days < 0 or days > 3650:
        raise CampaignFilterError("lead_max_age_days_must_be_between_0_and_3650")
    return None if days == 0 else days


def parse_created_at(value: Any) -> tuple[datetime | None, str | None]:
    """Parse lead.created_at without substituting the drop or conversation date.

    BSON datetimes read from PyMongo are naive UTC by convention. ISO strings
    without an offset are treated as Chile local time; explicit offsets win.
    """
    if isinstance(value, datetime):
        stamp = value
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
    elif isinstance(value, str) and value.strip():
        try:
            stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None, "created_at_invalid_review_pending"
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            stamp = stamp.replace(tzinfo=CHILE_TZ)
    else:
        return None, "created_at_missing_review_pending"
    return stamp.astimezone(timezone.utc), None


def lead_age_details(lead: dict[str, Any], *, now: datetime,
                     max_age_days: int | None) -> dict[str, Any]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise CampaignFilterError("now_must_be_timezone_aware")
    limit = normalize_lead_max_age_days(max_age_days)
    created, error = parse_created_at(lead.get("created_at"))
    if error:
        return {"created_at": None, "lead_age_days": None,
                "lead_age_status": error, "excluded_by_age": False}
    now_utc = now.astimezone(timezone.utc)
    if created > now_utc.replace(microsecond=0) + timedelta(minutes=5):
        return {"created_at": created, "lead_age_days": None,
                "lead_age_status": "created_at_future_review_pending",
                "excluded_by_age": False}
    age_days = max((now_utc.astimezone(CHILE_TZ).date()
                    - created.astimezone(CHILE_TZ).date()).days, 0)
    too_old = limit is not None and age_days > limit
    return {"created_at": created, "lead_age_days": age_days,
            "lead_age_status": "lead_age_exceeded" if too_old else "valid",
            "excluded_by_age": too_old}


def effective_temperature(lead: dict[str, Any], review: dict[str, Any] | None = None) -> str:
    raw = str(lead.get("lead_temperature_effective") or lead.get("lead_temperature") or "").strip().upper()
    if raw in KNOWN_TEMPERATURES:
        return raw
    reviewed = str((review or {}).get("reviewed_temperature") or "").strip().upper()
    return reviewed if reviewed in KNOWN_TEMPERATURES else "REVISIÓN_PENDIENTE"


def temperature_review_is_required(lead: dict[str, Any],
                                   review: dict[str, Any] | None = None) -> bool:
    raw = str(lead.get("lead_temperature_effective") or lead.get("lead_temperature") or "").strip().upper()
    if raw in KNOWN_TEMPERATURES:
        return False
    review = review or {}
    return not (
        str(review.get("reviewed_temperature") or "").strip().upper() in KNOWN_TEMPERATURES
        and len(str(review.get("evidence") or "").strip()) >= 8
        and bool(str(review.get("reviewed_by") or "").strip())
    )
