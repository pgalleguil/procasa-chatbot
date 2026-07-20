"""Canonical human-management results for Hot and Cold CRM leads."""
from __future__ import annotations

from datetime import timedelta
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from .crm_metrics import coerce_utc_datetime, utc_now

RESULT_RULES = {
    "MESSAGE_SENT_WAITING_RESPONSE": {"attempt": True, "effective": False, "follow_up": True, "status": "managed_waiting_response"},
    "CALL_NO_ANSWER": {"attempt": True, "effective": False, "follow_up": True, "status": "managed_waiting_response"},
    "EMAIL_SENT": {"attempt": True, "effective": False, "follow_up": True, "status": "managed_waiting_response"},
    "EFFECTIVE_CONTACT": {"attempt": True, "effective": True, "follow_up": False, "status": "managed_contacted"},
    "FOLLOW_UP_REQUESTED": {"attempt": True, "effective": True, "follow_up": True, "status": "managed_follow_up"},
    "NOT_INTERESTED": {"attempt": True, "effective": True, "follow_up": False, "status": "managed_closed"},
    "INVALID_NUMBER": {"attempt": True, "effective": False, "follow_up": False, "status": "managed_closed"},
    "DISCARDED_VALID_REASON": {"attempt": False, "effective": False, "follow_up": False, "status": "managed_closed"},
    "SCHEDULE_FOLLOW_UP": {"attempt": False, "effective": False, "follow_up": True, "status": "managed_follow_up"},
}


def _default_follow_up(occurred_at):
    from .lead_router import get_next_business_slot
    local = occurred_at.astimezone(__import__("chatbot.constants", fromlist=["CHILE_TZ"]).CHILE_TZ)
    return coerce_utc_datetime(get_next_business_slot(local + timedelta(days=1)))


def record_management_result(db, *, lead_id, assignment_cycle_id, actor_user_id,
                             result_type, source, idempotency_key, occurred_at=None,
                             next_follow_up_at=None) -> dict:
    result_type = str(result_type or "").upper()
    rule = RESULT_RULES.get(result_type)
    if not rule:
        raise ValueError("unsupported CRM management result")
    if not all(str(value or "").strip() for value in (lead_id, assignment_cycle_id, actor_user_id, source, idempotency_key)):
        raise ValueError("canonical management identity is incomplete")
    occurred = coerce_utc_datetime(occurred_at) or utc_now()
    cycle = db["crm_assignment_cycles"].find_one({
        "lead_id": lead_id, "assignment_cycle_id": assignment_cycle_id,
        "cycle_status": "active", "unassigned_at": None,
    })
    if not cycle:
        raise ValueError("active assignment cycle not found")
    if str(cycle.get("assigned_to_user_id")) != str(actor_user_id):
        raise PermissionError("management actor does not own the active cycle")

    record = {
        "_id": f"crm_management:{idempotency_key}", "idempotency_key": idempotency_key,
        "schema_version": "crm_management_result_v1", "lead_id": lead_id,
        "assignment_cycle_id": assignment_cycle_id, "actor_user_id": actor_user_id,
        "result_type": result_type, "occurred_at": occurred, "source": source,
        "status": "processing",
    }
    try:
        db["crm_management_results"].insert_one(record)
    except DuplicateKeyError:
        existing = db["crm_management_results"].find_one({"_id": record["_id"]})
        if existing and existing.get("status") == "completed":
            return existing

    follow_at = coerce_utc_datetime(next_follow_up_at)
    if rule["follow_up"] and not follow_at:
        follow_at = _default_follow_up(occurred)
    follow_cycle_id = f"followup:{assignment_cycle_id}:{idempotency_key}" if rule["follow_up"] else None
    lead_updates = {
        "management_status": rule["status"], "contact_attempted": rule["attempt"],
        "effective_contact": rule["effective"], "follow_up_required": rule["follow_up"],
        "follow_up_status": "pending" if rule["follow_up"] else "not_required",
    }
    if rule["follow_up"]:
        lead_updates.update({"next_follow_up_at": follow_at, "follow_up_owner_user_id": actor_user_id,
                             "follow_up_cycle_id": follow_cycle_id, "follow_up_completed_at": None})
    db["leads"].update_one({"_id": lead_id}, {"$set": lead_updates})
    # First timestamps use compare-and-set: duplicates and later results cannot replace them.
    db["leads"].update_one({"_id": lead_id, "lifecycle.first_valid_management_at": {"$exists": False}},
                            {"$set": {"lifecycle.first_valid_management_at": occurred}})
    if rule["attempt"]:
        db["leads"].update_one({"_id": lead_id, "lifecycle.first_contact_attempt_at": {"$exists": False}},
                                {"$set": {"lifecycle.first_contact_attempt_at": occurred}})
    if rule["effective"]:
        db["leads"].update_one({"_id": lead_id, "lifecycle.first_effective_contact_at": {"$exists": False}},
                                {"$set": {"lifecycle.first_effective_contact_at": occurred}})
    first_cycle_updates = {"first_valid_management_at": occurred, "first_valid_management_actor": actor_user_id}
    if rule["attempt"]: first_cycle_updates["first_contact_attempt_at"] = occurred
    if rule["effective"]: first_cycle_updates["first_effective_contact_at"] = occurred
    cycle_updates = {"sla_first_management_status": "completed", "sla_pending_alerts_cancelled_at": occurred,
                     "follow_up_required": rule["follow_up"], "last_management_result": result_type,
                     "sla_alert_claims.yellow.status": "suppressed", "sla_alert_claims.red.status": "suppressed"}
    if rule["follow_up"]:
        cycle_updates.update({"next_follow_up_at": follow_at, "follow_up_owner_user_id": actor_user_id,
                              "follow_up_status": "pending", "follow_up_cycle_id": follow_cycle_id})
    db["crm_assignment_cycles"].update_one(
        {"assignment_cycle_id": assignment_cycle_id, "first_valid_management_at": {"$exists": False}},
        {"$set": first_cycle_updates},
    )
    db["crm_assignment_cycles"].update_one(
        {"assignment_cycle_id": assignment_cycle_id, "cycle_status": "active"}, {"$set": cycle_updates}
    )
    db["crm_notifications_v1"].update_many(
        {"assignment_cycle_id": assignment_cycle_id, "notification_type": {"$in": ["sla_yellow", "sla_red"]},
         "state": {"$in": ["pending", "failed_retryable"]}},
        {"$set": {"state": "suppressed", "suppressed_reason": "management_completed", "updated_at": occurred}},
    )
    event = {"_id": f"crm_event:{idempotency_key}", "lead_id": lead_id,
             "assignment_cycle_id": assignment_cycle_id, "actor": actor_user_id,
             "actor_type": "human", "type": "CONTACT_RESULT", "result": result_type,
             "confirmed": True, "timestamp": occurred, "source": source,
             "idempotency_key": idempotency_key}
    try:
        db["crm_events"].insert_one(event)
    except DuplicateKeyError:
        pass
    db["crm_management_results"].update_one(
        {"_id": record["_id"]}, {"$set": {**record, "status": "completed",
                                             "follow_up_required": rule["follow_up"],
                                             "next_follow_up_at": follow_at}}, upsert=True,
    )
    return db["crm_management_results"].find_one({"_id": record["_id"]}) or record


def claim_sla_alert_if_still_eligible(db, *, assignment_cycle_id, level, recipient_user_id, claimed_at=None):
    """Final atomic eligibility check immediately before a shadow/real delivery."""
    now = coerce_utc_datetime(claimed_at) or utc_now()
    field = f"sla_alert_claims.{level}"
    return db["crm_assignment_cycles"].find_one_and_update(
        {"assignment_cycle_id": assignment_cycle_id, "cycle_status": "active",
         "assigned_to_user_id": recipient_user_id, "first_valid_management_at": {"$exists": False},
         field: {"$exists": False}},
        {"$set": {field: {"status": "claimed", "claimed_at": now}}},
        return_document=ReturnDocument.AFTER,
    )


def confirm_sla_alert_claim(db, *, assignment_cycle_id, level, recipient_user_id, confirmed_at=None):
    """Second CAS directly before provider use; management suppression wins."""
    now = coerce_utc_datetime(confirmed_at) or utc_now()
    field = f"sla_alert_claims.{level}.status"
    return db["crm_assignment_cycles"].find_one_and_update(
        {"assignment_cycle_id": assignment_cycle_id, "cycle_status": "active",
         "assigned_to_user_id": recipient_user_id, "first_valid_management_at": {"$exists": False},
         field: "claimed"},
        {"$set": {field: "sending", f"sla_alert_claims.{level}.confirmed_at": now}},
        return_document=ReturnDocument.AFTER,
    )


def eligible_for_first_sla_reassignment(*, cycle, lead, delivery_valid, red_overdue,
                                        executive_active, suppressed=False, quarantined=False) -> bool:
    return bool(
        cycle and cycle.get("schema_version") == "crm_assignment_cycle_v1"
        and cycle.get("cycle_status") == "active" and not cycle.get("first_valid_management_at")
        and not lead.get("follow_up_required") and lead.get("management_status") != "managed_waiting_response"
        and delivery_valid and red_overdue and executive_active and not suppressed and not quarantined
    )


def follow_up_shadow_status(lead, *, as_of=None) -> dict:
    now = coerce_utc_datetime(as_of) or utc_now()
    due = coerce_utc_datetime(lead.get("next_follow_up_at"))
    required = bool(lead.get("follow_up_required"))
    return {"required": required, "overdue": bool(required and due and due <= now),
            "alerts_enabled": False, "reassignment_enabled": False}
