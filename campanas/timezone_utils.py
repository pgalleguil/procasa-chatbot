"""Timezone conventions for owner-campaign timestamps."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


STORAGE_TIMEZONE = timezone.utc
DISPLAY_TIMEZONE = ZoneInfo("America/Santiago")


def format_event_at_for_display(event_at: datetime) -> str:
    """Format a UTC-stored event timestamp using Chile's legal timezone."""
    if event_at.tzinfo is None:
        # PyMongo commonly returns BSON datetimes as naive UTC values.
        event_at = event_at.replace(tzinfo=STORAGE_TIMEZONE)
    local = event_at.astimezone(DISPLAY_TIMEZONE)
    return f"{local:%d-%m-%Y %H:%M} America/Santiago"
