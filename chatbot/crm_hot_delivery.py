"""Canonical new-Hot assignment and delivery path.

The functions are dependency-injected so end-to-end tests never contact the
real provider. Production remains fail-closed through its feature flag.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
import logging

logger = logging.getLogger(__name__)

from .crm_metrics import (
    active_assignment_cycle,
    coerce_utc_datetime,
    create_assignment_cycle,
    sync_active_cycle_temperature,
    utc_now,
)
from .crm_notifications import (
    COLLECTION, DEDUP_ACTIVE_STATES, claim_next, create_pending, finalize_attempt,
    individual_identity, verified_commercial_source,
)

NOTIFICATION_TYPE = "lead_assignment_hot"
ALLOWED_COMMERCIAL_REASONS = (
    "lead_created", "inbound_message", "manual_lead_created",
)
ALLOWED_COMMERCIAL_ORIGINS = ("inbound_message", "manual_lead")


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
                           assigned_by="system", reason="inbound_message", assigned_at=None,
                           send_after=None, recipient_name=None, hot_context=None,
                           source_event_id=None):
    if lead.get("_id") is None:
        raise ValueError("canonical lead_id is required")
    assigned = coerce_utc_datetime(assigned_at) or utc_now()
    due = coerce_utc_datetime(send_after) or assigned
    previous_cycle = active_assignment_cycle(db, lead["_id"])
    cycle = create_assignment_cycle(
        db, lead=lead, assigned_to_user_id=recipient_user_id,
        assigned_by=assigned_by, reason=reason, assigned_at=assigned,
        assigned_to_display_name=recipient_name or str(recipient_user_id),
    )
    # A lead can become HOT after its normal assignment.  Reuse that same
    # assignment cycle and update its temperature; never create a second
    # commercial opportunity just because the temperature changed.
    hot_since = (lead.get("lifecycle") or {}).get("hot_since")
    synced_cycle = sync_active_cycle_temperature(
        db, lead["_id"], temperature="HOT", transition_at=hot_since,
    )
    if synced_cycle:
        cycle = synced_cycle
    source_id = str(
        source_event_id or lead.get("source_inbound_provider_id")
        or lead.get("source_event_id") or ""
    ).strip()
    if source_id:
        db["crm_assignment_cycles"].update_one(
            {
                "assignment_cycle_id": cycle["assignment_cycle_id"],
                "source_event_id": {"$exists": False},
            },
            {"$set": {
                "source_event_id": source_id,
                "source_inbound_provider_id": source_id,
            }},
        )
        cycle = db["crm_assignment_cycles"].find_one({
            "assignment_cycle_id": cycle["assignment_cycle_id"]
        }) or cycle
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
    # Apply hot_context to payload for template selection at send time.  When
    # the caller does not provide one, infer a temperature escalation from the
    # existing assignment cycle.  This is the key distinction between a new
    # HOT lead and the same lead becoming HOT after its normal alert: the
    # executive must receive an update, not a second-looking assignment.
    if hot_context:
        effective_context = hot_context
    else:
        from .lead_router import (
            HOT_CONTEXT_ESCALATED, HOT_CONTEXT_INITIAL, HOT_CONTEXT_REASSIGNMENT,
        )
        previous_recipient = str((previous_cycle or {}).get("assigned_to_user_id") or "")
        current_recipient = str(recipient_user_id or "")
        previous_temperature = str(
            (previous_cycle or {}).get("temperature_at_assignment") or ""
        ).upper()
        if previous_cycle and previous_recipient != current_recipient:
            effective_context = HOT_CONTEXT_REASSIGNMENT
        elif previous_cycle and previous_temperature != "HOT":
            effective_context = HOT_CONTEXT_ESCALATED
        else:
            effective_context = HOT_CONTEXT_INITIAL
    payload_with_context = dict(payload)
    payload_with_context["hot_context"] = effective_context

    # After-hours handling: defer HOT notifications to next business slot
    # unless the mode is ON_CALL_IMMEDIATE.
    final_send_after = due
    from .lead_router import is_business_hours, after_hours_hot_mode, get_next_business_slot
    if not is_business_hours(due):
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
            "notification_eligible": cycle.get("notification_eligible") is True,
            "cycle_reason": cycle.get("reason"),
            "cycle_origin": cycle.get("cycle_origin") or cycle.get("reason"),
        },
        metadata={"assignment_cycle_id": cycle["assignment_cycle_id"], "lead_id": str(lead["_id"])},
    )
    return {"cycle": cycle, "notification": notification}


def process_one_hot_sync(db, *, worker_id, now=None, sender=None):
    """Fully synchronous HOT notification delivery. Runs in threadpool, never in event loop.
    
    Args:
        sender: Optional override for testability. Called as sender(phone, message); must return dict
                with success, provider_message_id, http_status keys.
    """
    import os, threading
    current = coerce_utc_datetime(now) or utc_now()
    
    # Runtime diagnostic
    logger.info("[HOT_RUNTIME] pid=%s thread=%s is_main=%s",
                os.getpid(), threading.current_thread().name,
                threading.current_thread() is threading.main_thread())

    from config import Config as _C
    if not getattr(_C, "LEAD_HOT_NOTIFICATIONS_ENABLED", False):
        return {"status": "disabled"}

    notification = claim_next(db, worker_id=worker_id, now=current,
                              extra_filter={"notification_type": NOTIFICATION_TYPE,
                                            "message_domain": "commercial_notification",
                                            "send_after": {"$lte": current},
                                            "notification_eligible": True,
                                            "cycle_reason": {"$in": list(ALLOWED_COMMERCIAL_REASONS)},
                                            "cycle_origin": {"$in": list(ALLOWED_COMMERCIAL_ORIGINS)},
                                            "provider_message_id": {"$in": [None]},
                                            "actually_delivered": {"$ne": True}})
    if not notification:
        return {"status": "idle"}
    if notification.get("message_domain") != "commercial_notification":
        finalize_attempt(
            db, notification_id=notification["_id"], worker_id=worker_id,
            state="suppressed", error="wrong_message_domain", now=current,
        )
        return {"status": "suppressed", "reason": "wrong_message_domain"}

    cycle = db["crm_assignment_cycles"].find_one({
        "assignment_cycle_id": notification.get("assignment_cycle_id"),
        "notification_eligible": True,
        "reason": {"$in": list(ALLOWED_COMMERCIAL_REASONS)},
        "cycle_origin": {"$in": list(ALLOWED_COMMERCIAL_ORIGINS)},
        "cycle_status": "active",
    })
    lead = db["leads"].find_one({
        "_id": notification.get("lead_id"),
        "stage": {"$nin": ["ARCHIVED", "CLOSED_WON", "CLOSED_LOST", "REJECTED"]},
    })
    if not cycle or not lead or not verified_commercial_source(db, cycle):
        finalize_attempt(
            db, notification_id=notification["_id"], worker_id=worker_id,
            state="suppressed", error="ineligible_cycle_or_lead", now=current,
        )
        return {"status": "suppressed", "reason": "ineligible_cycle_or_lead"}
    # Resolve executive and build secure context
    from .crm_delivery import resolve_executive_user, get_executive_phone
    from .whatsapp_client import send_whatsapp_message_detailed
    from .crm_message_context import build_lead_notification_context
    from .lead_router import build_hot_lead_message
    import asyncio, json

    recipient = str(notification.get("recipient_user_id") or "")
    exec_user = resolve_executive_user(db, recipient)
    if not exec_user:
        finalize_attempt(db, notification_id=notification["_id"], worker_id=worker_id,
                         state="failed_recipient", error="executive_not_found", now=current)
        return {"status": "failed_recipient", "reason": "executive_not_found"}

    phone = get_executive_phone(exec_user)
    if not phone:
        finalize_attempt(db, notification_id=notification["_id"], worker_id=worker_id,
                         state="failed_recipient", error="executive_phone_missing", now=current)
        return {"status": "failed_recipient", "reason": "no_phone"}

    # Build message from canonical context
    metadata = notification.get("metadata") or {}
    lead_id = metadata.get("lead_id")
    if lead_id:
        ctx = build_lead_notification_context(db, lead_id)
    else:
        ctx = {}
    # The notification is the source of truth for why this HOT alert exists.
    # Preserve it through the canonical context builder so the final WhatsApp
    # template can say "this assigned lead became HOT" when applicable.
    ctx["hot_context"] = notification.get("hot_context") or (
        notification.get("payload") or {}
    ).get("hot_context") or "initial_hot"
    message = build_hot_lead_message(ctx)

    logger.info("[HOT_SEND] notif=%s user=%s phone_end=%s",
                str(notification["_id"])[:12], str(exec_user.get("_id"))[:12], phone[-4:])

    try:
        if sender is not None:
            receipt = sender(phone, message)
        else:
            receipt = asyncio.run(send_whatsapp_message_detailed(phone, message))
    except Exception as exc:
        finalize_attempt(db, notification_id=notification["_id"], worker_id=worker_id,
                         state="failed_retryable", error=type(exc).__name__, now=current)
        return {"status": "failed_retryable", "error": type(exc).__name__}

    success = bool(receipt.get("success"))
    provider_id = receipt.get("provider_message_id")
    http_status = receipt.get("http_status")

    # Determine state based on response
    if not success:
        http_status = receipt.get("http_status")
        if http_status == 422:
            state = "failed_validation"
            error_detail = f"http_422 body={str(receipt.get('response_body', receipt.get('error', '')))[:200]}"
            logger.warning("[HOT_422] notif=%s %s", str(notification["_id"])[:12], error_detail)
        elif http_status == 429:
            state = "failed_retryable"
            error_detail = f"http_429 retry_after={receipt.get('retry_after', '?')}"
        else:
            state = "failed_retryable"
            error_detail = receipt.get("error") or receipt.get("delivery_status") or f"http_{http_status}"
    elif provider_id:
        state = "sent"
        error_detail = None
    else:
        state = "quarantined"
        error_detail = "missing_provider_message_id"

    finalize_attempt(db, notification_id=notification["_id"], worker_id=worker_id,
                     state=state, provider_message_id=provider_id, error=error_detail, now=current)

    # After a confirmed non-delivery (no accepted provider ID), clear the
    # pre-call reservation markers so a retry can claim a fresh delivery slot.
    if state == "failed_retryable":
        retry_after = 60
        if http_status == 429:
            try:
                retry_after = max(int(receipt.get("retry_after", 60)), 30)
            except (TypeError, ValueError):
                retry_after = 60
        db[COLLECTION].update_one(
            {"_id": notification["_id"], "state": "failed_retryable"},
            {"$unset": {"provider_call_started_at": "", "delivery_token": ""},
             "$set": {"next_attempt_at": current + timedelta(seconds=retry_after),
                      "updated_at": current}},
        )

    if state == "sent":
        db[COLLECTION].update_one({"_id": notification["_id"]},
                                  {"$set": {"delivery_mode": "live", "actually_delivered": True}})

    return {"status": state, "provider_message_id": provider_id, "delivery_id": notification.get("delivery_id")}


async def process_one_hot(db, *, sender, worker_id, now=None, enabled=False):
    """Async wrapper that delegates to sync version via run_in_executor."""
    if not enabled:
        return {"status": "disabled"}
    import asyncio, functools
    loop = asyncio.get_running_loop()
    fn = functools.partial(process_one_hot_sync, db, worker_id=worker_id, now=now, sender=sender)
    result = await loop.run_in_executor(None, fn)
    return result


def canonical_delivery_evidence(db, assignment_cycle_id) -> bool:
    return bool(db[COLLECTION].find_one({
        "assignment_cycle_id": assignment_cycle_id, "notification_type": NOTIFICATION_TYPE,
        "state": "sent", "provider_message_id": {"$exists": True, "$ne": None},
    }))
