"""Canonical new-Hot assignment and delivery path.

The functions are dependency-injected so end-to-end tests never contact the
real provider. Production remains fail-closed through its feature flag.
"""
from __future__ import annotations

from .crm_metrics import coerce_utc_datetime, create_assignment_cycle, utc_now
from .crm_notifications import (
    COLLECTION, claim_next, create_pending, finalize_attempt, individual_identity,
)

NOTIFICATION_TYPE = "lead_assignment_hot"


def assign_and_enqueue_hot(db, *, lead, recipient_user_id, recipient_phone, payload,
                           assigned_by="system", reason="LeadRouter", assigned_at=None,
                           send_after=None):
    if lead.get("_id") is None:
        raise ValueError("canonical lead_id is required")
    assigned = coerce_utc_datetime(assigned_at) or utc_now()
    due = coerce_utc_datetime(send_after) or assigned
    cycle = create_assignment_cycle(
        db, lead=lead, assigned_to_user_id=recipient_user_id,
        assigned_by=assigned_by, reason=reason, assigned_at=assigned,
    )
    db["leads"].update_one({"_id": lead["_id"]}, {"$set": {
        "ejecutivo_asignado": recipient_user_id,
        "prospecto.ejecutivo": recipient_user_id,
        "lifecycle.assigned_at": assigned,
        "lifecycle.current_assignment_cycle_id": cycle["assignment_cycle_id"],
    }})
    identity = individual_identity(
        lead_id=lead["_id"], assignment_cycle_id=cycle["assignment_cycle_id"],
        notification_type=NOTIFICATION_TYPE, recipient_user_id=recipient_user_id,
    )
    notification = create_pending(
        db, identity_field="individual_identity", identity=identity,
        payload=payload, send_after=due,
        canonical_fields={
            "lead_id": lead["_id"], "assignment_cycle_id": cycle["assignment_cycle_id"],
            "notification_type": NOTIFICATION_TYPE, "recipient_user_id": recipient_user_id,
            "recipient_phone": recipient_phone,
        },
        metadata={"assignment_cycle_id": cycle["assignment_cycle_id"], "lead_id": str(lead["_id"])},
    )
    return {"cycle": cycle, "notification": notification}


async def process_one_hot(db, *, sender, worker_id, now=None, enabled=False):
    """Claim and deliver one due canonical Hot; sender must return a receipt dict."""
    if not enabled:
        return {"status": "disabled"}
    current = coerce_utc_datetime(now) or utc_now()
    notification = claim_next(
        db, worker_id=worker_id, now=current,
        extra_filter={"notification_type": NOTIFICATION_TYPE, "send_after": {"$lte": current}},
    )
    if not notification:
        return {"status": "idle"}
    try:
        receipt = await sender(notification["recipient_phone"], notification["payload"])
    except Exception as exc:
        finalize_attempt(db, notification_id=notification["_id"], worker_id=worker_id,
                         state="failed_retryable", error=type(exc).__name__, now=current)
        return {"status": "failed_retryable", "delivery_id": notification["delivery_id"]}
    success = bool(receipt.get("success"))
    provider_message_id = receipt.get("provider_message_id")
    # Accepted without provider evidence is ambiguous: quarantine instead of
    # retrying and risking a duplicate delivery.
    state = "sent" if success and provider_message_id else "quarantined" if success else "failed_retryable"
    result = finalize_attempt(
        db, notification_id=notification["_id"], worker_id=worker_id, state=state,
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
