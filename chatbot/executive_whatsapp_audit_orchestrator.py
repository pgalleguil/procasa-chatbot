"""Manual/supervised per-lead commercial audit, independent of all chatbot jobs.

Uses an injected model transport, with explicit opt-in for any persistence.
The only possible MongoDB write is leads.executive_whatsapp_audit, pending
human review. It never sends WhatsApp or changes CRM, SLA or campaigns.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from bson import ObjectId

from .executive_whatsapp_audit_runner import evaluate_with_injected_model
from .executive_whatsapp_audit_storage import persist_audit_candidate


def _lead_id(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError("valid_lead_identity_required")
    try:
        return ObjectId(value)
    except (ValueError, TypeError):
        return value


def evaluate_lead_in_isolation(
    db: Any,
    *,
    lead_id: Any,
    administrator_user_id: str,
    model_call: Callable[[dict[str, Any]], str | Mapping[str, Any]],
    coverage_complete: bool = False,
    persist_candidate: bool = False,
) -> dict[str, Any]:
    """No automatic scheduling and never called from WaSender webhooks.

    Authentication and permissions MUST be verified by a future privileged
    caller; merely providing a string as administrator_user_id is not an ACL.
    """
    if not isinstance(administrator_user_id, str) or not administrator_user_id.strip():
        raise PermissionError("administrator_authentication_required")
    if not callable(model_call):
        raise ValueError("explicit_model_transport_required")
    mongo_id = _lead_id(lead_id)
    lead = db["leads"].find_one(
        {"_id": mongo_id},
        {"_id": 1, "messages": 1, "executive_whatsapp_messages": 1},
    )
    if not lead:
        return {"status": "lead_not_found", "model_calls": 0, "writes": 0}
    executive_messages = lead.get("executive_whatsapp_messages") or []
    if not executive_messages:
        return {"status": "no_executive_chat_evidence", "model_calls": 0, "writes": 0}
    result = evaluate_with_injected_model(
        lead,
        model_call=model_call,
        coverage_complete=coverage_complete,
    )
    candidate = result["candidate"]
    saved = persist_audit_candidate(
        db,
        lead_id=mongo_id,
        candidate=candidate,
        admin_user_id=administrator_user_id,
        commit=persist_candidate,
    )
    return {
        "status": "audit_evaluated",
        "candidate_status": candidate["commercial_status"],
        "review_status": "PENDING_HUMAN_REVIEW",
        "persistence": saved["status"],
        "model_calls": 1,
        "writes": 1 if saved["written"] else 0,
        "campaign_eligible": False,
        "chatbot_messages_changed": False,
        "sla_changed": False,
    }
