"""Optional audit-candidate snapshot inside the SAME leads document.

No chatbot messages, CRM management or SLA fields are ever written.
This is not an API endpoint and is not invoked by any production worker.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping

from bson import BSON

from .executive_whatsapp_audit import MAX_LEAD_BSON_BYTES

AUDIT_VERSION = "executive_whatsapp_audit_candidate_v1"
_MAX_EVIDENCE_KEYS = 100


def _snapshot(candidate: Mapping[str, Any], *, created_at: datetime) -> dict:
    if not isinstance(candidate, Mapping) or candidate.get("review_status") != "PENDING_HUMAN_REVIEW":
        raise ValueError("unreviewed_audit_candidate_required")
    status = candidate.get("commercial_status")
    if status not in {
        "ACTIVE_INTEREST", "VISIT_COORDINATED", "FOLLOW_UP_PENDING",
        "NO_EFFECTIVE_CONTACT", "NO_LONGER_SEARCHING",
        "TRANSACTION_COMPLETED", "DO_NOT_CONTACT", "UNKNOWN",
    }:
        raise ValueError("invalid_audit_status")
    evidence = candidate.get("evidence_provider_keys")
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= _MAX_EVIDENCE_KEYS:
        raise ValueError("audit_evidence_required")
    if any(not isinstance(key, str) or not key.strip() or len(key) > 200 for key in evidence):
        raise ValueError("invalid_audit_evidence")
    if not isinstance(created_at, datetime) or created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("audit_time_requires_timezone")
    for flag in ("contact_effective", "follow_up_required", "opt_out"):
        if candidate.get(flag) is not None and type(candidate.get(flag)) is not bool:
            raise ValueError("invalid_audit_boolean")
    uncertainty = candidate.get("uncertainty")
    if not isinstance(uncertainty, str) or len(uncertainty) > 600:
        raise ValueError("invalid_uncertainty")
    # No unrestricted LLM free-text preferences or next_action in the stored
    # preview; those may contain additional personal information.
    data = {
        "schema": AUDIT_VERSION,
        "review_status": "PENDING_HUMAN_REVIEW",
        "commercial_status": status,
        "contact_effective": candidate.get("contact_effective"),
        "follow_up_required": candidate.get("follow_up_required"),
        "opt_out": candidate.get("opt_out"),
        "coverage_complete": candidate.get("coverage_complete") is True,
        "evidence_provider_keys": sorted(set(evidence)),
        "uncertainty": uncertainty,
        "created_at": created_at.astimezone(timezone.utc),
        "changes_crm_management": False,
        "campaign_eligible": False,
    }
    revision_input = {k: v for k, v in data.items() if k != "created_at"}
    data["revision_sha256"] = hashlib.sha256(
        json.dumps(revision_input, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return data


def persist_audit_candidate(
    db: Any,
    *,
    lead_id: Any,
    candidate: Mapping[str, Any],
    admin_user_id: str,
    commit: bool = False,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """Write pending review only, with explicit opt-in and evidence fencing.

    Caller MUST already have verified a privileged CRM administrator.
    NEVER expose this function from a public webhook or LLM-invocable tool.
    """
    if not isinstance(admin_user_id, str) or not admin_user_id.strip():
        raise ValueError("authenticated_admin_required")
    if lead_id is None or lead_id == "":
        raise ValueError("lead_id_required")
    when = created_at or datetime.now(timezone.utc)
    snapshot = _snapshot(candidate, created_at=when)
    snapshot["requested_by_user_id"] = admin_user_id
    observed = db["leads"].find_one(
        {"_id": lead_id},
        {"_id": 1, "executive_whatsapp_messages.provider_key": 1,
         "executive_whatsapp_audit.revision_sha256": 1},
    )
    if not observed:
        return {"status": "lead_not_found", "written": False}
    exec_evidence = {
        msg.get("provider_key")
        for msg in (observed.get("executive_whatsapp_messages") or [])
        if isinstance(msg, dict) and isinstance(msg.get("provider_key"), str)
    }
    if not set(snapshot["evidence_provider_keys"]).issubset(exec_evidence):
        return {"status": "evidence_not_in_executive_messages", "written": False}
    existing_revision = (observed.get("executive_whatsapp_audit") or {}).get("revision_sha256")
    if existing_revision == snapshot["revision_sha256"]:
        return {"status": "duplicate_confirmed", "written": False}
    if not commit:
        return {"status": "planned", "written": False}

    # Conservative upper-bound filter. No modifications to existing messages,
    # temperature, management, assignment, contacts, or SLA.
    required_capacity = len(BSON.encode(snapshot)) + 2048
    cap = MAX_LEAD_BSON_BYTES - required_capacity
    if cap <= 0:
        return {"status": "snapshot_too_large", "written": False}
    result = db["leads"].update_one(
        {
            "_id": lead_id,
            "executive_whatsapp_messages.provider_key": {
                "$all": snapshot["evidence_provider_keys"],
            },
            "$expr": {"$lt": [{"$bsonSize": "$$ROOT"}, cap]},
        },
        {"$set": {"executive_whatsapp_audit": snapshot}},
        upsert=False,
    )
    if result.modified_count != 1:
        return {"status": "write_unconfirmed", "written": False}
    return {"status": "pending_review_saved", "written": True}
