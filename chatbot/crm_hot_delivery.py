"""Canonical new-Hot assignment and delivery path.

The functions are dependency-injected so end-to-end tests never contact the
real provider. Production remains fail-closed through its feature flag.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

from .crm_metrics import coerce_utc_datetime, create_assignment_cycle, utc_now
from .crm_notifications import (
    COLLECTION, DEDUP_ACTIVE_STATES, claim_next, create_pending, finalize_attempt,
    individual_identity,
)

NOTIFICATION_TYPE = "lead_assignment_hot"


def _existing_hot_notification(db, *, lead_id, cycle_id, recipient_user_id):
    """Check for an existing non-terminal HOT notification for this identity."""
    identity = individual_identity(
        lead_id=lead_id, assignment_cycle_id=cycle_id,
        notification_type=NOTIFICATION_TYPE, recipient_user_id=recipient_user_id,
    )
    return db[COLLECTION].find_one({
        "individual_identity": identity,
        "state": {"$in": list(DEDUP_ACTIVE_STATES)},
    })


def assign_and_enqueue_hot(db, *, lead, recipient_user_id, recipient_phone, payload,
                           assigned_by="system", reason="LeadRouter", assigned_at=None,
                           send_after=None, recipient_name=None, hot_context=None):
    if lead.get("_id") is None:
        raise ValueError("canonical lead_id is required")
    assigned = coerce_utc_datetime(assigned_at) or utc_now()
    due = coerce_utc_datetime(send_after) or assigned
    cycle = create_assignment_cycle(
        db, lead=lead, assigned_to_user_id=recipient_user_id,
        assigned_by=assigned_by, reason=reason, assigned_at=assigned,
        assigned_to_display_name=recipient_name or str(recipient_user_id),
    )
    # Check for existing non-terminal notification before updating the lead.
    # If a notification already exists for this identity, return it instead
    # of creating a duplicate.  A new assignment cycle (different cycle_id)
    # produces a different identity and correctly allows a new notification.
    existing = _existing_hot_notification(
        db, lead_id=lead["_id"], cycle_id=cycle["assignment_cycle_id"],
        recipient_user_id=recipient_user_id,
    )
    if existing:
        logger.info(
            "[HOT_DELIVERY] Notificacion ya existe para lead=%s cycle=%s recipient=%s. "
            "Nueva razon=%s suprimida. Se reusa notificacion existente.",
            lead["_id"], cycle["assignment_cycle_id"], recipient_user_id, reason,
        )
        return {"cycle": cycle, "notification": existing, "dedup_suppressed": True}

    # Only update lifecycle.assigned_at when this is a genuinely new assignment
    # (the cycle was just created).  Do NOT overwrite it on re-alerting.
    if cycle.get("schema_version") == "crm_assignment_cycle_v1" and cycle.get("cycle_status") == "active":
        current_lead = db["leads"].find_one(
            {"_id": lead["_id"]},
            {"lifecycle.current_assignment_cycle_id": 1},
        )
        current_cycle_id = (current_lead.get("lifecycle") or {}).get("current_assignment_cycle_id") if current_lead else None
        if str(current_cycle_id or "") != str(cycle["assignment_cycle_id"]):
            db["leads"].update_one({"_id": lead["_id"]}, {"$set": {
                "ejecutivo_asignado": recipient_name or str(recipient_user_id),
                "prospecto.ejecutivo": recipient_name or str(recipient_user_id),
                "lifecycle.assigned_at": assigned,
                "lifecycle.current_assignment_cycle_id": cycle["assignment_cycle_id"],
            }})
    else:
        db["leads"].update_one({"_id": lead["_id"]}, {"$set": {
            "ejecutivo_asignado": recipient_name or str(recipient_user_id),
            "prospecto.ejecutivo": recipient_name or str(recipient_user_id),
            "lifecycle.assigned_at": assigned,
            "lifecycle.current_assignment_cycle_id": cycle["assignment_cycle_id"],
        }})

    # Remove from any open non-HOT digest before enqueuing HOT notification
    try:
        from .crm_non_hot_digest import exclude_from_open_digest
        exclude_from_open_digest(
            db, lead_id=lead["_id"], assignment_cycle_id=cycle["assignment_cycle_id"],
        )
    except Exception:
        pass
    # Apply hot_context to payload for template selection at send time.
    effective_context = hot_context or "initial_hot"
    payload_with_context = dict(payload)
    payload_with_context["hot_context"] = effective_context

    # After-hours handling: defer HOT notifications to next business slot
    # unless the mode is ON_CALL_IMMEDIATE.
    final_send_after = due
    from .lead_router import is_business_hours, after_hours_hot_mode, get_next_business_slot
    if not is_business_hours():
        mode = after_hours_hot_mode()
        if mode == "NEXT_BUSINESS_OPEN":
            final_send_after = get_next_business_slot(due)
            logger.info(
                "[HOT_DELIVERY] Fuera de horario. Hot diferido a %s (modo=%s)",
                final_send_after, mode,
            )

    identity = individual_identity(
        lead_id=lead["_id"], assignment_cycle_id=cycle["assignment_cycle_id"],
        notification_type=NOTIFICATION_TYPE, recipient_user_id=recipient_user_id,
    )
    notification = create_pending(
        db, identity_field="individual_identity", identity=identity,
        payload=payload_with_context, send_after=final_send_after,
        canonical_fields={
            "lead_id": lead["_id"], "assignment_cycle_id": cycle["assignment_cycle_id"],
            "notification_type": NOTIFICATION_TYPE, "recipient_user_id": recipient_user_id,
            "recipient_phone": recipient_phone,
            "dedupe_active": True,
            "hot_context": effective_context,
        },
        metadata={"assignment_cycle_id": cycle["assignment_cycle_id"], "lead_id": str(lead["_id"])},
    )
    return {"cycle": cycle, "notification": notification}


async def process_one_hot(db, *, sender, worker_id, now=None, enabled=False):
    """Claim and deliver one due canonical Hot; sender must return a receipt dict."""
    # Kill-switch check before claim
    if not enabled:
        return {"status": "disabled"}
    from config import Config
    if not getattr(Config, "LEAD_HOT_NOTIFICATIONS_ENABLED", False):
        # Suppress any pending document without sending
        current = coerce_utc_datetime(now) or utc_now()
        notification = await asyncio.to_thread(
            claim_next, db, worker_id=worker_id, now=current,
            extra_filter={"notification_type": NOTIFICATION_TYPE, "send_after": {"$lte": current}},
        )
        if notification:
            await asyncio.to_thread(
                finalize_attempt, db, notification_id=notification["_id"], worker_id=worker_id,
                state="suppressed", error="kill_switch_disabled", now=current,
            )
            return {"status": "suppressed", "reason": "kill_switch", "delivery_id": notification["delivery_id"]}
        return {"status": "disabled"}
    current = coerce_utc_datetime(now) or utc_now()
    # Re-check before claim for race safety
    if not getattr(Config, "LEAD_HOT_NOTIFICATIONS_ENABLED", False):
        return {"status": "disabled"}
    notification = await asyncio.to_thread(
        claim_next, db, worker_id=worker_id, now=current,
        extra_filter={"notification_type": NOTIFICATION_TYPE, "send_after": {"$lte": current}},
    )
    if not notification:
        return {"status": "idle"}

    # Final check before provider call — flag may have changed during claim
    if not getattr(Config, "LEAD_HOT_NOTIFICATIONS_ENABLED", False):
        await asyncio.to_thread(
            finalize_attempt, db, notification_id=notification["_id"], worker_id=worker_id,
            state="suppressed", error="kill_switch_disabled", now=current,
        )
        return {"status": "suppressed", "reason": "kill_switch", "delivery_id": notification["delivery_id"]}

    try:
        receipt = await sender(notification["recipient_phone"], notification["payload"])
    except Exception as exc:
        await asyncio.to_thread(
            finalize_attempt, db, notification_id=notification["_id"], worker_id=worker_id,
            state="failed_retryable", error=type(exc).__name__, now=current,
        )
        return {"status": "failed_retryable", "delivery_id": notification["delivery_id"]}
    success = bool(receipt.get("success"))
    provider_message_id = receipt.get("provider_message_id")
    state = "sent" if success and provider_message_id else "quarantined" if success else "failed_retryable"
    result = await asyncio.to_thread(
        finalize_attempt, db, notification_id=notification["_id"], worker_id=worker_id, state=state,
        provider_message_id=provider_message_id,
        error=("missing_provider_message_id" if success and not provider_message_id else
               None if success else receipt.get("error") or receipt.get("delivery_status")), now=current,
    )
    return {"status": state, "delivery_id": result["delivery_id"],
            "provider_message_id": result.get("provider_message_id")}


def canonical_delivery_evidence(db, assignment_cycle_id) -> bool:
    return bool(db[COLLECTION].find_one({
        "assignment_cycle_id": assignment_cycle_id, "notification_type": NOTIFICATION_TYPE,
        "state": "sent", "provider_message_id": {"$exists": True, "$ne": None},
    }))
