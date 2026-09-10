"""Future instrumentation contracts; no event is written in the prototype."""

from __future__ import annotations

from typing import Any

from .schemas import OwnerPortalEventContract

PORTAL_EVENT_NAMES = (
    "portal_opened",
    "market_section_viewed",
    "price_history_viewed",
    "pricing_recommendation_viewed",
    "price_authorization_started",
    "price_authorized",
    "price_rejected",
    "contact_executive_clicked",
)

ALLOWED_EVENT_METADATA = frozenset(
    {"source_section", "cta", "surface", "market_comparables_count"}
)


def portal_event_contract(
    event_name: str,
    *,
    property_code: str | None = None,
    event_at: str | None = None,
    session_id: str | None = None,
    portal_token_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> OwnerPortalEventContract:
    """Validate the future event shape without emitting or persisting it."""

    if event_name not in PORTAL_EVENT_NAMES:
        raise ValueError(f"Unknown owner portal event: {event_name}")
    metadata = dict(metadata or {})
    unknown = set(metadata) - ALLOWED_EVENT_METADATA
    if unknown:
        raise ValueError(f"Disallowed event metadata: {sorted(unknown)}")
    return OwnerPortalEventContract(
        event_name=event_name,
        property_code=property_code,
        event_at=event_at,
        session_id=session_id,
        portal_token_id=portal_token_id,
        metadata=metadata,
    )


FUTURE_FUNNEL = (
    "portal_opened -> market_section_viewed / price_history_viewed -> "
    "pricing_recommendation_viewed -> price_authorization_started -> "
    "price_authorized | price_rejected -> contact_executive_clicked"
)
