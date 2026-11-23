"""Event-driven central support alerts to the verified PROCASA administrator.

The task is the durable source of truth. Only the verified office WaSender
session may alert Pablo; this module NEVER contacts customers or uses an
executive's session. No periodic provider polling and no uncertain retries.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from hashlib import sha256

from .executive_session_admin_alerts import _pablo_recipient, PABLO_ID

logger = logging.getLogger(__name__)
COLLECTION = "crm_central_support_alerts"
ENABLED_FLAG = "PROCASA_CENTRAL_SUPPORT_WHATSAPP_ENABLED"


def _eligible(db, task: dict) -> bool:
    """Reject stale ownership, closed searches and synthetic contact records."""
    if str(task.get("recipient_user_id") or "") != str(PABLO_ID):
        return False
    lead = db["leads"].find_one({
        "_id": task["lead_id"],
        "lifecycle.current_assignment_cycle_id": task["assignment_cycle_id"],
        "lifecycle.assigned_to_user_id": task["related_executive_user_id"],
        "commercial_intelligence.active_search.status": "ACTIVE",
        "commercial_intelligence.active_search.followup_required": True,
    }, {
        "phone_is_synthetic": 1, "contact_identity_incomplete": 1,
        "contact_restrictions.marketing_opt_out": 1,
        "commercial_intelligence.active_search.opt_out": 1,
    })
    if not lead or lead.get("phone_is_synthetic") or lead.get("contact_identity_incomplete"):
        return False
    restriction = lead.get("contact_restrictions") or {}
    search = (lead.get("commercial_intelligence") or {}).get("active_search") or {}
    return not restriction.get("marketing_opt_out") and not search.get("opt_out")


def notify_central_support_task(db, task: dict, *, newly_created: bool,
                                send=None, now=None) -> str:
    """Record at most one admin alert per search revision. Send once if enabled.

    A provider timeout is *unknown*, not proof of failure: the alert is never
    retried blindly and never sent from an executive's phone.
    """
    if task.get("type") != "SEARCH_ALTERNATIVES":
        return "skipped_type"
    if not _eligible(db, task):
        return "not_eligible"
    now = now or datetime.now(timezone.utc)
    task_id = str(task["task_id"])
    alert_id = "support:" + sha256(task_id.encode("utf-8")).hexdigest()[:32]
    insert = db[COLLECTION].update_one(
        {"_id": alert_id},
        {"$setOnInsert": {
            "_id": alert_id,
            "task_id": task_id,
            "lead_id": task["lead_id"],
            "assignment_cycle_id": task["assignment_cycle_id"],
            # The executive is contextual: ONLY the verified admin is a recipient.
            "recipient_user_id": str(PABLO_ID),
            "related_executive_user_id": task["related_executive_user_id"],
            "executive_name": str(task.get("agent") or "")[:100],
            "action": "BUSCAR_ALTERNATIVAS",
            "property_codes_to_review": list(task.get("property_codes_to_review") or [])[:8],
            "delivery": "queued",
            "created_at": now,
            "source": "verified_executive_active_search",
        }},
        upsert=True,
    )
    # Existing tasks are stable and must never cause repeat notifications.
    if not newly_created or insert.upserted_id is None:
        return "existing"
    if os.getenv(ENABLED_FLAG, "0") != "1":
        return "queued"
    # Revalidate lead and cycle immediately before the irreversible provider call.
    if not _eligible(db, task):
        db[COLLECTION].update_one(
            {"_id": alert_id, "delivery": "queued"},
            {"$set": {"delivery": "stale", "finished_at": now}},
        )
        return "stale"
    recipient = _pablo_recipient(db)
    if recipient is None:
        db[COLLECTION].update_one(
            {"_id": alert_id, "delivery": "queued"},
            {"$set": {"delivery": "recipient_unverified", "finished_at": now}},
        )
        return "recipient_unverified"
    claimed = db[COLLECTION].update_one(
        {"_id": alert_id, "delivery": "queued"},
        {"$set": {"delivery": "sending", "attempted_at": now}},
    )
    if claimed.modified_count != 1:
        return "already_claimed"
    # The verified administrator receives only structured search information,
    # not the customer transcript or private/non-commercial conversations.
    note = str(task.get("note") or "")[:900]
    text = (
        "PROCASA - APOYO COMERCIAL CENTRAL\n"
        f"Ejecutivo: {task.get('agent') or 'Asignado'}\n"
        f"Lead: {task.get('phone') or 'Ver en CRM'}\n"
        f"{note}\n"
        "Revisar la propiedad, validar disponibilidad y coordinar con el ejecutivo. "
        "No se envió ningún mensaje al cliente."
    )
    if send is None:
        from .whatsapp_client import send_whatsapp_message_detailed_sync
        send = send_whatsapp_message_detailed_sync
    try:
        result = send(recipient, text)
        delivery = (
            "accepted" if result.get("success") is True and result.get("provider_message_id")
            else "delivery_unknown" if result.get("provider_call_uncertain")
            or result.get("success") is True
            else "failed"
        )
        provider_id = result.get("provider_message_id") if delivery == "accepted" else None
    except Exception as exc:
        delivery, provider_id = "delivery_unknown", None
        logger.warning("CENTRAL_SUPPORT delivery_unknown error_type=%s", type(exc).__name__)
    db[COLLECTION].update_one(
        {"_id": alert_id, "delivery": "sending"},
        {"$set": {"delivery": delivery, "provider_message_id": provider_id,
                  "finished_at": datetime.now(timezone.utc)}},
    )
    logger.info("CENTRAL_SUPPORT alert_delivery=%s", delivery)
    return delivery
