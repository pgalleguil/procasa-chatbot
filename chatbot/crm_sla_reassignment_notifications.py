"""Durable WhatsApp notification path for committed SLA reassignments.

The transaction owns the lead/cycle change.  This module is deliberately
post-commit: it only creates a durable notification after an ``APPLIED`` or
idempotent replay, and provider delivery can never roll back the assignment.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import uuid
from typing import Any, Callable, Mapping

from .crm_delivery import get_executive_phone, resolve_executive_user, validate_executive_recipient
from .crm_notifications import (
    COLLECTION,
    claim_next,
    content_hash,
    create_pending,
    finalize_attempt,
    individual_identity,
    record_delivery_attempt,
    refresh_lease,
    reserve_for_delivery,
)
from .crm_metrics import utc_now
from .mongo_identity import mongo_id_variants
from .crm_sla_cycle_links import build_sla_cycle_url


logger = logging.getLogger(__name__)

NOTIFICATION_TYPE = "sla_reassignment"
SLA_BREACH_WARNING = "SLA_BREACH_WARNING"
SLA_REASSIGNED_AWAY = "SLA_REASSIGNED_AWAY"
SLA_REASSIGNED_TO = "SLA_REASSIGNED_TO"
MAX_AUTOMATIC_REASSIGNMENT_NUMBER = 2
COMMITTED_STATUSES = frozenset({"APPLIED", "ALREADY_APPLIED"})


def _value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def _same_id(left: Any, right: Any) -> bool:
    return left not in (None, "") and right not in (None, "") and str(left) == str(right)


def _lead_for_notification(db: Any, lead_id: str) -> dict[str, Any]:
    lead = db["leads"].find_one({"_id": {"$in": list(mongo_id_variants(lead_id))}})
    return dict(lead or {})


def _field(lead: Mapping[str, Any], *paths: str, default: str = "") -> str:
    for path in paths:
        value: Any = lead
        for part in path.split("."):
            if not isinstance(value, Mapping) or part not in value:
                value = None
                break
            value = value[part]
        if value not in (None, ""):
            return str(value).strip()
    return default


def _notification_context(db: Any, lead_id: str, destination_cycle_id: str) -> dict[str, str]:
    lead = _lead_for_notification(db, lead_id)
    cycle = db["crm_assignment_cycles"].find_one({"assignment_cycle_id": destination_cycle_id}) or {}
    code = _field(
        cycle, "property_code", "codigo", "prospecto.codigo", "datos_propiedad.codigo",
        default="",
    ) or _field(
        lead, "property_code", "codigo", "prospecto.codigo", "datos_propiedad.codigo",
        default="S/N",
    )
    client = _field(cycle, "client_name", "lead_name", default="") or _field(
        lead, "prospecto.nombre", "client_name", default="Cliente"
    )
    operation = _field(
        cycle, "operation", "operacion", default=""
    ) or _field(lead, "operation", "operacion", "prospecto.operacion", "datos_propiedad.operacion", default="S/I")
    commune = _field(
        cycle, "commune", "comuna", default=""
    ) or _field(lead, "commune", "comuna", "prospecto.comuna", "datos_propiedad.comuna", default="S/I")
    priority = _field(cycle, "temperature_at_assignment", default="") or _field(
        lead, "lead_temperature_effective", default="NORMAL"
    )
    priority = priority.upper()
    if priority not in {"HOT", "NORMAL", "COLD"}:
        priority = "NORMAL"
    secure_url = ""
    destination_owner_id = str(cycle.get("assigned_to_user_id") or "").strip()
    if lead and destination_owner_id and destination_cycle_id:
        secure_url = build_sla_cycle_url(
            lead_id=lead.get("_id") or lead_id,
            recipient_user_id=destination_owner_id,
            assignment_cycle_id=destination_cycle_id,
        )
    return {
        "client": client,
        "code": code,
        "operation": operation,
        "commune": commune,
        "priority": "HOT" if priority == "HOT" else "NORMAL",
        "secure_url": secure_url,
    }


def _new_owner_message(context: Mapping[str, str]) -> str:
    return (
        "🔄 Nuevo lead reasignado\n\n"
        "Se te ha asignado un lead por vencimiento de SLA del ejecutivo anterior.\n\n"
        f"Cliente: {context['client']}\n"
        f"Propiedad: {context['code']}\n"
        f"Operación: {context['operation']}\n"
        f"Comuna: {context['commune']}\n"
        f"Prioridad: {context['priority']}\n\n"
        "⏱️ Tu SLA comienza desde esta nueva asignación.\n\n"
        f"👉 Gestionar lead:\n{context['secure_url']}"
    )


def _previous_owner_message(context: Mapping[str, str]) -> str:
    return (
        "🔄 Lead reasignado por vencimiento de SLA\n\n"
        f"El lead de la propiedad {context['code']} superó el tiempo de gestión establecido "
        "y fue reasignado automáticamente.\n\n"
        "Este lead ya no está disponible para tu gestión en el CRM."
    )


def _create_reassignment_notification(
    db: Any,
    *,
    lead_id: str,
    assignment_cycle_id: str,
    notification_type: str,
    recipient_user_id: str,
    decision_id: str,
    reassignment_number: int,
    message: str,
    role: str,
) -> dict[str, Any]:
    identity = individual_identity(
        lead_id=lead_id,
        assignment_cycle_id=assignment_cycle_id,
        notification_type=notification_type,
        recipient_user_id=recipient_user_id,
    )
    payload = {
        "message": message,
        "lead_id": lead_id,
        "assignment_cycle_id": assignment_cycle_id,
        "decision_id": decision_id,
        "automatic_reassignment_number": reassignment_number,
        "notification_role": role,
    }
    return create_pending(
        db,
        identity_field="individual_identity",
        identity=identity,
        payload=payload,
        send_after=utc_now(),
        canonical_fields={
            "lead_id": lead_id,
            "assignment_cycle_id": assignment_cycle_id,
            "notification_type": notification_type,
            "recipient_user_id": recipient_user_id,
            "sla_reassignment_decision_id": decision_id,
            "automatic_reassignment_number": reassignment_number,
            "notification_role": role,
            "dedupe_active": True,
        },
        metadata={
            "lead_id": lead_id,
            "assignment_cycle_id": assignment_cycle_id,
            "decision_id": decision_id,
            "notification_role": role,
        },
    )


def enqueue_sla_reassignment_notification(
    db: Any,
    *,
    decision: Any,
    result: Any,
) -> dict[str, Any] | None:
    """Create or reuse the post-commit notification for the new owner.

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

    context = _notification_context(db, lead_id, destination_cycle_id)
    return _create_reassignment_notification(
        db,
        lead_id=lead_id,
        assignment_cycle_id=destination_cycle_id,
        notification_type=SLA_REASSIGNED_TO,
        recipient_user_id=recipient_user_id,
        decision_id=decision_id,
        reassignment_number=reassignment_number,
        message=_new_owner_message(context),
        role="new_owner",
    )


def enqueue_sla_reassignment_away_notification(
    db: Any,
    *,
    decision: Any,
    result: Any,
) -> dict[str, Any] | None:
    """Create the independent post-commit notice for the previous owner."""
    status = str(_value(result, "status", "") or "")
    if not bool(_value(result, "committed", False)) or status not in COMMITTED_STATUSES:
        return None
    decision_id = str(_value(result, "decision_id") or _value(decision, "decision_id") or "").strip()
    lead_id = str(_value(result, "lead_id") or _value(decision, "lead_id") or "").strip()
    recipient_user_id = str(
        _value(result, "previous_owner_user_id")
        or _value(decision, "previous_owner_user_id")
        or ""
    ).strip()
    source_cycle_id = str(
        _value(result, "source_cycle_id")
        or _value(decision, "current_assignment_cycle_id")
        or ""
    ).strip()
    reassignment_number = _value(result, "automatic_reassignment_number")
    try:
        reassignment_number = int(reassignment_number)
    except (TypeError, ValueError):
        reassignment_number = None
    if not decision_id or not lead_id or not recipient_user_id or not source_cycle_id:
        raise ValueError("sla reassignment away notification identity is incomplete")
    if reassignment_number not in range(1, MAX_AUTOMATIC_REASSIGNMENT_NUMBER + 1):
        return None
    context = _notification_context(db, lead_id, source_cycle_id)
    return _create_reassignment_notification(
        db,
        lead_id=lead_id,
        assignment_cycle_id=source_cycle_id,
        notification_type=SLA_REASSIGNED_AWAY,
        recipient_user_id=recipient_user_id,
        decision_id=decision_id,
        reassignment_number=reassignment_number,
        message=_previous_owner_message(context),
        role="previous_owner",
    )


def enqueue_sla_reassignment_notifications(
    db: Any,
    *,
    decision: Any,
    result: Any,
) -> dict[str, Any] | None:
    """Create both notices after the transaction has committed.

    The existing singular function remains the new-owner compatibility entry
    point for callers/tests.  The production integration calls this function,
    which gives each recipient an independent idempotency identity.
    """
    if not bool(_value(result, "committed", False)) or str(_value(result, "status", "") or "") not in COMMITTED_STATUSES:
        return None
    previous = enqueue_sla_reassignment_away_notification(db, decision=decision, result=result)
    current = enqueue_sla_reassignment_notification(db, decision=decision, result=result)
    return {"previous_owner": previous, "new_owner": current}


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


def _validate_pending_notification(db: Any, notification: Mapping[str, Any]) -> tuple[bool, str]:
    """Revalidate the post-commit target immediately before delivery.

    A successful reassignment notification is only current while its lead still
    resolves, its destination cycle is active, and the lead points at that
    cycle/owner.  This is deliberately read-only and runs after claim but
    before the provider reservation/call.
    """
    lead_id = str(notification.get("lead_id") or "").strip()
    cycle_id = str(notification.get("assignment_cycle_id") or "").strip()
    recipient_id = str(notification.get("recipient_user_id") or "").strip()
    if not lead_id or not cycle_id or not recipient_id:
        return False, "missing_notification_identity"

    lead = db["leads"].find_one({"_id": {"$in": list(mongo_id_variants(lead_id))}})
    if not lead:
        return False, "lead_not_found"
    notification_type = str(notification.get("notification_type") or "").strip()
    role = str(
        notification.get("notification_role")
        or (notification.get("metadata") or {}).get("notification_role")
        or (notification.get("payload") or {}).get("notification_role")
        or ""
    ).strip()
    is_previous_owner = notification_type == SLA_REASSIGNED_AWAY or role == "previous_owner"
    if is_previous_owner:
        cycle = db["crm_assignment_cycles"].find_one({
            "assignment_cycle_id": cycle_id,
            "cycle_status": {"$in": ["reassigned", "closed"]},
        })
        if not cycle:
            return False, "source_cycle_not_reassigned"
        if str(cycle.get("assigned_to_user_id") or "") != recipient_id:
            return False, "source_owner_changed"
        if not _same_id(cycle.get("lead_id"), lead.get("_id")):
            return False, "cycle_lead_mismatch"
        lifecycle = lead.get("lifecycle") or {}
        current_cycle = lifecycle.get("current_assignment_cycle_id") or lead.get("assignment_cycle_id")
        if current_cycle in (None, "") or str(current_cycle) == cycle_id:
            return False, "lead_current_cycle_changed"
    else:
        cycle = db["crm_assignment_cycles"].find_one({
            "assignment_cycle_id": cycle_id,
            "cycle_status": "active",
        })
        if not cycle:
            return False, "destination_cycle_not_current_active"
        if str(cycle.get("assigned_to_user_id") or "") != recipient_id:
            return False, "destination_owner_changed"
        if not _same_id(cycle.get("lead_id"), lead.get("_id")):
            return False, "cycle_lead_mismatch"

        lifecycle = lead.get("lifecycle") or {}
        current_cycle = lifecycle.get("current_assignment_cycle_id") or lead.get("assignment_cycle_id")
        if current_cycle in (None, "") or str(current_cycle) != cycle_id:
            return False, "lead_current_cycle_changed"
        owner_ids = (
            lifecycle.get("assigned_to_user_id"),
            lead.get("assignment_mirror_owner_user_id"),
            lead.get("assigned_to_user_id"),
        )
        if any(value not in (None, "") and str(value) != recipient_id for value in owner_ids):
            return False, "lead_current_owner_changed"

    successful = db[COLLECTION].find_one({
        "individual_identity": notification.get("individual_identity"),
        "state": "sent",
    })
    if successful and successful.get("_id") != notification.get("_id"):
        return False, "successful_delivery_already_exists"
    return True, ""


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
            "notification_type": {"$in": [
                NOTIFICATION_TYPE, SLA_REASSIGNED_AWAY, SLA_REASSIGNED_TO,
            ]},
            "message_domain": "commercial_notification",
            "provider_message_id": {"$in": [None]},
            "actually_delivered": {"$ne": True},
        },
    )
    if not notification:
        return {"status": "idle"}

    valid, invalid_reason = _validate_pending_notification(db, notification)
    if not valid:
        finalize_attempt(
            db,
            notification_id=notification["_id"],
            worker_id=worker_id,
            state="stale_not_sent",
            error=invalid_reason,
            now=current,
        )
        return {"status": "stale_not_sent", "reason": invalid_reason}

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
    delivery_token = str(uuid.uuid4())
    reserved = reserve_for_delivery(
        db,
        notification_id=notification["_id"],
        worker_id=worker_id,
        delivery_token=delivery_token,
        now=current,
    )
    if not reserved:
        logger.warning(
            "[SLA_REASSIGNMENT_NOTIFICATION] delivery slot already reserved notif=%s",
            str(notification["_id"])[-12:],
        )
        return {"status": "already_reserved", "reason": "delivery_in_progress"}
    refresh_lease(db, notification_id=notification["_id"], worker_id=worker_id, now=current)
    call_started = utc_now()
    payload_hash = content_hash(payload)
    try:
        if sender is not None:
            receipt = dict(sender(phone, message) or {})
        else:
            from .whatsapp_client import send_whatsapp_message_detailed_sync
            receipt = send_whatsapp_message_detailed_sync(phone, message)
    except Exception as exc:
        record_delivery_attempt(
            db,
            notification_id=notification["_id"],
            delivery_token=delivery_token,
            attempt_data={
                "started_at": call_started,
                "worker_id": worker_id,
                "content_hash": payload_hash,
                "result": "exception",
            },
            now=current,
        )
        finalize_attempt(
            db,
            notification_id=notification["_id"],
            worker_id=worker_id,
            state="failed_retryable",
            error=type(exc).__name__,
            now=current,
        )
        db[COLLECTION].update_one(
            {"_id": notification["_id"], "state": "failed_retryable", "provider_message_id": None},
            {"$unset": {"provider_call_started_at": "", "delivery_token": ""},
             "$set": {"next_attempt_at": current + timedelta(seconds=60), "updated_at": current}},
        )
        return {"status": "failed_retryable", "error": type(exc).__name__}

    state, error = _delivery_state(receipt)
    provider_id = receipt.get("provider_message_id")
    record_delivery_attempt(
        db,
        notification_id=notification["_id"],
        delivery_token=delivery_token,
        attempt_data={
            "started_at": call_started,
            "worker_id": worker_id,
            "http_status": receipt.get("http_status"),
            "provider_message_id": provider_id,
            "provider_request_id": receipt.get("provider_request_id"),
            "content_hash": payload_hash,
            "result": state,
        },
        now=current,
    )
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
            {"$unset": {"provider_call_started_at": "", "delivery_token": ""},
             "$set": {"next_attempt_at": current + timedelta(seconds=retry_after), "updated_at": current}},
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
