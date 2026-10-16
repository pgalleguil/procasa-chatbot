"""Meeting commitments stored separately from CRM leads and events."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from uuid import uuid4
from typing import Any, Mapping


COLLECTION = "crm_control_meeting_commitments"
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_STATUSES = {"pending", "completed", "blocked"}


def previous_month(month: str) -> str:
    if not _MONTH_RE.fullmatch(str(month or "")):
        raise ValueError("meeting_month_invalid")
    year, number = (int(part) for part in month.split("-"))
    return f"{year - 1:04d}-12" if number == 1 else f"{year:04d}-{number - 1:02d}"


def get_meeting_commitments(db: Any, *, executive: str, meeting_month: str) -> dict[str, Any]:
    executive = str(executive or "").strip()
    if not executive or not _MONTH_RE.fullmatch(str(meeting_month or "")):
        raise ValueError("executive_and_meeting_month_required")
    collection = db[COLLECTION]
    projection = {"_id": 0, "executive": 1, "meeting_month": 1, "meeting_date": 1,
                  "next_review_date": 1, "commitments": 1, "updated_at": 1}
    current = collection.find_one({"executive": executive, "meeting_month": meeting_month}, projection) or {
        "executive": executive, "meeting_month": meeting_month, "commitments": [],
    }
    prior_month = previous_month(meeting_month)
    previous = collection.find_one({"executive": executive, "meeting_month": prior_month}, projection) or {
        "executive": executive, "meeting_month": prior_month, "commitments": [],
    }
    return {"current_meeting": current, "previous_meeting": previous,
            "source": COLLECTION, "independent_of_crm": True}


def save_meeting_commitment(
    db: Any,
    *,
    executive: str,
    meeting_month: str,
    actor: str,
    payload: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    executive = str(executive or "").strip()
    if not executive or len(executive) > 120 or not _MONTH_RE.fullmatch(str(meeting_month or "")):
        raise ValueError("executive_and_meeting_month_required")
    action = str(payload.get("action") or "").strip()
    responsible = str(payload.get("responsible") or "").strip()
    due_date = str(payload.get("due_date") or "").strip()
    if not action or len(action) > 1000 or not responsible or len(responsible) > 120:
        raise ValueError("action_and_responsible_required")
    try:
        datetime.strptime(due_date, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("due_date_invalid") from exc
    now = now or datetime.now(timezone.utc)
    entry = {
        "id": str(uuid4()), "action": action, "responsible": responsible,
        "due_date": due_date, "status": "pending", "evidence": "",
        "created_at": now, "created_by": str(actor or "Supervisor").strip(),
    }
    meeting_date = str(payload.get("meeting_date") or "").strip()
    next_review_date = str(payload.get("next_review_date") or "").strip()
    for field, value in (("meeting_date", meeting_date), ("next_review_date", next_review_date)):
        if value:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError as exc:
                raise ValueError(f"{field}_invalid") from exc
    db[COLLECTION].update_one(
        {"executive": executive, "meeting_month": meeting_month},
        {"$setOnInsert": {"executive": executive, "meeting_month": meeting_month,
                          "created_at": now},
         "$set": {"meeting_date": meeting_date or None,
                  "next_review_date": next_review_date or None, "updated_at": now},
         "$push": {"commitments": entry}},
        upsert=True,
    )
    return {"saved": True, "commitment": {**entry, "created_at": now.isoformat()}}


def update_meeting_commitment(
    db: Any,
    *,
    executive: str,
    meeting_month: str,
    commitment_id: str,
    status: str,
    evidence: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not executive or not _MONTH_RE.fullmatch(str(meeting_month or "")):
        raise ValueError("executive_and_meeting_month_required")
    if status not in _STATUSES:
        raise ValueError("commitment_status_invalid")
    evidence = str(evidence or "").strip()
    if len(evidence) > 2000:
        raise ValueError("evidence_too_long")
    result = db[COLLECTION].update_one(
        {"executive": executive, "meeting_month": meeting_month,
         "commitments.id": str(commitment_id)},
        {"$set": {"commitments.$.status": status,
                  "commitments.$.evidence": evidence,
                  "updated_at": now or datetime.now(timezone.utc)}},
    )
    if not getattr(result, "matched_count", 0):
        raise LookupError("commitment_not_found")
    return {"saved": True, "commitment_id": str(commitment_id), "status": status}
