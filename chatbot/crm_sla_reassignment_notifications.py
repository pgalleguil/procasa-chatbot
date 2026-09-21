"""Durable WhatsApp notification path for committed SLA reassignments.

The transaction owns the lead/cycle change.  This module is deliberately
post-commit: it only creates a durable notification after an ``APPLIED`` or
idempotent replay, and provider delivery can never roll back the assignment.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import uuid
from typing import Any, Callable, Mapping

from pymongo import ReturnDocument

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
from .crm_metrics import coerce_utc_datetime, utc_now
from .mongo_identity import mongo_id_variants
from .crm_sla_cycle_links import build_sla_cycle_url
from .whatsapp_client import normalize_provider_status


logger = logging.getLogger(__name__)

NOTIFICATION_TYPE = "sla_reassignment"
SLA_BREACH_WARNING = "SLA_BREACH_WARNING"
SLA_REASSIGNED_AWAY = "SLA_REASSIGNED_AWAY"
SLA_REASSIGNED_TO = "SLA_REASSIGNED_TO"
DELIVERY_RECONCILIATION_TYPES = frozenset({SLA_REASSIGNED_TO, SLA_REASSIGNED_AWAY})
DELIVERY_RECONCILIATION_GRACE_SECONDS = 30
DELIVERY_RECONCILIATION_MAX_INTERVAL_SECONDS = 15 * 60
DELIVERY_RECONCILIATION_LEASE_SECONDS = 120
COMMITTED_STATUSES = frozenset({"APPLIED", "ALREADY_APPLIED"})
DELIVERY_STATUS_RANK = {
    "unknown": -1,
    "pending": 0,
    "failed": 0,
    "accepted": 1,
    "sent": 1,
    "delivered": 2,
    "read": 3,
    "played": 4,
}
DELIVERY_CONFIRMED_STATUSES = frozenset({"delivered", "read", "played"})

# This is intentionally a closed migration allow-list, not a history scanner.
# It protects the one legacy destination whose delivery evidence was lost
# during the first production rollout while keeping every other old cycle
# outside the repair primitive.
LEGACY_DELIVERY_MIGRATION_CASES = frozenset({
    (
        "6aab2a66da2187c900af35f9",
        "sla-reassignment:2b6d721a01dd17305818bfb91fff403b01b8348aef7e5512b24ddbf1e503dc68",
        "69c19b98fbbbf113235ba844",
    ),
})


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
        "⏱️ Tu SLA comenzará desde la entrega de esta notificación.\n\n"
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
    recipient.  The reassignment counter is recorded for auditability; pool
    exhaustion and anti-ping-pong policy, not a numeric ceiling, determine
    whether a later reassignment may proceed.
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
    if reassignment_number is None or reassignment_number < 1:
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
    if reassignment_number is None or reassignment_number < 1:
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
    if receipt.get("provider_error_code") == "PROVIDER_AUTH_ERROR" or http_status in {401, 403}:
        # Credentials/authorization cannot be repaired by replaying the same
        # request.  Keep the notification terminal and require an explicit,
        # idempotent rearm after the production credential is restored.
        return "failed_final", "PROVIDER_AUTH_ERROR"
    if http_status == 422:
        return "failed_validation", "provider_rejected_payload"
    if http_status == 429:
        return "failed_retryable", "provider_rate_limited"
    return "failed_retryable", str(
        receipt.get("error") or receipt.get("delivery_status") or "provider_rejected"
    )


def _provider_event_timestamp(payload: Mapping[str, Any], fallback: Any = None) -> Any:
    """Resolve a provider event timestamp without falling back to local time."""
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
    update = data.get("update") if isinstance(data.get("update"), Mapping) else {}
    candidates = (
        update.get("timestamp"),
        update.get("timestamp_ms"),
        update.get("timestampMs"),
        update.get("updated_at"),
        update.get("updatedAt"),
        data.get("timestamp"),
        data.get("timestamp_ms"),
        data.get("timestampMs"),
        payload.get("timestamp"),
        payload.get("timestamp_ms"),
        payload.get("timestampMs"),
        payload.get("event_timestamp"),
    )
    for value in candidates:
        if value in (None, ""):
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            numeric = float(value)
            if numeric > 10_000_000_000:
                numeric /= 1000.0
            try:
                return datetime.fromtimestamp(numeric, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                continue
        parsed = coerce_utc_datetime(value)
        if parsed:
            return parsed
    # ``fallback`` is retained for call-site compatibility, but it is never
    # used for delivery evidence.  A local handler/poll time is not a provider
    # delivery timestamp and must not start an SLA clock.
    return None


def _delivery_status_rank(status: Any) -> int:
    return DELIVERY_STATUS_RANK.get(normalize_provider_status(status), -1)


def _delivery_status_transition(
    current_status: Any,
    incoming_status: Any,
) -> tuple[str, bool]:
    """Return the monotonic canonical status and whether a downgrade occurred."""
    current = normalize_provider_status(current_status)
    incoming = normalize_provider_status(incoming_status)
    if _delivery_status_rank(incoming) < _delivery_status_rank(current):
        return current, True
    return incoming, False


def record_sla_reassignment_delivery_status(
    db: Any,
    *,
    provider_message_id: Any,
    delivery_status: Any,
    delivered_at: Any = None,
    status_code: int | None = None,
    provider_timestamp_source: str | None = None,
    delivery_confirmed_observed_at: Any = None,
    authenticated_provider_confirmation: bool = False,
) -> dict[str, Any]:
    """Consume one canonical provider status for an SLA notification.

    Provider acceptance remains ``state=sent`` and never starts a destination
    SLA clock.  Only ``delivered``/``read``/``played`` marks the notification
    as actually delivered.  A provider event timestamp wins; otherwise the
    caller must provide an authenticated first-observation timestamp.  The
    effective clock is written with a compare-and-set guard and is immutable.
    """
    provider_id = str(provider_message_id or "").strip()
    status = normalize_provider_status(delivery_status)
    if not provider_id:
        return {"status": "ignored", "reason": "provider_message_id_required"}
    notification = db[COLLECTION].find_one({
        "provider_message_id": provider_id,
        "notification_type": {"$in": [SLA_REASSIGNED_TO, SLA_REASSIGNED_AWAY]},
    })
    if not notification:
        return {"status": "unmatched", "provider_message_id": provider_id}

    provider_event_at = coerce_utc_datetime(delivered_at)
    observed_at = (
        coerce_utc_datetime(delivery_confirmed_observed_at)
        if authenticated_provider_confirmation else None
    )
    event_at = utc_now()
    current_status = normalize_provider_status(
        notification.get("delivery_status") or notification.get("provider_status")
    )
    canonical_status, regression = _delivery_status_transition(current_status, status)

    if status in DELIVERY_CONFIRMED_STATUSES:
        effective_candidate = provider_event_at or observed_at
        effective_source = (
            "PROVIDER_EVENT_TIMESTAMP"
            if provider_event_at is not None
            else "PROVIDER_STATUS_FIRST_OBSERVED_AT"
            if observed_at is not None
            else None
        )
        current_effective = coerce_utc_datetime(notification.get("effective_delivery_at"))
        set_fields: dict[str, Any] = {
            "delivery_mode": "live",
            "delivery_status": canonical_status,
            "provider_status_last_observed": status,
            "actually_delivered": True,
            "delivery_timestamp_missing": current_effective is None and effective_candidate is None,
            "provider_status_code": status_code,
            "updated_at": event_at,
        }
        if regression:
            set_fields.update({
                "provider_status_regression_observed": True,
                "provider_status_regression_last": status,
                "provider_status_regression_observed_at": event_at,
            })
        if current_effective is None and effective_candidate is not None:
            set_fields.update({
                "effective_delivery_at": effective_candidate,
                "delivery_time_source": effective_source,
                "delivery_timestamp_missing": False,
            })
        if provider_event_at is not None and not coerce_utc_datetime(notification.get("provider_delivered_at")):
            set_fields["provider_delivered_at"] = provider_event_at
            if not coerce_utc_datetime(notification.get("delivered_at")):
                set_fields["delivered_at"] = provider_event_at
            if provider_timestamp_source:
                set_fields["provider_timestamp_source"] = provider_timestamp_source
        if observed_at is not None and not coerce_utc_datetime(notification.get("delivery_confirmed_observed_at")):
            set_fields["delivery_confirmed_observed_at"] = observed_at
        update_filter = {
            "_id": notification.get("_id"),
            "provider_message_id": provider_id,
        }
        if current_effective is None:
            update_filter["$or"] = [
                {"effective_delivery_at": {"$exists": False}},
                {"effective_delivery_at": None},
                {"effective_delivery_at": ""},
            ]
        db[COLLECTION].update_one(update_filter, {"$set": set_fields})
        refreshed = db[COLLECTION].find_one({"_id": notification.get("_id")}) or notification
        effective_delivered_at = coerce_utc_datetime(refreshed.get("effective_delivery_at"))
        if refreshed.get("notification_type") == SLA_REASSIGNED_TO:
            activation = mark_new_owner_notification_delivered(
                db,
                notification_id=refreshed.get("_id"),
                provider_message_id=provider_id,
                delivered_at=effective_delivered_at,
            )
            if activation.get("status") == "blocked" and activation.get("reason") == "destination_identity_changed":
                return {
                    "status": "stale",
                    "provider_message_id": provider_id,
                    "notification_id": refreshed.get("_id"),
                    "activation": activation,
                }
        else:
            activation = None
        if effective_delivered_at is None:
            return {
                "status": "delivered_timestamp_missing",
                "provider_status": canonical_status,
                "provider_message_id": provider_id,
                "notification_id": refreshed.get("_id"),
                "activation": {
                    "status": "blocked",
                    "reason": "DELIVERY_CONFIRMED_TIMESTAMP_MISSING",
                },
            }
        return {
            "status": "delivered",
            "provider_status": canonical_status,
            "provider_message_id": provider_id,
            "notification_id": refreshed.get("_id"),
            "delivery_time_source": refreshed.get("delivery_time_source"),
            "activation": activation,
        }

    if status in {"accepted", "sent", "pending"}:
        set_fields = {
            "delivery_status": canonical_status,
            "provider_status_last_observed": status,
            "provider_status_code": status_code,
            "updated_at": event_at,
        }
        if notification.get("actually_delivered") is not True:
            set_fields["actually_delivered"] = False
        if regression:
            set_fields.update({
                "provider_status_regression_observed": True,
                "provider_status_regression_last": status,
                "provider_status_regression_observed_at": event_at,
            })
        db[COLLECTION].update_one(
            {"_id": notification.get("_id"), "provider_message_id": provider_id},
            {"$set": set_fields},
        )
        return {
            "status": "sent",
            "provider_status": canonical_status,
            "provider_message_id": provider_id,
            "notification_id": notification.get("_id"),
            "activation": None,
        }

    if status == "failed":
        set_fields = {
            "delivery_status": canonical_status,
            "provider_status_last_observed": status,
            "provider_status_code": status_code,
            "updated_at": event_at,
        }
        if _delivery_status_rank(current_status) >= _delivery_status_rank("delivered"):
            set_fields.update({
                "actually_delivered": True,
                "provider_status_regression_observed": True,
                "provider_status_regression_last": status,
                "provider_status_regression_observed_at": event_at,
            })
        else:
            set_fields.update({
                "actually_delivered": False,
                "delivery_failed_at": event_at,
            })
        db[COLLECTION].update_one(
            {"_id": notification.get("_id"), "provider_message_id": provider_id},
            {"$set": set_fields},
        )
        return {
            "status": canonical_status if canonical_status in DELIVERY_CONFIRMED_STATUSES else "failed",
            "provider_status": canonical_status,
            "provider_message_id": provider_id,
            "notification_id": notification.get("_id"),
            "activation": None,
        }

    return {
        "status": "ignored",
        "provider_status": status,
        "provider_message_id": provider_id,
        "notification_id": notification.get("_id"),
    }


def record_sla_reassignment_delivery_status_webhook(
    payload: Mapping[str, Any], *, db: Any = None, now: Any = None
) -> dict[str, Any]:
    """Bridge the existing provider status webhook to SLA notifications."""
    if not isinstance(payload, Mapping) or str(payload.get("event") or "") not in {
        "messages.update", "message.update", "messages.status", "message.status",
    }:
        return {"status": "ignored", "reason": "unsupported_event"}
    from .storage import _extract_chatbot_delivery_update, get_db

    provider_id, status, status_code = _extract_chatbot_delivery_update(dict(payload))
    if not provider_id:
        return {"status": "ignored", "reason": "provider_message_id_missing"}
    target_db = db or get_db()
    event_at = _provider_event_timestamp(payload, now or utc_now())
    return record_sla_reassignment_delivery_status(
        target_db,
        provider_message_id=provider_id,
        delivery_status=status,
        delivered_at=event_at,
        status_code=status_code,
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


def rearm_stale_new_owner_notification(
    db: Any,
    *,
    lead_id: str,
    assignment_cycle_id: str,
    recipient_user_id: str,
    now: Any = None,
) -> dict[str, Any]:
    """Re-open one stale notification without creating a new identity.

    This is a narrowly-scoped recovery primitive for a committed destination
    cycle whose first delivery was incorrectly marked ``stale_not_sent``.
    It refuses to create a replacement record, requires the exact lead/cycle/
    recipient identity, and reuses the existing idempotency key/payload.
    """
    identity = individual_identity(
        lead_id=str(lead_id).strip(),
        assignment_cycle_id=str(assignment_cycle_id).strip(),
        notification_type=SLA_REASSIGNED_TO,
        recipient_user_id=str(recipient_user_id).strip(),
    )
    notification = db[COLLECTION].find_one({
        "individual_identity": identity,
        "notification_type": SLA_REASSIGNED_TO,
        "notification_role": "new_owner",
    })
    if not notification:
        return {"status": "not_found", "identity": identity}
    previous_state = str(notification.get("state") or "")
    if notification.get("provider_message_id") or notification.get("actually_delivered") is True:
        return {"status": "already_delivered", "notification_id": notification.get("_id")}
    recoverable_states = {
        "stale_not_sent", "failed_retryable", "failed_recipient", "failed_validation",
    }
    auth_failure = (
        notification.get("state") == "failed_final"
        and notification.get("delivery_error_code") == "PROVIDER_AUTH_ERROR"
    )
    if notification.get("state") not in recoverable_states and not auth_failure:
        return {"status": "not_recoverable", "state": notification.get("state"), "notification_id": notification.get("_id")}

    allowed_states = sorted(recoverable_states)

    valid, reason = _validate_pending_notification(db, notification)
    if not valid:
        return {
            "status": "blocked",
            "reason": reason,
            "notification_id": notification.get("_id"),
        }
    recovered_at = now or utc_now()
    update = db[COLLECTION].update_one(
        {
            "_id": notification.get("_id"),
            "individual_identity": identity,
            "$or": [
                {"state": {"$in": allowed_states}},
                {"state": "failed_final", "delivery_error_code": "PROVIDER_AUTH_ERROR"},
            ],
            "provider_message_id": {"$in": [None]},
            "actually_delivered": {"$ne": True},
        },
        {
            "$set": {
                "state": "pending",
                "next_attempt_at": recovered_at,
                "rearmed_at": recovered_at,
                "repair_reason": (
                    "STALE_NEW_OWNER_NOTIFICATION_RECOVERY"
                    if previous_state == "stale_not_sent"
                    else "PROVIDER_AUTH_ERROR_REARM"
                    if auth_failure
                    else "NEW_OWNER_NOTIFICATION_RECOVERY"
                ),
                "updated_at": recovered_at,
            },
            "$unset": {
                "error": "",
                "delivery_error_code": "",
                "provider_auth_failure_at": "",
                "lease_owner": "",
                "lease_expires_at": "",
                "delivery_token": "",
                "provider_call_started_at": "",
            },
        },
    )
    if int(getattr(update, "modified_count", 0) or 0) != 1:
        return {"status": "race_lost", "notification_id": notification.get("_id")}
    return {
        "status": "rearmed",
        "notification_id": notification.get("_id"),
        "identity": identity,
    }


def mark_new_owner_notification_delivered(
    db: Any,
    *,
    notification_id: Any,
    provider_message_id: Any,
    delivered_at: Any = None,
) -> dict[str, Any]:
    """Activate the destination SLA clock only after confirmed delivery.

    ``provider_message_id`` is evidence for the notification record, but the
    cycle clock is advanced only by this routine after it has validated the
    complete current lead/cycle/recipient identity.  ``delivered_at`` is the
    already-resolved ``effective_delivery_at`` and may represent either the
    provider event timestamp or the first authenticated provider observation.
    Repeating the call is idempotent and never creates another notification or
    reassignment.
    """
    provider_id = str(provider_message_id or "").strip()
    if not provider_id:
        return {"status": "blocked", "reason": "provider_message_id_required"}
    notification = db[COLLECTION].find_one({
        "_id": notification_id,
        "notification_type": SLA_REASSIGNED_TO,
        "notification_role": "new_owner",
    })
    if not notification:
        return {"status": "not_found", "notification_id": notification_id}
    # A provider message ID proves that the provider accepted/identified a
    # message, but it is not by itself the delivery authority.  The caller
    # must first persist ``actually_delivered=True`` from its delivery
    # confirmation path; this routine only activates the SLA clock after that
    # explicit fact is present.
    if notification.get("provider_message_id") != provider_id:
        return {"status": "blocked", "reason": "provider_message_id_mismatch", "notification_id": notification_id}
    if notification.get("actually_delivered") is not True:
        return {"status": "blocked", "reason": "notification_delivery_not_confirmed", "notification_id": notification_id}
    delivered = coerce_utc_datetime(delivered_at) or coerce_utc_datetime(
        notification.get("effective_delivery_at")
    )
    if delivered is None:
        return {
            "status": "blocked",
            "reason": "DELIVERY_CONFIRMED_TIMESTAMP_MISSING",
            "notification_id": notification_id,
        }

    lead_id = str(notification.get("lead_id") or "").strip()
    cycle_id = str(notification.get("assignment_cycle_id") or "").strip()
    recipient_id = str(notification.get("recipient_user_id") or "").strip()
    lead = _lead_for_notification(db, lead_id)
    cycle = db["crm_assignment_cycles"].find_one({"assignment_cycle_id": cycle_id}) or {}
    lifecycle = lead.get("lifecycle") if isinstance(lead.get("lifecycle"), Mapping) else {}
    current_cycle = lifecycle.get("current_assignment_cycle_id") or lead.get("assignment_cycle_id")
    if (
        not lead or not cycle
        or cycle.get("cycle_status") != "active"
        or str(cycle.get("assigned_to_user_id") or "") != recipient_id
        or str(current_cycle or "") != cycle_id
        or any(
            value not in (None, "") and str(value) != recipient_id
            for value in (
                lifecycle.get("assigned_to_user_id"),
                lead.get("assignment_mirror_owner_user_id"),
                lead.get("assigned_to_user_id"),
            )
        )
    ):
        return {"status": "blocked", "reason": "destination_identity_changed", "notification_id": notification_id}

    if (
        str(cycle.get("reassignment_state") or "").upper() != "AWAITING_OWNER_NOTIFICATION"
        and coerce_utc_datetime(cycle.get("sla_started_at"))
    ):
        return {"status": "already_active", "assignment_cycle_id": cycle_id, "notification_id": notification_id}

    cycle_update = db["crm_assignment_cycles"].update_one(
        {
            "assignment_cycle_id": cycle_id,
            "cycle_status": "active",
            "assigned_to_user_id": recipient_id,
            "reassignment_state": {"$in": ["AWAITING_OWNER_NOTIFICATION", "awaiting_owner_notification"]},
            "owner_notified_at": {"$in": [None]},
        },
        {"$set": {
            "owner_notified_at": delivered,
            "sla_started_at": delivered,
            "reassignment_state": "active",
            "notification_delivery_status": "delivered",
            "updated_at": delivered,
        }},
    )
    if int(getattr(cycle_update, "modified_count", 0) or 0) != 1:
        refreshed = db["crm_assignment_cycles"].find_one({"assignment_cycle_id": cycle_id}) or {}
        if not (
            str(refreshed.get("reassignment_state") or "").upper() == "ACTIVE"
            and coerce_utc_datetime(refreshed.get("owner_notified_at"))
            and coerce_utc_datetime(refreshed.get("sla_started_at"))
        ):
            return {"status": "race_lost", "assignment_cycle_id": cycle_id, "notification_id": notification_id}

    # Keep the lead's derived lifecycle start aligned with the cycle's
    # canonical delivery timestamp.  Ownership and cycle pointers are not
    # changed here.
    db["leads"].update_one(
        {
            "_id": lead.get("_id"),
            "lifecycle.current_assignment_cycle_id": cycle_id,
            "lifecycle.assigned_to_user_id": recipient_id,
        },
        {"$set": {
            "lifecycle.sla_started_at": delivered,
            "lifecycle.owner_notified_at": delivered,
        }},
    )
    return {
        "status": "activated",
        "assignment_cycle_id": cycle_id,
        "owner_notified_at": delivered,
        "sla_started_at": delivered,
        "notification_id": notification_id,
    }


def recover_sla_reassignment_delivery_from_evidence(
    db: Any,
    *,
    notification_id: Any,
    provider_message_id: Any,
    first_confirmed_observed_at: Any,
    evidence_source: str,
    evidence_reference: str,
) -> dict[str, Any]:
    """Recover one existing delivery using verified first-observation evidence.

    This primitive is deliberately narrow and idempotent.  It never creates a
    notification/cycle, changes ownership, or sends a provider message.  The
    supplied observation is operationally conservative: delivery occurred no
    later than the first authenticated observation.
    """
    provider_id = str(provider_message_id or "").strip()
    source = str(evidence_source or "").strip()
    reference = str(evidence_reference or "").strip()
    observed_at = coerce_utc_datetime(first_confirmed_observed_at)
    if not provider_id or not observed_at or not source or not reference:
        return {"status": "blocked", "reason": "historical_evidence_incomplete"}
    notification = db[COLLECTION].find_one({"_id": notification_id})
    if not notification:
        return {"status": "not_found", "notification_id": notification_id}
    if notification.get("provider_message_id") != provider_id:
        return {"status": "blocked", "reason": "provider_message_id_mismatch"}
    if notification.get("notification_type") not in {SLA_REASSIGNED_TO, SLA_REASSIGNED_AWAY}:
        return {"status": "blocked", "reason": "unsupported_notification_type"}
    existing_effective = coerce_utc_datetime(notification.get("effective_delivery_at"))
    if existing_effective is not None:
        return {
            "status": "already_recovered",
            "notification_id": notification_id,
            "effective_delivery_at": existing_effective,
        }

    valid, reason = _validate_pending_notification(db, notification)
    if not valid:
        return {"status": "blocked", "reason": reason, "notification_id": notification_id}

    current_status = normalize_provider_status(
        notification.get("delivery_status") or notification.get("provider_status")
    )
    canonical_status, regression = _delivery_status_transition(current_status, "delivered")
    update = db[COLLECTION].update_one(
        {
            "_id": notification_id,
            "provider_message_id": provider_id,
            "$or": [
                {"effective_delivery_at": {"$exists": False}},
                {"effective_delivery_at": None},
                {"effective_delivery_at": ""},
            ],
        },
        {"$set": {
            "delivery_mode": "historical_recovery",
            "delivery_status": canonical_status,
            "provider_status_last_observed": "delivered",
            "actually_delivered": True,
            "delivery_timestamp_missing": False,
            "delivery_confirmed_observed_at": observed_at,
            "effective_delivery_at": observed_at,
            "delivery_time_source": "PROVIDER_STATUS_FIRST_OBSERVED_AT",
            "delivery_evidence_source": source,
            "delivery_evidence_reference": reference,
            "delivery_recovered_at": utc_now(),
            "updated_at": utc_now(),
            **({
                "provider_status_regression_observed": True,
                "provider_status_regression_last": current_status,
                "provider_status_regression_observed_at": utc_now(),
            } if regression else {}),
        }},
    )
    refreshed = db[COLLECTION].find_one({"_id": notification_id}) or notification
    effective = coerce_utc_datetime(refreshed.get("effective_delivery_at"))
    if effective is None:
        return {"status": "race_lost", "notification_id": notification_id}
    if int(getattr(update, "modified_count", 0) or 0) == 0:
        return {
            "status": "already_recovered",
            "notification_id": notification_id,
            "effective_delivery_at": effective,
        }
    activation = None
    if refreshed.get("notification_type") == SLA_REASSIGNED_TO:
        activation = mark_new_owner_notification_delivered(
            db,
            notification_id=notification_id,
            provider_message_id=provider_id,
            delivered_at=effective,
        )
    return {
        "status": "recovered",
        "notification_id": notification_id,
        "provider_message_id": provider_id,
        "effective_delivery_at": effective,
        "delivery_time_source": "PROVIDER_STATUS_FIRST_OBSERVED_AT",
        "activation": activation,
    }


def migrate_legacy_sla_reassignment_to_waiting(
    db: Any,
    *,
    lead_id: str,
    assignment_cycle_id: str,
    recipient_user_id: str,
    now: Any = None,
) -> dict[str, Any]:
    """Safely place one allow-listed legacy destination behind delivery.

    This is an explicit, idempotent migration for the known Maria case.  It
    does not scan history, create a cycle, change ownership, increment an
    assignment number, or alter source-breach fields.  The caller must rearm
    the exact existing notification separately after this migration succeeds.
    """
    identity_key = (
        str(lead_id or "").strip(),
        str(assignment_cycle_id or "").strip(),
        str(recipient_user_id or "").strip(),
    )
    if identity_key not in LEGACY_DELIVERY_MIGRATION_CASES:
        return {"status": "blocked", "reason": "unsupported_legacy_case"}

    identity = individual_identity(
        lead_id=identity_key[0],
        assignment_cycle_id=identity_key[1],
        notification_type=SLA_REASSIGNED_TO,
        recipient_user_id=identity_key[2],
    )
    notifications = list(db[COLLECTION].find({
        "individual_identity": identity,
        "notification_type": SLA_REASSIGNED_TO,
        "notification_role": "new_owner",
    }))
    if len(notifications) != 1:
        return {
            "status": "blocked",
            "reason": "notification_identity_not_unique",
            "notification_count": len(notifications),
        }
    notification = notifications[0]
    if (
        notification.get("provider_message_id")
        or notification.get("actually_delivered") is True
        or notification.get("delivery_status") in {"delivered", "read"}
        or notification.get("delivered_at") not in (None, "")
    ):
        return {"status": "blocked", "reason": "delivery_evidence_exists"}
    notification_state = str(notification.get("state") or "")
    if notification_state not in {"stale_not_sent", "pending"}:
        return {
            "status": "blocked",
            "reason": "legacy_notification_state_not_migratable",
            "state": notification_state,
        }

    lead = _lead_for_notification(db, identity_key[0])
    if not lead:
        return {"status": "blocked", "reason": "lead_not_found"}
    active_cycles = list(db["crm_assignment_cycles"].find({
        "lead_id": {"$in": list(mongo_id_variants(identity_key[0]))},
        "cycle_status": "active",
    }))
    cycle = next(
        (row for row in active_cycles
         if str(row.get("assignment_cycle_id") or "") == identity_key[1]),
        None,
    )
    if len(active_cycles) != 1 or not cycle:
        return {
            "status": "blocked",
            "reason": "current_active_cycle_not_unambiguous",
            "active_cycle_count": len(active_cycles),
        }
    lifecycle = lead.get("lifecycle") if isinstance(lead.get("lifecycle"), Mapping) else {}
    if str(lifecycle.get("current_assignment_cycle_id") or "") != identity_key[1]:
        return {"status": "blocked", "reason": "lead_current_cycle_mismatch"}
    if str(cycle.get("assigned_to_user_id") or "") != identity_key[2]:
        return {"status": "blocked", "reason": "current_owner_mismatch"}
    if not str(cycle.get("assignment_cycle_id") or "").startswith("sla-reassignment:"):
        return {"status": "blocked", "reason": "not_sla_reassignment_cycle"}
    if not _same_id(cycle.get("lead_id"), lead.get("_id")):
        return {"status": "blocked", "reason": "cycle_lead_mismatch"}
    migration_version = "crm_sla_legacy_delivery_gate_v1"
    already_migrated = cycle.get("delivery_gate_migration") == migration_version
    if notification_state == "pending" and not already_migrated:
        return {"status": "blocked", "reason": "pending_notification_not_migrated"}

    from .crm_sla_reassignment_transaction import build_current_cycle_owner_mirror_repair

    migrated_at = coerce_utc_datetime(now) or utc_now()
    mirror_update = build_current_cycle_owner_mirror_repair(
        lead=lead,
        active_cycles=[cycle],
        repaired_at=migrated_at,
    )
    if not mirror_update:
        return {"status": "blocked", "reason": "owner_mirror_not_unambiguous"}
    # The legacy cycle may still carry its old clock.  Migration explicitly
    # moves that clock behind the delivery gate; it never carries the old
    # value into the derived lifecycle mirror.
    mirror_update["$set"]["lifecycle.sla_started_at"] = None
    mirror_update["$set"]["lifecycle.owner_notified_at"] = None

    # The lead update is derived-only; it cannot change canonical ownership.
    db["leads"].update_one(
        {
            "_id": lead.get("_id"),
            "lifecycle.current_assignment_cycle_id": identity_key[1],
        },
        mirror_update,
    )
    cycle_update = db["crm_assignment_cycles"].update_one(
        {
            "assignment_cycle_id": identity_key[1],
            "cycle_status": "active",
            "assigned_to_user_id": identity_key[2],
            "lead_id": {"$in": list(mongo_id_variants(identity_key[0]))},
        },
        {"$set": {
            "reassignment_state": "AWAITING_OWNER_NOTIFICATION",
            "owner_notified_at": None,
            "sla_started_at": None,
            "notification_delivery_status": "pending",
            "delivery_gate_migration": migration_version,
            "delivery_gate_migration_reason": "LEGACY_SLA_REASSIGNED_TO_WITHOUT_DELIVERY",
            "delivery_gate_migrated_at": migrated_at,
            "updated_at": migrated_at,
        }},
    )
    if int(getattr(cycle_update, "matched_count", 0) or 0) != 1:
        return {"status": "race_lost", "reason": "cycle_changed_during_migration"}
    return {
        "status": "already_waiting" if already_migrated else "migrated",
        "lead_id": identity_key[0],
        "assignment_cycle_id": identity_key[1],
        "recipient_user_id": identity_key[2],
        "notification_id": notification.get("_id"),
        "notification_identity": identity,
    }


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
    if state == "failed_final" and error == "PROVIDER_AUTH_ERROR":
        # Keep auth failures visible and terminal.  The document remains
        # explicitly rearmable by an operator after credentials are restored,
        # but it is excluded from the automatic claim query.
        db[COLLECTION].update_one(
            {"_id": notification["_id"], "state": "failed_final"},
            {"$set": {
                "delivery_status": "provider_auth_error",
                "delivery_error_code": "PROVIDER_AUTH_ERROR",
                "actually_delivered": False,
                "provider_auth_failure_at": current,
            }},
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
    delivery_activation = None
    if state == "quarantined":
        # An uncertain provider call is retained for explicit reconciliation;
        # it is never treated as delivered and never starts the SLA clock.
        db[COLLECTION].update_one(
            {"_id": notification["_id"], "state": "quarantined"},
            {"$set": {
                "delivery_status": "delivery_unknown",
                "actually_delivered": False,
            }},
        )
    if state == "sent":
        # A provider response with an ID proves acceptance/correlation only.
        # Delivery is established later by the canonical provider status
        # webhook.  In particular, do not start the destination SLA clock here.
        db[COLLECTION].update_one(
            {
                "_id": notification["_id"],
                "state": "sent",
                "provider_message_id": provider_id,
            },
            {"$set": {
                "delivery_mode": "live",
                "delivery_status": "sent",
                "actually_delivered": False,
                "delivery_timestamp_missing": False,
                "status_check_attempts": 0,
                "next_status_check_at": current + timedelta(
                    seconds=DELIVERY_RECONCILIATION_GRACE_SECONDS
                ),
                "status_reconciliation_blocked": False,
                "status_reconciliation_last_result": "accepted",
            }},
        )
    return {
        "status": state,
        "provider_message_id": provider_id,
        "error": error,
        "delivery_id": notification.get("delivery_id"),
        "cycle_activation": delivery_activation if state == "sent" and notification.get("notification_type") == SLA_REASSIGNED_TO else None,
    }


def _reconciliation_backoff_seconds(status_check_attempts: int) -> int:
    """Return bounded exponential backoff for provider status GETs."""
    attempts = max(int(status_check_attempts or 1), 1)
    return min(
        DELIVERY_RECONCILIATION_MAX_INTERVAL_SECONDS,
        DELIVERY_RECONCILIATION_GRACE_SECONDS * (2 ** max(attempts - 1, 0)),
    )


def _claim_next_delivery_reconciliation(db: Any, *, worker_id: str, now: Any) -> dict[str, Any] | None:
    query = {
        "state": "sent",
        "notification_type": {"$in": list(DELIVERY_RECONCILIATION_TYPES)},
        "provider_message_id": {"$nin": [None, ""]},
        "$or": [
            {"actually_delivered": {"$ne": True}},
            {"delivery_timestamp_missing": True},
        ],
        "status_reconciliation_blocked": {"$ne": True},
        "$and": [
            {"$or": [
                {"next_status_check_at": {"$exists": False}},
                {"next_status_check_at": None},
                {"next_status_check_at": {"$lte": now}},
            ]},
            {"$or": [
                {"status_reconciliation_lease_expires_at": {"$exists": False}},
                {"status_reconciliation_lease_expires_at": None},
                {"status_reconciliation_lease_expires_at": {"$lte": now}},
            ]},
        ],
    }
    return db[COLLECTION].find_one_and_update(
        query,
        {
            "$set": {
                "status_reconciliation_lease_owner": worker_id,
                "status_reconciliation_lease_expires_at": now + timedelta(
                    seconds=DELIVERY_RECONCILIATION_LEASE_SECONDS
                ),
                "status_reconciliation_claimed_at": now,
                "updated_at": now,
            },
            "$inc": {"status_check_attempts": 1},
        },
        sort=[("created_at", 1)],
        return_document=ReturnDocument.AFTER,
    )


def _finish_delivery_reconciliation(
    db: Any,
    *,
    notification_id: Any,
    worker_id: str,
    now: Any,
    set_fields: Mapping[str, Any] | None = None,
    unset_fields: tuple[str, ...] = (),
) -> None:
    update: dict[str, Any] = {
        "$set": {
            "status_reconciliation_lease_owner": None,
            "status_reconciliation_lease_expires_at": None,
            "status_reconciliation_last_at": now,
            "updated_at": now,
            **dict(set_fields or {}),
        }
    }
    if unset_fields:
        update["$unset"] = {field: "" for field in unset_fields}
    db[COLLECTION].update_one(
        {
            "_id": notification_id,
            "status_reconciliation_lease_owner": worker_id,
        },
        update,
    )


def get_sla_delivery_reconciliation_snapshot(db: Any, *, now: Any = None) -> dict[str, Any]:
    """Return operational delivery backlog metrics without changing Mongo."""
    current = now or utc_now()
    waiting_query = {
        "state": "sent",
        "notification_type": {"$in": list(DELIVERY_RECONCILIATION_TYPES)},
        "provider_message_id": {"$nin": [None, ""]},
        "actually_delivered": {"$ne": True},
    }
    missing_query = {
        "state": "sent",
        "notification_type": {"$in": list(DELIVERY_RECONCILIATION_TYPES)},
        "provider_message_id": {"$nin": [None, ""]},
        "delivery_timestamp_missing": True,
    }
    oldest = db[COLLECTION].find_one(
        waiting_query,
        {"created_at": 1, "sent_at": 1},
        sort=[("created_at", 1)],
    )
    oldest_at = coerce_utc_datetime(
        (oldest or {}).get("created_at") or (oldest or {}).get("sent_at")
    )
    age = None
    if oldest_at:
        age = max(0, int((coerce_utc_datetime(current) - oldest_at).total_seconds()))
    return {
        "sent_awaiting_delivery_count": db[COLLECTION].count_documents(waiting_query),
        "oldest_sent_awaiting_delivery_age": age,
        "delivered_timestamp_missing": db[COLLECTION].count_documents(missing_query),
    }


def reconcile_one_sla_reassignment_delivery_sync(
    db: Any,
    *,
    worker_id: str,
    now: Any = None,
    status_getter: Callable[[str], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Reconcile one accepted SLA message using provider GET only.

    This path never calls the send endpoint and never changes ownership.  A
    confirmed delivery delegates to the exact same persistence/activation
    routine used by the webhook bridge.
    """
    current = now or utc_now()
    notification = _claim_next_delivery_reconciliation(
        db, worker_id=worker_id, now=current
    )
    if not notification:
        return {"status": "idle"}

    provider_id = str(notification.get("provider_message_id") or "").strip()
    getter = status_getter
    if getter is None:
        from .whatsapp_client import get_whatsapp_message_status_sync
        getter = get_whatsapp_message_status_sync
    try:
        provider_result = dict(getter(provider_id) or {})
    except Exception as exc:
        attempts = int(notification.get("status_check_attempts") or 1)
        _finish_delivery_reconciliation(
            db,
            notification_id=notification.get("_id"),
            worker_id=worker_id,
            now=current,
            set_fields={
                "delivery_status": "unknown",
                "status_reconciliation_last_result": "network_unknown",
                "next_status_check_at": current + timedelta(
                    seconds=_reconciliation_backoff_seconds(attempts)
                ),
                "last_status_check_error": type(exc).__name__,
            },
        )
        return {"status": "network_unknown", "error": type(exc).__name__}

    status = normalize_provider_status(
        provider_result.get("delivery_status") or provider_result.get("provider_status")
    )
    status_code = provider_result.get("provider_status_code")
    http_status = provider_result.get("http_status")
    provider_error = provider_result.get("provider_error_code")
    attempts = int(notification.get("status_check_attempts") or 1)

    if provider_error == "PROVIDER_AUTH_ERROR" or http_status in {401, 403}:
        _finish_delivery_reconciliation(
            db,
            notification_id=notification.get("_id"),
            worker_id=worker_id,
            now=current,
            set_fields={
                "delivery_status": "provider_auth_error",
                "delivery_error_code": "PROVIDER_AUTH_ERROR",
                "status_reconciliation_last_result": "PROVIDER_AUTH_ERROR",
                "status_reconciliation_blocked": True,
                "next_status_check_at": None,
            },
        )
        return {"status": "provider_auth_error", "error": "PROVIDER_AUTH_ERROR"}

    if status in {"delivered", "read", "played"}:
        returned_provider_id = str(provider_result.get("provider_message_id") or "").strip()
        if http_status != 200 or returned_provider_id != provider_id:
            _finish_delivery_reconciliation(
                db,
                notification_id=notification.get("_id"),
                worker_id=worker_id,
                now=current,
                set_fields={
                    "status_reconciliation_last_result": "provider_confirmation_invalid",
                    "next_status_check_at": current + timedelta(
                        seconds=_reconciliation_backoff_seconds(attempts)
                    ),
                },
            )
            return {"status": "provider_confirmation_invalid", "provider_message_id": provider_id}
        valid_identity, identity_reason = _validate_pending_notification(db, notification)
        if not valid_identity:
            _finish_delivery_reconciliation(
                db,
                notification_id=notification.get("_id"),
                worker_id=worker_id,
                now=current,
                set_fields={
                    "status_reconciliation_last_result": identity_reason,
                    "status_reconciliation_blocked": True,
                    "next_status_check_at": None,
                },
            )
            return {
                "status": "stale",
                "reason": identity_reason,
                "provider_message_id": provider_id,
            }
        provider_timestamp = coerce_utc_datetime(
            provider_result.get("provider_event_timestamp")
            or provider_result.get("delivered_at")
        )
        observed_at = None if provider_timestamp is not None else current
        delivery_result = record_sla_reassignment_delivery_status(
            db,
            provider_message_id=provider_id,
            delivery_status=status,
            delivered_at=provider_timestamp,
            status_code=status_code,
            provider_timestamp_source=provider_result.get("provider_event_timestamp_source"),
            delivery_confirmed_observed_at=observed_at,
            authenticated_provider_confirmation=True,
        )
        missing_timestamp = delivery_result.get("status") == "delivered_timestamp_missing"
        _finish_delivery_reconciliation(
            db,
            notification_id=notification.get("_id"),
            worker_id=worker_id,
            now=current,
            set_fields={
                "status_reconciliation_last_result": delivery_result.get("status"),
                "delivery_timestamp_missing": missing_timestamp,
                "next_status_check_at": current + timedelta(
                    seconds=_reconciliation_backoff_seconds(attempts)
                ) if missing_timestamp else None,
            },
        )
        return delivery_result

    if status in {"pending", "sent", "accepted"}:
        delivery_result = record_sla_reassignment_delivery_status(
            db,
            provider_message_id=provider_id,
            delivery_status=status,
            status_code=status_code,
        )
        _finish_delivery_reconciliation(
            db,
            notification_id=notification.get("_id"),
            worker_id=worker_id,
            now=current,
            set_fields={
                "status_reconciliation_last_result": delivery_result.get("status"),
                "next_status_check_at": current + timedelta(
                    seconds=_reconciliation_backoff_seconds(attempts)
                ),
            },
        )
        return {"status": "waiting_delivery", "provider_status": delivery_result.get("provider_status")}

    if status == "failed":
        delivery_result = record_sla_reassignment_delivery_status(
            db,
            provider_message_id=provider_id,
            delivery_status=status,
            status_code=status_code,
        )
        _finish_delivery_reconciliation(
            db,
            notification_id=notification.get("_id"),
            worker_id=worker_id,
            now=current,
            set_fields={
                "status_reconciliation_last_result": delivery_result.get("status"),
                **({"delivery_error_code": "PROVIDER_CONFIRMED_FAILED"}
                   if delivery_result.get("status") == "failed" else {}),
                "status_reconciliation_blocked": delivery_result.get("status") == "failed",
                "next_status_check_at": None,
            },
        )
        return {
            "status": "provider_confirmed_failed"
            if delivery_result.get("status") == "failed"
            else delivery_result.get("status"),
            "provider_status": delivery_result.get("provider_status"),
        }

    _finish_delivery_reconciliation(
        db,
        notification_id=notification.get("_id"),
        worker_id=worker_id,
        now=current,
        set_fields={
            "delivery_status": "unknown",
            "status_reconciliation_last_result": "network_unknown",
            "next_status_check_at": current + timedelta(
                seconds=_reconciliation_backoff_seconds(attempts)
            ),
        },
    )
    return {"status": "network_unknown", "provider_status": status}


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
