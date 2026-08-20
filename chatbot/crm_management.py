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
    "VISIT_SCHEDULED": {"attempt": True, "effective": True, "follow_up": False, "status": "managed_contacted"},
    "FOLLOW_UP_REQUESTED": {"attempt": True, "effective": True, "follow_up": True, "status": "managed_follow_up"},
    "NOT_INTERESTED": {"attempt": True, "effective": True, "follow_up": False, "status": "managed_closed"},
    "CLOSED_WON": {"attempt": True, "effective": True, "follow_up": False, "status": "managed_closed"},
    "CLOSED_LOST": {"attempt": True, "effective": True, "follow_up": False, "status": "managed_closed"},
    "INVALID_NUMBER": {"attempt": True, "effective": False, "follow_up": False, "status": "managed_closed"},
    "DISCARDED_VALID_REASON": {"attempt": False, "effective": False, "follow_up": False, "status": "managed_closed"},
    "SCHEDULE_FOLLOW_UP": {"attempt": False, "effective": False, "follow_up": True, "status": "managed_follow_up"},
}


class StaleAssignmentCycleError(ValueError):
    """The client attempted to manage a cycle that is no longer active."""

    code = "stale_assignment_cycle"


_LEGACY_RESULT_MAP = {
    "NO_RESPONDIO": "CALL_NO_ANSWER",
    "OCUPADO": "CALL_NO_ANSWER",
    "NUMERO_INVALIDO": "INVALID_NUMBER",
    "MENSAJE_ENVIADO": "MESSAGE_SENT_WAITING_RESPONSE",
    "CONTACTADO": "EFFECTIVE_CONTACT",
    "SOLICITA_SEGUIMIENTO": "FOLLOW_UP_REQUESTED",
    "NO_INTERESADO": "NOT_INTERESTED",
}


def canonical_result_type(value):
    """Translate supported legacy UI values to the existing domain taxonomy."""
    from .crm_metrics import normalize_result

    normalized = normalize_result(value)
    if normalized in RESULT_RULES:
        return normalized
    return _LEGACY_RESULT_MAP.get(normalized)


def _default_follow_up(occurred_at):
    from .lead_router import get_next_business_slot
    local = occurred_at.astimezone(__import__("chatbot.constants", fromlist=["CHILE_TZ"]).CHILE_TZ)
    return coerce_utc_datetime(get_next_business_slot(local + timedelta(days=1)))


def record_management_result(db, *, lead_id, assignment_cycle_id, actor_user_id,
                             result_type, source, idempotency_key, occurred_at=None,
                             next_follow_up_at=None, details_json=None,
                             stage_override=None, legacy_stage=None,
                             actor_can_manage_any_cycle=False) -> dict:
    result_type = canonical_result_type(result_type)
    rule = RESULT_RULES.get(result_type)
    if not rule:
        raise ValueError("unsupported CRM management result")
    if not stage_override and rule["status"] == "managed_closed":
        stage_override = "CLOSED_WON" if result_type == "CLOSED_WON" else "CLOSED_LOST"
        legacy_stage = "cerrado"
    if not all(str(value or "").strip() for value in (lead_id, assignment_cycle_id, actor_user_id, source, idempotency_key)):
        raise ValueError("canonical management identity is incomplete")
    occurred = coerce_utc_datetime(occurred_at) or utc_now()
    # Try active cycle first, then fallback to any cycle for idempotent retries.
    cycle = db["crm_assignment_cycles"].find_one({
        "lead_id": lead_id, "assignment_cycle_id": assignment_cycle_id,
        "cycle_status": "active", "unassigned_at": None,
    })
    if not cycle:
        existing = db["crm_management_results"].find_one({"_id": f"crm_management:{idempotency_key}"})
        if existing:
            return existing
        active_cycle = db["crm_assignment_cycles"].find_one({
            "lead_id": lead_id, "cycle_status": "active", "unassigned_at": None,
        })
        if active_cycle and str(active_cycle.get("assignment_cycle_id")) != str(assignment_cycle_id):
            raise StaleAssignmentCycleError(StaleAssignmentCycleError.code)
        raise ValueError("active assignment cycle not found")
    if (not actor_can_manage_any_cycle and
            str(cycle.get("assigned_to_user_id")) != str(actor_user_id)):
        raise PermissionError("management actor does not own the active cycle")

    record = {
        "_id": f"crm_management:{idempotency_key}", "idempotency_key": idempotency_key,
        "management_request_id": idempotency_key,
        "schema_version": "crm_management_result_v1", "lead_id": lead_id,
        "assignment_cycle_id": assignment_cycle_id, "actor_user_id": actor_user_id,
        "result_type": result_type, "occurred_at": occurred, "source": source,
        "details_json": details_json if isinstance(details_json, dict) else {},
        "pipeline_stage_at_result": str(stage_override or "CONTACTED"),
        "legacy_stage_at_result": str(legacy_stage or (stage_override or "CONTACTED")),
        "status": "processing",
    }
    try:
        db["crm_management_results"].insert_one(record)
    except DuplicateKeyError:
        existing = db["crm_management_results"].find_one({"_id": record["_id"]})
        if existing and existing.get("status") == "completed":
            return existing
        if existing and any(existing.get(field) != record.get(field) for field in
                            ("lead_id", "assignment_cycle_id", "actor_user_id", "result_type")):
            raise ValueError("management_request_id was reused for a different management")

    follow_at = coerce_utc_datetime(next_follow_up_at)
    if rule["follow_up"] and not follow_at:
        follow_at = _default_follow_up(occurred)
    follow_cycle_id = f"followup:{assignment_cycle_id}:{idempotency_key}" if rule["follow_up"] else None
    lead_updates = {
        "management_status": rule["status"], "contact_attempted": rule["attempt"],
        "effective_contact": rule["effective"], "follow_up_required": rule["follow_up"],
        "follow_up_status": "pending" if rule["follow_up"] else "not_required",
        "last_crm_update": occurred,
    }
    if stage_override:
        lead_updates["pipeline_stage"] = str(stage_override)
        lead_updates["stage"] = str(legacy_stage or stage_override)
    else:
        lead_updates.update({"pipeline_stage": "CONTACTED", "stage": "gestion"})
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
    if rule["follow_up"] and follow_at:
        lead_doc = db["leads"].find_one({"_id": lead_id}) or {}
        task = {
            "_id": f"crm_task:{follow_cycle_id}",
            "task_id": follow_cycle_id,
            "phone": lead_doc.get("phone"),
            "lead_id": lead_id,
            "assignment_cycle_id": assignment_cycle_id,
            "idempotency_key": idempotency_key,
            "lead_type": "crm",
            "message_domain": "crm_management_follow_up",
            "type": "REMINDER_WHATSAPP",
            "status": "pending",
            "execute_at": follow_at,
            "created_at": occurred,
            "note": details_json.get("notes") if isinstance(details_json, dict) else None,
        }
        try:
            db["crm_tasks"].insert_one(task)
        except DuplicateKeyError:
            pass
    # If the result closes the lead (managed_closed), also close the cycle
    if rule["status"] == "managed_closed":
        cycle_updates["cycle_status"] = "closed"
        cycle_updates["closed_at"] = occurred
        cycle_updates["closed_reason"] = "management_result_closed"
        cycle_updates["unassigned_at"] = occurred
    db["crm_assignment_cycles"].update_one(
        {"assignment_cycle_id": assignment_cycle_id, "cycle_status": "active"}, {"$set": cycle_updates}
    )
    db["crm_notifications_v1"].update_many(
        {"assignment_cycle_id": assignment_cycle_id, "notification_type": {"$in": ["sla_yellow", "sla_red"]},
         "state": {"$in": ["pending", "failed_retryable"]}},
        {"$set": {"state": "suppressed", "suppressed_reason": "management_completed", "updated_at": occurred}},
    )
    event_type = {"MESSAGE_SENT_WAITING_RESPONSE": "SEND_WA_LEAD", "EMAIL_SENT": "SEND_EMAIL_LEAD",
                  "CALL_NO_ANSWER": "CALL_COMPLETED_LEAD"}.get(result_type, "CONTACT_RESULT")
    event = {"_id": f"crm_event:{idempotency_key}", "lead_id": lead_id,
             "assignment_cycle_id": assignment_cycle_id, "actor": actor_user_id,
             "actor_type": "human", "type": event_type, "result": result_type,
             "confirmed": True, "timestamp": occurred, "source": source,
             "idempotency_key": idempotency_key, "management_request_id": idempotency_key}
    try:
        db["crm_events"].insert_one(event)
    except DuplicateKeyError:
        pass
    completed_record = {key: value for key, value in record.items() if key != "_id"}
    completed_record.update({"status": "completed", "follow_up_required": rule["follow_up"],
                             "next_follow_up_at": follow_at})
    db["crm_management_results"].update_one(
        {"_id": record["_id"]}, {"$set": completed_record}, upsert=True,
    )
    return db["crm_management_results"].find_one({"_id": record["_id"]}) or record


def _legacy_idempotency_key(*, data):
    supplied = data.get("management_request_id") or data.get("idempotency_key") or data.get("request_id")
    if not str(supplied or "").strip():
        raise ValueError("management_request_id is required; retry the same action with the same key")
    return str(supplied).strip()


def record_legacy_management_result(db, *, lead, actor_user_id, actor_can_manage_any_cycle,
                                    assignment_cycle_id, data) -> dict:
    """Adapt the existing detail payload into the one canonical result service."""
    from .constants import PipelineStage

    raw_result = data.get("resultado_gestion")
    result_type = canonical_result_type(raw_result)

    next_date = data.get("next_action_date")
    if data.get("interaction_type") == "hable" and result_type != "NOT_INTERESTED" and not next_date:
        raise ValueError("next_action_date is required after effective contact")

    raw_normalized = str(raw_result or "").strip().lower()
    details = data.get("details_json") if isinstance(data.get("details_json"), dict) else {}
    stage_override = None
    legacy_stage = None
    if raw_normalized in {"intento_fallido", "no_respondio", "ocupado"}:
        result_type = "CALL_NO_ANSWER"
        stage_override, legacy_stage = PipelineStage.NEW.value, "new"
    elif raw_normalized == "visita_agendada":
        result_type = "VISIT_SCHEDULED"
        stage_override, legacy_stage = PipelineStage.VISIT_SCHEDULED.value, "visita"
    elif raw_normalized in {"requiere_seguimiento", "lead_pausado"}:
        stage_override, legacy_stage = PipelineStage.CONTACTED.value, "gestion"
    elif raw_normalized == "lead_cerrado":
        close_category = str(details.get("close_cat_radio") or "").strip().lower()
        close_reason = str(details.get("close_reason") or "").strip().lower()
        if close_category == "ganado":
            result_type = "CLOSED_WON"
        elif close_reason == "contacto_invalido":
            result_type = "INVALID_NUMBER"
        elif close_category in {"precio", "producto", "cliente", "competencia", "tecnico"}:
            result_type = "CLOSED_LOST"
        else:
            raise ValueError("close category is required for lead_cerrado")
        close_stage = PipelineStage.CLOSED_WON if result_type == "CLOSED_WON" else PipelineStage.CLOSED_LOST
        stage_override, legacy_stage = close_stage.value, "cerrado"
    elif result_type == "NOT_INTERESTED":
        stage_override, legacy_stage = PipelineStage.CLOSED_LOST.value, "cerrado"
    elif result_type:
        stage_override, legacy_stage = PipelineStage.CONTACTED.value, "gestion"
    if not result_type:
        raise ValueError(f"unsupported legacy management result: {raw_result}")

    result = record_management_result(
        db,
        lead_id=lead["_id"],
        assignment_cycle_id=assignment_cycle_id,
        actor_user_id=actor_user_id,
        result_type=result_type,
        source=str(data.get("source") or "crm_detail"),
        idempotency_key=_legacy_idempotency_key(data=data),
        next_follow_up_at=next_date,
        details_json={
            **details,
            "interaction_type": data.get("interaction_type"),
            "notes": data.get("notas"),
            "action_label": data.get("action_label"),
            "legacy_result": raw_result,
        },
        stage_override=stage_override,
        legacy_stage=legacy_stage,
        actor_can_manage_any_cycle=actor_can_manage_any_cycle,
    )
    result["new_state"] = result.get("pipeline_stage") or stage_override
    result["next_action_date"] = result.get("next_follow_up_at")
    return result


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
