"""Read-only 30/60-day assignment cohort report for executive WhatsApp audit.

No conversation bodies or client phone numbers are returned. It counts CRM
assignment cycles, not WaSender sessions, recovered chats or effective contact.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


def assignment_cohort_pipeline(*, days: int = 30, as_of: datetime) -> list[dict[str, Any]]:
    """Build a bounded aggregate on crm_assignment_cycles, never writing."""
    if type(days) is not int or days not in (30, 60):
        raise ValueError("unsupported_cohort_window")
    if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("timezone_aware_as_of_required")
    until = as_of.astimezone(timezone.utc)
    since = until - timedelta(days=days)
    return [
        {"$project": {
            "assigned_at_utc": {"$convert": {
                "input": "$assigned_at", "to": "date",
                "onError": None, "onNull": None,
            }},
            "lead_id": 1,
            "assigned_to_user_id": 1,
            "assigned_to_display_name": 1,
        }},
        {"$match": {
            "assigned_at_utc": {"$gte": since, "$lte": until},
            "lead_id": {"$ne": None},
        }},
        {"$facet": {
            "total": [
                {"$group": {
                    "_id": "$lead_id",
                    "assignment_cycles": {"$sum": 1},
                }},
                {"$group": {
                    "_id": None,
                    "unique_leads": {"$sum": 1},
                    "assignment_cycles": {"$sum": "$assignment_cycles"},
                }},
                {"$project": {"_id": 0, "unique_leads": 1, "assignment_cycles": 1}},
            ],
            "by_executive": [
                {"$group": {
                    "_id": {
                        "lead_id": "$lead_id",
                        "assigned_to_user_id": "$assigned_to_user_id",
                        "display_name": "$assigned_to_display_name",
                    },
                    "assignment_cycles": {"$sum": 1},
                }},
                {"$group": {
                    "_id": {
                        "user_id": "$_id.assigned_to_user_id",
                        "display_name": "$_id.display_name",
                    },
                    "unique_leads_for_executive": {"$sum": 1},
                    "assignment_cycles": {"$sum": "$assignment_cycles"},
                }},
                {"$sort": {"assignment_cycles": -1}},
                {"$limit": 50},
            ],
        }},
    ]


def read_assignment_cohort(db: Any, *, days: int = 30,
                           as_of: datetime) -> dict[str, Any]:
    """Execute only a read aggregation; caller must use read-only credentials."""
    pipeline = assignment_cohort_pipeline(days=days, as_of=as_of)
    rows = list(db["crm_assignment_cycles"].aggregate(pipeline))
    result = rows[0] if rows else {}
    total = (result.get("total") or [{}])[0] if result.get("total") else {}
    executives = []
    for row in result.get("by_executive") or []:
        owner = row.get("_id") or {}
        executives.append({
            "executive_user_id": str(owner.get("user_id") or ""),
            "display_name": str(owner.get("display_name") or ""),
            "leads": int(row.get("unique_leads_for_executive") or 0),
            "assignment_cycles": int(row.get("assignment_cycles") or 0),
        })
    return {
        "days": days,
        "as_of_utc": as_of.astimezone(timezone.utc).isoformat(),
        "unique_leads": int(total.get("unique_leads") or 0),
        "assignment_cycles": int(total.get("assignment_cycles") or 0),
        "by_executive": executives,
        "interpretation": "assigned_leads_not_recovered_conversations",
        "read_only": True,
    }
