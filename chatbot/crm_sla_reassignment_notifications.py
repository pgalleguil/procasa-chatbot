"""Durable WhatsApp notification path for committed SLA reassignments.

The transaction owns the lead/cycle change.  This module is deliberately
post-commit: it only creates a durable notification after an ``APPLIED`` or
idempotent replay, and provider delivery can never roll back the assignment.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from typing import Any, Callable, Mapping

from .crm_delivery import get_executive_phone, resolve_executive_user, validate_executive_recipient
from .crm_notifications import COLLECTION, claim_next, create_pending, finalize_attempt, individual_identity
from .crm_metrics import utc_now


logger = logging.getLogger(__name__)

NOTIFICATION_TYPE = "sla_reassignment"
MAX_AUTOMATIC_REASSIGNMENT_NUMBER = 2
COMMITTED_STATUSES = frozenset({"APPLIED", "ALREADY_APPLIED"})


def _value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def _message(*, lead_id: str, destination_cycle_id: str) -> str:
    return (
        "Nuevo lead asignado por vencimiento de SLA.\n"
        f"Lead: {lead_id}\n"
        f"Ciclo: {destination_cycle_id}\n"
        "Abre el lead en el CRM para revisar los datos de contacto."
    )


def enqueue_sla_reassignment_notification(
    db: Any,
    *,
    decision: Any,
    result: Any,
) -> dict[str, Any] | None:
    """Create or reuse the notification for the new owner only.

    The deterministic identity is scoped to the reassignment decision and
    recipient.  A third automatic reassignment is rejected defensively even if
    a malformed caller tries to enqueue one outside the frozen worker policy.
    """
    status = str(_value(result, "status", "") or "")
    if not bool(_value(result, "committed", False)) or status not in COMMITTED_STATUSES:
        return None

    decision_id = str(_value(result, "decision_id") or _value(decision, "decision_id") or "").strip()
    lead_id = str(_value(result, "lead_id") or _value(decision, "lead_id") or "").strip()
    recipient_user_id = str(_value(result, "selected_user_id") or _value(decision, "selected_user_id") or "").strip()
    destination_cycle_id = str(_value(result, "destination_cycle_id") or "").strip()
    reassignment_number = _value(result, "automatic_reassignment_number")
    try:
        reassignment_number = int(reassignment_number)
    except (TypeError, ValueError):
        reassignment_number = None

    if not decision_id or not lead_id or not recipient_user_id or not destination_cycle_id:
        raise ValueError("sla reassignment notification identity is incomplete")
    if reassignment_number not in range(1, MAX_AUTOMATIC_REASSIGNMENT_NUMBER + 1):
        logger.warning(
            "[SLA_REASSIGNMENT_NOTIFICATION] suppressed invalid reassignment number decision_id=%s number=%s",
            decision_id,
            reassignment_number,
        )
        return None

    identity = f"sla-reassignment:{decision_id}:{recipient_user_id}"
    payload = {
        "message": _message(lead_id=lead_id, destination_cycle_id=destination_cycle_id),
        "lead_id": lead_id,
        "assignment_cycle_id": destination_cycle_id,
        "decision_id": decision_id,
        "automatic_reassignment_number": reassignment_number,
    }
    return create_pending(
        db,
        identity_field="individual_identity",
        identity=identity,
        payload=payload,
        send_after=utc_now(),
        canonical_fields={
            "lead_id": lead_id,
            "assignment_cycle_id": destination_cycle_id,
            "notification_type": NOTIFICATION_TYPE,
            "recipient_user_id": recipient_user_id,
            "sla_reassignment_decision_id": decision_id,
            "automatic_reassignment_number": reassignment_number,
            "dedupe_active": True,
        },
        metadata={
            "lead_id": lead_id,
            "assignment_cycle_id": destination_cycle_id,
            "decision_id": decision_id,
        },
    )


def _delivery_state(receipt: Mapping[str, Any]) -> tuple[str, str | None]:
    if receipt.get("success") and receipt.get("provider_message_id"):
        return "sent", None
    if receipt.get("provider_call_uncertain"):
        # Do not automatically retry an accepted-but-unknown provider call.
        return "quarantined", "delivery_unknown"
    http_status = receipt.get("http_status")
    if http_status == 422:
        return "failed_validation", "provider_rejected_payload"
    if http_status == 429:
        return "failed_retryable", "provider_rate_limited"
    return "failed_retryable", str(
        receipt.get("error") or receipt.get("delivery_status") or "provider_rejected"
    )


def process_one_sla_reassignment_sync(
    db: Any,
    *,
    worker_id: str,
    now: Any = None,
    sender: Callable[[str, str], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Claim and deliver one queued SLA notification synchronously.

    This function is intended for a worker thread.  It never runs on an async
    event loop and never changes lead ownership.
    """
    current = now or utc_now()
    notification = claim_next(
        db,
        worker_id=worker_id,
        now=current,
        extra_filter={
            "notification_type": NOTIFICATION_TYPE,
            "message_domain": "commercial_notification",
            "provider_message_id": {"$in": [None]},
            "actually_delivered": {"$ne": True},
        },
    )
    if not notification:
        return {"status": "idle"}

    recipient_id = str(notification.get("recipient_user_id") or "")
    user = resolve_executive_user(db, recipient_id)
    if not user:
        finalize_attempt(
            db,
            notification_id=notification["_id"],
            worker_id=worker_id,
            state="failed_recipient",
            error="executive_not_found",
            now=current,
        )
        return {"status": "failed_recipient", "reason": "executive_not_found"}

    phone = validate_executive_recipient(get_executive_phone(user))
    if not phone:
        finalize_attempt(
            db,
            notification_id=notification["_id"],
            worker_id=worker_id,
            state="failed_recipient",
            error="executive_phone_missing_or_invalid",
            now=current,
        )
        return {"status": "failed_recipient", "reason": "executive_phone_missing_or_invalid"}

    payload = notification.get("payload") or {}
    message = str(payload.get("message") or "").strip()
    try:
        if sender is not None:
            receipt = dict(sender(phone, message) or {})
        else:
            from .whatsapp_client import send_whatsapp_message_detailed_sync
            receipt = send_whatsapp_message_detailed_sync(phone, message)
    except Exception as exc:
        finalize_attempt(
            db,
            notification_id=notification["_id"],
            worker_id=worker_id,
            state="failed_retryable",
            error=type(exc).__name__,
            now=current,
        )
        return {"status": "failed_retryable", "error": type(exc).__name__}

    state, error = _delivery_state(receipt)
    provider_id = receipt.get("provider_message_id")
    finalize_attempt(
        db,
        notification_id=notification["_id"],
        worker_id=worker_id,
        state=state,
        provider_message_id=provider_id,
        error=error,
        now=current,
    )
    if state == "failed_retryable":
        retry_after = 60
        if receipt.get("http_status") == 429:
            try:
                retry_after = max(int(receipt.get("retry_after", 60)), 30)
            except (TypeError, ValueError):
                retry_after = 60
        db[COLLECTION].update_one(
            {"_id": notification["_id"], "state": "failed_retryable"},
            {"$set": {"next_attempt_at": current + timedelta(seconds=retry_after), "updated_at": current}},
        )
    if state == "sent":
        db[COLLECTION].update_one(
            {"_id": notification["_id"]},
            {"$set": {"delivery_mode": "live", "actually_delivered": True}},
        )
    return {
        "status": state,
        "provider_message_id": provider_id,
        "delivery_id": notification.get("delivery_id"),
    }


async def process_one_sla_reassignment(
    db: Any,
    *,
    worker_id: str,
    now: Any = None,
    sender: Callable[[str, str], Mapping[str, Any]] | None = None,
    enabled: bool = False,
) -> dict[str, Any]:
    """Async-safe delivery adapter; all PyMongo/provider work runs in a thread."""
    if not enabled:
        return {"status": "disabled"}
    return await asyncio.to_thread(
        process_one_sla_reassignment_sync,
        db,
        worker_id=worker_id,
        now=now,
        sender=sender,
    )
