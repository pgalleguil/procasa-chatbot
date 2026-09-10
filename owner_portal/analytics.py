"""Future owner-portal instrumentation contracts; no event is persisted."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from analytics.pricing_intelligence.time_utils import BUSINESS_TZ, parse_aware_datetime

from .schemas import (
    EVENT_METADATA_ALLOWLIST,
    EVENT_SCHEMA_VERSION,
    OWNER_PORTAL_EVENT_NAMES,
    OwnerPortalEventV1,
)

PORTAL_EVENT_NAMES = tuple(sorted(OWNER_PORTAL_EVENT_NAMES))
ALLOWED_EVENT_METADATA = frozenset().union(*EVENT_METADATA_ALLOWLIST.values())
FUTURE_FUNNEL = (
    "email_sent -> portal_opened -> market_section_viewed -> "
    "pricing_recommendation_viewed -> price_authorization_started -> "
    "price_authorized / price_rejected -> post_change_inquiries -> "
    "visits -> closed_operation"
)


def portal_event_contract(
    event_name: str,
    *,
    property_code: str | None = None,
    event_at_utc: str | None = None,
    event_at_local: str | None = None,
    event_at: str | None = None,
    session_id: str | None = None,
    portal_token_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> OwnerPortalEventV1:
    """Validate the future event shape without emitting or persisting it."""

    if event_at:
        parsed_event_at = parse_aware_datetime(event_at, field_name="event_at")
        timestamp_utc = event_at_utc or parsed_event_at.astimezone(timezone.utc).isoformat()
        local_timestamp = event_at_local or parsed_event_at.astimezone(BUSINESS_TZ).isoformat()
    else:
        now = datetime.now(BUSINESS_TZ)
        timestamp_utc = event_at_utc or now.astimezone(timezone.utc).isoformat()
        local_timestamp = event_at_local or now.isoformat()
    parse_aware_datetime(timestamp_utc, field_name="event_at_utc")
    parse_aware_datetime(local_timestamp, field_name="event_at_local")
    return OwnerPortalEventV1(
        event_name=event_name,
        property_code=property_code,
        event_at_utc=timestamp_utc,
        event_at_local=local_timestamp,
        session_id=session_id,
        portal_token_id=portal_token_id,
        metadata=dict(metadata or {}),
        schema_version=EVENT_SCHEMA_VERSION,
    )
